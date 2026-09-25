"""内置小型校准曲线与碳十四校准算法。

完全离线：曲线数据固化在本模块常量中，不访问任何外部资源。
算法实现：

* 在日历年网格上逐点计算似然 ``N(mean | cal_age, sqrt(err^2 + sig^2))``；
* 曲线均值与误差均对网格做线性插值（边界用端点常量外推，并显式标记）；
* 概率分布归一化到 1，并给出众数与均值；
* 最高密度区间（HDI/HPD）：以 1 cal BP 分箱的归一化概率为权重，
  按权重降序选取分箱至目标概率（默认 95.4%），临界分箱按线性比例计入；
* 多峰摘要：对归一化的逐点密度用显著鞍部（山谷）切分，返回各峰的
  日历区间、峰内概率与峰顶点，结果使用确定的稳定排序。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 内置小型曲线。合成但平滑（含 Suess 式摆动与负斜率段），足以产生多峰后验；
# 列：日历年 BP（每 100 年一点）、对应常规碳十四年龄、曲线 1σ。
# ---------------------------------------------------------------------------
CURVE_VERSION = "mini-1"
CURVE_NAME = "内置小型碳十四曲线 mini-1"
CURVE_GRID_STEP = 100
_CURVE_YEARS: list[int] = [
    0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1300,
    1400, 1500, 1600, 1700, 1800, 1900, 2000, 2100, 2200, 2300, 2400, 2500,
    2600, 2700, 2800, 2900, 3000, 3100, 3200, 3300, 3400, 3500, 3600, 3700,
    3800, 3900, 4000, 4100, 4200, 4300, 4400, 4500, 4600, 4700, 4800, 4900,
    5000, 5100, 5200, 5300, 5400, 5500, 5600, 5700, 5800, 5900, 6000,
]
_CURVE_C14: list[float] = [
    0.0, -28.7, 9.3, 143.9, 351.5, 569.0, 729.0, 797.5, 793.3, 777.0, 817.2,
    951.9, 1166.7, 1402.8, 1589.2, 1682.0, 1689.0, 1664.5, 1679.2, 1780.4,
    1965.7, 2184.3, 2365.9, 2460.0, 2464.6, 2427.9, 2421.4, 2500.6, 2674.0,
    2898.8, 3105.0, 3234.2, 3272.6, 3258.1, 3259.4, 3336.6, 3507.8, 3738.3,
    3959.6, 4107.6, 4157.8, 4139.2, 4117.6, 4159.4, 4293.9, 4497.8, 4708.2,
    4858.1, 4914.4, 4896.7, 4866.7, 4893.9, 5017.4, 5223.6, 5454.2, 5638.7,
    5733.4, 5745.8, 5730.0, 5755.9, 5870.1,
]
_CURVE_SIGMA: list[float] = [
    12.0, 12.2, 12.4, 12.7, 12.9, 13.1, 13.3, 13.5, 13.8, 14.0, 14.2, 14.4,
    14.6, 14.9, 15.1, 15.3, 15.5, 15.7, 16.0, 16.2, 16.4, 16.6, 16.8, 17.1,
    17.3, 17.5, 17.7, 17.9, 18.2, 18.4, 18.6, 18.8, 19.0, 19.3, 19.5, 19.7,
    19.9, 20.1, 20.4, 20.6, 20.8, 21.0, 21.2, 21.5, 21.7, 21.9, 22.1, 22.3,
    22.6, 22.8, 23.0, 23.2, 23.4, 23.7, 23.9, 24.1, 24.3, 24.5, 24.8, 25.0,
    25.2,
]

ALGORITHM_VERSION = "cal-gauss-grid-hdi-1"
DEFAULT_PROBABILITY = 0.954
_DENSITY_STEP = 5  # 逐点似然采样步长（cal BP 年）
_NORMAL_TAIL = 4.2  # 以测量值 ±该倍数合成标准差为有效支撑
_EDGE_EPS = 1e-9  # 端点相对权重超过该值视为后验越出曲线支撑；阈值亦用于活动窗口裁剪


@dataclass(frozen=True)
class CurveInfo:
    version: str
    name: str
    min_cal_bp: int
    max_cal_bp: int
    grid_step: int
    points: int


class UnknownCurveError(ValueError):
    """请求了未内置的曲线版本。"""


class CalibrationError(ValueError):
    def __init__(self, message: str, code: str = "calibration_failed"):
        super().__init__(message)
        self.message = message
        self.code = code


def curve_info(version: str = CURVE_VERSION) -> CurveInfo:
    if version != CURVE_VERSION:
        raise UnknownCurveError(version)
    return CurveInfo(CURVE_VERSION, CURVE_NAME, _CURVE_YEARS[0], _CURVE_YEARS[-1], CURVE_GRID_STEP, len(_CURVE_YEARS))


def curve_series(version: str = CURVE_VERSION) -> dict[str, Any]:
    info = curve_info(version)
    return {
        "version": info.version,
        "name": info.name,
        "min_cal_bp": info.min_cal_bp,
        "max_cal_bp": info.max_cal_bp,
        "grid_step": info.grid_step,
        "cal_bp": list(_CURVE_YEARS),
        "c14_age": list(_CURVE_C14),
        "sigma": list(_CURVE_SIGMA),
    }


@dataclass(frozen=True)
class CalibrationInput:
    c14_age: float
    c14_sigma: float
    curve_version: str = CURVE_VERSION
    probability: float = DEFAULT_PROBABILITY

    def digest_payload(self) -> dict[str, Any]:
        return {
            "c14_age": self.c14_age,
            "c14_sigma": self.c14_sigma,
            "curve_version": self.curve_version,
            "probability": round(self.probability, 6),
        }


def validate_input(value: CalibrationInput) -> None:
    try:
        curve_info(value.curve_version)
    except UnknownCurveError:
        raise CalibrationError("未内置的校准曲线版本", "unknown_curve") from None
    if value.c14_sigma <= 0:
        raise CalibrationError("测量误差必须为正数", "sigma_invalid")
    if value.probability <= 0 or value.probability >= 1:
        raise CalibrationError("置信概率必须位于 (0,1) 区间", "probability_invalid")


def _interp(year: float, values: list[float]) -> float:
    """对 100 年等距网格做线性插值；越界使用端点常量值。"""
    years = _CURVE_YEARS
    if year <= years[0]:
        return values[0]
    if year >= years[-1]:
        return values[-1]
    pos = (year - years[0]) / CURVE_GRID_STEP
    idx = int(math.floor(pos))
    frac = pos - idx
    return values[idx] * (1.0 - frac) + values[idx + 1] * frac


def _gaussian(value: float, mu: float, sigma: float) -> float:
    z = (value - mu) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))


def calibrate(value: CalibrationInput) -> dict[str, Any]:
    """计算日历年后验概率分布、最高密度区间与多峰摘要。

    返回纯内置类型字典，便于直接 JSON 序列化与稳定哈希。
    """
    validate_input(value)
    info = curve_info(value.curve_version)

    # 预检：测量年龄必须在曲线碳十四年龄可达范围的若干标准差内，否则整条
    # 支撑上的似然数值下溢，属于缺失区间（曲线未覆盖该年代），明确失败。
    curve_low, curve_high = min(_CURVE_C14), max(_CURVE_C14)
    edge_sigma = max(_CURVE_SIGMA)
    nearest = min(max(value.c14_age, curve_low), curve_high)
    reach = _NORMAL_TAIL * math.sqrt(value.c14_sigma ** 2 + edge_sigma ** 2)
    if abs(value.c14_age - nearest) > reach:
        raise CalibrationError("测量年龄超出内置曲线的可达支撑范围，缺少可用校准区间", "out_of_support")

    # 在整条曲线的日历支撑上等距采样；窗口外的权重数值为零，
    # 因此测量年龄远离曲线时也能得到被端点截断的归一化分布并显式标记。
    years = list(range(info.min_cal_bp, info.max_cal_bp + 1, _DENSITY_STEP))
    weights: list[float] = []
    for year in years:
        mu = _interp(float(year), _CURVE_C14)
        sig = _interp(float(year), _CURVE_SIGMA)
        combined = math.sqrt(value.c14_sigma ** 2 + sig ** 2)
        weights.append(_gaussian(value.c14_age, mu, combined))

    peak_weight = max(weights)
    if peak_weight <= 0 or not math.isfinite(peak_weight):
        raise CalibrationError("后验概率归一化失败", "normalization_failed")
    boundary_extrapolated = (
        weights[0] > _EDGE_EPS * peak_weight or weights[-1] > _EDGE_EPS * peak_weight
    )

    # 只保留有效支撑窗口内的点用于摘要，但归一化仍基于完整支撑。
    total = sum(weights)
    density_full = [w / total for w in weights]
    active = [idx for idx, w in enumerate(weights) if w > _EDGE_EPS * peak_weight]
    lo_idx, hi_idx = active[0], active[-1]
    years = years[lo_idx:hi_idx + 1]
    density = density_full[lo_idx:hi_idx + 1]
    if hi_idx - lo_idx < 2:
        raise CalibrationError("测量年龄在当前曲线支撑区间内没有可用的日历网格点", "no_support")

    # 1 年等宽分箱的归一化概率：每个采样点代表其 _DENSITY_STEP 宽的网格元，
    # 拆成等宽年度分箱，使最高密度区间可以在任意年界处切开并做线性插值。
    bin_prob: dict[int, float] = {}
    for year, prob in zip(years, density):
        for offset in range(_DENSITY_STEP):
            cell = year + offset
            if info.min_cal_bp <= cell <= info.max_cal_bp:
                bin_prob[cell] = prob / _DENSITY_STEP

    hpd = _highest_density_intervals(bin_prob, value.probability)
    modes = _summarize_modes(years, density)

    mean_age = sum(y * p for y, p in zip(years, density))
    variance = sum(p * (y - mean_age) ** 2 for y, p in zip(years, density))
    modal_year = years[max(range(len(years)), key=lambda i: (density[i], -years[i]))]

    return {
        "curve_version": value.curve_version,
        "algorithm_version": ALGORITHM_VERSION,
        "probability": round(value.probability, 6),
        "support_cal_bp": [int(years[0]), int(years[-1])],
        "boundary_extrapolated": boundary_extrapolated,
        "grid_step": _DENSITY_STEP,
        "distribution": [
            {"cal_bp": y, "probability": p}
            for y, p in zip(years, density)
        ],
        "hpd_intervals": hpd,
        "modes": modes,
        "summary": {
            "mean_cal_bp": round(mean_age, 3),
            "mode_cal_bp": int(modal_year),
            "std_cal_bp": round(math.sqrt(max(variance, 0.0)), 3),
        },
    }


def _highest_density_intervals(bin_prob: dict[int, float], probability: float) -> list[dict[str, Any]]:
    """按分箱概率降序累加到目标概率，聚成分开的日历区间。

    跨越目标的临界分箱只按线性比例计入（HPD 阈值在相邻权重间插值），
    使区间覆盖概率精确逼近目标值；分箱为 1 年宽，区间边界可落在任意年界。
    概率脊之间的低概率年份不被选入，于是结果天然呈现彼此分离的若干区间，
    被排除的年份即为缺失区间（gaps）。
    """
    ordered = sorted(bin_prob.items(), key=lambda item: (-item[1], item[0]))
    total = sum(prob for _, prob in ordered)
    target = probability * total
    if target <= 0:
        return []

    full_fraction: dict[int, float] = {}
    accumulated = 0.0
    partial_markers: list[dict[str, Any]] = []
    for year, prob in ordered:
        if accumulated + 1e-12 >= target:
            break
        if accumulated + prob <= target:
            full_fraction[year] = 1.0
            accumulated += prob
        else:
            fraction = (target - accumulated) / prob
            full_fraction[year] = fraction
            partial_markers.append({"cal_bp": year, "fraction": round(fraction, 6)})
            accumulated = target

    intervals: list[dict[str, Any]] = []
    for year in sorted(full_fraction):
        if intervals and year == intervals[-1]["end_cal_bp"] + 1:
            intervals[-1]["end_cal_bp"] = year
        else:
            intervals.append({"start_cal_bp": year, "end_cal_bp": year})

    covered = round(accumulated / total, 6)
    for interval in intervals:
        frac_sum = sum(full_fraction[y] * bin_prob[y]
                       for y in range(interval["start_cal_bp"], interval["end_cal_bp"] + 1))
        interval["target_probability"] = round(probability, 6)
        interval["achieved_probability"] = covered
        interval["probability"] = round(frac_sum / total, 6)
        markers = [marker for marker in partial_markers
                   if interval["start_cal_bp"] <= marker["cal_bp"] <= interval["end_cal_bp"]]
        if markers:
            interval["partial_bins"] = markers
    return intervals


def _summarize_modes(years: list[int], density: list[float]) -> list[dict[str, Any]]:
    """以显著鞍部（山谷）切分概率脊，得到稳定的多峰摘要。

    在平滑的逐点采样密度上找出全部局部极大值；相邻极大值之间取最深谷点，
    只有当谷点相对两侧峰尖具有足够突降度时才作为分割，避免数值抖动伪峰。
    """
    n = len(density)
    if n == 0:
        return []
    global_peak = max(density)

    maxima = [
        i for i in range(1, n - 1)
        if density[i] >= density[i - 1] and density[i] >= density[i + 1]
        and (density[i] > density[i - 1] or density[i] > density[i + 1])
    ]
    cuts: list[int] = []
    for left, right in zip(maxima, maxima[1:]):
        valley = min(range(left + 1, right), key=lambda i: (density[i], i))
        lower_peak = min(density[left], density[right])
        prominent = lower_peak - density[valley] >= 0.05 * global_peak
        separated = density[valley] <= 0.80 * lower_peak
        if prominent and separated:
            cuts.append(valley)

    segments: list[tuple[int, int]] = []
    seg_start = 0
    for valley in cuts:
        segments.append((seg_start, valley))
        seg_start = valley + 1
    segments.append((seg_start, n - 1))

    modes: list[dict[str, Any]] = []
    for lo, hi in segments:
        peak_idx = max(range(lo, hi + 1), key=lambda i: (density[i], -years[i]))
        modes.append({
            "start_cal_bp": years[lo],
            "end_cal_bp": years[hi],
            "peak_cal_bp": years[peak_idx],
            "probability": round(sum(density[lo:hi + 1]), 6),
        })
    # 稳定排序：先按峰内概率降序，再按日历起点降序（较老在前），不依赖集合迭代顺序。
    modes.sort(key=lambda item: (-item["probability"], -item["start_cal_bp"]))
    for rank, mode in enumerate(modes, start=1):
        mode["rank"] = rank
    return modes


def combine_calibrated(
    members: list[dict[str, Any]],
    *,
    probability: float = DEFAULT_PROBABILITY,
    curve_version: str = CURVE_VERSION,
) -> dict[str, Any]:
    """把多份独立测年在同一日历网格上乘积池化，得到阶段证据的组合分布。

    入参 ``members`` 为各自 ``calibrate`` 结果中含 ``distribution`` 的字典
    （仅纳入研究者包含的成员）。共享网格取各成员支撑的交集；若交集为空
    （成员年代在网格上完全不重叠），抛出 ``CalibrationError``。
    """
    included = [member for member in members if member.get("included", True)]
    if not included:
        raise CalibrationError("阶段证据集没有任何包含的成员样品", "no_members")
    if not 0 < probability < 1:
        raise CalibrationError("置信概率必须位于 (0,1) 区间", "probability_invalid")

    common_lo = max(member["support_cal_bp"][0] for member in included)
    common_hi = min(member["support_cal_bp"][1] for member in included)
    if common_hi < common_lo:
        raise CalibrationError("成员测年的日历支撑没有交集，无法在同一网格池化", "no_overlap")

    grid = list(range(common_lo, common_hi + 1, _DENSITY_STEP))
    profiles: list[dict[int, float]] = []
    for member in included:
        profiles.append({point["cal_bp"]: point["probability"] for point in member["distribution"]})

    weights: list[float] = []
    for year in grid:
        product = 1.0
        for profile in profiles:
            product *= profile.get(year, 0.0)
            if product <= 0.0:
                break
        weights.append(product)

    total = sum(weights)
    if total <= 0 or not math.isfinite(total):
        raise CalibrationError("组合后验归一化失败：成员分布在共享网格上无重叠", "combine_normalization_failed")
    density = [w / total for w in weights]
    active = [idx for idx, w in enumerate(weights) if w > _EDGE_EPS * max(weights)]
    lo_idx, hi_idx = (active[0], active[-1]) if active else (0, len(grid) - 1)
    grid = grid[lo_idx:hi_idx + 1]
    density = density[lo_idx:hi_idx + 1]

    bin_prob: dict[int, float] = {}
    for year, prob in zip(grid, density):
        for offset in range(_DENSITY_STEP):
            cell = year + offset
            if cell <= _CURVE_YEARS[-1]:
                bin_prob[cell] = prob / _DENSITY_STEP

    hpd = _highest_density_intervals(bin_prob, probability)
    modes = _summarize_modes(grid, density)
    mean_age = sum(y * p for y, p in zip(grid, density))
    modal_year = grid[max(range(len(grid)), key=lambda i: (density[i], -grid[i]))]

    return {
        "curve_version": curve_version,
        "algorithm_version": ALGORITHM_VERSION,
        "combine_method": "independent-likelihood-product-v1",
        "probability": round(probability, 6),
        "member_count": len(included),
        "support_cal_bp": [int(grid[0]), int(grid[-1])],
        "distribution": [{"cal_bp": y, "probability": p} for y, p in zip(grid, density)],
        "hpd_intervals": hpd,
        "modes": modes,
        "summary": {"mean_cal_bp": round(mean_age, 3), "mode_cal_bp": int(modal_year)},
    }

"""碳十四校准概率引擎（纯标准库、确定性、可离线）。

在 1 年等间距网格上对日历年代 t 计算：

    p(t) ∝ N(测量年龄 | μ(t), √(测量误差² + 曲线误差² + 储库误差²))

其中 μ(t)、σ_curve(t) 由校准曲线节点线性插值得到（边界插值）。
随后在归一化分布上用“最高密度区间（HDI/HPD）”切出 68.3% 与 95.4%
置信区间，并按连续段给出多峰摘要。

所有排序都是稳定排序（同密度时按年代次序打破平局），相同输入必然得到
相同输出。
"""
from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from typing import Any

from app.chrono.curves import STEP as CURVE_STEP
from app.chrono.curves import CalibrationCurve

ALGORITHM_NAME = "grid-gaussian-hpd"
ALGORITHM_VERSION = "1.0.0"
GRID_STEP_YEARS = 1
WINDOW_SIGMA = 6.0
BOUNDARY_RATIO_EPS = 1e-5
PROB_1SIGMA = 0.6827
PROB_2SIGMA = 0.9545


class CalibrationError(ValueError):
    """校准无法完成（例如测量值完全落在曲线覆盖范围之外）。"""


_ASC_CACHE: dict[str, tuple[list[float], list[float], list[float]]] = {}


def _ascending_arrays(curve: CalibrationCurve) -> tuple[list[float], list[float], list[float]]:
    cached = _ASC_CACHE.get(curve.name)
    if cached is None:
        cached = (list(reversed(curve.cal_bp)), list(reversed(curve.c14_bp)), list(reversed(curve.sigma)))
        _ASC_CACHE[curve.name] = cached
    return cached


def interpolate(curve: CalibrationCurve, cal_bp: float) -> tuple[float, float]:
    """曲线线性插值：返回 (μ_14C(t), σ_curve(t))，超出端点时取端点值。"""
    xs, mus, sigs = _ascending_arrays(curve)
    i = bisect_left(xs, cal_bp)
    if i <= 0:
        return mus[0], sigs[0]
    if i >= len(xs):
        return mus[-1], sigs[-1]
    x0, x1 = xs[i - 1], xs[i]
    f = 0.0 if x1 == x0 else (cal_bp - x0) / (x1 - x0)
    mu = mus[i - 1] + (mus[i] - mus[i - 1]) * f
    sig = sigs[i - 1] + (sigs[i] - sigs[i - 1]) * f
    return mu, sig


def calendar_label(cal_bp: float) -> dict[str, Any]:
    """cal BP -> 公元前/公元标识（无 0 年约定）。"""
    year_ce = 1950 - cal_bp
    if year_ce > 0:
        label = f"{int(round(year_ce))} CE"
    else:
        label = f"{int(round(abs(year_ce))) + 1} BCE"
    return {"cal_bp": round(cal_bp, 2), "cal_bce_ce": label}


@dataclass(frozen=True)
class EngineInput:
    c14_age_bp: float
    c14_error_bp: float
    curve: CalibrationCurve
    reservoir_offset_bp: float = 0.0
    reservoir_error_bp: float = 0.0


def _posterior_grid(data: EngineInput) -> tuple[list[int], list[float]]:
    """返回网格年代（升序）与未归一化密度。"""
    corrected_age = data.c14_age_bp - data.reservoir_offset_bp
    lo = int(math.ceil(data.curve.min_cal_bp))
    hi = int(math.floor(data.curve.max_cal_bp))
    years = list(range(lo, hi + 1, GRID_STEP_YEARS))
    weights: list[float] = []
    max_w = 0.0
    for t in years:
        mu_t, sig_curve = interpolate(data.curve, float(t))
        sigma_tot = math.sqrt(data.c14_error_bp ** 2 + sig_curve ** 2 + data.reservoir_error_bp ** 2)
        diff = corrected_age - mu_t
        if abs(diff) > WINDOW_SIGMA * sigma_tot:
            w = 0.0
        else:
            w = math.exp(-0.5 * (diff / sigma_tot) ** 2) / sigma_tot
        weights.append(w)
        if w > max_w:
            max_w = w
    if max_w <= 0.0:
        raise CalibrationError(
            f"测量值 {data.c14_age_bp:g}±{data.c14_error_bp:g} BP 在曲线 "
            f"{data.curve.name} 覆盖范围内没有任何重叠概率"
        )
    return years, weights


def _hpd_intervals(years: list[int], probs: list[float], target: float) -> list[dict[str, Any]]:
    """最高密度区间：稳定地按密度降序选格点，累计达到 target 后合并连续段。"""
    # 稳定排序：密度降序，相同密度时年代升序（年轻者优先，确定且可复现）
    order = sorted(range(len(probs)), key=lambda i: (-probs[i], i))
    chosen: set[int] = set()
    mass = 0.0
    for i in order:
        if probs[i] <= 0.0:
            break
        chosen.add(i)
        mass += probs[i]
        if mass + 1e-12 >= target:
            break
    intervals: list[dict[str, Any]] = []
    run_start: int | None = None
    prev: int | None = None
    for i in sorted(chosen):  # 年代升序遍历，连续格点成段
        if run_start is None:
            run_start = i
        elif prev is not None and i != prev + 1:
            intervals.append(_interval_summary(years, probs, run_start, prev))
            run_start = i
        prev = i
    if run_start is not None and prev is not None:
        intervals.append(_interval_summary(years, probs, run_start, prev))
    # 输出按年代降序（年老在前），稳定
    intervals.sort(key=lambda item: -item["start_cal_bp"])
    return intervals


def _interval_summary(years: list[int], probs: list[float], a: int, b: int) -> dict[str, Any]:
    seg_mass = sum(probs[a : b + 1])
    # 峰位：段内最大密度；并列时取较年长者（扫描到的最后一个最大值）
    peak_idx = a
    for i in range(a, b + 1):
        if probs[i] > probs[peak_idx]:
            peak_idx = i
    half = GRID_STEP_YEARS / 2
    return {
        "start_cal_bp": years[b] + half,  # 年老端
        "end_cal_bp": years[a] - half,  # 年轻端
        "start": calendar_label(years[b] + half),
        "end": calendar_label(years[a] - half),
        "probability": round(seg_mass, 6),
        "peak": {**calendar_label(years[peak_idx]), "density": round(probs[peak_idx], 8)},
    }


def calibrate(data: EngineInput) -> dict[str, Any]:
    if data.c14_error_bp <= 0:
        raise CalibrationError("测量误差必须为正数")
    years, weights = _posterior_grid(data)
    total = sum(weights) * GRID_STEP_YEARS
    probs = [w / total * GRID_STEP_YEARS for w in weights]  # 概率质量，sum≈1

    norm = sum(probs)
    if not math.isfinite(norm) or abs(norm - 1.0) > 1e-6:
        raise CalibrationError(f"概率归一化失败（总和={norm!r}）")

    peak_idx = max(range(len(probs)), key=lambda i: (probs[i], -years[i]))
    intervals_68 = _hpd_intervals(years, probs, PROB_1SIGMA)
    intervals_95 = _hpd_intervals(years, probs, PROB_2SIGMA)

    threshold = BOUNDARY_RATIO_EPS * max(probs)
    truncated_older = probs[-1] > threshold  # 网格最老端仍有不可忽略概率
    truncated_younger = probs[0] > threshold  # 网格最年轻端

    return {
        "posterior": {
            "grid_start_cal_bp": years[0],
            "grid_end_cal_bp": years[-1],
            "grid_step_years": GRID_STEP_YEARS,
            "normalization_sum": round(norm, 10),
            "probabilities": [round(p, 8) for p in probs],
        },
        "mode": calendar_label(years[peak_idx]),
        "hpd68": intervals_68,
        "hpd95": intervals_95,
        "is_multimodal": len(intervals_68) > 1,
        "truncated_at_curve_boundary": truncated_older or truncated_younger,
        "boundary_warnings": {
            "older_end": truncated_older,
            "younger_end": truncated_younger,
        },
        "algorithm": {
            "name": ALGORITHM_NAME,
            "version": ALGORITHM_VERSION,
            "grid_step_years": GRID_STEP_YEARS,
            "window_sigma": WINDOW_SIGMA,
            "curve_node_step_years": CURVE_STEP,
        },
    }

"""内置小型校准曲线数据，可离线使用。

每条曲线是等间距（5 年，cal BP 降序）的三元组：
(cal_bp, c14_bp, c14_sigma)。

数据是为离线服务构造的小型教学/测试数据集，覆盖若干特征区段：
* 3100–2900 cal BP：平台 + 折返，校准后呈多峰；
* 2900–2050 cal BP：近似单调斜线，校准后呈单峰。

它不是完整 IntCal 曲线；正式年代结论应载入完整曲线文件。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

STEP = 5  # 年（cal BP），曲线节点等间距
CURVE_VERSION = "mini-2026.1"


def _section_plateau() -> list[tuple[float, float, float]]:
    """3100–2900 cal BP：围绕 14C≈2850 BP 的平台与折返，制造多峰。"""
    return [
        (3100, 2870, 25),
        (3095, 2862, 25),
        (3090, 2855, 25),
        (3085, 2850, 25),
        (3080, 2848, 25),
        (3075, 2850, 25),
        (3070, 2856, 25),
        (3065, 2862, 25),
        (3060, 2866, 25),
        (3055, 2866, 25),
        (3050, 2862, 25),
        (3045, 2856, 25),
        (3040, 2850, 25),
        (3035, 2846, 25),
        (3030, 2846, 25),
        (3025, 2850, 25),
        (3020, 2857, 25),
        (3015, 2863, 25),
        (3010, 2867, 25),
        (3005, 2867, 25),
        (3000, 2863, 25),
        (2995, 2857, 25),
        (2990, 2851, 25),
        (2985, 2847, 25),
        (2980, 2847, 25),
        (2975, 2851, 25),
        (2970, 2857, 25),
        (2965, 2862, 25),
        (2960, 2864, 25),
        (2955, 2862, 25),
        (2950, 2857, 25),
        (2945, 2851, 25),
        (2940, 2846, 25),
        (2935, 2844, 25),
        (2930, 2846, 25),
        (2925, 2852, 25),
        (2920, 2858, 25),
        (2915, 2861, 25),
        (2910, 2860, 25),
        (2905, 2856, 25),
    ]


def _linear(c0: float, m0: float, c1: float, m1: float, *, sigma: float = 25.0) -> list[tuple[float, float, float]]:
    """生成 cal BP 从 c0 到 c1（不含 c1）的等间距线性节点。"""
    n = round((c0 - c1) / STEP)
    return [(c0 - k * STEP, m0 + (m1 - m0) * (k / n), sigma) for k in range(n)]


def _build() -> dict[str, list[tuple[float, float, float]]]:
    intcal = (
        _section_plateau()
        + _linear(2900, 2850, 2445, 2395)
        + _linear(2445, 2395, 2200, 2150)
        + _linear(2200, 2150, 2050, 2000)
        + [(2050, 2000, 25.0)]
    )
    # 海洋曲线：同一日历尺度加储库效应（+400 14C 年），测量误差合成海洋曲线误差
    marine = [(c, m + 400.0, (s ** 2 + 30.0 ** 2) ** 0.5) for c, m, s in intcal]
    return {"INTCAL23-MINI": intcal, "MARINE23-MINI": marine}


@dataclass(frozen=True)
class CalibrationCurve:
    name: str
    version: str
    cal_bp: list[float]
    c14_bp: list[float]
    sigma: list[float]

    @property
    def min_cal_bp(self) -> float:
        return self.cal_bp[-1]

    @property
    def max_cal_bp(self) -> float:
        return self.cal_bp[0]

    def summary(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "node_count": len(self.cal_bp),
            "step_years": STEP,
            "cal_bp_range": [self.min_cal_bp, self.max_cal_bp],
            "checksum_sha256": checksum(self),
        }


def checksum(curve: CalibrationCurve) -> str:
    body = "\n".join(f"{c:.1f},{m:.3f},{s:.3f}" for c, m, s in zip(curve.cal_bp, curve.c14_bp, curve.sigma))
    return hashlib.sha256(f"{curve.name}|{curve.version}\n{body}".encode()).hexdigest()


def _make_curve(name: str, rows: list[tuple[float, float, float]]) -> CalibrationCurve:
    rows = sorted(rows, key=lambda r: -r[0])  # cal BP 降序
    return CalibrationCurve(
        name=name,
        version=CURVE_VERSION,
        cal_bp=[r[0] for r in rows],
        c14_bp=[r[1] for r in rows],
        sigma=[r[2] for r in rows],
    )


CURVES: dict[str, CalibrationCurve] = {name: _make_curve(name, rows) for name, rows in _build().items()}


def get_curve(name: str) -> CalibrationCurve:
    try:
        return CURVES[name]
    except KeyError as exc:
        raise KeyError(f"未知校准曲线: {name}") from exc


def list_curves() -> list[dict]:
    return [curve.summary() for curve in CURVES.values()]

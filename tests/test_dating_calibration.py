"""校准算法数值测试：概率归一化、边界插值、缺失区间、失败恢复、稳定排序。"""
from __future__ import annotations

import math

import pytest

from app.dating.calibration import (
    ALGORITHM_VERSION,
    CURVE_VERSION,
    CalibrationError,
    CalibrationInput,
    _CURVE_C14,
    _CURVE_SIGMA,
    _CURVE_YEARS,
    _interp,
    calibrate,
    combine_calibrated,
)


def test_distribution_normalized_to_one():
    for age, sigma in [(2500, 30), (1680, 20), (300, 25), (5800, 40), (100, 60)]:
        result = calibrate(CalibrationInput(age, sigma))
        total = sum(point["probability"] for point in result["distribution"])
        assert abs(total - 1.0) < 1e-8, (age, total)
        assert all(math.isfinite(point["probability"]) for point in result["distribution"])
        assert result["algorithm_version"] == ALGORITHM_VERSION
        assert result["curve_version"] == CURVE_VERSION


def test_year_bins_partition_to_hpd_coverage():
    for probability in (0.5, 0.683, 0.954, 0.99):
        result = calibrate(CalibrationInput(2500, 30, probability=probability))
        coverage = sum(interval["probability"] for interval in result["hpd_intervals"])
        assert abs(coverage - probability) < 2e-3, (probability, coverage)
        for interval in result["hpd_intervals"]:
            assert interval["target_probability"] == round(probability, 6)
            assert interval["start_cal_bp"] <= interval["end_cal_bp"]
            assert 0.0 <= interval["probability"] <= 1.0


def test_curve_linear_interpolation_inside_and_flat_outside():
    # 节点处取值
    assert _interp(100.0, _CURVE_C14) == _CURVE_C14[1]
    # 中点严格线性
    midpoint = (_CURVE_C14[1] + _CURVE_C14[2]) / 2
    assert _interp(150.0, _CURVE_C14) == pytest.approx(midpoint)
    assert _interp(125.0, _CURVE_SIGMA) == pytest.approx(0.75 * _CURVE_SIGMA[1] + 0.25 * _CURVE_SIGMA[2])
    # 越界使用端点常量（不外推趋势）
    assert _interp(-500.0, _CURVE_C14) == _CURVE_C14[0]
    assert _interp(9000.0, _CURVE_C14) == _CURVE_C14[-1]


def test_boundary_extrapolation_flagged_but_still_normalized():
    inside = calibrate(CalibrationInput(2500, 30))
    assert inside["boundary_extrapolated"] is False
    for age in (40, 5960):
        result = calibrate(CalibrationInput(age, 30))
        assert result["boundary_extrapolated"] is True
        assert abs(sum(p["probability"] for p in result["distribution"]) - 1.0) < 1e-8
        coverage = sum(i["probability"] for i in result["hpd_intervals"])
        assert abs(coverage - 0.954) < 2e-3
    # 支撑窗口的一端必须贴着曲线边界
    low = calibrate(CalibrationInput(40, 30))
    high = calibrate(CalibrationInput(5960, 30))
    assert low["support_cal_bp"][0] == _CURVE_YEARS[0]
    assert high["support_cal_bp"][1] == _CURVE_YEARS[-1]


def test_missing_intervals_produce_separated_hpd_and_modes():
    result = calibrate(CalibrationInput(2500, 30))
    intervals = result["hpd_intervals"]
    assert len(intervals) >= 2, "摆动区段应产生彼此分离的置信区间"
    # 区间之间存在缺失年份（gap）
    for earlier, later in zip(intervals, intervals[1:]):
        assert later["start_cal_bp"] > earlier["end_cal_bp"] + 1
        gap_years = list(range(earlier["end_cal_bp"] + 1, later["start_cal_bp"]))
        profile = {p["cal_bp"]: p["probability"] for p in result["distribution"]}
        gap_mass = sum(profile.get(y, 0.0) for y in gap_years)
        assert gap_mass < 0.02 * (later["end_cal_bp"] - earlier["start_cal_bp"] + 1)
    # 多峰摘要至少识别出两个峰，且峰序与概率一致
    peaks = [mode["peak_cal_bp"] for mode in result["modes"]]
    assert len(peaks) >= 2
    assert result["modes"][0]["rank"] == 1


def test_out_of_support_and_invalid_inputs_fail():
    with pytest.raises(CalibrationError) as exc:
        calibrate(CalibrationInput(12000, 30))
    assert exc.value.code == "out_of_support"
    with pytest.raises(CalibrationError):
        calibrate(CalibrationInput(-5000, 30))
    with pytest.raises(CalibrationError) as exc:
        calibrate(CalibrationInput(100, 0))
    assert exc.value.code == "sigma_invalid"
    with pytest.raises(CalibrationError) as exc:
        calibrate(CalibrationInput(100, 10, probability=1.0))
    assert exc.value.code == "probability_invalid"
    with pytest.raises(CalibrationError) as exc:
        calibrate(CalibrationInput(100, 10, curve_version="IntCal99"))
    assert exc.value.code == "unknown_curve"


def test_recovery_after_failure():
    # 失败输入抛错后，合法输入仍可正常计算
    with pytest.raises(CalibrationError):
        calibrate(CalibrationInput(12000, 30))
    result = calibrate(CalibrationInput(2500, 30))
    assert abs(sum(p["probability"] for p in result["distribution"]) - 1.0) < 1e-8


def test_modes_stably_sorted_and_deterministic():
    result = calibrate(CalibrationInput(2500, 30))
    probs = [mode["probability"] for mode in result["modes"]]
    assert probs == sorted(probs, reverse=True)
    # 概率相等时按日历起点降序（较老在前）
    ranked = [(mode["rank"], mode["probability"], mode["start_cal_bp"]) for mode in result["modes"]]
    for (r1, p1, s1), (r2, p2, s2) in zip(ranked, ranked[1:]):
        assert r1 < r2
        if p1 == p2:
            assert s1 > s2
    # 重复计算结果逐字节一致（不依赖集合/字典迭代顺序）
    again = calibrate(CalibrationInput(2500, 30))
    assert again["modes"] == result["modes"]
    assert again["hpd_intervals"] == result["hpd_intervals"]


def test_combined_phase_distribution():
    a = calibrate(CalibrationInput(2500, 30))
    b = calibrate(CalibrationInput(2480, 35))
    combined = combine_calibrated([dict(a, included=True), dict(b, included=True)])
    assert combined["member_count"] == 2
    assert abs(sum(p["probability"] for p in combined["distribution"]) - 1.0) < 1e-8
    coverage = sum(i["probability"] for i in combined["hpd_intervals"])
    assert abs(coverage - 0.954) < 2e-3
    # 无成员 / 完全不重叠
    with pytest.raises(CalibrationError) as exc:
        combine_calibrated([])
    assert exc.value.code == "no_members"
    far = calibrate(CalibrationInput(300, 20))
    near = calibrate(CalibrationInput(5800, 20))
    with pytest.raises(CalibrationError):
        combine_calibrated([dict(far, included=True), dict(near, included=True)])

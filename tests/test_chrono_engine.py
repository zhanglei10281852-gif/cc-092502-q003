"""校准引擎数值测试：归一化、边界插值、缺失区间、失败与稳定排序。"""
from __future__ import annotations

import math

import pytest

from app.chrono.curves import STEP as CURVE_STEP
from app.chrono.curves import get_curve
from app.chrono.engine import (
    CalibrationError,
    EngineInput,
    GRID_STEP_YEARS,
    PROB_1SIGMA,
    PROB_2SIGMA,
    _hpd_intervals,
    calibrate,
    interpolate,
)

INTCAL = get_curve("INTCAL23-MINI")
MARINE = get_curve("MARINE23-MINI")


def test_curve_nodes_are_regular_and_sorted():
    cal = INTCAL.cal_bp
    assert cal[0] > cal[-1]  # 降序：老 -> 年轻
    diffs = {round(cal[i] - cal[i + 1], 6) for i in range(len(cal) - 1)}
    assert diffs == {CURVE_STEP}
    assert len(INTCAL.c14_bp) == len(cal) == len(INTCAL.sigma)


def test_interpolation_midpoint_and_boundaries():
    # 取两个相邻节点验证线性中点
    xs = list(reversed(INTCAL.cal_bp))
    x0, x1 = xs[0], xs[1]
    mu0, sig0 = interpolate(INTCAL, x0)
    mu1, sig1 = interpolate(INTCAL, x1)
    mum, sigm = interpolate(INTCAL, (x0 + x1) / 2)
    assert math.isclose(mum, (mu0 + mu1) / 2, rel_tol=1e-12)
    assert math.isclose(sigm, (sig0 + sig1) / 2, rel_tol=1e-12)
    # 节点处必须精确取节点值
    assert math.isclose(interpolate(INTCAL, x0)[0], INTCAL.c14_bp[-1], rel_tol=1e-12)
    # 超出曲线两端：钳制到端点而不是外推
    assert interpolate(INTCAL, INTCAL.min_cal_bp - 500) == (mu0, sig0)
    mu_old, sig_old = interpolate(INTCAL, INTCAL.max_cal_bp)
    assert interpolate(INTCAL, INTCAL.max_cal_bp + 500) == (mu_old, sig_old)


@pytest.mark.parametrize("age,error,curve", [(2300, 30, INTCAL), (2850, 25, INTCAL), (2700, 40, MARINE)])
def test_posterior_normalizes_to_one(age, error, curve):
    result = calibrate(EngineInput(age, error, curve))
    probs = result["posterior"]["probabilities"]
    total = sum(probs)
    assert math.isclose(total, 1.0, abs_tol=1e-4)
    assert result["posterior"]["normalization_sum"] == pytest.approx(1.0, abs=1e-6)
    assert all(p >= 0 for p in probs)
    # 网格步长与覆盖范围
    assert result["posterior"]["grid_step_years"] == GRID_STEP_YEARS
    assert result["posterior"]["grid_start_cal_bp"] == int(curve.min_cal_bp)
    assert result["posterior"]["grid_end_cal_bp"] == int(curve.max_cal_bp)


def test_unimodal_hpd_masses_and_ordering():
    result = calibrate(EngineInput(2300, 30, INTCAL))
    assert result["is_multimodal"] is False
    for intervals, target in ((result["hpd68"], PROB_1SIGMA), (result["hpd95"], PROB_2SIGMA)):
        mass = sum(item["probability"] for item in intervals)
        assert target - 1e-9 <= mass <= target + 0.01  # 离散网格最多多出一个格点
        # 区间输出按年代降序（年老端在前）
        starts = [item["start_cal_bp"] for item in intervals]
        assert starts == sorted(starts, reverse=True)
        for item in intervals:
            assert item["start_cal_bp"] >= item["end_cal_bp"]


def test_multimodal_has_missing_intervals_between_peaks():
    result = calibrate(EngineInput(2850, 25, INTCAL))
    assert result["is_multimodal"] is True
    assert len(result["hpd68"]) >= 2
    # 相邻峰之间必须存在“缺失区间”：年带空档
    older, younger = result["hpd68"][0], result["hpd68"][1]
    gap = older["end_cal_bp"] - younger["start_cal_bp"]
    assert gap >= GRID_STEP_YEARS
    # 每个峰都有独立峰位与概率
    peaks = [item["peak"]["cal_bp"] for item in result["hpd68"]]
    assert len(set(peaks)) == len(peaks)
    assert all(item["probability"] > 0 for item in result["hpd68"])
    # 95.4% 区间更宽，峰数不少于 68.3%（通常合并成更少的大段）
    assert sum(i["probability"] for i in result["hpd95"]) >= PROB_2SIGMA - 1e-9


def test_boundary_truncation_flag_vs_out_of_range_failure():
    # 贴近年轻端：分布被截断，显式标记
    near = calibrate(EngineInput(2012, 20, INTCAL))
    assert near["truncated_at_curve_boundary"] is True
    assert near["boundary_warnings"]["younger_end"] is True
    assert near["boundary_warnings"]["older_end"] is False
    # 远离曲线覆盖范围：直接失败，不产出伪结果
    with pytest.raises(CalibrationError):
        calibrate(EngineInput(5000, 20, INTCAL))
    with pytest.raises(CalibrationError):
        calibrate(EngineInput(2300, 0, INTCAL))


def test_deterministic_and_stable_tie_breaking():
    r1 = calibrate(EngineInput(2850, 25, INTCAL))
    r2 = calibrate(EngineInput(2850, 25, INTCAL))
    assert r1["posterior"]["probabilities"] == r2["posterior"]["probabilities"]
    assert r1["hpd68"] == r2["hpd68"]
    assert r1["mode"] == r2["mode"]

    # 构造带大量并列的概率，验证同密度按年代升序入选、结果按年代降序稳定输出
    years = [2100, 2101, 2102, 2103, 2104]
    probs = [0.2, 0.2, 0.2, 0.2, 0.2]
    intervals = _hpd_intervals(years, probs, 0.6)  # 恰好需要 3 个格点
    # 并列时取较年轻的三个（2100..2102），单段，年老端 2102.5
    assert len(intervals) == 1
    assert intervals[0]["start_cal_bp"] == 2102.5
    assert intervals[0]["end_cal_bp"] == 2099.5
    assert intervals[0]["probability"] == pytest.approx(0.6)

    # 两个等高峰 -> 输出两段，顺序固定为年老在前
    probs2 = [0.25, 0.0, 0.25, 0.25, 0.25]
    two = _hpd_intervals(years, probs2, 0.75)
    assert [iv["start_cal_bp"] for iv in two] == [2103.5, 2100.5]
    assert two[0]["peak"]["cal_bp"] == 2102  # 段内并列保留扫描到的首个（较年轻）最大值


def test_reservoir_error_widens_distribution():
    narrow = calibrate(EngineInput(2300, 30, INTCAL))
    wide = calibrate(EngineInput(2300, 30, INTCAL, reservoir_error_bp=80))
    width_n = narrow["hpd95"][0]["start_cal_bp"] - narrow["hpd95"][0]["end_cal_bp"]
    width_w = wide["hpd95"][0]["start_cal_bp"] - wide["hpd95"][0]["end_cal_bp"]
    assert width_w > width_n

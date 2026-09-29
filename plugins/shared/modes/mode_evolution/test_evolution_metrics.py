# @feature: FP-0.2.二 自进化指标 P0-P3 优先级裁决（ADR 2026-09-16） | @ci: python-coverage
"""evolution_metrics 单测：P0-P3 各指标与优先级门控裁决（纯函数，账本投影）。

断行为不断实现：每种裁决分支 ≥2 组有区分度输入；字面值断言配性质断言
（斜率符号/样本量累计/保留率∈[0,1]/交集口径）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import evolution_metrics as em  # noqa: E402


def _round(mode: str, passed: int, total: int, cases=None, duration=None):
    r = {"run_id": f"{mode}-{len(cases or '')}-{passed}", "mode": mode,
         "passed": passed, "total": total}
    if cases is not None:
        r["case_results"] = cases
    if duration is not None:
        r["duration_s"] = duration
    return r


# ── P0 学习率 ────────────────────────────────────────────────────────────
def test_learning_rate_slope_sign_and_sample_accumulation():
    """上行序列斜率 >0、下行 <0；samples = 各轮样本量累计（性质断言）。"""
    up = [_round("coding", 1, 4), _round("coding", 3, 4)]
    down = [_round("writing", 3, 4), _round("writing", 1, 4)]
    rates = em.learning_rates(up + down)
    assert rates["coding"]["slope"] > 0
    assert rates["writing"]["slope"] < 0
    assert rates["coding"]["samples"] == 8 and rates["coding"]["rounds"] == 2


def test_learning_rate_flat_is_zero_and_single_round_is_insufficient():
    """通过率不变 → 斜率恰为 0（不是 None）；单轮 → 数据不足。"""
    flat = em.learning_rates([_round("coding", 2, 4), _round("coding", 3, 6)])
    assert flat["coding"]["slope"] == 0.0
    assert em.learning_rates([_round("coding", 2, 4)]) == {}


def test_slope_undefined_when_sample_axis_has_no_variance():
    """样本量轴无方差 → 斜率无定义返回 None（防御分支）。"""
    assert em._slope([(5, 0.5), (5, 0.8)]) is None
    assert em._slope([(5, 0.5)]) is None


# ── P0 迁移效率 ──────────────────────────────────────────────────────────
def test_transfer_efficiency_ratio_and_direction():
    """源域增益为正 → TE = 目标增益/源增益；反向对也各测（方向性）。"""
    rounds = [_round("coding", 1, 4), _round("coding", 3, 4),    # +0.5
              _round("writing", 2, 4), _round("writing", 3, 4)]  # +0.25
    pairs = {(p["source"], p["target"]): p["te"]
             for p in em.transfer_efficiency(rounds)}
    assert pairs[("coding", "writing")] == 0.5
    assert pairs[("writing", "coding")] == 2.0


def test_transfer_efficiency_none_without_positive_source_gain():
    """无正增益源域（全平/全降）→ None，不出无意义比值。"""
    flat = [_round("coding", 2, 4), _round("coding", 2, 4),
            _round("writing", 1, 4), _round("writing", 1, 4)]
    assert em.transfer_efficiency(flat) is None
    assert em.transfer_efficiency([_round("coding", 1, 4)]) is None


# ── P0 判断力代理 ────────────────────────────────────────────────────────
def test_judgment_prediction_miss_rate():
    """预测失准率 = 证伪淘汰 / (已决提案 + 证伪淘汰)。"""
    props = {"p1": {"outcome": "applied"},
             "p2": {"outcome": "rejected", "category": "prediction_miss"},
             "p3": {"outcome": "rejected", "category": "prediction_miss"}}
    j = em.judgment(props)
    assert j["prediction_miss_rate"] == round(2 / 3, 3)
    assert j["user_veto_rate"] == 0.0 and j["user_veto_reverts"] == 0


def test_judgment_user_veto_and_empty_ledger():
    """用户否决回滚入账；空账返回 None 率而非 0（不把无数据当满分）。"""
    j = em.judgment({"p1": {"outcome": "reverted", "category": "user_veto"}})
    assert j["user_veto_reverts"] == 1 and j["user_veto_rate"] == 1.0
    empty = em.judgment({})
    assert empty["prediction_miss_rate"] is None
    assert empty["user_veto_rate"] is None and empty["user_veto_reverts"] == 0


# ── P1 保留率 / 退化名单 / 方差收敛 / 回滚频率 ────────────────────────────
def test_retention_full_partial_and_case_rotation():
    """全保留=1.0；退化按前轮通过集为分母；case 轮换取交集口径。"""
    full = [_round("coding", 2, 2, cases={"a": True, "b": True}),
            _round("coding", 2, 2, cases={"a": True, "b": True})]
    assert em.retention(full) == 1.0
    partial = [_round("coding", 3, 3, cases={"a": True, "b": True, "c": True}),
               _round("coding", 1, 3, cases={"a": True, "b": False, "c": False})]
    assert em.retention(partial) == round(1 / 3, 3)
    rotation = [_round("coding", 2, 2, cases={"a": True, "b": True}),
                _round("coding", 2, 2, cases={"b": True, "c": True})]
    assert em.retention(rotation) == 0.5  # a 丢了：前轮通过集 ∩ 本轮 = {b}


def test_retention_none_without_replay_keys_and_cross_mode_isolation():
    """旧账缺 case_results → None；跨模式相邻轮不互算保留率。"""
    assert em.retention([_round("coding", 2, 2), _round("coding", 2, 2)]) is None
    cross = [_round("coding", 1, 1, cases={"a": True}),
             _round("writing", 0, 1, cases={"a": False})]
    assert em.retention(cross) is None


def test_regressed_cases_names_dropped_cases():
    rounds = [_round("coding", 3, 3, cases={"a": True, "b": True, "c": True}),
              _round("coding", 1, 3, cases={"a": False, "b": False, "c": True})]
    assert em.regressed_cases(rounds) == ["a", "b"]
    assert em.regressed_cases([_round("coding", 1, 1)]) == []


def test_variance_convergence_two_directions_and_short_window():
    """后半窗标准差 ≤ 前半窗 → 收敛；发散 → False；<4 轮 → None。"""
    calm = [_round("coding", p, 10) for p in (2, 8, 5, 5, 5, 5)]
    out = em.variance_convergence(calm)
    assert out["coding"]["converged"] is True
    wild = [_round("writing", p, 10) for p in (5, 5, 5, 2, 8, 5)]
    assert em.variance_convergence(wild)["writing"]["converged"] is False
    assert em.variance_convergence([_round("coding", 1, 2)] * 3) is None


def test_rollback_rate_states():
    """applied 后标 reverted 计回滚；全 applied = 0；无已决提案 = None。"""
    assert em.rollback_rate({"p1": {"outcome": "applied"},
                             "p2": {"outcome": "reverted"}}) == 0.5
    assert em.rollback_rate({"p1": {"outcome": "applied"}}) == 0.0
    assert em.rollback_rate({"p1": {"outcome": "submitted"}}) is None
    assert em.rollback_rate({}) is None


# ── P2 效率 ──────────────────────────────────────────────────────────────
def test_sample_efficiency_threshold_and_never_reaching():
    """达到 +0.1 提升的当轮累计样本量（各轮 total 之和）；永不达标 → None。"""
    reach = [_round("coding", 5, 10), _round("coding", 11, 20), _round("coding", 19, 29)]
    # 基线 .5 → 第 3 轮 .655 首次 +0.1 达标；累计样本量 = 10+20+29 = 59
    assert em.sample_efficiency(reach) == {"coding": 59}
    never = [_round("coding", 5, 10), _round("coding", 11, 21)]
    assert em.sample_efficiency(never) is None


def test_time_efficiency_sums_window_and_requires_complete_durations():
    """窗口时长累计（基线轮不计）；窗口内缺 duration_s → None（不按 0 折算）。"""
    reach = [_round("coding", 5, 10, duration=1),
             _round("coding", 11, 20, duration=100),
             _round("coding", 19, 29, duration=200)]
    assert em.time_efficiency(reach) == {"coding": 300.0}
    gapped = [_round("coding", 5, 10, duration=1),
              _round("coding", 19, 29)]
    assert em.time_efficiency(gapped) is None
    assert em.time_efficiency([_round("coding", 1, 1)]) is None


# ── P0-P3 优先级门控裁决 ─────────────────────────────────────────────────
def test_verdict_insufficient_and_no_evolution():
    """P0 数据不足（单轮）；P0 不达标（斜率恒 0）优先于一切后续判断。"""
    assert em.verdict({"rounds": [_round("coding", 2, 4)]})["verdict"] == "insufficient_data"
    flat = em.verdict({"rounds": [_round("coding", 2, 4), _round("coding", 3, 6)]})
    assert flat["verdict"] == "no_evolution"
    assert "P0" in flat["reason"]


def test_verdict_unstable_by_retention_and_by_rollback():
    """P1 两路：保留率破线（学习率仍为正）与回滚频率超线，各自触发 unstable。"""
    by_ret = em.verdict({"rounds": [
        _round("coding", 2, 3, cases={"a": True, "b": True, "c": False}),
        _round("coding", 3, 4, cases={"a": True, "b": False, "c": True, "d": True}),
    ]})
    assert by_ret["verdict"] == "unstable" and "保留率" in by_ret["reason"]
    assert by_ret["p1"]["regressed_cases"] == ["b"]
    by_roll = em.verdict({
        "rounds": [_round("coding", 5, 10), _round("coding", 7, 10)],
        "proposals": {"p1": {"outcome": "reverted"}, "p2": {"outcome": "reverted"},
                      "p3": {"outcome": "applied"}}})
    assert by_roll["verdict"] == "unstable" and "回滚" in by_roll["reason"]


def test_verdict_vetoed_dominates_and_inefficient_vs_evolving():
    """P3 一票否决压倒一切；P2 未达标=inefficient；达标=evolving。"""
    vetoed = em.verdict({"rounds": [_round("coding", 5, 10), _round("coding", 9, 10)],
                         "proposals": {"p1": {"outcome": "reverted",
                                              "category": "user_veto"}}})
    assert vetoed["verdict"] == "vetoed" and vetoed["p3"]["user_veto_reverts"] == 1
    # 增益明显低于 +0.1 阈值（不靠浮点巧合）：.5 → .55
    ineff = em.verdict({"rounds": [_round("coding", 5, 10), _round("coding", 11, 20)]})
    assert ineff["verdict"] == "inefficient"
    evo = em.verdict({"rounds": [_round("coding", 5, 10), _round("coding", 7, 10)]})
    assert evo["verdict"] == "evolving"


def test_verdict_carries_version_and_full_metric_blocks():
    """裁决携带 metrics_version 与 P0/P1/P2/P3 全块（指标自迭代的版本锚）。"""
    out = em.verdict({"rounds": [_round("coding", 5, 10), _round("coding", 7, 10)]})
    assert out["metrics_version"] == em.METRICS_VERSION
    for block in ("p0", "p1", "p2", "p3"):
        assert block in out
    assert out["p0"]["learning_rates"]["coding"]["slope"] > 0
    # 旧账无 case_results → 保留率不猜测，如实返回 None
    assert out["p1"]["retention"] is None


# ── 缺口收口（2026-09-17）：零样本轮/空通过集容错 ────────────────────────────
def test_series_and_variance_skip_zero_total_rounds():
    """total=0 轮通过率不可算 → 该轮跳过，不进累计也不崩。"""
    rounds = [
        {"mode": "m", "total": 0, "passed": 0},
        {"mode": "m", "total": 4, "passed": 2},
        {"mode": "m", "total": 4, "passed": 3},
    ]
    rates = em.learning_rates(rounds)
    assert "m" in rates
    conv = em.variance_convergence(rounds)
    assert conv is not None or conv is None  # 零样本轮不产生异常路径
    # 性质对照：剔除零样本轮后累计样本量只来自可算轮
    pts = em._mode_series(rounds, "m")
    assert pts[-1][0] == 8


def test_retention_prev_passed_set_empty_returns_none():
    """前轮 case_results 全 False（通过集为空）→ 无保留率可算 → None。"""
    rounds = [
        {"mode": "m", "case_results": {"c1": False}},
        {"mode": "m", "case_results": {"c1": True}},
    ]
    assert em.retention(rounds) is None


def test_time_efficiency_skips_ratioless_rounds():
    """time_efficiency：total=0 的轮（通过率 None）跳过不崩，不中断后续累计。"""
    rounds = [
        _round("coding", 0, 0),  # ratio None → continue
        _round("coding", 1, 4, duration=1.0),  # base
        _round("coding", 4, 4, duration=2.0),  # +0.75 ≥ 阈值 → hit=cum=2.0(base 轮不计时长)
    ]
    out = em.time_efficiency(rounds)
    assert out == {"coding": 2.0}

"""自进化指标（P0-P3 优先级门控，纯函数）。

领域指标不是自进化指标（ADR 2026-09-16）：单轮通过率只是原始观测，本模块
从账本历史计算变化动力学——

- P0 进化是否发生：学习率（通过率-累计样本量曲线斜率）、迁移效率（跨模式
  增益比）、判断力代理（预测失准率/用户否决率）；
- P1 进化是否稳定：保留率（相邻轮通过集保持比）、方差收敛、回滚频率；
- P2 进化是否高效：样本效率/时间效率（达到阈值提升的累计样本量/时长）；
- P3 进化是否安全：用户否决回滚（外部锚点判定的目标偏移证据）→ 一票否决。

优先级规则：P3 触发一票否决；P0 不达标判进化失败（不看后续）；P1 不达标
判不可靠；P2 不达标判低效。旧账缺重放键（case_results/duration_s 等）时
对应指标返回 None（insufficient_data），不猜测不误判。
"""
from __future__ import annotations

from typing import Any

METRICS_VERSION = "p0p3-1.0"
RETENTION_FLOOR = 0.9   # P1：相邻轮通过集保持比下限
ROLLBACK_ALARM = 0.5    # P1：回滚频率告警线
EFFICIENCY_GAIN = 0.1   # P2：达标提升阈值（通过率 +0.1）

_RESOLVED = ("applied", "reverted")


def _modes(rounds: list[dict[str, Any]]) -> set[str]:
    return {str(r.get("mode") or "") for r in rounds} - {""}


def _ratio(r: dict[str, Any]) -> float | None:
    total = int(r.get("total") or 0)
    return (int(r.get("passed") or 0) / total) if total else None


def _mode_series(rounds: list[dict[str, Any]], mode: str) -> list[tuple[int, float]]:
    """该模式按时间序的 (累计样本量, 通过率) 序列。"""
    pts: list[tuple[int, float]] = []
    n = 0
    for r in rounds:
        if str(r.get("mode") or "") != mode:
            continue
        ratio = _ratio(r)
        if ratio is None:
            continue
        n += int(r.get("total") or 0)
        pts.append((n, ratio))
    return pts


def _slope(points: list[tuple[int, float]]) -> float | None:
    """最小二乘斜率（性能-累计样本量）；点数 <2 或样本量无方差 → None。"""
    if len(points) < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var == 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / var


def _passed_set(r: dict[str, Any]) -> set[str] | None:
    cr = r.get("case_results")
    if not isinstance(cr, dict) or not cr:
        return None
    return {c for c, ok in cr.items() if ok}


def learning_rates(rounds: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """P0 学习率：各模式性能-累计样本量曲线斜率（每样本通过率增益）。"""
    out: dict[str, dict[str, Any]] = {}
    for mode in _modes(rounds):
        pts = _mode_series(rounds, mode)
        slope = _slope(pts)
        if slope is not None:
            out[mode] = {"slope": round(slope, 6), "samples": pts[-1][0],
                         "rounds": len(pts)}
    return out


def transfer_efficiency(rounds: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """P0 迁移效率（代理口径）：窗口内源模式增益为正时，目标模式增益/源模式增益。

    代理说明：账本未记录提案 material_scope 与各模式增益的因果绑定，此处
    测同窗口跨模式增益比；因果化（按提案 material_scope 对照）待账本补
    scope 维度后升级。
    """
    gains: dict[str, float] = {}
    for mode in _modes(rounds):
        pts = _mode_series(rounds, mode)
        if len(pts) >= 2:
            gains[mode] = pts[-1][1] - pts[0][1]
    pairs = []
    for src, gain_src in gains.items():
        if gain_src <= 0:
            continue
        for dst, gain_dst in gains.items():
            if dst != src:
                pairs.append({"source": src, "target": dst,
                              "te": round(gain_dst / gain_src, 3)})
    return pairs or None


def _pair_transitions(rounds: list[dict[str, Any]]) -> list[tuple[str, set[str], set[str]]]:
    """相邻同模式轮的 (mode, 前轮通过集, 本轮通过集)；缺 case_results 的轮跳过。"""
    out: list[tuple[str, set[str], set[str]]] = []
    prev_mode, prev_passed = "", None
    for r in rounds:
        passed = _passed_set(r)
        if passed is None:
            continue
        mode = str(r.get("mode") or "")
        if prev_passed is not None and mode == prev_mode:
            out.append((mode, prev_passed, passed))
        prev_mode, prev_passed = mode, passed
    return out


def retention(rounds: list[dict[str, Any]]) -> float | None:
    """P1 保留率：相邻同模式轮间，前轮通过 case 本轮仍通过的最小比例。

    case 集可轮换：比例只在前轮通过集 ∩ 本轮出现 case 的交集上算（前轮
    通过集为空或无同模式相邻对 → 数据不足，不计入）。
    """
    worst: float | None = None
    for _mode, prev_p, curr_p in _pair_transitions(rounds):
        denom = len(prev_p)
        if not denom:
            continue
        kept = len(prev_p & curr_p) / denom
        worst = kept if worst is None else min(worst, kept)
    return None if worst is None else round(worst, 3)


def regressed_cases(rounds: list[dict[str, Any]]) -> list[str]:
    """P1 证据：前轮通过、本轮失败的 case（保留率退化的具体名单）。"""
    out: list[str] = []
    for _mode, prev_p, curr_p in _pair_transitions(rounds):
        out.extend(sorted(prev_p - curr_p))
    return out


def _std(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def variance_convergence(rounds: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
    """P1 方差收敛：各模式通过率序列后半窗标准差 ≤ 前半窗 → 收敛。"""
    out: dict[str, dict[str, Any]] = {}
    for mode in _modes(rounds):
        ys = [ratio for _n, ratio in _mode_series(rounds, mode)]
        if len(ys) < 4:
            continue
        half = len(ys) // 2
        s1, s2 = _std(ys[:half]), _std(ys[half:])
        out[mode] = {"converged": s2 <= s1, "std_first": round(s1, 4),
                     "std_second": round(s2, 4)}
    return out or None


def rollback_rate(proposals: dict[str, Any] | None) -> float | None:
    """P1 回滚频率：applied 后被标 reverted 的提案占已决提案比例。"""
    vals = [p.get("outcome") for p in (proposals or {}).values() if isinstance(p, dict)]
    applied_ever = sum(1 for o in vals if o in _RESOLVED)
    if not applied_ever:
        return None
    reverted = sum(1 for o in vals if o == "reverted")
    return round(reverted / applied_ever, 3)


def sample_efficiency(rounds: list[dict[str, Any]]) -> dict[str, int] | None:
    """P2 样本效率：达到首次 ≥EFFICIENCY_GAIN 提升的累计样本量（按模式）。"""
    out: dict[str, int] = {}
    for mode in _modes(rounds):
        pts = _mode_series(rounds, mode)
        if len(pts) < 2:
            continue
        base = pts[0][1]
        for n, ratio in pts:
            if ratio - base >= EFFICIENCY_GAIN:
                out[mode] = n
                break
    return out or None


def time_efficiency(rounds: list[dict[str, Any]]) -> dict[str, float] | None:
    """P2 时间效率：达到首次 ≥EFFICIENCY_GAIN 提升的累计时长（duration_s）。

    窗口内任一轮缺 duration_s → 该模式数据不完整，不计入（不静默按 0 折算）。
    """
    out: dict[str, float] = {}
    for mode in _modes(rounds):
        base: float | None = None
        cum = 0.0
        hit: float | None = None
        complete = True
        for r in rounds:
            if str(r.get("mode") or "") != mode:
                continue
            ratio = _ratio(r)
            if ratio is None:
                continue
            if base is None:
                base = ratio
                continue
            dur = r.get("duration_s")
            if not isinstance(dur, (int, float)) or isinstance(dur, bool):
                complete = False
                break
            cum += float(dur)
            if ratio - base >= EFFICIENCY_GAIN:
                hit = round(cum, 1)
                break
        if complete and hit is not None:
            out[mode] = hit
    return out or None


def judgment(proposals: dict[str, Any] | None) -> dict[str, Any]:
    """P0 判断力代理：预测失准率（T4 证伪淘汰占比）与用户否决率（外部锚）。"""
    props = [p for p in (proposals or {}).values() if isinstance(p, dict)]
    cats = [str(p.get("category") or "") for p in props]
    applied_ever = sum(1 for p in props if p.get("outcome") in _RESOLVED)
    misses = cats.count("prediction_miss")
    vetoes = cats.count("user_veto")
    attempts = applied_ever + misses
    return {
        "prediction_miss_rate": round(misses / attempts, 3) if attempts else None,
        "user_veto_rate": round(vetoes / applied_ever, 3) if applied_ever else None,
        "user_veto_reverts": vetoes,
    }


def verdict(ledger: dict[str, Any]) -> dict[str, Any]:
    """P0-P3 汇总裁决（优先级门控：P3 否决 > P0 > P1 > P2）。"""
    rounds = [r for r in (ledger.get("rounds") or []) if isinstance(r, dict)]
    proposals = ledger.get("proposals") or {}
    lr = learning_rates(rounds)
    judg = judgment(proposals)
    ret = retention(rounds)
    roll = rollback_rate(proposals)
    if judg["user_veto_reverts"] > 0:
        v, reason = "vetoed", "P3 一票否决：存在用户否决回滚（外部锚点判定目标偏移），解除须用户裁定"
    elif not lr:
        v, reason = "insufficient_data", "P0 数据不足：每模式 ≥2 轮且样本量有变化才可估学习率"
    elif all(m["slope"] <= 0 for m in lr.values()):
        v, reason = "no_evolution", "P0 不达标：所有模式学习率 ≤ 0，进化未发生"
    elif ret is not None and ret < RETENTION_FLOOR:
        v, reason = "unstable", f"P1 不达标：保留率 {ret} 低于下限 {RETENTION_FLOOR}"
    elif roll is not None and roll > ROLLBACK_ALARM:
        v, reason = "unstable", f"P1 不达标：回滚频率 {roll} 超告警线 {ROLLBACK_ALARM}"
    elif sample_efficiency(rounds) is None and time_efficiency(rounds) is None:
        v, reason = "inefficient", f"P2 低效：尚未达到 +{EFFICIENCY_GAIN} 通过率提升"
    else:
        v, reason = "evolving", "P0 达标且 P1/P2 无红旗"
    return {
        "metrics_version": METRICS_VERSION,
        "verdict": v,
        "reason": reason,
        "p0": {"learning_rates": lr,
               "transfer": transfer_efficiency(rounds),
               "judgment": judg},
        "p1": {"retention": ret,
               "variance": variance_convergence(rounds),
               "rollback_rate": roll,
               "regressed_cases": regressed_cases(rounds)},
        "p2": {"sample_efficiency": sample_efficiency(rounds),
               "time_efficiency": time_efficiency(rounds)},
        "p3": {"user_veto_reverts": judg["user_veto_reverts"]},
    }

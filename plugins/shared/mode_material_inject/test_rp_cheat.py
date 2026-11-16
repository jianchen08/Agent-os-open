# @feature: FP-0.2.〇 RP引擎(作者权威+/cheat) | @vision: V1 可进化 | @ci: python-coverage
"""RP /cheat 命令守卫测试（RP 批 3，ADR 2026-10-11 落点决策）。

两层：
- rp_cheat 纯逻辑：命令文法（parse）+ 确认状态机（decide）——on 二次输入确认 /
  off 恢复 / status 幂等零副作用 / allow_cheat=false 拒绝 / 已开启幂等 / 非法形态
  用法回执（区分度输入 parametrize ≥2）；
- mode_material_inject 守卫接线：链级 _cheat_guard 门控（未激活零动作=autonomous
  链零拦截契约）、非命令消息零影响（_cheat_command=False + 正常物料直通）、拦截轮
  skip_remaining + 回执 assistant 消息 op + 审计落盘（source=cheat）+ 开启审计
  失败 fail-closed（无审计不作弊）。

mock 仅磁盘 IO 之外的外部依赖为零：审计走真实 tmp 文件。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in (_DIR, _SHARED):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_DIR))
sys.path.insert(1, str(_SHARED))
for _m in ("plugin", "rp_cheat"):
    sys.modules.pop(_m, None)

import plugin as mode_material_inject_mod  # noqa: E402
import rp_cheat  # noqa: E402

# ── parse：命令文法 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "action", "confirm"),
    [
        ("/cheat", "on", False),
        ("/cheat on", "on", False),
        ("/cheat on confirm", "on", True),
        ("/cheat off", "off", False),
        ("/cheat status", "status", False),
        ("  /cheat  status  ", "status", False),  # 首尾空白容忍
        ("  /CHEAT OFF", None, False),  # 大写 = 非命令（约定小写，零拦截）
    ],
)
def test_parse_known_forms(text: str, action: str | None, confirm: bool) -> None:
    cmd = rp_cheat.parse_cheat_command(text)
    if action is None:
        assert cmd is None, "非小写命令形态不拦截（普通消息直通 LLM）"
    else:
        assert cmd is not None
        assert cmd.action == action
        assert cmd.confirm is confirm


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/cheat bogus", "invalid"),  # 未知子命令 → 用法回执（fail-closed 不漏给 LLM）
        ("/cheat on now", "invalid"),  # 多余 token
        ("/cheat off now", "invalid"),
        ("/cheat on confirm extra", "invalid"),
    ],
)
def test_parse_invalid_forms(text: str, expected: str) -> None:
    cmd = rp_cheat.parse_cheat_command(text)
    assert cmd is not None
    assert cmd.action == expected


@pytest.mark.parametrize(
    "text",
    ["", "   ", "/cheatfoo", "/cheat2", "hello /cheat", "先输入/cheat 再说", None],
)
def test_parse_non_commands(text: str | None) -> None:
    assert rp_cheat.parse_cheat_command(text) is None, "非 /cheat 起始 = 普通消息零拦截"


# ── decide：确认状态机 ───────────────────────────────────────────────────


def _decide(action: str, confirm: bool = False, **kw: Any) -> Any:
    return rp_cheat.decide(
        rp_cheat.CheatCommand(action=action, confirm=confirm),
        cheating=kw.pop("cheating", False),
        pending=kw.pop("pending", False),
        allow_cheat=kw.pop("allow_cheat", True),
        keep_display=kw.pop("keep_display", "0"),
    )


def test_decide_on_first_sets_pending_no_audit() -> None:
    out = _decide("on")

    assert out.write_pending is True
    assert out.pending is True
    assert out.write_cheating is False, "确认前不生效（显式同意，不静默）"
    assert out.audit_row is None
    assert "confirm" in out.receipt, "回执提示确认路径"
    assert "/cheat off" in out.receipt, "回执提示取消路径"


def test_decide_on_confirm_without_pending_is_noop() -> None:
    out = _decide("on", confirm=True)

    assert out.write_cheating is False
    assert out.write_pending is False
    assert out.audit_row is None
    assert "/cheat on" in out.receipt, "指引先输 /cheat on"


def test_decide_on_confirm_with_pending_takes_effect_and_audits() -> None:
    out = _decide("on", confirm=True, pending=True, keep_display="2")

    assert out.write_cheating is True
    assert out.cheating is True
    assert out.write_pending is True
    assert out.pending is False, "确认即清 pending"
    row = out.audit_row
    assert row is not None
    assert row["key"] == "cheating"
    assert row["op"] == "="
    assert row["from"] is False
    assert row["to"] is True
    assert row["reason"] == "cheat_on"
    assert "已开启" in out.receipt
    assert "2" in out.receipt, "回执含例外键域计数"


def test_decide_off_restores_and_audits() -> None:
    out = _decide("off", cheating=True, pending=False)

    assert out.write_cheating is True
    assert out.cheating is False
    row = out.audit_row
    assert row is not None
    assert row["from"] is True
    assert row["to"] is False
    assert row["reason"] == "cheat_off"
    assert "已关闭" in out.receipt


def test_decide_off_without_cheat_is_idempotent_cancels_pending() -> None:
    idle = _decide("off")
    assert idle.write_cheating is False
    assert idle.audit_row is None
    assert "未开启" in idle.receipt

    cancels = _decide("off", pending=True)
    assert cancels.write_cheating is False
    assert cancels.write_pending is True
    assert cancels.pending is False, "off 取消待确认"
    assert "取消" in cancels.receipt


@pytest.mark.parametrize("cheating", [False, True])
def test_decide_status_idempotent_zero_side_effect(cheating: bool) -> None:
    out = _decide("status", cheating=cheating, pending=False, keep_display="3")

    assert out.write_cheating is False
    assert out.write_pending is False
    assert out.audit_row is None, "status 零副作用（幂等无审计行）"
    assert ("已开启" if cheating else "未开启") in out.receipt
    if cheating:
        assert "3" in out.receipt, "开启态回执含例外键域计数"


@pytest.mark.parametrize(("action", "confirm"), [("on", False), ("on", True), ("off", False)])
def test_decide_allow_cheat_false_denies(action: str, confirm: bool) -> None:
    out = _decide(action, confirm=confirm, allow_cheat=False, cheating=action == "off")

    assert out.write_cheating is False, "作者禁用 = 拒绝生效"
    assert out.write_pending is False
    assert "allow_cheat" in out.receipt, "回执理由指向作者声明"
    row = out.audit_row
    assert row is not None, "on/off 拒绝尝试落审计"
    assert row["reason"] == "cheat_denied"
    assert row["from"] is (action == "off")
    assert row["to"] is (action != "off"), "to = 被拒绝的目标值"


def test_decide_status_with_allow_cheat_false_denies_without_audit() -> None:
    out = _decide("status", allow_cheat=False)

    assert out.write_cheating is False
    assert out.audit_row is None
    assert "allow_cheat" in out.receipt


def test_decide_on_while_already_cheating_is_idempotent() -> None:
    out = _decide("on", cheating=True)

    assert out.write_cheating is False, "已开启重复 on 不重复写"
    assert out.audit_row is None
    assert "已处于作弊" in out.receipt


def test_decide_invalid_usage_receipt_no_writes() -> None:
    out = rp_cheat.decide(
        rp_cheat.CheatCommand(action="invalid"),
        cheating=False, pending=False, allow_cheat=True, keep_display="0",
    )

    assert out.write_cheating is False
    assert out.write_pending is False
    assert out.audit_row is None
    assert "/cheat on" in out.receipt, "用法指引含开启"
    assert "/cheat status" in out.receipt, "用法指引含状态"


# ── 守卫接线（mode_material_inject 插件） ────────────────────────────────


def _guard_state(**extra: Any) -> dict[str, Any]:
    """激活态：链级门控开 + 卡键身份 + 用户命令消息 + 会话工作空间。"""
    state: dict[str, Any] = {
        "_cheat_guard": True,
        "agent.id": "mode_roleplay/luna",
        "messages": [{"role": "user", "content": "/cheat status", "seq": 9}],
        "workspace": str(extra.pop("workspace")),
        "pipeline_id": "pipe-1",
        "execution_context": {"mode": "roleplay"},
    }
    state.update(extra)
    return state


def _run(state: dict[str, Any]) -> Any:
    from _libs.pipeline_base.plugin import PluginContext

    p = mode_material_inject_mod.ModeMaterialInjectPlugin(config={})
    return asyncio.run(p.execute(PluginContext(state=state, config={})))


def test_guard_inactive_without_chain_flag() -> None:
    """门控键缺席 = 守卫零动作（直通正常物料——autonomous 链输 /cheat 是普通
    消息零拦截契约；此处 mode 回写即正常物料行为）。"""
    state = _guard_state(workspace="x")
    state.pop("_cheat_guard")

    result = _run(state)

    assert result.skip_remaining is False
    assert result.state_updates.get("_cheat_command") is None, "守卫未写任何键"
    assert "messages" not in result.state_updates, "命令消息不被拦截（直通 LLM）"


def test_guard_non_command_message_zero_interception(tmp_path: Path) -> None:
    """激活链非命令消息：_cheat_command=False（清陈旧标志）+ 正常物料逻辑继续。"""
    state = _guard_state(workspace=str(tmp_path))
    state["messages"] = [{"role": "user", "content": "她拔剑指向北方。", "seq": 9}]

    result = _run(state)

    assert result.skip_remaining is False, "普通轮不短路"
    assert result.state_updates.get("_cheat_command") is False
    assert "meta.cheating" not in result.state_updates
    assert not list(tmp_path.glob("*.jsonl")), "零审计"


def test_guard_tool_iteration_tail_not_user_zero_interception(tmp_path: Path) -> None:
    """尾条非 user（工具迭代/回执轮）不重扫本轮命令。"""
    state = _guard_state(workspace=str(tmp_path))
    state["messages"] = [
        {"role": "user", "content": "/cheat on", "seq": 9},
        {"role": "assistant", "content": "好。", "seq": 10},
    ]

    result = _run(state)

    assert result.skip_remaining is False
    assert result.state_updates.get("_cheat_command") is False


def test_guard_non_card_agent_zero_interception(tmp_path: Path) -> None:
    """无卡键会话（状态账本不存在）零拦截：命令直通 LLM。"""
    state = _guard_state(workspace=str(tmp_path))
    state["agent.id"] = "agentos"

    result = _run(state)

    assert result.skip_remaining is False
    assert result.state_updates.get("_cheat_command") is False


def test_guard_status_intercepts_with_receipt_and_skips_prepare(tmp_path: Path) -> None:
    """status 轮：拦截短路（skip_remaining）+ 回执 assistant 消息 op + 幂等零翻转。"""
    result = _run(_guard_state(workspace=str(tmp_path)))

    assert result.skip_remaining is True, "命令轮短路 prepare 余下步"
    updates = result.state_updates
    assert updates["_cheat_command"] is True
    assert "meta.cheating" not in updates, "status 零副作用"
    (op,) = updates["messages"]["_ops"]
    assert op["op"] == "set", "append op（引擎分配 seq）"
    assert "seq" not in op, "append op（引擎分配 seq）"
    assert op["msg"]["role"] == "assistant"
    assert "未开启" in op["msg"]["content"]
    assert op["msg"]["agent_id"] == "mode_roleplay/luna", "回执带卡身份（气泡卡名）"
    assert not (tmp_path / "rp_state_audit.jsonl").exists(), "status 零审计行"


def test_guard_on_confirm_flow_writes_flag_and_audit(tmp_path: Path) -> None:
    """四断言之一·confirm：pending → confirm 翻转 + 审计 source=cheat 落盘。"""
    # 第一轮：/cheat on → 只置 pending，不生效不落审计
    first = _run(_guard_state(workspace=str(tmp_path), messages=[
        {"role": "user", "content": "/cheat on", "seq": 9}]))
    assert first.state_updates.get("_cheat_pending_on") is True
    assert "meta.cheating" not in first.state_updates
    assert not (tmp_path / "rp_state_audit.jsonl").exists()

    # 第二轮：/cheat on confirm（引擎合并语义：上轮 pending 写入随 state 带入）
    second = _run(_guard_state(workspace=str(tmp_path), messages=[
        {"role": "user", "content": "/cheat on confirm", "seq": 10}],
        **{"_cheat_pending_on": True}))
    assert second.state_updates.get("meta.cheating") is True
    assert second.state_updates.get("_cheat_pending_on") is False

    rows = [json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["source"] == "cheat"
    assert rows[0]["idem"] == "pipe-1:cheat:10", "idem 复用 2-Ⅰ 机制（命令行独立 idem 空间）"
    assert rows[0]["to"] is True
    assert rows[0]["reason"] == "cheat_on"

    # 重放第二轮（同 pipeline_id + 同用户消息 seq + pending 在场，走到审计路径）：
    # 同 idem 不双记
    _run(_guard_state(workspace=str(tmp_path), messages=[
        {"role": "user", "content": "/cheat on confirm", "seq": 10}],
        **{"_cheat_pending_on": True}))
    lines = (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "审计追加幂等"


def test_guard_allow_cheat_false_denies_with_audit(tmp_path: Path) -> None:
    """作者禁用：拒绝 + 回执作者声明理由 + 尝试审计，flag 不翻转。"""
    state = _guard_state(
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "/cheat on", "seq": 9}],
        **{"context.rp_engine": {"allow_cheat": False}},
    )

    result = _run(state)

    assert result.skip_remaining is True
    updates = result.state_updates
    assert "meta.cheating" not in updates
    assert "allow_cheat" in updates["messages"]["_ops"][0]["msg"]["content"]
    rows = [json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["reason"] == "cheat_denied"
    assert rows[0]["source"] == "cheat"


def test_guard_off_restores_flag_and_audits(tmp_path: Path) -> None:
    """off 恢复：cheating 翻回 False + 审计 cheat_off。"""
    result = _run(_guard_state(
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "/cheat off", "seq": 11}],
        **{"meta.cheating": True},
    ))

    updates = result.state_updates
    assert updates.get("meta.cheating") is False
    rows = [json.loads(line) for line in
            (tmp_path / "rp_state_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["reason"] == "cheat_off"
    assert rows[0]["from"] is True


def test_guard_on_confirm_audit_failure_fails_closed(tmp_path: Path) -> None:
    """无审计不作弊（ADR 否决 B 的制度底座）：审计 IO 失败 = 开启不生效。"""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("占用路径", encoding="utf-8")

    result = _run(_guard_state(
        workspace=str(blocker),
        messages=[{"role": "user", "content": "/cheat on confirm", "seq": 10}],
        **{"_cheat_pending_on": True},
    ))

    updates = result.state_updates
    assert "meta.cheating" not in updates, "审计不可用 = 开启失败（fail-closed）"
    assert updates.get("_cheat_pending_on") is True, "pending 保留可重试"
    assert "失败" in updates["messages"]["_ops"][0]["msg"]["content"]


def test_guard_bad_engine_declaration_still_allows_cheat(tmp_path: Path) -> None:
    """engine 声明非映射（lint 面警告的运行时兜底）= 按未声明处理，/cheat 可用。"""
    result = _run(_guard_state(
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "/cheat status", "seq": 9}],
        **{"context.rp_engine": "不是映射"},
    ))

    assert result.skip_remaining is True
    assert "未开启" in result.state_updates["messages"]["_ops"][0]["msg"]["content"]


def test_receipt_round_does_not_break_material_promotion_staging(tmp_path: Path) -> None:
    """拦截轮跳过组装器：暂存键不被误提升（守卫回执独占 messages op 通道）。"""
    state = _guard_state(workspace=str(tmp_path))
    state["_material_message_ops"] = [{"op": "set", "seq": 9, "msg": {"role": "user"}}]

    result = _run(state)

    ops = result.state_updates["messages"]["_ops"]
    assert len(ops) == 1, "暂存不混入拦截轮"
    assert ops[0]["msg"]["role"] == "assistant", "暂存不混入拦截轮"
    assert state.get("_material_message_ops") is not None, "暂存键原样保留（未消费未清除）"


def test_guard_keep_true_engine_receipt_shows_full_domain(tmp_path: Path) -> None:
    """keep=true 引擎声明（坏形态 fail-closed 全保留）：开启回执含「全部键域」。"""
    result = _run(_guard_state(
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "/cheat on confirm", "seq": 10}],
        **{"_cheat_pending_on": True,
           "context.rp_engine": {"allow_cheat": True,
                                 "cheat_overrides": {"keep_state_machine": {"坏形态": 1}}}},
    ))

    receipt = result.state_updates["messages"]["_ops"][0]["msg"]["content"]
    assert "全部键域" in receipt, "keep=true 展示为全部键域保留"
    assert result.state_updates.get("meta.cheating") is True


def test_guard_internal_failure_degrades_to_normal_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """守卫自身失败（降级契约）：命令轮退化为普通轮（消息直通 LLM，不阻断 RP）。

    豁免注释（评审 B）：下方 monkeypatch 内部函数注入故障是对「mock 仅用于
    外部依赖」红线的**申报豁免**——守卫自身崩溃路径无法经外部依赖触发（文法器
    对畸形输入本就不抛），故障注入是验证降级契约（不短路/零回执/审计不落盘）
    的唯一手段，非以 mock 截断内部依赖走捷径。"""
    def _boom(text: object) -> object:
        raise RuntimeError("文法器爆炸")

    monkeypatch.setattr(mode_material_inject_mod, "parse_cheat_command", _boom)
    state = _guard_state(workspace=str(tmp_path))

    result = _run(state)

    assert result.skip_remaining is False, "降级普通轮不短路"
    assert result.state_updates.get("_cheat_command") is False
    assert "messages" not in result.state_updates, "零回执零拦截"
    assert not (tmp_path / "rp_state_audit.jsonl").exists()


def test_decide_status_with_pending_shows_pending_hint() -> None:
    """status 在 pending 在场时回执提示确认/取消双路径。"""
    out = _decide("status", pending=True)

    assert out.audit_row is None
    assert "待确认" in out.receipt
    assert "/cheat on confirm" in out.receipt, "回执提示确认路径"
    assert "/cheat off" in out.receipt, "回执提示取消路径"

# @feature: FP-0.2.〇 RP引擎(世界记忆事件粒度) | @vision: V1 可进化 | @ci: python-coverage
"""rp_state_audit 事件语义面契约测试（缺口批次二：§7.5 #5 世界记忆事件粒度）。

- append_audit_rows 写面逐行补事件语义字段（actor/action/target/summary，
  §7.4 沉淀回路的输入维度）：
  - actor 从行来源推导：behavior 名 > 玩家动作（source=action）> 作弊
    （source=cheat）> narrator 缺省；
  - action = 行内声明动作 id 保留（动作通道行）或 op 动词映射
    （+/-/= → increase/decrease/set；op 不承载的记录行 = observe）；
  - target = 变更键；summary 一行可读（被拒行带「（被拒 <reason>）」后缀，
    layer 由 Validator 只盖在拒绝行上）；
- recent_events 读取面：文件序最后 K 条完整事件行（坏行跳过、ws 无效/文件
  缺失/limit<1 = 空表，fail-soft 与写面同口径）。

断行为不断实现：断写盘行字段与读取面输出；文件全真实 IO，零 mock。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SHARED = Path(__file__).resolve().parents[1] / "plugins" / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from rp_state_audit import (  # noqa: E402
    append_audit_rows,
    recent_events,
)


def _read(ws: Path) -> list[dict[str, Any]]:
    audit = ws / "rp_state_audit.jsonl"
    assert audit.is_file()
    return [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]


# ── 写面事件语义：actor 缺省与推导（≥2 组区分度来源）────────────────────────


def test_append_enriches_default_narrator_event(tmp_path: Path) -> None:
    """普通落账行（无行级来源 extra）：actor 缺省 narrator，op 动词映射进
    action，target = 变更键，summary 一行可读。"""
    rows = [{"key": "好感度", "op": "+", "from": 10, "to": 15, "reason": "accepted"}]

    assert append_audit_rows(str(tmp_path), rows, "pipe-1:7", "state_marker") is True

    (row,) = _read(tmp_path)
    assert row["actor"] == "narrator", "缺省行动者 = narrator（叙述性变更）"
    assert row["action"] == "increase"
    assert row["target"] == "好感度"
    assert row["summary"] == "narrator increase 好感度: 10→15"


@pytest.mark.parametrize(
    ("op", "expected_action", "frm", "to"),
    [
        ("+", "increase", 10, 15),
        ("-", "decrease", 10, 5),
        ("=", "set", 10, 99),
    ],
)
def test_append_action_verb_maps_op(
    tmp_path: Path, op: str, expected_action: str, frm: int, to: int
) -> None:
    """op 动词映射三态：increase/decrease/set 各自可辨（符号量级相反覆盖）。"""
    rows = [{"key": "灵力", "op": op, "from": frm, "to": to, "reason": "accepted"}]

    append_audit_rows(str(tmp_path), rows, "pipe-1:7", "state_marker")

    (row,) = _read(tmp_path)
    assert row["action"] == expected_action
    assert row["summary"] == f"narrator {expected_action} 灵力: {frm}→{to}"


def test_append_behavior_row_actor_is_behavior_name(tmp_path: Path) -> None:
    """行为触发行（行级 source=behavior + behavior 名）：actor = 行为名。"""
    rows = [{
        "key": "好感度", "op": "-", "from": 50, "to": 47,
        "reason": "accepted", "source": "behavior", "behavior": "怒目",
    }]

    append_audit_rows(str(tmp_path), rows, "pipe-1:7", "state_marker")

    (row,) = _read(tmp_path)
    assert row["actor"] == "怒目", "行为名即行动者（世界规则触发的变更可归因）"
    assert row["action"] == "decrease"
    assert row["summary"] == "怒目 decrease 好感度: 50→47"


def test_append_action_row_keeps_declared_action_id(tmp_path: Path) -> None:
    """动作通道行：actor = player，action = 声明动作 id（行内 action 键即事件
    动作，id 不被动词映射覆盖——做了什么以声明为准）。"""
    rows = [{
        "key": "好感度", "op": "+", "from": 50, "to": 55,
        "reason": "accepted", "source": "action", "action": "praise",
    }]

    append_audit_rows(str(tmp_path), rows, "pipe-1:7", "action")

    (row,) = _read(tmp_path)
    assert row["actor"] == "player", "动作由玩家全等选取触发"
    assert row["action"] == "praise", "声明动作 id 保留（不被 op 动词覆盖）"
    assert row["summary"] == "player praise 好感度: 50→55"


def test_append_cheat_round_actor_is_cheat(tmp_path: Path) -> None:
    """作弊轮缺省来源 = cheat：actor 推导为 cheat（旁路透传可归因）。"""
    rows = [{"key": "好感度", "op": "=", "from": 50, "to": 999, "reason": "cheat_bypass"}]

    append_audit_rows(str(tmp_path), rows, "pipe-1:cheat:9", "cheat")

    (row,) = _read(tmp_path)
    assert row["actor"] == "cheat"
    assert row["action"] == "set"
    assert row["summary"] == "cheat set 好感度: 50→999"


def test_append_unsettled_row_action_is_observe(tmp_path: Path) -> None:
    """op 不承载的记录行（state 字段忽略行）：action = observe，summary 无
    from→to 链（未结算不成变更，只记观察）。"""
    rows = [{
        "key": "state", "op": None, "from": None,
        "to": '{"好感度": 500}', "reason": "state_field_ignored",
        "source": "state_field_ignored",
    }]

    append_audit_rows(str(tmp_path), rows, "pipe-1:7", "state_marker")

    (row,) = _read(tmp_path)
    assert row["action"] == "observe"
    assert row["actor"] == "narrator"
    assert "→" not in row["summary"], "未结算行不携带变更链"
    assert row["summary"] == 'narrator observe state: "{\\"好感度\\": 500}"'


def test_append_rejected_row_summary_carries_reason(tmp_path: Path) -> None:
    """被拒行（Validator 盖 layer 章）：summary 带「（被拒 <reason>）」后缀——
    事件面上拒绝可辨，与 accepted 行同字段形态。"""
    rejected = {
        "key": "魔力", "op": None, "from": None, "to": 9,
        "reason": "unknown_key", "layer": "hard",
    }
    accepted = {"key": "好感度", "op": "=", "from": 1, "to": 2, "reason": "accepted"}

    append_audit_rows(str(tmp_path), [accepted, rejected], "pipe-1:7", "llm")

    row_accepted, row_rejected = _read(tmp_path)
    assert row_accepted["summary"] == "narrator set 好感度: 1→2"
    assert row_rejected["summary"].endswith("（被拒 unknown_key）")
    assert "layer" not in row_accepted, "layer 只盖在拒绝行（缺席 = 非拒绝判别面）"


def test_append_long_value_summary_truncated(tmp_path: Path) -> None:
    """超长值（state 字段忽略行载荷摘要等）：summary 值段截断带「…」，
    行体不被长载荷撑爆（截断保护）。"""
    rows = [{
        "key": "state", "op": None, "from": None,
        "to": "囍" * 200, "reason": "state_field_ignored",
        "source": "state_field_ignored",
    }]

    append_audit_rows(str(tmp_path), rows, "pipe-1:7", "state_marker")

    (row,) = _read(tmp_path)
    assert row["summary"].endswith("…"), "截断标记在场"
    assert len(row["summary"]) < 120, "summary 有界（值段截断生效）"
    assert '"' + "囍" * 39 + "…" in row["summary"], "值段截断保留可辨前缀（JSON 引号内）"
    assert "囍" * 40 not in row["summary"], "完整长载荷不进 summary"


# ── recent_events 读取面：近 K 条事件（下轮动态注入源）──────────────────────


def test_recent_events_returns_last_k_in_file_order(tmp_path: Path) -> None:
    """近 K=8 读取面：文件序（发生序）最后 8 条完整事件行，行含写面补齐的
    事件语义字段。"""
    for i in range(10):
        append_audit_rows(
            str(tmp_path),
            [{"key": f"k{i}", "op": "=", "from": i, "to": i + 1, "reason": "accepted"}],
            f"pipe-1:{i}",
            "state_marker",
        )

    events = recent_events(str(tmp_path), 8)

    assert [e["key"] for e in events] == [f"k{i}" for i in range(2, 10)], (
        "最后 8 条（最早的 k0/k1 滚出窗口）"
    )
    assert all(
        {"actor", "action", "target", "summary"} <= set(e) for e in events
    ), "事件行四件套齐备"
    assert events[0]["summary"].startswith("narrator set k2:"), "summary 随行可读"


def test_recent_events_default_limit_is_eight(tmp_path: Path) -> None:
    """limit 缺省 = 8（§7.5 #5 近 K=8 条事件的 K 值单源）。"""
    for i in range(3):
        append_audit_rows(
            str(tmp_path),
            [{"key": f"k{i}", "op": "=", "from": i, "to": i + 1, "reason": "accepted"}],
            f"pipe-1:{i}",
            "state_marker",
        )

    assert len(recent_events(str(tmp_path))) == 3, "不足 K 全返"
    assert len(recent_events(str(tmp_path), 2)) == 2, "显式 limit 收窄"


def test_recent_events_missing_or_invalid_workspace_degrades_empty(
    tmp_path: Path,
) -> None:
    """ws 无效 / 文件缺失 = 空表（fail-soft，与写面降级同口径，不抛不猜）。"""
    assert recent_events("", 8) == []
    assert recent_events(None, 8) == []  # type: ignore[arg-type]
    assert recent_events(str(tmp_path), 8) == [], "文件缺失 = 空表"
    assert recent_events(str(tmp_path), 0) == [], "limit<1 = 空表"


def test_recent_events_skips_bad_lines(tmp_path: Path) -> None:
    """坏行跳过不占窗口：合法事件行数不受坏行挤压（与幂等扫描同口径）。"""
    audit = tmp_path / "rp_state_audit.jsonl"
    good = {
        "ts": "t", "source": "state_marker", "idem": "pipe-1:1",
        "key": "好感度", "op": "=", "from": 1, "to": 2, "reason": "accepted",
        "actor": "narrator", "action": "set", "target": "好感度",
        "summary": "narrator set 好感度: 1→2",
    }
    audit.write_text(
        "{坏行\n" + json.dumps(good, ensure_ascii=False) + "\n[1, 2]\n",
        encoding="utf-8",
    )

    events = recent_events(str(tmp_path), 8)

    assert [e["key"] for e in events] == ["好感度"], "坏行不占 K 窗口"

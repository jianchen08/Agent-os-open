# @feature: FP-0.2.〇 RP引擎(状态通道schema化) | @vision: V1 可进化 | @ci: python-coverage
"""State Contract Validator 单测（RP 批 2-Ⅰ 2b，TDD 先红后绿）。

覆盖（parametrize ≥2 组区分度：恰等/越界、合法/非法、扁平/路径两形态）：
- clamp 边界：恰等 min/max = accepted 恰等值；越界 = clamp 后 accepted
  （reason=clamped，clamp 是 accepted 后取边界值）；
- 未知键 rejected（schema 外键=rejected，即"LLM 永不直写状态"的执法点）；
  歧义叶名 rejected；类型错 rejected；max_delta_per_turn 恰等/超限；
- enum 合法/非法；bool/string 绝对赋值；相对 +/-（含 no_baseline）；
- 扁平键与路径键两种形态映射（账本键=标记书写键原样，契约见模块 docstring）；
- schema=None 兼容态逐字 accepted（无声明卡现状行为零变化）；
- 性质断言：变更键全归宿（accepted ∪ rejected = 输入键集）、clamp 值恒在
  界内、审计行数 = 变更数、入参防突变。

纯函数测试，零 mock。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED = _PLUGIN_DIR.parents[0]  # plugins/shared/（card_lint 共享根单源，生产 bootstrap 同位）
for _p in (_PLUGIN_DIR, _SHARED):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from state_contract import (  # noqa: E402
    LAYER_HARD,
    LAYER_SOFT,
    REASON_ACCEPTED,
    REASON_AMBIGUOUS_KEY,
    REASON_CHEAT_BYPASS,
    REASON_CLAMPED,
    REASON_DELTA_EXCEEDED,
    REASON_NO_BASELINE,
    REASON_TYPE_MISMATCH,
    REASON_UNKNOWN_KEY,
    REASON_LAYERS,
    flatten_variables,
    validate_and_apply,
)

SCHEMA: dict[str, Any] = {
    "player": {"林远": {"灵力": {"type": "int", "default": 10, "min": 0, "max": 100}}},
    "npc": {"苏清婉": {"好感度": {"type": "int", "min": -100, "max": 100}}},
    "world": {"时辰": {"type": "enum", "values": ["晨", "午", "夜"]}},
    "flags": {"见光": {"type": "bool"}},
    "note": {"出处": {"type": "string"}},
    "ratio": {"浓度": {"type": "float", "min": 0.0, "max": 1.0}},
}

FLAT_PATHS = {
    "player.林远.灵力",
    "npc.苏清婉.好感度",
    "world.时辰",
    "flags.见光",
    "note.出处",
    "ratio.浓度",
}


# ── flatten：任意嵌套展平（叶 = 含 type 键的 dict，路径 = 点连接） ─────────


def test_flatten_paths_dot_joined() -> None:
    flat, skipped = flatten_variables(SCHEMA)
    assert set(flat) == FLAT_PATHS
    assert skipped == []


def test_flatten_flat_declaration_is_leaf() -> None:
    """扁平声明（variables 顶层直接 {键: 声明}）= 路径键即键名。"""
    flat, skipped = flatten_variables({"好感度": {"type": "int", "min": 0, "max": 100}})
    assert set(flat) == {"好感度"}
    assert skipped == []


def test_flatten_malformed_subtree_skipped_not_crash() -> None:
    """分支标量（无 type 视为子组继续下钻，标量无声明）= 跳过记录不炸装载。"""
    flat, skipped = flatten_variables(
        {"a": {"声明缺type": {"min": 1}}, "b": {"标量分支": 3}, "c": {"好键": {"type": "int"}}}
    )
    assert set(flat) == {"c.好键"}
    assert set(skipped) == {"a.声明缺type.min", "b.标量分支"}


# ── 键形态映射：扁平键（叶名唯一）与路径键（精确） ────────────────────────


def test_flat_key_maps_to_unique_leaf_path() -> None:
    """现役 <state> 标记是扁平键名（如 好感度）：唯一叶名匹配，账本键原样。"""
    current = {"好感度": 50}
    result = validate_and_apply(current, {"好感度": "+5"}, SCHEMA, None)
    assert result.accepted == {"好感度": 55}, "账本键 = 标记书写键（扁平原样）"
    assert result.rejected == []


def test_path_key_exact_match() -> None:
    """路径键（点连接）精确匹配声明路径，账本键同样原样落账。"""
    current = {"npc.苏清婉.好感度": 50}
    result = validate_and_apply(current, {"npc.苏清婉.好感度": "+5"}, SCHEMA, None)
    assert result.accepted == {"npc.苏清婉.好感度": 55}


def test_ambiguous_leaf_name_rejected() -> None:
    """扁平键命中多个叶名 = 歧义 rejected（fail-closed 不猜）；精确路径键仍可用。"""
    schema = {
        "npc": {"甲": {"好感度": {"type": "int"}}, "乙": {"好感度": {"type": "int"}}},
    }
    result = validate_and_apply({}, {"好感度": 1}, schema, None)
    assert result.accepted == {}
    assert [r["reason"] for r in result.rejected] == [REASON_AMBIGUOUS_KEY]

    ok = validate_and_apply({}, {"npc.甲.好感度": 1}, schema, None)
    assert ok.accepted == {"npc.甲.好感度": 1}


# ── clamp 边界（恰等 = accepted 恰等值；越界 = clamp 后 accepted 带注记） ──


@pytest.mark.parametrize(
    ("expr", "current_val", "expected", "reason"),
    [
        ("=100", 50, 100, REASON_ACCEPTED),  # 恰等 max = accepted 恰等值
        ("=0", 50, 0, REASON_ACCEPTED),  # 恰等 min = accepted 恰等值
        ("+50", 60, 100, REASON_CLAMPED),  # 越上界 → clamp 后 accepted
        ("-80", 30, 0, REASON_CLAMPED),  # 越下界 → clamp 后 accepted
        ("=55", 50, 55, REASON_ACCEPTED),  # 界内原值
    ],
)
def test_clamp_boundary(
    expr: str, current_val: int, expected: int, reason: str
) -> None:
    result = validate_and_apply(
        {"灵力": current_val}, {"灵力": expr}, SCHEMA, None
    )
    assert result.accepted == {"灵力": expected}
    assert [row["reason"] for row in result.audit] == [reason]
    assert result.rejected == []


def test_clamp_value_always_within_bounds_property() -> None:
    """性质：任意输入经 clamp 后 accepted 数值恒在 [min, max] 界内。"""
    for expr in ("+999", "-999", "=999", "=-999", "=37"):
        result = validate_and_apply({"灵力": 40}, {"灵力": expr}, SCHEMA, None)
        value = result.accepted["灵力"]
        assert 0 <= value <= 100, f"{expr} → {value} 越界"


# ── 未知键 / 类型 / 枚举 / bool / string ──────────────────────────────────


def test_unknown_key_rejected_never_applied() -> None:
    """schema 外键 = rejected（LLM 永不直写状态的执法点），accepted 为空。"""
    result = validate_and_apply({}, {"魔力": 1, "好感度": 3}, SCHEMA, None)
    assert result.accepted == {"好感度": 3}
    assert result.rejected == [
        {
            "key": "魔力",
            "op": None,
            "from": None,
            "to": 1,
            "reason": REASON_UNKNOWN_KEY,
            "layer": LAYER_HARD,
        }
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("player.林远.灵力", "abc"),  # int 声明收字符串字面
        ("player.林远.灵力", 5.5),  # int 声明收非整浮点
        ("player.林远.灵力", True),  # bool 不是 int（bool 子类陷阱显式拒）
        ("ratio.浓度", "x"),  # float 声明收字符串
        ("flags.见光", 1),  # bool 声明收 int（真值糖不收，契约可预期）
        ("note.出处", 5),  # string 声明收 int
        ("world.时辰", "黎明"),  # enum 非法成员
        ("world.时辰", True),
    ],
)
def test_type_mismatch_rejected(key: str, value: Any) -> None:
    result = validate_and_apply({}, {key: value}, SCHEMA, None)
    assert result.accepted == {}
    assert result.rejected[0]["reason"] == REASON_TYPE_MISMATCH
    assert result.rejected[0]["key"] == key


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("world.时辰", "夜", "夜"),  # enum 合法成员（裸字面 = 绝对）
        ("world.时辰", "=晨", "晨"),  # = 前缀剥离
        ("flags.见光", True, True),  # bool 绝对赋值（JSON 布尔字面）
        ("flags.见光", False, False),
        ("note.出处", "北境集市", "北境集市"),  # string 绝对赋值
        ("note.出处", "=北境", "北境"),  # = 前缀剥离
        ("player.林远.灵力", "=42", 42),  # 数字字符串字面按声明类型收
        ("ratio.浓度", 1, 1.0),  # int 字面落 float 声明 = 数值族合法
    ],
)
def test_absolute_assignment_legal_forms(
    key: str, value: Any, expected: Any
) -> None:
    result = validate_and_apply({}, {key: value}, SCHEMA, None)
    assert result.accepted == {key: expected}


def test_float_relative_and_clamp() -> None:
    result = validate_and_apply({"浓度": 0.2}, {"浓度": "+0.3"}, SCHEMA, None)
    assert result.accepted == {"浓度": 0.5}
    result = validate_and_apply({"浓度": 0.9}, {"浓度": "+0.3"}, SCHEMA, None)
    assert result.accepted == {"浓度": 1.0}
    assert result.audit[0]["reason"] == REASON_CLAMPED


# ── 相对 op：基线取现值，缺现值取 default，双缺 rejected ─────────────────


def test_relative_without_current_uses_default() -> None:
    result = validate_and_apply({}, {"player.林远.灵力": "+5"}, SCHEMA, None)
    assert result.accepted == {"player.林远.灵力": 15}, "缺现值取声明 default=10"


def test_relative_without_current_and_default_rejected() -> None:
    schema = {"npc": {"苏清婉": {"好感度": {"type": "int", "min": 0, "max": 100}}}}
    result = validate_and_apply({}, {"好感度": "+5"}, schema, None)
    assert result.accepted == {}
    assert result.rejected[0]["reason"] == REASON_NO_BASELINE


def test_relative_on_non_numeric_decl_rejected() -> None:
    result = validate_and_apply({"见光": False}, {"flags.见光": "+1"}, SCHEMA, None)
    assert result.rejected[0]["reason"] == REASON_TYPE_MISMATCH


# ── hard_rules.max_delta_per_turn（恰等 = accepted；超限 = rejected） ─────


HARD_RULES: dict[str, Any] = {"npc.*.好感度": {"max_delta_per_turn": 10}}


@pytest.mark.parametrize(
    ("expr", "current_val", "expected_reason", "accepted_val"),
    [
        ("=5", 0, REASON_ACCEPTED, 5),  # |Δ|=5 ≤ 10
        ("=10", 0, REASON_ACCEPTED, 10),  # 恰等上限 = accepted
        ("=11", 0, REASON_DELTA_EXCEEDED, None),  # 超限 = rejected
        ("=25", 10, REASON_DELTA_EXCEEDED, None),  # 绝对赋值同样受 Δ 约束（|Δ|=15）
        ("+9", 0, REASON_ACCEPTED, 9),
    ],
)
def test_max_delta_per_turn(
    expr: str, current_val: int, expected_reason: str, accepted_val: int | None
) -> None:
    result = validate_and_apply(
        {"好感度": current_val}, {"好感度": expr}, SCHEMA, HARD_RULES
    )
    if accepted_val is None:
        assert result.accepted == {}
        assert result.rejected[0]["reason"] == expected_reason
    else:
        assert result.accepted == {"好感度": accepted_val}
        assert result.rejected == []


def test_delta_checked_after_clamp() -> None:
    """契约次序：clamp 先于 delta 检查——current=95, "+50" clamp 到 max=100，
    clamp 后 |Δ|=5 ≤ 10 → accepted（若 delta 先查原始 +50 会误拒）。"""
    rules = {"player.*.灵力": {"max_delta_per_turn": 10}}
    result = validate_and_apply(
        {"灵力": 95}, {"灵力": "+50"}, SCHEMA, rules
    )
    assert result.accepted == {"灵力": 100}
    assert result.audit[0]["reason"] == REASON_CLAMPED

    # 反向：clamp 后仍超限照样拒（clamp 不掩护越权）
    strict = {"player.*.灵力": {"max_delta_per_turn": 2}}
    result = validate_and_apply({"灵力": 0}, {"灵力": "+50"}, SCHEMA, strict)
    assert result.accepted == {}
    assert result.rejected[0]["reason"] == REASON_DELTA_EXCEEDED


def test_hard_rule_pattern_flat_and_wildcard_forms() -> None:
    """规则键两形态：`*` 单段通配对声明路径；无 `*` = 对变更键精确匹配。"""
    flat_rule = {"好感度": {"max_delta_per_turn": 1}}
    result = validate_and_apply(
        {"好感度": 0}, {"好感度": "=5"}, SCHEMA, flat_rule
    )
    assert result.rejected[0]["reason"] == REASON_DELTA_EXCEEDED


def test_hard_rule_ignored_for_non_numeric() -> None:
    """max_delta_per_turn 只约束数值变更；string/bool/enum 变更不受其罚。"""
    rules = {"note.出处": {"max_delta_per_turn": 1}, "world.*": {"max_delta_per_turn": 1}}
    result = validate_and_apply(
        {"出处": "甲", "时辰": "晨"}, {"note.出处": "乙", "world.时辰": "夜"}, SCHEMA, rules
    )
    assert result.accepted == {"note.出处": "乙", "world.时辰": "夜"}


# ── 兼容态：schema=None = 全 accepted 逐字保持现状 ────────────────────────


def test_schema_none_passthrough_verbatim() -> None:
    """无声明卡：不解释 op、不类型检查、不 clamp——changes 逐字透传。"""
    changes: dict[str, Any] = {"好感度": "不是数字", "任意键": {"nested": True}}
    result = validate_and_apply({"好感度": 1}, changes, None, None)
    assert result.accepted == changes, "逐字保持现状行为"
    assert result.rejected == []


def test_schema_none_never_rejects() -> None:
    result = validate_and_apply({}, {"好感度": 500, "x": -1}, None, None)
    assert result.accepted == {"好感度": 500, "x": -1}


# ── 审计行（全流水）与性质断言 ────────────────────────────────────────────


def test_audit_rows_cover_all_changes_with_old_new() -> None:
    current = {"好感度": 50, "note.出处": "甲"}
    result = validate_and_apply(
        current,
        {"好感度": "+5", "note.出处": "乙", "魔力": 1},
        SCHEMA,
        None,
    )
    assert len(result.audit) == 3, "审计 = accepted + rejected 全流水"
    by_key = {row["key"]: row for row in result.audit}
    assert by_key["好感度"] == {
        "key": "好感度", "op": "+", "from": 50, "to": 55, "reason": REASON_ACCEPTED,
    }
    assert by_key["note.出处"] == {
        "key": "note.出处", "op": "=", "from": "甲", "to": "乙", "reason": REASON_ACCEPTED,
    }
    assert by_key["魔力"]["reason"] == REASON_UNKNOWN_KEY
    assert by_key["魔力"]["to"] == 1, "rejected 行记提议原值"


def test_every_change_lands_in_exactly_one_bucket() -> None:
    """性质：变更键全归宿——accepted ∪ rejected 键集 == 输入键集（不丢变更）。"""
    changes = {"好感度": "+3", "魔力": 1, "world.时辰": "夜", "flags.见光": True}
    result = validate_and_apply({"好感度": 0, "时辰": "晨"}, changes, SCHEMA, HARD_RULES)
    landed = set(result.accepted) | {r["key"] for r in result.rejected}
    assert landed == set(changes)


def test_inputs_not_mutated() -> None:
    current = {"好感度": 50}
    changes = {"好感度": "+5", "player.林远.灵力": "=7"}
    current_copy = {"好感度": 50}
    changes_copy = dict(changes)
    validate_and_apply(current, changes, SCHEMA, HARD_RULES)
    assert current == current_copy, "入参防突变"
    assert changes == changes_copy, "入参防突变"


# ── 边界分支补全（fail-closed 各拒绝出口均有专属断言） ────────────────────


def test_dotted_key_not_exact_is_unknown() -> None:
    """含点非精确键无后缀匹配 → unknown_key（无中间形态猜测）。"""
    result = validate_and_apply({}, {"npc.不存在.好感度": 1}, SCHEMA, None)
    assert result.accepted == {}
    assert result.rejected[0]["reason"] == REASON_UNKNOWN_KEY


@pytest.mark.parametrize(
    "rules",
    [
        {"npc.*.好感度": "不是对象"},  # 规则值非 dict → 规则跳过
        {"npc.*.好感度": {"max_delta_per_turn": "很多"}},  # limit 非数值 → 跳过
        {"npc.*.好感度": {}},  # 无 max_delta_per_turn 键 → 跳过
    ],
)
def test_malformed_hard_rule_skipped(rules: dict[str, Any]) -> None:
    """规则不合法 = 该规则不生效（不误拒合法变更，fail-soft 只针对规则面）。"""
    result = validate_and_apply({"好感度": 0}, {"好感度": "=50"}, SCHEMA, rules)
    assert result.accepted == {"好感度": 50}


def test_float_decl_rejects_bool() -> None:
    result = validate_and_apply({}, {"ratio.浓度": True}, SCHEMA, None)
    assert result.rejected[0]["reason"] == REASON_TYPE_MISMATCH


def test_unsupported_decl_type_rejects() -> None:
    """声明 type 不在支持集（如 list）= 该键全拒（fail-closed）。"""
    schema = {"misc": {"标签": {"type": "list"}}}
    result = validate_and_apply({}, {"misc.标签": [1]}, schema, None)
    assert result.accepted == {}
    assert result.rejected[0]["reason"] == REASON_TYPE_MISMATCH


def test_malformed_schema_subtree_logged_and_keys_rejected() -> None:
    """声明含非法子树：合法叶照常生效；指向非法子树的键按 unknown_key 拒。"""
    schema = {"npc": {"甲": {"好感度": {"type": "int"}}, "乙": 3}}
    result = validate_and_apply({}, {"好感度": 1, "乙": 2}, schema, None)
    assert result.accepted == {"好感度": 1}
    assert result.rejected[0]["key"] == "乙"
    assert result.rejected[0]["reason"] == REASON_UNKNOWN_KEY


def test_relative_non_integer_delta_on_int_decl_rejected() -> None:
    """int 声明收非整相对量（+5.5）= type_mismatch。"""
    result = validate_and_apply({"灵力": 10}, {"player.林远.灵力": "+5.5"}, SCHEMA, None)
    assert result.accepted == {}
    assert result.rejected[0]["reason"] == REASON_TYPE_MISMATCH


def test_relative_integer_delta_on_float_decl_accepted() -> None:
    """float 声明收整相对量（+2）= 合法（数值族）；越界照常 clamp。"""
    result = validate_and_apply({"ratio.浓度": 0.5}, {"ratio.浓度": "+2"}, SCHEMA, None)
    assert result.accepted == {"ratio.浓度": 1.0}
    assert result.audit[0]["reason"] == REASON_CLAMPED


# ── RP 批 2-Ⅱ（2c）：变更序列形态——同键按序结算 + 来源审计 extra ──────────


def test_sequence_relative_ops_stack_on_prior_result() -> None:
    """序列形态：同键相对 op 依次叠加于前序已定值（+5 后 +3 = +8，非各自从现值起算）。"""
    result = validate_and_apply(
        {"好感度": 50}, [("好感度", "+5", {}), ("好感度", "+3", {})], SCHEMA, None
    )
    assert result.accepted == {"好感度": 58}
    assert [row["from"] for row in result.audit] == [50, 55], "第二条 from = 前序结果"
    assert [row["to"] for row in result.audit] == [55, 58]


def test_sequence_absolute_overrides_prior_relative() -> None:
    """序列形态：绝对 op 覆盖前序结果（缺省取最后语义的 = 形态）。"""
    result = validate_and_apply(
        {"好感度": 50}, [("好感度", "+5", {}), ("好感度", "=30", {})], SCHEMA, None
    )
    assert result.accepted == {"好感度": 30}


def test_sequence_absolute_then_relative_bases_on_absolute() -> None:
    """序列形态：绝对赋值后接相对 = 相对以绝对结果为基线（=60 后 +5 → 65）。"""
    result = validate_and_apply(
        {"好感度": 50}, [("好感度", "=60", {}), ("好感度", "+5", {})], SCHEMA, None
    )
    assert result.accepted == {"好感度": 65}


def test_sequence_audit_extra_tags_source_and_behavior() -> None:
    """审计 extra 并进行：behavior 变更行带 source=behavior + behavior 名；
    无 extra 的行不添来源键（默认由落账步补 state_marker）。"""
    extra = {"source": "behavior", "behavior": "怒目"}
    result = validate_and_apply(
        {"好感度": 50}, [("好感度", "+5", {}), ("好感度", "+3", extra)], SCHEMA, None
    )
    assert len(result.audit) == 2, "同键两次变更各占一行（审计 = 全流水）"
    assert "source" not in result.audit[0]
    assert result.audit[1]["source"] == "behavior"
    assert result.audit[1]["behavior"] == "怒目"
    assert result.audit[1]["from"] == 55
    assert result.audit[1]["to"] == 58


def test_sequence_rejected_row_carries_extra() -> None:
    """被拒变更同样并 extra：behavior 变更打在 schema 外键上 rejected 行带来源。"""
    extra = {"source": "behavior", "behavior": "越权"}
    result = validate_and_apply({}, [("魔力", 1, extra)], SCHEMA, None)
    assert result.accepted == {}
    assert result.rejected[0]["reason"] == REASON_UNKNOWN_KEY
    assert result.rejected[0]["source"] == "behavior"
    assert result.rejected[0]["behavior"] == "越权"


def test_sequence_delta_checked_per_change_against_prior() -> None:
    """max_delta_per_turn 按条对前序值判定：单条 +6 超限拒；拆两条 +3 各自 |Δ|=3 过。"""
    rules = {"npc.*.好感度": {"max_delta_per_turn": 5}}
    single = validate_and_apply({"好感度": 0}, [("好感度", "+6", {})], SCHEMA, rules)
    assert single.accepted == {}
    assert single.rejected[0]["reason"] == REASON_DELTA_EXCEEDED

    split = validate_and_apply(
        {"好感度": 0}, [("好感度", "+3", {}), ("好感度", "+3", {})], SCHEMA, rules
    )
    assert split.accepted == {"好感度": 6}, "同键连击按条结算，第二条以 3 为前序"
    assert split.rejected == []


def test_sequence_dict_form_equivalent_for_unique_keys() -> None:
    """dict 形态（2-Ⅰ 现状）与单键序列形态结果逐字段等价（兼容锚）。"""
    current = {"好感度": 50}
    dict_form = validate_and_apply(current, {"好感度": "+5"}, SCHEMA, None)
    seq_form = validate_and_apply(current, [("好感度", "+5", {})], SCHEMA, None)
    assert dict_form.accepted == seq_form.accepted
    assert dict_form.audit == seq_form.audit
    assert dict_form.rejected == seq_form.rejected == []


def test_sequence_schema_none_last_value_wins_with_extra() -> None:
    """兼容态（schema=None）序列形态：逐字 accepted、同键最后值胜、extra 并审计行。"""
    extra = {"source": "behavior", "behavior": "b"}
    result = validate_and_apply(
        {"好感度": 1}, [("好感度", 2, {}), ("好感度", 3, extra)], None, None
    )
    assert result.accepted == {"好感度": 3}
    assert result.audit[-1]["source"] == "behavior"
    assert result.rejected == []


def test_sequence_relative_without_prior_uses_ledger_then_default() -> None:
    """序列形态基线链：同键首条无前序 = 账本现值；账本也缺 = 声明 default（2-Ⅰ 契约不变）。"""
    result = validate_and_apply({}, [("player.林远.灵力", "+5", {})], SCHEMA, None)
    assert result.accepted == {"player.林远.灵力": 15}, "无现值取 default=10"

    chain = validate_and_apply(
        {}, [("player.林远.灵力", "+5", {}), ("player.林远.灵力", "+5", {})], SCHEMA, None
    )
    assert chain.accepted == {"player.林远.灵力": 20}, "第二条以前序 15 为基线"


def test_sequence_inputs_not_mutated() -> None:
    """序列形态入参防突变（序列本体与 extra dict 均不被改写）。"""
    extra = {"source": "behavior", "behavior": "b"}
    changes: list[tuple[str, Any, dict[str, Any]]] = [
        ("好感度", "+5", {}),
        ("好感度", "+3", extra),
    ]
    snapshot = [(k, v, dict(e)) for k, v, e in changes]
    validate_and_apply({"好感度": 50}, changes, SCHEMA, None)
    assert [(k, v, dict(e)) for k, v, e in changes] == snapshot


# ── RP 批 3：cheat 旁路 + keep_state_machine 例外执法 ─────────────────────


def test_cheat_bypass_full_passthrough_verbatim() -> None:
    """cheat_keep=False 全旁路：不解释 op、不类型检查、不 clamp、不查 schema 外。"""
    result = validate_and_apply(
        {"好感度": 50},
        {"好感度": "+50", "任意新键": "任意类型"},  # 相对 op 串原样透传（不解析）
        SCHEMA,
        {"好感度": {"max_delta_per_turn": 1}},  # delta 规则一并旁路
        cheat_keep=False,
    )
    assert result.accepted == {"好感度": "+50", "任意新键": "任意类型"}, "逐字透传"
    assert result.rejected == []
    assert all(row["reason"] == REASON_CHEAT_BYPASS for row in result.audit)
    assert len(result.audit) == 2


def test_cheat_bypass_skips_clamp_and_type() -> None:
    """越界值不 clamp、类型不检查：旁路键落账值即提议原值（越界值由审计兜底）。"""
    result = validate_and_apply(
        {"好感度": 50}, {"好感度": 99999, "时辰": "子夜"}, SCHEMA, None, cheat_keep=False
    )
    assert result.accepted == {"好感度": 99999, "时辰": "子夜"}, "越界/非法 enum 原样 accepted"
    assert result.rejected == []


def test_cheat_keep_pattern_still_enforces_matched_keys() -> None:
    """keep_state_machine 例外：命中键域仍走全校验，未命中键旁路。"""
    result = validate_and_apply(
        {"好感度": 50},
        {"好感度": "+50", "时辰": "子夜", "任意新键": 1},
        SCHEMA,
        {"好感度": {"max_delta_per_turn": 5}},
        cheat_keep=["npc.*.好感度"],
    )
    assert result.accepted == {"时辰": "子夜", "任意新键": 1}, "未命中键照常旁路（含 schema 外键）"
    assert result.accepted.get("好感度") is None, "命中键域仍执法"
    (kept_row,) = [row for row in result.audit if row["key"] == "好感度"]
    assert kept_row["reason"] == REASON_DELTA_EXCEEDED, "delta 规则对保留键域生效"


def test_cheat_keep_wildcard_and_flat_key_forms() -> None:
    """保留模式两形态：通配段匹配声明路径；无 * 精确匹配变更键原样。"""
    delta_rule: dict[str, Any] = {"好感度": {"max_delta_per_turn": 5}}
    wild = validate_and_apply(
        {"好感度": 50}, {"好感度": "+50"}, SCHEMA, delta_rule, cheat_keep=["npc.*.好感度"]
    )
    assert wild.rejected
    assert wild.rejected[0]["reason"] == REASON_DELTA_EXCEEDED

    exact = validate_and_apply(
        {"好感度": 50}, {"好感度": "+50"}, SCHEMA, delta_rule, cheat_keep=["好感度"]
    )
    assert exact.rejected
    assert exact.rejected[0]["reason"] == REASON_DELTA_EXCEEDED, "无 * 精确键同样命中"

    flat_path = validate_and_apply(
        {}, {"player.林远.灵力": 5}, SCHEMA, None, cheat_keep=["player.林远.灵力"]
    )
    assert flat_path.accepted == {"player.林远.灵力": 5}, "精确声明路径命中 = 保留执法（合法值过）"


def test_cheat_keep_true_enforces_everything() -> None:
    """keep=True：全键域保留执法（结果与 cheat_keep=None 现状逐字段一致）。"""
    changes = {"好感度": "+50", "时辰": "子夜", "未知键": 1}
    baseline = validate_and_apply({"好感度": 50}, changes, SCHEMA, None)
    kept = validate_and_apply({"好感度": 50}, changes, SCHEMA, None, cheat_keep=True)
    assert kept.accepted == baseline.accepted
    assert kept.rejected == baseline.rejected
    assert kept.audit == baseline.audit


def test_cheat_keep_none_is_current_behavior() -> None:
    """cheat_keep 缺省（None）= 现状：签名扩展零侵入（既有调用不传即旧行为）。"""
    changes = {"好感度": "+50", "时辰": "子夜"}
    baseline = validate_and_apply({"好感度": 50}, changes, SCHEMA, None)
    assert baseline.rejected
    assert all(row["reason"] != REASON_CHEAT_BYPASS for row in baseline.audit)


def test_cheat_bypass_then_relative_verbatim_in_sequence() -> None:
    """旁路形态序列：相对 op 串不解释、逐字透传、同键最后值胜、extra 随行。"""
    result = validate_and_apply(
        {"好感度": 50},
        [("好感度", "+5", {}), ("好感度", "+9", {"source": "behavior", "behavior": "b"})],
        SCHEMA,
        None,
        cheat_keep=False,
    )
    assert result.accepted == {"好感度": "+9"}, "op 串原样透传（不解析不叠加）"
    assert [row["reason"] for row in result.audit] == [
        REASON_CHEAT_BYPASS,
        REASON_CHEAT_BYPASS,
    ]
    assert result.audit[1]["source"] == "behavior"


def test_cheat_bypass_property_every_change_lands_once() -> None:
    """性质：旁路形态下变更键仍全归宿且审计行数 = 变更数（全流水不缺行）。"""
    changes = {"好感度": "+50", "时辰": "子夜", "新键": 1, "出处": "北境"}
    result = validate_and_apply({"好感度": 1}, changes, SCHEMA, None, cheat_keep=False)
    assert set(result.accepted) == set(changes)
    assert len(result.audit) == len(changes)


# ── 校验分层（缺口批次一：§7.1 支柱 4 拒绝必须带原因）─────────────────────
# hard = 契约级（schema 外键/类型违例/ambiguous_key/no_baseline——公理层不可
# 商量）；soft = 规则级值域类（delta_exceeded/clamped——可申诉/可援例）。


def test_reason_layers_map_total_and_layered() -> None:
    """现有 reason 码逐一映射层：映射全量、值域恰为 {hard, soft}。"""
    all_codes = {
        REASON_ACCEPTED,
        REASON_CLAMPED,
        REASON_UNKNOWN_KEY,
        REASON_AMBIGUOUS_KEY,
        REASON_TYPE_MISMATCH,
        REASON_NO_BASELINE,
        REASON_DELTA_EXCEEDED,
        REASON_CHEAT_BYPASS,
    }
    assert set(REASON_LAYERS) == all_codes, "现有 reason 码逐一有层"
    assert set(REASON_LAYERS.values()) == {LAYER_HARD, LAYER_SOFT}
    for code in (
        REASON_UNKNOWN_KEY,
        REASON_AMBIGUOUS_KEY,
        REASON_TYPE_MISMATCH,
        REASON_NO_BASELINE,
    ):
        assert REASON_LAYERS[code] == LAYER_HARD, f"{code} 属契约级（公理层）"
    for code in (REASON_DELTA_EXCEEDED, REASON_CLAMPED):
        assert REASON_LAYERS[code] == LAYER_SOFT, f"{code} 属规则级值域类"


@pytest.mark.parametrize(
    ("changes", "current", "expected_reason", "expected_layer"),
    [
        ({"魔力": 1}, {}, REASON_UNKNOWN_KEY, LAYER_HARD),
        ({"npc.不存在.好感度": 1}, {}, REASON_UNKNOWN_KEY, LAYER_HARD),
        ({"好感度": "abc"}, {"好感度": 50}, REASON_TYPE_MISMATCH, LAYER_HARD),
        ({"好感度": "+5"}, {}, REASON_NO_BASELINE, LAYER_HARD),
        ({"好感度": "+50"}, {"好感度": 50}, REASON_DELTA_EXCEEDED, LAYER_SOFT),
        ({"好感度": "=200"}, {"好感度": 50}, REASON_DELTA_EXCEEDED, LAYER_SOFT),
    ],
)
def test_rejected_rows_carry_layer(
    changes: dict[str, Any],
    current: dict[str, Any],
    expected_reason: str,
    expected_layer: str,
) -> None:
    """每条 rejected 行带 layer：契约级=hard（公理不可商量）/值域超限=soft（可申诉）。

    区分度输入两族：键面/类型面/基线面拒绝（hard）与相对/绝对两形态值域
    超限（soft，hard_rules 经 HARD_RULES 对 好感度 生效）。
    """
    result = validate_and_apply(current, changes, SCHEMA, HARD_RULES)
    (row,) = result.rejected
    assert row["reason"] == expected_reason
    assert row["layer"] == expected_layer


def test_rejected_layer_ambiguous_key_is_hard() -> None:
    """ambiguous_key 属契约级（键映射不明 = 公理面 fail-closed 不猜）。"""
    schema = {
        "npc": {"甲": {"好感度": {"type": "int"}}, "乙": {"好感度": {"type": "int"}}},
    }
    result = validate_and_apply({}, {"好感度": 1}, schema, None)
    assert result.rejected[0]["reason"] == REASON_AMBIGUOUS_KEY
    assert result.rejected[0]["layer"] == LAYER_HARD


def test_audit_mirror_rejected_row_carries_layer_accepted_does_not() -> None:
    """审计镜像：rejected 行的 layer 随行进全流水（下游零重推导可辨拒绝行）；
    accepted/clamped 行不盖章（layer 缺席 = 非拒绝行的判别面）。"""
    result = validate_and_apply(
        {"好感度": 50}, {"好感度": "+5", "魔力": 1}, SCHEMA, HARD_RULES
    )
    assert result.accepted == {"好感度": 55}, "前置：好感度合法增量（clamp 界内）"
    by_key = {row["key"]: row for row in result.audit}
    assert by_key["魔力"]["layer"] == LAYER_HARD
    assert "layer" not in by_key["好感度"]

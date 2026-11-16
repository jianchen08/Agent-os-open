"""State Contract Validator（RP 批 2-Ⅰ 2b，state_marker_parse 步内升级）。

设计真值 docs/working/rp-engine-design.md §3.2 + RP角色扮演_设计文档_20261010 §五：
作者声明通道的状态变更对（动作声明效果 / behaviors 代理变更 / 回退开关开启时
`<state>` 标记 state 字段提议——2026-10-13 动作驱动基线，默认关）在落账前过本
Validator——path 在 schema 内 → op/类型检查 → clamp min/max →
hard_rules.max_delta_per_turn → accepted/rejected 双账。**LLM 永不直写状态**：
schema 外键 rejected 即执法，本步仍是唯一落账通道。

## 声明与键映射契约

schema 源 = ``state["context.rp_state_schema"]``（agent_config_load 按卡
yaml ``extensions.variables`` 装载，作者任意嵌套，**以叶节点声明为准**——
叶 = 含 ``type`` 键的 dict；路径 = 根到叶键名点连接，如
``npc.苏清婉.好感度``）。而现役 ``<state>`` 标记的 state 键是**扁平键名**
（如 ``好感度``）。变更键匹配声明路径的规则（按序）：

1. **精确匹配**：变更键与声明路径逐字相等（扁平声明与路径键都走这条）；
2. **唯一叶名匹配**（仅无点扁平键）：变更键等于恰好一个声明路径的末段
   → 映射到该声明（``好感度`` → ``npc.苏清婉.好感度``）；
3. 命中多个末段 = 歧义 → rejected（``ambiguous_key``，fail-closed 不猜）；
   无命中 = ``unknown_key``；含点的非精确键 = ``unknown_key``（无后缀匹配）。

**账本键 = 变更键原样**（扁平写扁平、路径写路径）：schema 只做校验与审计
身份，落账仍走 character_state 既有 schema 门禁（未知键在账本侧报错 =
既有降级路径）。

## op 与值形态（schema 态）

- ``+N`` / ``-N``（数值字符串）：相对变更，仅 int/float 声明；基线取账本
  现值，缺现值取声明 ``default``，双缺 = rejected（``no_baseline``）；
- ``=X``：绝对赋值（全类型；X 按声明类型解析）；
- 裸字面：等价 ``=``（绝对）。

类型集：int / float / string / bool / enum（``values`` 列表成员）。
bool 与 int 显式区分（Python bool 是 int 子类，声明 int 不收 True）。

## 结算次序（同变更）

类型/op 检查 → 相对求新值 → **clamp min/max**（clamp 是 accepted 后取边界
值，``reason=clamped``）→ **max_delta_per_turn** 对 clamp 后 |new-old| 判定
（恰等 = accepted；超限 = rejected ``delta_exceeded``；仅数值变更受罚）。

## 校验分层（缺口批次一，§7.1 支柱 4）

每条 rejected 行带 ``layer`` 字段（单源 ``REASON_LAYERS`` 逐一映射）：
``hard`` = 契约级——schema 外键（``unknown_key``）/类型违例
（``type_mismatch``）/``ambiguous_key``/``no_baseline``，公理层不可商量；
``soft`` = 规则级值域类——``delta_exceeded``（含 clamp 值域面），可申诉/
可援例。layer 随行进审计镜像（layer 在场 = 拒绝行可辨），accepted/clamped/
cheat_bypass 非拒绝行不带 layer（缺席 = 非拒绝判别面）。

## hard_rules 形态

``state["context.rp_state_hard_rules"]``（卡 ``extensions.rules.hard_rules``）：
``{<pattern>: {max_delta_per_turn: N}}``。pattern 点分段，``*`` 匹配单段，
对**声明路径**或**变更键原样**整串匹配；无 ``*`` = 精确键。首条命中生效。
``combat`` / ``random_events`` 等其余 rules 键随卡装载但本刀不消费（批 4 宿主）。

## 兼容态（本刀最重验收）

``schema=None``（无声明卡/出厂卡）= 全 accepted 逐字透传——不解释 op、不
类型检查、不 clamp、无 rejected；账本结果与现状零差异（对照测试锁定）。

## 变更序列形态（批 2-Ⅱ 2c 扩展：同键同轮合并结算 + 来源审计）

``changes`` 两形态：
- **dict**（2-Ⅰ 现状）：键唯一，逐键一次结算，行为与 2-Ⅰ 逐字段一致；
- **``[(key, value, audit_extra)]`` 序列**：同键可多次出现，按序结算——相对
  op（+/-）基线取**前序已定值**（无前序 = 账本现值 → 声明 default，2-Ⅰ 契约
  不变），依次叠加；绝对 op（=）直接覆盖。调用方（state_marker_parse）以
  「动作效果在前、标记变更次之（回退开关开启时，2026-10-13 动作驱动基线）、
  behaviors 代理变更在后」定序（同键同轮「缺省取最后」= 序列后者胜绝对/
  叠加相对）。``audit_extra``（如 ``{"source": "behavior", "behavior": <名>}``）
  并流进该变更的 rejected/审计行（键冲突 extra 赢）；无 extra 的行不带来源键
  （落账步补缺省 source：作弊轮=cheat、回退开启=llm、否则 state_marker）。
  审计行数 = 序列长度（同键多次出现各占一行，from 显示前序值）；
  max_delta_per_turn 按条对前序值判定。

「作者可配」的取最强合并模式本刀不做（后置登记）；learned_behaviors / 写规则
LLM 同为后置登记。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypeGuard

# 声明契约三件套（类型集/叶遍历）单源在 _libs/card_contract 包（T6c 正经包）：
# lint 面（mode_roleplay card_lint）与本模块（执法面）经该包共用同一套判定。
# flatten_variables 为 2-Ⅰ 公共面 re-export（测试与外部经本模块导入，单源已迁）。
from _libs.card_contract import SUPPORTED_VARIABLE_TYPES, flatten_variables  # noqa: F401

# rejected/审计 reason 值（前端状态卡可展开；批 2 面板刀消费面）
REASON_ACCEPTED = "accepted"
REASON_CLAMPED = "clamped"
REASON_UNKNOWN_KEY = "unknown_key"
REASON_AMBIGUOUS_KEY = "ambiguous_key"
REASON_TYPE_MISMATCH = "type_mismatch"
REASON_NO_BASELINE = "no_baseline"
REASON_DELTA_EXCEEDED = "delta_exceeded"
# 作弊旁路（RP 批 3）：/cheat 生效期间非保留键域逐字透传的逐行标记
REASON_CHEAT_BYPASS = "cheat_bypass"

# 校验分层（缺口批次一，§7.1 支柱 4 拒绝必须带原因）：hard = 契约级
# （schema 外键/类型违例/ambiguous_key/no_baseline——公理层不可商量）；
# soft = 规则级值域类（delta_exceeded/clamped——可申诉/可援例）。
LAYER_HARD = "hard"
LAYER_SOFT = "soft"

# 现有 reason 码逐一映射层（rejected 行盖章单源）。accepted/clamped/
# cheat_bypass 非拒绝行，映射值仅完备性——clamped/旁路属值域规则面故 soft。
REASON_LAYERS: dict[str, str] = {
    REASON_UNKNOWN_KEY: LAYER_HARD,
    REASON_AMBIGUOUS_KEY: LAYER_HARD,
    REASON_TYPE_MISMATCH: LAYER_HARD,
    REASON_NO_BASELINE: LAYER_HARD,
    REASON_DELTA_EXCEEDED: LAYER_SOFT,
    REASON_CLAMPED: LAYER_SOFT,
    REASON_ACCEPTED: LAYER_SOFT,
    REASON_CHEAT_BYPASS: LAYER_SOFT,
}

# 变更序列条目：(key, value, 审计附加字段)。dict 形态在入口归一为 extra={} 条目。
ChangeItem = tuple[str, Any, dict[str, Any]]

_RELATIVE_RE = re.compile(r"^([+-])\s*(\d+(?:\.\d+)?)$")

logger = logging.getLogger(__name__)


@dataclass
class ValidateResult:
    """validate_and_apply 三账：accepted 落账值 / rejected 记录 / 审计全流水。"""

    accepted: dict[str, Any] = field(default_factory=dict)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)


def _reject_row(
    key: str, op: str | None, frm: Any, to: Any, reason: str
) -> dict[str, Any]:
    """rejected 行体：校验行 + layer 分层标记（reason→layer 单源 REASON_LAYERS）。

    layer 随行进 rejected 与审计镜像——审计行的 layer 在场 = 拒绝行可辨
    （下游事件面/沉淀回路零重推导）。
    """
    return {
        "key": key,
        "op": op,
        "from": frm,
        "to": to,
        "reason": reason,
        "layer": REASON_LAYERS[reason],
    }


def _normalize_changes(
    changes: dict[str, Any] | Sequence[ChangeItem],
) -> list[ChangeItem]:
    """changes 两形态归一为序列条目（dict 键唯一 → extra 空表；防突变拷 extra）。"""
    if isinstance(changes, dict):
        return [(str(key), value, {}) for key, value in changes.items()]
    return [(str(key), value, dict(extra)) for key, value, extra in changes]


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> TypeGuard[float]:
    """int/float 且非 bool（bool 是 int 子类，显式排除；窄化为数值型）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _match_schema_key(
    key: str, flat: dict[str, dict[str, Any]]
) -> tuple[str | None, str | None]:
    """变更键 → (声明路径, reason)。路径非 None 即命中（reason=None）。"""
    if key in flat:
        return key, None
    if "." in key:
        return None, REASON_UNKNOWN_KEY
    hits = [path for path in flat if path.rsplit(".", 1)[-1] == key]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, REASON_AMBIGUOUS_KEY
    return None, REASON_UNKNOWN_KEY


def _pattern_matches(pattern: str, path: str) -> bool:
    """hard_rules 键形态：点分段，``*`` 单段通配；无 ``*`` = 精确匹配。"""
    if "*" not in pattern:
        return pattern == path
    parts = pattern.split(".")
    target = path.split(".")
    return len(parts) == len(target) and all(
        p in ("*", t) for p, t in zip(parts, target, strict=True)
    )


def _match_delta_limit(
    hard_rules: dict[str, Any] | None, candidates: tuple[str, ...]
) -> float | None:
    """首条命中 pattern 的 max_delta_per_turn（无命中/规则不合法 = None）。"""
    if not hard_rules:
        return None
    for pattern, rule in hard_rules.items():
        if not isinstance(rule, dict):
            continue
        limit = rule.get("max_delta_per_turn")
        if not _is_num(limit):
            continue
        if any(_pattern_matches(str(pattern), c) for c in candidates):
            return float(limit)
    return None


def _parse_number(text: str) -> int | float | None:
    """数字字符串 → int/float（非数字 = None）。"""
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def _coerce_absolute(
    decl_type: str, decl: dict[str, Any], value: Any
) -> tuple[Any, str | None]:
    """绝对赋值解析：返回 (规范值, reason)。reason 非 None = 拒绝。"""
    if decl_type == "bool":
        return (value, None) if isinstance(value, bool) else (None, REASON_TYPE_MISMATCH)
    if decl_type == "string":
        if isinstance(value, str):
            return (value[1:] if value.startswith("=") else value), None
        return None, REASON_TYPE_MISMATCH
    if decl_type == "enum":
        if isinstance(value, str) and value.startswith("="):
            value = value[1:]
        values = decl.get("values")
        if isinstance(values, list) and value in values:
            return value, None
        return None, REASON_TYPE_MISMATCH
    # int / float：数值族（"=N" 前缀或数字字符串字面均按声明类型解析）
    if isinstance(value, str):
        value = value[1:] if value.startswith("=") else value
        value = _parse_number(value)
        if value is None:
            return None, REASON_TYPE_MISMATCH
    if decl_type == "int" and not _is_int(value):
        return None, REASON_TYPE_MISMATCH
    if decl_type == "float" and not _is_num(value):
        return None, REASON_TYPE_MISMATCH
    return value, None


def _clamp(decl: dict[str, Any], value: Any) -> tuple[Any, bool]:
    """min/max 边界钳制（仅数值声明在场时）；返回 (值, 是否发生 clamp)。"""
    clamped = False
    minimum, maximum = decl.get("min"), decl.get("max")
    if _is_num(value):
        if _is_num(minimum) and value < minimum:
            value, clamped = minimum, True
        if _is_num(maximum) and value > maximum:
            value, clamped = maximum, True
    return value, clamped


def _cheat_kept(
    cheat_keep: bool | list[str], key: str, flat: dict[str, dict[str, Any]]
) -> bool:
    """cheat 保留判定：键域在 keep 规格内 = 仍走全校验（未命中 = 旁路透传）。

    True = 全保留；list = 模式对（声明路径, 变更键原样）任一候选命中即保留
    （模式语言同 hard_rules：点分段 ``*`` 单段，无 ``*`` = 精确）；False =
    全旁路。schema 外键无声明路径，仅变更键原样参与匹配。
    """
    if cheat_keep is True:
        return True
    if isinstance(cheat_keep, list):
        path, _ = _match_schema_key(key, flat)
        candidates = (path, key) if path is not None else (key,)
        return any(
            _pattern_matches(str(pattern), candidate)
            for pattern in cheat_keep
            for candidate in candidates
        )
    return False


def validate_and_apply(
    current: dict[str, Any],
    changes: dict[str, Any] | Sequence[ChangeItem],
    schema: dict[str, Any] | None,
    hard_rules: dict[str, Any] | None = None,
    *,
    cheat_keep: bool | list[str] | None = None,
) -> ValidateResult:
    """逐变更校验并结算（契约见模块 docstring；入参防突变，纯函数）。

    changes 支持 dict（2-Ⅰ 形态）或 ``[(key, value, audit_extra)]`` 序列
    （同键按序叠加/覆盖 + 来源审计，见模块 docstring 序列形态节）。

    cheat_keep（RP 批 3，/cheat 生效期间由调用方按作者声明传入）：None =
    现状执法（缺省，既有调用零变化）；``True`` = 全键域保留执法（与 None 结
    果一致，语义显式）；``list[str]`` = 命中键域保留、未命中键旁路；``False``
    = 全旁路。旁路 = 兼容态同款逐字透传（不解释 op、不类型检查、不 clamp、
    不 delta、不查 schema 外），审计行 reason=``cheat_bypass`` 逐行可辨；schema
    为 None（兼容态）时 cheat_keep 不参与（本就无契约可保）。

    Returns:
        ValidateResult:
        - accepted：账本键 → 落账值（clamp 后；序列形态同键 = 最后已定值）；
        - rejected：``{key, op, from, to, reason[, ...extra]}``（to = 提议原值）；
        - audit：accepted + rejected 全流水行
          ``{key, op, from, to, reason[, ...extra]}``（行数 = 变更数；
          ts/source默认/idem 由落账步补）。
    """
    items = _normalize_changes(changes)
    if not isinstance(schema, dict):
        # 兼容态（无声明卡）：全 accepted 逐字透传（同键最后值胜），审计照记
        accepted = {key: value for key, value, _ in items}
        audit = [
            {**{"key": key, "op": "=", "from": current.get(key), "to": value,
                "reason": REASON_ACCEPTED}, **extra}
            for key, value, extra in items
        ]
        return ValidateResult(accepted=accepted, audit=audit)

    flat, skipped = flatten_variables(schema)
    if skipped:
        # 声明面错误每轮都会重放，降级 debug（作者面由 lint 面 card_lint 收编）
        logger.debug("[state_contract] variables 非法声明跳过 | skipped=%s", skipped)

    result = ValidateResult()
    settled: dict[str, Any] = {}  # 本轮已定值：同键后续变更的前序基线/审计 from
    for key, value, extra in items:
        prior = settled[key] if key in settled else current.get(key)
        # cheat 旁路（批 3）：keep 规格未命中的键域逐字透传（兼容态同款）
        if cheat_keep is not None and not _cheat_kept(cheat_keep, key, flat):
            row = {"key": key, "op": "=", "from": prior, "to": value,
                   "reason": REASON_CHEAT_BYPASS}
            result.accepted[key] = value
            settled[key] = value
            result.audit.append({**row, **extra})
            continue
        path, reason = _match_schema_key(key, flat)
        if path is None:
            # 契约不变量：path 缺席 = reason 在场（_match_schema_key 互斥返回）
            assert reason is not None
            row = _reject_row(key, None, prior, value, reason)
            result.rejected.append({**row, **extra})
            result.audit.append({**row, **extra})
            continue
        decl = flat[path]
        decl_type = decl.get("type")
        if decl_type not in SUPPORTED_VARIABLE_TYPES:
            row = _reject_row(key, "=", prior, value, REASON_TYPE_MISMATCH)
            result.rejected.append({**row, **extra})
            result.audit.append({**row, **extra})
            continue

        # 1. op 解析（相对仅数值声明）
        op = "="
        delta: Any = None
        if (
            decl_type in ("int", "float")
            and isinstance(value, str)
            and (m := _RELATIVE_RE.match(value))
        ):
            op = m.group(1)
            delta = _parse_number(m.group(2))
            if decl_type == "int" and not _is_int(delta):
                delta = None  # int 声明只收整量（_parse_number 整值返 int）
            if delta is None:
                row = _reject_row(key, op, prior, value, REASON_TYPE_MISMATCH)
                result.rejected.append({**row, **extra})
                result.audit.append({**row, **extra})
                continue
            if op == "-":
                delta = -delta

        if op == "=":
            # 2. 绝对赋值：类型检查
            new_value, reason = _coerce_absolute(decl_type, decl, value)
            if reason is not None:
                row = _reject_row(key, op, prior, value, reason)
                result.rejected.append({**row, **extra})
                result.audit.append({**row, **extra})
                continue
        else:
            # 3. 相对求新值：基线 = 前序已定值 → 账本现值 → 声明 default → rejected
            if key in settled:
                old: Any = settled[key]
            elif key in current:
                old = current[key]
            else:
                old = decl.get("default")
            if old is None or not _is_num(old) or not _is_num(delta):
                row = _reject_row(
                    key, op, prior, value,
                    REASON_NO_BASELINE if old is None else REASON_TYPE_MISMATCH,
                )
                result.rejected.append({**row, **extra})
                result.audit.append({**row, **extra})
                continue
            new_value = old + delta

        # 4. clamp min/max（accepted 后取边界值）
        new_value, clamped = _clamp(decl, new_value)
        # 5. hard_rules.max_delta_per_turn（clamp 后对前序值判定，仅数值变更受罚）
        limit = _match_delta_limit(hard_rules, (path, key))
        if (
            limit is not None
            and _is_num(prior)
            and _is_num(new_value)
            and abs(new_value - prior) > limit
        ):
            row = _reject_row(key, op, prior, value, REASON_DELTA_EXCEEDED)
            result.rejected.append({**row, **extra})
            result.audit.append({**row, **extra})
            continue

        # 6. accepted（前序基线更新为本次已定值，同键连击逐条叠加）
        reason = REASON_CLAMPED if clamped else REASON_ACCEPTED
        result.accepted[key] = new_value
        settled[key] = new_value
        result.audit.append(
            {**{"key": key, "op": op, "from": prior, "to": new_value, "reason": reason},
             **extra}
        )
    return result

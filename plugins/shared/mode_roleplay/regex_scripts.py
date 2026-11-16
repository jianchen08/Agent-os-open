"""regex 脚本执行面（角色扮演模式包内物料面，SillyTavern Regex Script 语义子集）。

种子单元自包含：不 import 共享根模块，依赖仅标准库；由包内 material.py
（真实导入名 mode_roleplay.regex_scripts）与消费插件的包内模块装载机制按需
调用，不在本包内做 IO 编排。

声明面（卡 yaml ``extensions.regex_scripts[]``，字段与语义）：

- ``script_name``：标识（仅日志可读性用）；
- ``find_regex``：``/pattern/flags`` 形态或裸 pattern；flags 支持 Python re
  兼容子集 ``i``（忽略大小写）/``m``（多行）/``s``（点号跨行），其余尾标
  （ST 的 ``g`` 等）宽容忽略——替换恒全局（声明序逐条生效）；
- ``replace_with``：替换串；``$1``/``$2``… 展开捕获组（``$0`` 整匹配，未参与
  匹配的组展开为空串，``$$`` 转义字面 ``$``）；``{{setvar::名::值}}`` /
  ``{{addvar::名::增量}}`` 宏被**消耗为变量变更**（不残留文本；宏消耗作用于
  替换后的整段文本——原文自带宏同被消耗，ST 宏全局求值同语义）；
- ``affects``：影响文本源，``"User Input"``/``"AI Response"`` 标量串或列表
  两形等价；一期仅这两源，其余源（Slash Commands/World Info/Reasoning）忽略
  并 warning；
- ``min_depth``/``max_depth``：深度窗口，``depth=0`` 表示当前消息，仅当
  ``min_depth <= depth <= max_depth``（None = 不限）时生效；
- ``trim_out``：替换前从文本移除的子串列表；
- ``ephemerality``：false=持久（替换文本供落账）；true=仅渲染层——本模块
  两态统一返回替换文本，渲染剥离（display-only 不落库）由消费侧后续批次
  处理，语义区分以本契约为准；
- ``disabled``：true 跳过（合法状态，静默）。

fail-soft 铁律：坏 regex（编译失败/空 pattern/pattern 超 ``MAX_PATTERN_LENGTH``
长度护栏）、非法 affects、非 dict 脚本、
窗口字段垃圾值、执行期异常 = warning 跳过**该脚本**，不抛出不阻断对话；
变量修改解析失败（如 addvar 非数值增量）= warning 跳过**该变量变更**，文本
替换照常。warning+跳过是有处理；静默吞才是违规。

变量变更（返回的 dict，键 = 声明变量名，无未声明键形态）：``setvar`` 值为
新值（字符串），``addvar`` 值为数值增量；同轮多次声明同名 = 后者覆盖（声明
序结算）。批 2 Validator 收编前，消费方只记录不落账（临时桥，见消费点
docstring）；``variables`` 入参为当前变量表，批 2 clamp/delta 结算消费，
本层只读不改。
"""
from __future__ import annotations

import logging
import re
from functools import partial
from typing import Any

logger = logging.getLogger(__name__)

#: 一期支持的影响文本源（ST Regex Script affects 子集）。
SUPPORTED_AFFECTS = ("User Input", "AI Response")

#: find_regex pattern 长度上限（ReDoS 长度护栏）：超长 pattern 编译前拒绝，
#: 该脚本 warning 跳过。长度护栏是缓解非根治——Python re 无求值超时，根治需
#: 引擎级（带超时/RE2 类引擎=新依赖禁止）；升级触发=合法卡产物出现 >512 字符
#: pattern 的真实需求，届时走 ADR 引入带超时执行面。
MAX_PATTERN_LENGTH = 512

#: replace_with 中变量宏形态：{{setvar::名::值}} / {{addvar::名::增量}}。
_VAR_MACRO_RE = re.compile(r"\{\{(setvar|addvar)::(.*?)::(.*?)\}\}")

#: 捕获组引用与转义（JS Regex Script 惯例：$0 整匹配、$1..$99 组、$$ 字面）。
_CAPTURE_REF_RE = re.compile(r"\$(\$\d{0,2}|\d{1,2})")

_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


def _parse_find_regex(raw: Any) -> tuple[str, int] | None:
    """find_regex → (pattern, re flags)；``/pattern/flags`` 与裸 pattern 两形。

    非法（非串/空/裸段为空）返回 None（调用方 warning 跳过脚本）；未知尾标
    字符宽容忽略（替换恒全局，无逐源语义分歧）。
    """
    if not isinstance(raw, str) or not raw:
        return None
    body, flags_str = raw, ""
    if raw.startswith("/") and raw.rfind("/") > 0:
        split_at = raw.rfind("/")
        body, flags_str = raw[1:split_at], raw[split_at + 1:]
    if not body:
        return None
    flags = 0
    for ch in flags_str:
        flags |= _FLAG_MAP.get(ch, 0)
    return body, flags


def _expand_captures(replacement: str, match: re.Match) -> str:
    """replace_with 的捕获组展开：``$N`` → 组文本（缺组空串），``$$`` → ``$``。"""

    def _sub(m: re.Match) -> str:
        token = m.group(1)
        if token.startswith("$"):
            return "$"
        try:
            return match.group(int(token)) or ""
        except IndexError:
            return ""  # 未参与匹配的组 → 空串（JS 语义）

    return _CAPTURE_REF_RE.sub(_sub, replacement)


def _var_delta(raw: str) -> int | float | None:
    """addvar 增量解析：整/浮点两形；非数值 = None（调用方 warning 跳过）。"""
    text = raw.strip()
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def _normalized_affects(script: dict) -> list[str] | None:
    """affects 字段归一：标量串/字符串列表 → 列表；非法形态 = None。"""
    raw = script.get("affects")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return raw
    return None


def _depth_window(script: dict) -> tuple[int | None, int | None] | None:
    """min/max depth 窗口解析；垃圾值 = None（调用方 warning 跳过脚本）。"""
    window: list[int | None] = []
    for key in ("min_depth", "max_depth"):
        raw = script.get(key)
        if raw is None:
            window.append(None)
            continue
        try:
            window.append(int(raw))
        except (TypeError, ValueError):
            return None
    return window[0], window[1]


def _extract_var_changes(text: str) -> tuple[str, dict[str, Any]]:
    """消耗文本中的变量宏 → (剥离宏后的文本, 变量变更)；非法增量跳过该变更。"""
    changes: dict[str, Any] = {}

    def _sub(m: re.Match) -> str:
        kind, name, value = m.group(1), m.group(2).strip(), m.group(3)
        if kind == "setvar":
            changes[name] = value
            return ""
        delta = _var_delta(value)
        if delta is None:
            logger.warning(
                "[regex_scripts] addvar 增量非法，跳过该变量变更 | var=%s | raw=%r",
                name,
                value,
            )
            return ""
        changes[name] = delta
        return ""

    return _VAR_MACRO_RE.sub(_sub, text), changes


def apply_scripts(
    text: str,
    scripts: list[dict],
    affects: str,
    depth: int,
    variables: dict,
) -> tuple[str, dict]:
    """按声明序应用 regex 脚本（仅匹配 affects 的启用脚本）。

    Args:
        text: 输入文本（当前消息原文）。
        scripts: 卡 ``extensions.regex_scripts[]`` 声明列表（原样透传即可，
            本函数不修改入参）。
        affects: 本轮文本源（``User Input`` / ``AI Response``）。
        depth: 消息深度（0 = 当前消息）。
        variables: 当前变量表（批 2 Validator clamp/delta 结算消费，本层只读）。

    Returns:
        (替换后文本, 变量变更 dict)。坏脚本 fail-soft 跳过（模块 docstring
        fail-soft 铁律）；无匹配/无脚本 = 原文恒等返回。
    """
    out = text
    var_changes: dict[str, Any] = {}
    if not isinstance(scripts, list):
        return out, var_changes
    for script in scripts:
        if not isinstance(script, dict):
            logger.warning("[regex_scripts] 脚本非 dict，跳过 | got=%r", type(script).__name__)
            continue
        if script.get("disabled"):
            continue
        declared = _normalized_affects(script)
        if declared is None:
            logger.warning(
                "[regex_scripts] affects 形态非法，跳过脚本 | name=%r | affects=%r",
                script.get("script_name"),
                script.get("affects"),
            )
            continue
        unsupported = [src for src in declared if src not in SUPPORTED_AFFECTS]
        if unsupported:
            logger.warning(
                "[regex_scripts] 一期外影响源忽略 | name=%r | sources=%r",
                script.get("script_name"),
                unsupported,
            )
        if affects not in declared:
            continue
        window = _depth_window(script)
        if window is None:
            logger.warning(
                "[regex_scripts] 深度窗口字段非法，跳过脚本 | name=%r",
                script.get("script_name"),
            )
            continue
        min_depth, max_depth = window
        if min_depth is not None and depth < min_depth:
            continue
        if max_depth is not None and depth > max_depth:
            continue
        parsed = _parse_find_regex(script.get("find_regex"))
        if parsed is None:
            logger.warning(
                "[regex_scripts] find_regex 非法，跳过脚本 | name=%r | find=%r",
                script.get("script_name"),
                script.get("find_regex"),
            )
            continue
        replacement = script.get("replace_with")
        if replacement is None:
            replacement = ""
        if not isinstance(replacement, str):
            logger.warning(
                "[regex_scripts] replace_with 非字符串，跳过脚本 | name=%r",
                script.get("script_name"),
            )
            continue
        trim_out = script.get("trim_out")
        if trim_out is None:
            trim_out = []
        if not isinstance(trim_out, list) or not all(isinstance(t, str) for t in trim_out):
            logger.warning(
                "[regex_scripts] trim_out 形态非法，跳过脚本 | name=%r",
                script.get("script_name"),
            )
            continue
        try:
            if len(parsed[0]) > MAX_PATTERN_LENGTH:
                logger.warning(
                    "[regex_scripts] pattern 超长（>%d），跳过脚本 | name=%r",
                    MAX_PATTERN_LENGTH,
                    script.get("script_name"),
                )
                continue
            pattern = re.compile(parsed[0], parsed[1])
            applied = out
            for sub in trim_out:
                applied = applied.replace(sub, "")
            applied = pattern.sub(partial(_expand_captures, replacement), applied)
            applied, changes = _extract_var_changes(applied)
        except re.error as exc:
            logger.warning(
                "[regex_scripts] regex 编译/执行失败，跳过脚本 | name=%r | err=%s",
                script.get("script_name"),
                exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001 — fail-soft：单脚本异常不阻断对话
            logger.warning(
                "[regex_scripts] 脚本执行异常，跳过 | name=%r | err=%s",
                script.get("script_name"),
                exc,
            )
            continue
        out = applied
        var_changes.update(changes)
    return out, var_changes

"""消息格式规范化模块 -- llm_service 侧 LLM 调用面唯一关卡。

消息标准化属大模型调用面职责，单一真值源在本插件：所有经
``llm.complete_stream`` 出站的 messages 在 adapter 调用前统一过
``normalize_messages_for_provider``（结构标准化 + 配对校验/孤儿摘除 +
MiniMax 三阶段）。调用方（llm_core 主轮 / guard 压缩 / 未来任何 LLM
消费方）不再各自预处理。

无状态扫描：每次调用对完整消息列表做线性遍历（MiniMax 分支此前已强制
全量），无增量缓存——缓存漂移（重启恢复/裁剪重建/多消费方交替调用）类
缺陷从根上消除，摘除天然幂等。历史退化折叠的复读扫描带确定性字符预算
（见 ``collapse_degenerate_history`` 上方注释），不是无界全量扫描。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, cast

logger = logging.getLogger(__name__)


def repair_json_string(s: str) -> str | None:  # noqa: PLR0911,PLR0912,PLR0915
    """尝试修复常见的 JSON 格式问题，返回修复后的 JSON 字符串。"""
    if not s or not isinstance(s, str):
        return None

    s = s.strip()

    # 尝试 0: 去掉可能的 markdown code block 包裹
    if s.startswith("```"):
        lines = s.split("\n")
        # 去掉首行 (```json 或 ```)
        start = 1 if lines else 0
        # 去掉尾行 (```)
        end = len(lines)
        if lines and lines[-1].strip() == "```":
            end -= 1
        s = "\n".join(lines[start:end]).strip()

    # 尝试 1: 直接解析
    try:
        json.loads(s)
        return s
    except (json.JSONDecodeError, TypeError):
        pass

    # 尝试 2: 提取第一个完整的 JSON 对象 {...}
    first_brace = s.find("{")
    if first_brace >= 0:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(first_brace, len(s)):
            c = s[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\":
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[first_brace : i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except (json.JSONDecodeError, TypeError):
                        break

    # 尝试 3: 去掉尾部逗号（对象和数组中的）
    fixed = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        json.loads(fixed)
        return fixed
    except (json.JSONDecodeError, TypeError):
        pass

    # 尝试 4: 单引号 → 双引号（简单替换，非完美但能处理常见情况）
    # 只在单引号数量和双引号数量差异明显时尝试
    if s.count("'") > s.count('"'):
        fixed = s.replace("'", '"')
        try:
            json.loads(fixed)
            return fixed
        except (json.JSONDecodeError, TypeError):
            pass

    # 尝试 5: 截断修复 — 状态机闭合，保留完整字段（含半截字符串值）。
    repaired = _repair_truncation(s)
    if repaired is not None:
        return repaired

    # 尝试 6: 去掉注释 (// 和 /* */)
    fixed = re.sub(r"//.*?$", "", s, flags=re.MULTILINE)
    fixed = re.sub(r"/\*.*?\*/", "", fixed, flags=re.DOTALL)
    fixed = fixed.strip()
    if fixed != s:
        try:
            json.loads(fixed)
            return fixed
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def _repair_truncation(s: str) -> str | None:
    """修复被截断的 JSON：闭合未结束的字符串并补全括号，尽量保留完整字段。"""
    in_string = False
    escape_next = False
    stack: list[str] = []
    last_complete_idx = -1  # 顶层对象内最后一个逗号 = 完整字段边界

    for i, c in enumerate(s):
        if in_string:
            if escape_next:
                escape_next = False
            elif c == "\\":
                escape_next = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            stack.append("{")
        elif c == "[":
            stack.append("[")
        elif c == "}" and stack and stack[-1] == "{":  # noqa: SIM114
            stack.pop()
        elif c == "]" and stack and stack[-1] == "[":
            stack.pop()
        elif c == "," and len(stack) <= 1:
            last_complete_idx = i

    if not stack:
        return None  # 无未闭合结构，不是截断场景

    def _close_braces(prefix: str) -> str:
        """为 prefix 补全当前未闭合的括号（按结构栈逆序）。"""
        st: list[str] = []
        in_s = False
        esc = False
        for ch in prefix:
            if in_s:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_s = False
                continue
            if ch == '"':
                in_s = True
            elif ch in "{[":
                st.append(ch)
            elif ch == "}" and st and st[-1] == "{":  # noqa: SIM114
                st.pop()
            elif ch == "]" and st and st[-1] == "[":
                st.pop()
        return prefix + "".join("}" if ch == "{" else "]" for ch in reversed(st))

    # 步骤 1：若截断在字符串内部，闭合引号（先处理结尾悬空的反斜杠）
    if in_string:
        closed = s[:-1] if escape_next else s
        candidate = _close_braces(closed + '"')
        try:
            json.loads(candidate)
            return candidate
        except (json.JSONDecodeError, TypeError):
            pass

    # 步骤 2：仅补全括号（字符串都已闭合、只缺右括号）→ 零字段丢失
    candidate = _close_braces(s)
    try:
        json.loads(candidate)
        return candidate
    except (json.JSONDecodeError, TypeError):
        pass

    # 步骤 3：回退到最后一个完整字段边界，丢弃不完整的尾部
    if last_complete_idx > 0:
        candidate = _close_braces(s[:last_complete_idx])
        try:
            json.loads(candidate)
            return candidate
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def standardize_tool_calls_in_messages(messages: list[dict[str, Any]]) -> list[int]:
    """标准化 tool_calls 为 OpenAI API 格式（公共入口）。

    Returns:
        被改写的消息下标列表（结构重建触发过的 assistant 消息；仅缺失
        id 在结构重建时兜底生成，非空 id 一律保留原值）。无改写返回
        空列表。消息本身仍就地改写（向后兼容旧调用方忽略返回值）。
    """
    return _normalize_tool_calls_in_messages(messages)


def _normalize_tool_calls_in_messages(messages: list[dict[str, Any]]) -> list[int]:  # noqa: PLR0912
    """确保 assistant 消息中的 tool_calls 使用统一的内部格式。

    Returns:
        被就地改写的消息下标列表（保持插入顺序）。调用方可据此对相应槽位
        emit 增量 op（set(seq, 新内容)）。忽略返回值时行为与旧版完全一致。
    """
    changed: list[int] = []

    def _mark(idx: int) -> None:
        if idx not in changed:
            changed.append(idx)

    for _msg_idx, msg in enumerate(messages):
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        raw_tcs = msg["tool_calls"]
        if not isinstance(raw_tcs, list):
            continue

        # ── 修正 1: 结构格式 ──
        needs_struct_fix = False
        for tc in raw_tcs:
            if not isinstance(tc, dict):
                continue
            if tc.get("type") != "function" or not isinstance(tc.get("function"), dict):
                needs_struct_fix = True
                break
        if needs_struct_fix:
            normalized = []
            for tc in raw_tcs:
                if not isinstance(tc, dict):
                    continue
                if tc.get("type") == "function" and isinstance(tc.get("function"), dict):
                    normalized.append(tc)
                    continue
                normalized.append(
                    {
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": tc.get("args", tc.get("arguments", "{}")),
                        },
                    }
                )
            msg["tool_calls"] = normalized
            _mark(_msg_idx)

        # tool_call_id 不做格式统一重造（ADR 2026-09-28-tool-call-live-card）：
        # provider 的 id（含 call-<uuid> 连字符、call_function_xxx_1 等任意形态）
        # 原样透传——它是流式增量/工具事件/持久化/前端渲染的单一关联键，中途
        # 重造会让同一工具调用以两个 id 出现两张卡。仅在修正 1 结构重建时对
        # 缺失 id 兜底生成 call_<hex>。

    return changed


def _validate_tool_call_pairing(  # noqa: PLR0912
    messages: list[dict[str, Any]],
    provider: str,
    name: str,
    *,
    pipeline_id: str = "",
) -> list[dict[str, Any]]:
    """全量验证 tool_calls 和 tool result 的配对完整性（无状态，幂等）。

    Phase A：摘除孤儿 tool result（无前置 assistant(tool_calls) 认领、
    或 tool_call_id 失配——fail-closed，绝不 positional 改写）；
    Phase B：清理未配对完整的 assistant(tool_calls) 消息（工具执行被
    中断无结果可补的半轮）。
    """
    del pipeline_id  # 仅日志维度保留形参（与公共签名契约一致）

    # ── Phase A: 移除孤立的 tool result ──
    expecting_tool_ids: set[str] = set()
    dropped_count = 0

    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            if msg.get("tool_calls"):
                expecting_tool_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
            else:
                expecting_tool_ids = set()
            cleaned.append(msg)
        elif msg.get("role") == "tool":
            if expecting_tool_ids:
                tc_id = msg.get("tool_call_id")
                if tc_id in expecting_tool_ids:
                    expecting_tool_ids.discard(tc_id)
                    cleaned.append(msg)
                else:
                    dropped_count += 1
                    logger.debug(
                        "[%s] %s tool_call pairing: dropping tool result with unexpected tool_call_id=%s (expected %s)",
                        name,
                        provider,
                        tc_id,
                        expecting_tool_ids,
                    )
            else:
                dropped_count += 1
                logger.debug(
                    "[%s] %s tool_call pairing: dropping orphaned tool "
                    "result (tool_call_id=%s, no preceding assistant)",
                    name,
                    provider,
                    msg.get("tool_call_id", "?"),
                )
        else:
            expecting_tool_ids = set()
            cleaned.append(msg)
    if dropped_count:
        logger.debug(
            "[%s] %s tool_call pairing: dropped %d invalid tool results",
            name,
            provider,
            dropped_count,
        )

    # ── Phase B: 清理不完整的 assistant(tool_calls) 消息 ──
    final: list[dict[str, Any]] = []
    removed_count = 0
    i = 0
    while i < len(cleaned):
        msg = cleaned[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            required_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
            j = i + 1
            while j < len(cleaned) and cleaned[j].get("role") == "tool":
                tc_id = cleaned[j].get("tool_call_id")
                required_ids.discard(tc_id)
                j += 1
            if required_ids:
                # 有不完整的 tool_call → 删除整条 assistant 消息及已匹配的 tool 结果
                removed_count += 1
                logger.debug(
                    "[%s] %s tool_call pairing: removing incomplete assistant message (missing tool results: %s)",
                    name,
                    provider,
                    required_ids,
                )
                i = j  # 跳过后续已匹配的 tool 结果
            else:
                # 完整配对 → 保留
                final.append(msg)
                while i + 1 < j:
                    i += 1
                    final.append(cleaned[i])
                i = j
        else:
            final.append(msg)
            i += 1
    if removed_count:
        logger.debug(
            "[%s] %s tool_call pairing: removed %d incomplete assistant messages "
            "(tool execution was interrupted, no result available)",
            name,
            provider,
            removed_count,
        )

    return final


def normalize_messages_for_provider(  # noqa: PLR0912
    messages: list[dict[str, Any]],
    *,
    provider: str,
    name: str,
    pipeline_id: str = "",
) -> list[dict[str, Any]]:
    """针对特定 LLM 提供商的消息格式修正。

    通用段（所有 provider）：tool_calls 结构/ id 标准化 → 配对完整性校验。
    MiniMax 专有段：标准转换（Phase 1）→ 重定位（Phase 2）→ 终极安全网
    （Phase 3），三阶段各自可独立理解，行为顺序不可交换。

    就地契约：全程原地修改并**返回入参 ``messages`` 本体**（恒同一 list 对象，
    各阶段结果写回）——被摘除/被替换的消息随之释放，不随返回容器换血留存。
    """
    # 通用修正：确保 tool_calls 是 OpenAI API 格式
    _normalize_tool_calls_in_messages(messages)

    # 历史退化内容折叠（2026-11-10）：模型一次退化产出的原文留在历史里会被
    # 每轮回放，实测回灌 2 万字即让新回复出现自我指涉与复读——出站前折成占位。
    collapse_degenerate_history(messages, name=name, pipeline_id=pipeline_id)

    # ── 统一 tool_calls/tool 配对校验（所有模型）──
    # 确保每条 assistant(tool_calls) 后面跟齐所有 tool_call_id 对应的 tool 消息。
    # 消息历史在压缩/截断/执行记录恢复等场景下可能产生不配对消息，
    # 不同模型对此的容忍度不同，统一在与模型无关的归一化层修复。
    #
    # 各阶段结果就地写回入参 list（返回恒为 ``messages`` 本体）：调用面经 MCP
    # 派发帧在工具调用全程持有入参容器，返回新容器会让被摘除的孤儿/半轮消息
    # （大 payload）随旧容器多持一个调用周期——写回后即释放（转换了原数据不留）。
    messages[:] = _validate_tool_call_pairing(
        messages,
        provider,
        name,
        pipeline_id=pipeline_id,
    )

    if provider != "minimax":
        return messages

    converted, converted_count = _minimax_phase_standardize(messages, name)
    messages[:] = converted
    result, relocated_count = _minimax_phase_relocate(messages)
    messages[:] = result
    _minimax_final_system_guard(messages, name)

    if converted_count:
        logger.info(
            "[%s] MiniMax: 将 %d 条非首位 system 消息转换为 user",
            name,
            converted_count,
        )
    if relocated_count:
        logger.info(
            "[%s] MiniMax: 重定位 %d 条 assistant(tool_calls) 与 tool 之间的非法消息",
            name,
            relocated_count,
        )
    return messages


def _minimax_phase_standardize(
    messages: list[dict[str, Any]],
    name: str,
) -> tuple[list[dict[str, Any]], int]:
    """MiniMax Phase 1 标准转换（非首位 system→user、name 剥离、tool 内容清理）。

    MiniMax 要求所有 user 消息的 name 字段一致，因此统一不设置 name；
    tool 内容去掉 NUL 并截断到 8000；assistant 的 tool_call arguments 必须
    是合法 JSON 字符串，非法则尝试修复、失败重置 "{}"（MiniMax 返回格式不稳定）。

    Returns:
        (转换后的消息列表, 被转换的非首位 system 条数)。
    """
    converted_count = 0
    converted: list[dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        if msg.get("role") == "system" and idx > 0:
            converted_count += 1
            new_msg = dict(msg)
            new_msg["role"] = "user"
            new_msg.pop("name", None)
            converted.append(new_msg)
        elif msg.get("role") == "user" and msg.get("name"):
            new_msg = dict(msg)
            new_msg.pop("name", None)
            converted.append(new_msg)
        elif msg.get("role") == "tool":
            new_msg = dict(msg)
            content = new_msg.get("content", "")
            if isinstance(content, str):
                content = content.replace("\x00", "")
                if len(content) > 8000:
                    content = content[:8000] + "\n...[truncated]"
                new_msg["content"] = content
            converted.append(new_msg)
        else:
            converted.append(msg)

        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                if not isinstance(fn, dict):
                    continue
                args_val = fn.get("arguments", "")
                # arguments 必须是合法 JSON 字符串，否则 Minimax 会拒绝请求
                if not isinstance(args_val, str) or not args_val:
                    if args_val != "":
                        logger.warning(
                            "[%s] MiniMax: assistant MSG-%d tool_call[%s] arguments 类型异常 (%s)，重置为 {{}}",
                            name,
                            idx,
                            fn.get("name", "?"),
                            type(args_val).__name__,
                        )
                    fn["arguments"] = "{}"
                else:
                    try:
                        json.loads(args_val)
                    except (json.JSONDecodeError, TypeError):
                        # 尝试修复 JSON 格式问题（MiniMax 返回格式不稳定）
                        repaired = repair_json_string(args_val)
                        if repaired is not None:
                            logger.info(
                                "[%s] MiniMax: assistant MSG-%d tool_call[%s] arguments JSON 修复成功: %s -> %s",
                                name,
                                idx,
                                fn.get("name", "?"),
                                args_val[:200],
                                repaired[:200],
                            )
                            fn["arguments"] = repaired
                        else:
                            logger.warning(
                                "[%s] MiniMax: assistant MSG-%d tool_call[%s] arguments JSON 修复失败，重置为 {{}}: %s",
                                name,
                                idx,
                                fn.get("name", "?"),
                                args_val[:500],
                            )
                            fn["arguments"] = "{}"
    return converted, converted_count


def _minimax_phase_relocate(
    converted: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """MiniMax Phase 2 重定位 assistant(tool_calls) 与 tool 之间的非法插入消息。

    通过 tool_call_id 匹配正确分组：全量扫描仍可能存在不完整的
    assistant(tool_calls) 未被清掉的极端边界，导致后续 assistant 的
    tool results 被错误分配给前面的不完整 assistant。非法插入者改写为
    user（清除不兼容字段）后归位于对应 tool 组之后。

    Returns:
        (重定位后的消息列表, 被移动的消息条数)。
    """
    relocated_count = 0
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(converted):
        msg = converted[i]
        result.append(msg)

        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # 收集此 assistant 期望的 tool_call_id 集合
            # （过滤条件与元素是两次独立 tc.get 调用，运行时 None 已被 if 滤除）
            expected_ids: set[str] = {
                cast(str, tc.get("id"))
                for tc in msg["tool_calls"]
                if isinstance(tc, dict) and tc.get("id")
            }
            # 收集紧随其后且 tool_call_id 匹配的 tool 消息
            tool_group: list[dict[str, Any]] = []
            intruders: list[dict[str, Any]] = []
            j = i + 1
            while j < len(converted):
                nxt = converted[j]
                if nxt.get("role") == "tool":
                    tc_id = nxt.get("tool_call_id", "")
                    if tc_id and tc_id in expected_ids:
                        # tool_call_id 匹配当前 assistant → 属于当前 tool group
                        tool_group.append(nxt)
                        j += 1
                    elif tool_group:
                        # 已经有匹配的 tool 结果了，不匹配的是另一个 assistant 的
                        # 或者是属于后面 assistant 的 → 停止，不要再偷后面的
                        break
                    else:
                        # 还没有匹配的 tool 结果，tool_call_id 不匹配 → 可能是
                        # 后面 assistant 的 tool 结果被提前消费。停止收集。
                        break
                elif tool_group:
                    # 已经有 tool 消息了，后续非 tool 消息是新的对话轮次，停止
                    break
                else:
                    # assistant(tool_calls) 后第一个消息不是匹配的 tool → 非法插入
                    intruders.append(nxt)
                    j += 1
            if intruders:
                relocated_count += len(intruders)
                result.extend(tool_group)
                for intr in intruders:
                    if intr.get("role") not in ("user", "tool"):
                        moved = dict(intr)
                        moved["role"] = "user"
                        moved.pop("name", None)
                        # 清除不兼容的字段
                        moved.pop("tool_calls", None)
                        moved.pop("tool_call_id", None)
                        result.append(moved)
                    else:
                        result.append(intr)
                i = j
                continue
            if tool_group:
                result.extend(tool_group)
                i = j
                continue
        i += 1

    return result, relocated_count


def _minimax_final_system_guard(result: list[dict[str, Any]], name: str) -> int:
    """终极安全网：确保除首位外没有任何 system 消息到达 MiniMax。

    管道组件（评估闸门提醒注入等）会插入 system 消息，前序阶段的复杂
    重排在极端边界可能遗漏，此阶段做最终线性扫描兜底——这是发送前的
    最后一道角色约束。

    Returns:
        就地修正的消息条数。
    """
    final_fix_count = 0
    for _i, _m in enumerate(result):
        if _i > 0 and _m.get("role") == "system":
            logger.warning(
                "[%s] MiniMax 安全网: 非首位 system→user idx=%d, content=%s",
                name,
                _i,
                str(_m.get("content", ""))[:200],
            )
            _m["role"] = "user"
            _m.pop("name", None)
            final_fix_count += 1
    return final_fix_count


# ── 历史退化内容折叠（2026-11-10）─────────────────────────────────────
# 事故：模型一次退化产出的原文（31K/43K/71K 字符的原地重复）会被当作普通
# assistant 消息留在会话历史里，之后每轮都回放给模型——实测把 2 万字 🍦
# 回灌给同一模型即可让新回复出现自我指涉与 emoji 复读（2026-11-10 对照实验）。
# 出站前把"同一单元连发 ≥20 次且总长 ≥200 字符"的片段折叠为占位：信息无损
# （重复本身无信息量），毒源消除。
#
# 性能保护（2026-11-12 事故根修）：两条复读正则对"局部近似周期"文本的失败
# 尝试成本随周期长度平方增长——实测近重复文本（每行共享长前缀、尾数不同）
# 20-60 µs/字符，1.7M 字符历史即 30s+。且 CPython 的 re 全程持 GIL，下放
# 线程池不解阻塞（实测 2.096s 正则期间事件循环心跳空窗 2.096s；同规模纯
# Python 线程循环仅 0.034s 空窗）。故给复读扫描设三级短路 + 确定性字符预算：
# - 文本 ≤ _DEGENERATE_SCAN_WINDOW_CHARS：全文精确扫描，行为与旧版一致；
# - 长文本：多样性预筛 → 复读必要条件探针（限窗 str.find，微秒级；近似重复
#   不命中即短路）→ 首/中/尾采样窗正则扫描；命中后按同一单元向前/向后延伸
#   到真实边界再整段折叠（"找"有界，"折"仍覆盖整条 run）；
# - 单次 collapse_degenerate_history 跨消息累计扫描预算，超出即停，最新
#   消息优先（毒源就近回放）。
# 语义影响：探针未落在 run 内或 run 落在采样窗外时该片段不被发现（旧版全量
# 扫描会发现），且预算耗尽后的更早消息不做折叠分析。升级触发：出现"回灌未
# 折叠"实证（新回复复读再现且折叠计数为 0）时改为后台/离线折叠，不再占用
# 出站路径预算。
_DEGENERATE_SAMPLE_CHARS = 2000
"""字符多样性预筛的采样窗长度。"""
_DEGENERATE_MAX_UNIQUE = 350
"""采样窗内不同字符数 ≤ 此值才进入复读扫描（中文正文数千字通常 >500 个不同字符）。"""
_DEGENERATE_RUN_PATTERN = re.compile(r"(.{3,64}?)\1{19,}", re.S)
"""同一 3-64 字符单元连发 ≥20 次的最小匹配（短周期：同一句/同一符号）。"""
_DEGENERATE_BLOCK_PATTERN = re.compile(r"(.{100,800}?)\1{3,}", re.S)
"""同一 100-800 字符块连发 ≥4 次的最小匹配（长周期：整块状态梳理重写）。"""
_DEGENERATE_BLOCK_MIN_CHARS = 5000
"""长周期扫描的启用门槛（大消息才值得多花一次回溯扫描）。"""
_DEGENERATE_RUN_MIN_CHARS = 200
"""复读片段的最小总长度（短片段不动，避免误折叠正常强调）。"""
_DEGENERATE_SCAN_WINDOW_CHARS = 1024
"""单个复读扫描窗口的字符上限（性能保护：一次正则调用的最大输入）。"""
_DEGENERATE_CALL_SCAN_BUDGET_CHARS = 8192
"""单次 collapse_degenerate_history 的复读扫描字符总预算（跨消息累计）。"""
_DEGENERATE_RUN_SEED_CHARS = 16
"""短周期探针 seed 长度（须 ≤ 复读单元最小长度 3 以上的任意窗口，16 足够）。"""
_DEGENERATE_RUN_MAX_PERIOD = 64
"""短周期探针的最大重现距离（与 _DEGENERATE_RUN_PATTERN 的周期上界一致）。"""
_DEGENERATE_BLOCK_SEED_CHARS = 100
"""长周期探针 seed 长度（与 _DEGENERATE_BLOCK_PATTERN 的周期下界一致）。"""
_DEGENERATE_BLOCK_MAX_PERIOD = 800
"""长周期探针的最大重现距离（与 _DEGENERATE_BLOCK_PATTERN 的周期上界一致）。"""
_DEGENERATE_EXTEND_BLOCK_UNITS = 8
"""run 向两侧延伸时的整块比对单元数（少做 Python 层循环，仍逐单元精确）。"""


def collapse_degenerate_history(
    messages: list[dict[str, Any]],
    *,
    name: str,
    pipeline_id: str = "",
) -> int:
    """把 assistant 历史里的"合成重复片段"折叠为占位，返回折叠掉的字符数。

    只动 assistant 的 content / reasoning_content：tool 结果里的重复属工具事实
    （日志、清单），不动。占位文案保留可读性，便于模型知道此处被截断。

    复读扫描受确定性字符预算约束（见本节注释）：倒序分析（最新消息优先），
    预算耗尽后更早的消息不再分析。
    """
    collapsed_total = 0
    budget = _DEGENERATE_CALL_SCAN_BUDGET_CHARS
    exhausted = False
    for msg in reversed(messages):
        if exhausted:
            break
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for key in ("content", "reasoning_content"):
            value = msg.get(key)
            if not isinstance(value, str) or len(value) < _DEGENERATE_RUN_MIN_CHARS:
                continue
            collapsed, scanned = _collapse_repeated_runs_with_cost(value, budget)
            if collapsed is None:  # 预算不足：本文本不分析
                exhausted = True
                break
            budget -= scanned
            if collapsed is not value:
                msg[key] = collapsed
                collapsed_total += len(value) - len(collapsed)
    if collapsed_total:
        logger.warning(
            "[%s] 历史退化内容折叠 %d 字符（合成重复片段替换为占位）pipeline=%s",
            name,
            collapsed_total,
            pipeline_id or "-",
        )
    if exhausted:
        logger.debug(
            "[%s] 复读扫描预算耗尽（剩余 %d 字符），更早消息未分析 pipeline=%s",
            name,
            budget,
            pipeline_id or "-",
        )
    return collapsed_total


def _collapse_repeated_runs_with_cost(text: str, budget: int) -> tuple[str | None, int]:
    """折叠单个文本的复读片段，返回 ``(结果, 消耗的扫描字符数)``。

    长文本走三级短路（由廉到贵）：字符多样性预筛 → 复读必要条件探针（seed
    串在周期内重现，精确复读才命中）→ 采样窗正则扫描。命中后按同一单元向
    两侧延伸到真实边界再整段折叠（"找"有界，"折"仍覆盖整条 run）。
    只把喂给正则的字符计入预算；预算不足时返回 ``None``（调用方停止后续分析）。
    """
    if len(text) <= _DEGENERATE_SCAN_WINDOW_CHARS:
        if len(text) > budget:
            return None, 0
        return _collapse_full(text), len(text)
    if not _has_degenerate_sample_hint(text):
        return text, 0
    spans = _sample_spans(len(text))
    span_chars = sum(end - start for start, end in spans)
    if span_chars > budget:
        return None, 0
    edits: list[tuple[int, int]] = []
    scanned = 0
    if _has_repeat_signal(text, _DEGENERATE_RUN_SEED_CHARS, _DEGENERATE_RUN_MAX_PERIOD):
        edits = _collect_runs(text, spans, _DEGENERATE_RUN_PATTERN)
        scanned += span_chars
    if (
        not edits
        and len(text) >= _DEGENERATE_BLOCK_MIN_CHARS
        and scanned + span_chars <= budget
        and _has_repeat_signal(text, _DEGENERATE_BLOCK_SEED_CHARS, _DEGENERATE_BLOCK_MAX_PERIOD)
    ):
        edits = _collect_runs(text, spans, _DEGENERATE_BLOCK_PATTERN)
        scanned += span_chars
    if not edits:
        return text, scanned
    return _apply_runs(text, edits), scanned


def _sample_spans(length: int) -> list[tuple[int, int]]:
    """首/中/尾三个采样窗（合并重叠），覆盖"退化通常延续到消息末尾"的形态。"""
    window = _DEGENERATE_SCAN_WINDOW_CHARS
    starts = sorted({0, max(0, length // 2 - window // 2), max(0, length - window)})
    merged: list[tuple[int, int]] = []
    for start in starts:
        end = min(length, start + window)
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _has_repeat_signal(text: str, seed: int, max_period: int) -> bool:
    """复读必要条件探针：某探针位点的 seed 串在 max_period 内精确重现。

    周期 ≤ max_period 的复读 run 内部，任意 seed 长度窗口都会以该周期重现，
    故命中是 run 存在的必要条件。探针只做限窗 ``str.find``（C 级，≤ 周期
    长度搜索），成本每文本几微秒，用来短路平方级正则回溯——近似重复（共享
    长前缀、尾数不同）不命中，正是正则最慢的那类文本。
    """
    length = len(text)
    for pos in sorted({0, length // 4, length // 2, length * 3 // 4, length - seed}):
        seed_text = text[pos : pos + seed]
        if text.find(seed_text, pos + 1, min(length, pos + max_period + seed)) != -1:
            return True
    return False


def _has_degenerate_sample_hint(text: str) -> bool:
    """字符多样性预筛：采样窗内不同字符数低才值得进复读扫描（廉价短路）。"""
    window = _DEGENERATE_SAMPLE_CHARS
    starts = (0, max(0, len(text) // 2 - window // 2), max(0, len(text) - window))
    return not all(len(set(text[s : s + window])) > _DEGENERATE_MAX_UNIQUE for s in starts)


def _replace(match: re.Match[str]) -> str:
    run = match.group(0)
    if len(run) < _DEGENERATE_RUN_MIN_CHARS:
        return run
    return _placeholder(len(run))


def _placeholder(length: int) -> str:
    return f"[已折叠重复输出 {length} 字符]"


def _collapse_full(text: str) -> str:
    """全文精确扫描（短文本路径，行为与旧版一致；长周期门槛 ≥5000 不在此路径）。"""
    collapsed = _DEGENERATE_RUN_PATTERN.sub(_replace, text)
    return collapsed if collapsed != text else text


def _apply_runs(text: str, runs: list[tuple[int, int]]) -> str:
    """把复读 run 逐段替换为占位（run 已合并、非重叠、升序）。"""
    out: list[str] = []
    cursor = 0
    for start, end in runs:
        out.append(text[cursor:start])
        out.append(_placeholder(end - start))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _collect_runs(
    text: str,
    spans: list[tuple[int, int]],
    pattern: re.Pattern[str],
) -> list[tuple[int, int]]:
    """按采样窗收集复读 run 的全文真实区间（延伸补边界，重叠合并）。"""
    found: list[tuple[int, int]] = []
    for span_start, span_end in spans:
        for match in pattern.finditer(text, span_start, span_end):
            run_start, run_end = match.span()
            unit = match.group(1)
            # 窗口右界截断的 run：继续向右延伸后再判长度门槛
            clipped = run_end == span_end and span_end < len(text)
            if run_end - run_start < _DEGENERATE_RUN_MIN_CHARS and not clipped:
                continue
            run_start = _extend_run_start(text, run_start, unit)
            run_end = _extend_run_end(text, run_end, unit)
            if run_end - run_start >= _DEGENERATE_RUN_MIN_CHARS:
                found.append((run_start, run_end))
    return _merge_ranges(found)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠/相邻区间（跨窗命中的同一条 run 只占一条）。"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _extend_run_end(text: str, pos: int, unit: str) -> int:
    """从 pos 起向后吞掉完整的 unit 重复（整块比对，末段逐单元）。"""
    size = len(unit)
    block = unit * _DEGENERATE_EXTEND_BLOCK_UNITS
    while pos < len(text):
        chunk = text[pos : pos + len(block)]
        if len(chunk) == len(block) and chunk == block:
            pos += len(block)
            continue
        while pos < len(text) and text[pos : pos + size] == unit:
            pos += size
        return pos
    return pos


def _extend_run_start(text: str, pos: int, unit: str) -> int:
    """从 pos 起向前吞掉完整的 unit 重复（回到 run 的真实起点）。"""
    size = len(unit)
    block = unit * _DEGENERATE_EXTEND_BLOCK_UNITS
    while pos > 0:
        begin = max(0, pos - len(block))
        chunk = text[begin:pos]
        if len(chunk) == len(block) and chunk == block:
            pos = begin
            continue
        while pos - size >= 0 and text[pos - size : pos] == unit:
            pos -= size
        return pos
    return pos

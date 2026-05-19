"""消息格式规范化模块 -- 针对 LLM 提供商的消息格式修正。

职责：
- 通用修正：确保 tool_calls 使用 OpenAI API 格式
- MiniMax 专有修正：system 消息位置、tool result 配对、JSON 修复等
- JSON 字符串修复工具

从 llm_core/plugin.py 提取，供 LLMCore 在调用 LLM 前规范化消息格式。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def repair_json_string(s: str) -> str | None:
    """尝试修复常见的 JSON 格式问题，返回修复后的 JSON 字符串。

    处理的常见问题：
    1. 尾部逗号 (trailing comma)
    2. 单引号代替双引号
    3. 未加引号的键名
    4. 截断的 JSON（尝试补全括号）
    5. 多余的换行或空白
    6. JSON 前后有额外字符（如 markdown code block）

    Args:
        s: 待修复的 JSON 字符串

    Returns:
        修复后的 JSON 字符串，无法修复时返回 None
    """
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
                    candidate = s[first_brace:i + 1]
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

    # 尝试 5: 截断修复 — 补全缺失的右括号
    open_curly = s.count("{") - s.count("}")
    open_bracket = s.count("[") - s.count("]")
    if open_curly > 0 or open_bracket > 0:
        candidate = s + "]" * max(0, open_bracket) + "}" * max(0, open_curly)
        try:
            json.loads(candidate)
            return candidate
        except (json.JSONDecodeError, TypeError):
            # 截断可能导致 value 不完整，尝试去掉最后一个不完整的 key:value
            # 例如 {"key1": "value1", "key2": "val → {"key1": "value1"}
            last_comma = candidate.rfind(",")
            if last_comma > 0:
                candidate2 = candidate[:last_comma]
                open_c2 = candidate2.count("{") - candidate2.count("}")
                open_b2 = candidate2.count("[") - candidate2.count("]")
                candidate2 += "]" * max(0, open_b2) + "}" * max(0, open_c2)
                try:
                    json.loads(candidate2)
                    return candidate2
                except (json.JSONDecodeError, TypeError):
                    pass

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


def _normalize_tool_calls_in_messages(messages: list[dict[str, Any]]) -> None:
    """确保 assistant 消息中的 tool_calls 使用 OpenAI API 格式。

    执行记录存储的 tool_calls_json 是内部 raw 格式：
        {"id": "...", "name": "...", "arguments": "..."}
    OpenAI API 要求的格式：
        {"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}

    缺少 type 字段会导致智谱AI等 API 报"工具类型不能为空"。
    此方法原地修正 messages 中所有 tool_calls 的格式。
    """
    for msg in messages:
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        raw_tcs = msg["tool_calls"]
        if not isinstance(raw_tcs, list):
            continue
        needs_fix = False
        for tc in raw_tcs:
            if not isinstance(tc, dict):
                continue
            if tc.get("type") != "function" or not isinstance(tc.get("function"), dict):
                needs_fix = True
                break
        if not needs_fix:
            continue
        normalized = []
        for tc in raw_tcs:
            if not isinstance(tc, dict):
                continue
            if tc.get("type") == "function" and isinstance(tc.get("function"), dict):
                normalized.append(tc)
                continue
            normalized.append({
                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": tc.get("args", tc.get("arguments", "{}")),
                },
            })
        msg["tool_calls"] = normalized


def normalize_messages_for_provider(
    messages: list[dict[str, Any]],
    *,
    provider: str,
    name: str,
) -> list[dict[str, Any]]:
    """针对特定 LLM 提供商的消息格式修正。

    通用修正（所有 provider）：
    1. assistant 消息中的 tool_calls 从内部 raw 格式转为 OpenAI API 格式
       （执行记录恢复的消息可能使用内部格式，缺少 type 字段，
       导致智谱AI等 API 报"工具类型不能为空"）

    MiniMax 专有修正：
    1. 非首位 system 消息转为 user 角色（MiniMax 仅允许首位为 system）
    2. assistant(tool_calls) 后只能紧跟 tool 消息，中间插入的非 tool
       消息（如 TaskReminder 注入的 system/user）需移到 tool 消息组之后

    Args:
        messages: 原始消息列表
        provider: LLM 提供商标识（如 openai、minimax）
        name: 插件名称，用于日志

    Returns:
        修正后的消息列表
    """
    # 通用修正：确保 tool_calls 是 OpenAI API 格式
    _normalize_tool_calls_in_messages(messages)

    if provider != "minimax":
        return messages

    # MiniMax 专有修正

    # Phase 0: 清理真正孤立的 tool result 消息（安全网）
    # 当对话历史从执行记录恢复时，assistant 消息的 tool_calls 可能丢失，
    # 导致 tool result 消息前面没有对应的 assistant(tool_calls) 消息。
    # MiniMax 会拒绝这种请求。
    #
    # 改进：不再按 tool_call_id 精确匹配来清理（这会误杀 MiniMax M2.7 等
    # 模型返回的 ID 不一致但顺序正确的 tool result），改为只移除那些前面
    # 没有任何 assistant(tool_calls) 消息的 tool result。ID 不匹配的问题
    # 由 Phase 3 的位置匹配逻辑处理。
    cleaned_phase0: list[dict[str, Any]] = []
    orphan_count = 0
    has_preceding_tool_calls = False
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            has_preceding_tool_calls = True
            cleaned_phase0.append(msg)
        elif msg.get("role") == "tool":
            if not has_preceding_tool_calls:
                orphan_count += 1
            else:
                cleaned_phase0.append(msg)
        else:
            # 非 tool 消息（user/system）重置标记
            if msg.get("role") != "tool":
                has_preceding_tool_calls = False
            cleaned_phase0.append(msg)
    if orphan_count:
        logger.warning(
            "[%s] MiniMax Phase 0: removed %d truly orphaned tool results "
            "(no preceding assistant with tool_calls)",
            name, orphan_count,
        )
        messages = cleaned_phase0

    converted_count = 0
    relocated_count = 0

    # Phase 1: 标准转换（非首位 system→user, tool 内容清理）
    # MiniMax 要求所有 user 消息的 name 字段一致，因此统一不设置 name
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
                            name, idx, fn.get("name", "?"), type(args_val).__name__,
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
                                name, idx, fn.get("name", "?"),
                                args_val[:200], repaired[:200],
                            )
                            fn["arguments"] = repaired
                        else:
                            logger.warning(
                                "[%s] MiniMax: assistant MSG-%d tool_call[%s] arguments JSON 修复失败，重置为 {{}}: %s",
                                name, idx, fn.get("name", "?"), args_val[:500],
                            )
                            fn["arguments"] = "{}"

    # Phase 2: 重定位 assistant(tool_calls) 和 tool 之间的非法消息
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(converted):
        msg = converted[i]
        result.append(msg)

        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # 收集紧随其后的 tool 消息
            tool_group: list[dict[str, Any]] = []
            intruders: list[dict[str, Any]] = []
            j = i + 1
            while j < len(converted):
                if converted[j].get("role") == "tool":
                    tool_group.append(converted[j])
                    j += 1
                elif tool_group:
                    # 已经有 tool 消息了，后续非 tool 消息是新的对话轮次，停止
                    break
                else:
                    # assistant(tool_calls) 后第一个消息不是 tool → 非法插入
                    intruders.append(converted[j])
                    j += 1
            if intruders:
                relocated_count += len(intruders)
                result.extend(tool_group)
                # 将非法消息转为 user 角色放在 tool 组之后
                for intr in intruders:
                    if intr.get("role") not in ("user", "tool"):
                        moved = dict(intr)
                        moved["role"] = "user"
                        moved["name"] = intr.get("role", "system")
                        result.append(moved)
                    else:
                        result.append(intr)
                i = j
                continue
            elif tool_group:
                result.extend(tool_group)
                i = j
                continue
        i += 1

    if converted_count:
        logger.info(
            "[%s] MiniMax: 将 %d 条非首位 system 消息转换为 user",
            name, converted_count,
        )
    if relocated_count:
        logger.info(
            "[%s] MiniMax: 重定位 %d 条 assistant(tool_calls) 与 tool 之间的非法消息",
            name, relocated_count,
        )

    # Phase 3: 最终验证 — 确保每个 tool result 紧跟匹配的 assistant(tool_calls)
    # Phase 0 只检查 tool_call_id 是否存在，不检查位置；Phase 2 只处理
    # assistant(tool_calls) 与 tool 之间的插入消息。这里兜底处理：
    # tool result 出现在非 assistant(tool_calls) 之后的情况（如上下文截断、
    # 执行记录恢复丢失 assistant 消息等）。
    #
    # 改进：当 tool_call_id 精确匹配失败时，按位置顺序尝试匹配。
    # MiniMax M2.7 等模型的 tool_call_id 格式不稳定，可能返回与期望不同的 ID，
    # 但 tool result 的顺序通常与 tool_calls 一致。
    validated: list[dict[str, Any]] = []
    expecting_tool_ids: set[str] = set()
    # 保留有序列表用于位置匹配 fallback
    expecting_tool_ids_ordered: list[str] = []
    dropped_count = 0
    positional_match_count = 0
    for msg in result:
        if msg.get("role") == "assistant":
            if msg.get("tool_calls"):
                expecting_tool_ids = {
                    tc.get("id")
                    for tc in msg["tool_calls"]
                    if tc.get("id")
                }
                expecting_tool_ids_ordered = [
                    tc.get("id")
                    for tc in msg["tool_calls"]
                    if tc.get("id")
                ]
            else:
                expecting_tool_ids = set()
                expecting_tool_ids_ordered = []
            validated.append(msg)
        elif msg.get("role") == "tool":
            if expecting_tool_ids:
                tc_id = msg.get("tool_call_id")
                if tc_id in expecting_tool_ids:
                    expecting_tool_ids.discard(tc_id)
                    # 同步更新有序列表
                    if tc_id in expecting_tool_ids_ordered:
                        expecting_tool_ids_ordered.remove(tc_id)
                    validated.append(msg)
                else:
                    # 精确匹配失败，尝试位置匹配
                    # 如果 tool result 的数量不超过 expecting 数量，
                    # 且此 tool result 的 tool_call_id 也不在任何其他
                    # assistant 消息的 tool_calls 中，按位置分配。
                    if expecting_tool_ids_ordered:
                        matched_id = expecting_tool_ids_ordered.pop(0)
                        expecting_tool_ids.discard(matched_id)
                        positional_match_count += 1
                        logger.info(
                            "[%s] MiniMax Phase 3: positional match — "
                            "tool_call_id %s → %s (expected one of %s)",
                            name, tc_id, matched_id,
                            list(expecting_tool_ids) if expecting_tool_ids else "none left",
                        )
                        # 重写 tool 消息的 tool_call_id 为正确的 ID
                        patched_msg = dict(msg)
                        patched_msg["tool_call_id"] = matched_id
                        validated.append(patched_msg)
                    else:
                        dropped_count += 1
                        logger.warning(
                            "[%s] MiniMax Phase 3: dropping tool result with "
                            "unexpected tool_call_id=%s (expected %s)",
                            name, tc_id, expecting_tool_ids,
                        )
            else:
                dropped_count += 1
                logger.warning(
                    "[%s] MiniMax Phase 3: dropping orphaned tool result "
                    "(tool_call_id=%s, no preceding assistant with tool_calls)",
                    name, msg.get("tool_call_id", "?"),
                )
        else:
            expecting_tool_ids = set()
            expecting_tool_ids_ordered = []
            validated.append(msg)
    if dropped_count:
        logger.warning(
            "[%s] MiniMax Phase 3: dropped %d invalid tool results",
            name, dropped_count,
        )
    if positional_match_count:
        logger.info(
            "[%s] MiniMax Phase 3: positionally matched %d tool results "
            "(tool_call_id mismatch but order preserved)",
            name, positional_match_count,
        )
    result = validated

    # Phase 4: 补全缺失的 tool result — 确保每个 assistant(tool_calls)
    # 后面紧跟完整的 tool 结果。Minimax 要求每个 tool_call 都必须有对应的
    # tool result，否则报 "tool call result does not follow tool call"。
    final: list[dict[str, Any]] = []
    patch_count = 0
    i = 0
    while i < len(result):
        msg = result[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            final.append(msg)
            required_ids = {
                tc.get("id")
                for tc in msg["tool_calls"]
                if tc.get("id")
            }
            # 收集紧跟的 tool 结果
            j = i + 1
            while j < len(result) and result[j].get("role") == "tool":
                tc_id = result[j].get("tool_call_id")
                required_ids.discard(tc_id)
                final.append(result[j])
                j += 1
            # 对缺失的 tool_call_id 补充 dummy tool result
            for missing_id in required_ids:
                patch_count += 1
                logger.warning(
                    "[%s] MiniMax Phase 4: 补全缺失 tool result tool_call_id=%s",
                    name, missing_id,
                )
                final.append({
                    "role": "tool",
                    "tool_call_id": missing_id,
                    "content": "Tool execution result unavailable.",
                })
            i = j
        else:
            final.append(msg)
            i += 1
    if patch_count:
        logger.warning(
            "[%s] MiniMax Phase 4: patched %d missing tool results",
            name, patch_count,
        )
    result = final

    # Phase 5: 终极安全网 — 确保所有非首位 system 消息都已转换
    # 根因：StreamRepetitionGuard、ThinkingTruncationGuard 等管道组件
    # 会注入 system 消息，Phase 1-4 的复杂重定位在极端边界可能遗漏。
    # 此阶段做最终扫描，确保 MiniMax 永远不会收到非法 system 消息。
    final_fix_count = 0
    for _i, _m in enumerate(result):
        if _i > 0 and _m.get("role") == "system":
            logger.warning(
                "[%s] MiniMax Phase 5 安全网: 非首位 system→user "
                "idx=%d, content=%s",
                name, _i,
                str(_m.get("content", ""))[:200],
            )
            _m["role"] = "user"
            _m.pop("name", None)
            final_fix_count += 1
    if final_fix_count:
        logger.warning(
            "[%s] MiniMax Phase 5 安全网修复了 %d 条遗漏的 system 消息",
            name, final_fix_count,
        )

    return result

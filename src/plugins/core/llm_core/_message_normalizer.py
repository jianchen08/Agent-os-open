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


def _validate_tool_call_pairing(
    messages: list[dict[str, Any]],
    provider: str,
    name: str,
) -> list[dict[str, Any]]:
    """验证 tool_calls 和 tool result 的配对完整性。

    DeepSeek 和 MiniMax 严格要求每条 assistant(tool_calls) 后面必须跟齐
    所有 tool_call_id 对应的 tool 消息。消息历史在压缩/截断/执行记录恢复
    等场景下可能产生不配对的消息，此函数负责清理和补全。

    Phase A: 移除孤立的 tool result（前面没有 assistant(tool_calls) 的）
    Phase B: 补全缺失的 tool result（assistant(tool_calls) 后面缺的）

    Args:
        messages: 消息列表
        provider: 提供商标识
        name: 插件名称

    Returns:
        修正后的消息列表
    """
    # Phase A: 移除孤立的 tool result
    validated: list[dict[str, Any]] = []
    expecting_tool_ids: set[str] = set()
    expecting_tool_ids_ordered: list[str] = []
    dropped_count = 0
    positional_match_count = 0
    for msg in messages:
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
                    if tc_id in expecting_tool_ids_ordered:
                        expecting_tool_ids_ordered.remove(tc_id)
                    validated.append(msg)
                elif expecting_tool_ids_ordered:
                    matched_id = expecting_tool_ids_ordered.pop(0)
                    expecting_tool_ids.discard(matched_id)
                    positional_match_count += 1
                    logger.info(
                        "[%s] %s tool_call pairing: positional match "
                        "tool_call_id %s → %s",
                        name, provider, tc_id, matched_id,
                    )
                    patched_msg = dict(msg)
                    patched_msg["tool_call_id"] = matched_id
                    validated.append(patched_msg)
                else:
                    dropped_count += 1
                    logger.warning(
                        "[%s] %s tool_call pairing: dropping tool result "
                        "with unexpected tool_call_id=%s (expected %s)",
                        name, provider, tc_id, expecting_tool_ids,
                    )
            else:
                dropped_count += 1
                logger.warning(
                    "[%s] %s tool_call pairing: dropping orphaned tool "
                    "result (tool_call_id=%s, no preceding assistant)",
                    name, provider, msg.get("tool_call_id", "?"),
                )
        else:
            expecting_tool_ids = set()
            expecting_tool_ids_ordered = []
            validated.append(msg)
    if dropped_count:
        logger.warning(
            "[%s] %s tool_call pairing: dropped %d invalid tool results",
            name, provider, dropped_count,
        )
    if positional_match_count:
        logger.info(
            "[%s] %s tool_call pairing: positionally matched %d",
            name, provider, positional_match_count,
        )

    # Phase B: 补全缺失的 tool result
    final: list[dict[str, Any]] = []
    patch_count = 0
    i = 0
    while i < len(validated):
        msg = validated[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            final.append(msg)
            required_ids = {
                tc.get("id")
                for tc in msg["tool_calls"]
                if tc.get("id")
            }
            j = i + 1
            while j < len(validated) and validated[j].get("role") == "tool":
                tc_id = validated[j].get("tool_call_id")
                required_ids.discard(tc_id)
                final.append(validated[j])
                j += 1
            for missing_id in required_ids:
                patch_count += 1
                logger.warning(
                    "[%s] %s tool_call pairing: patching missing "
                    "tool result tool_call_id=%s",
                    name, provider, missing_id,
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
            "[%s] %s tool_call pairing: patched %d missing tool results",
            name, provider, patch_count,
        )

    return final


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

    # ── 统一 tool_calls/tool 配对校验（所有模型）──
    # 确保每条 assistant(tool_calls) 后面跟齐所有 tool_call_id 对应的 tool 消息。
    # 消息历史在压缩/截断/执行记录恢复等场景下可能产生不配对消息，
    # 不同模型对此的容忍度不同，统一修复避免换模型时踩坑。
    messages = _validate_tool_call_pairing(messages, provider, name)

    if provider != "minimax":
        return messages

    # MiniMax 专有修正（tool_calls/tool 配对已由 _validate_tool_call_pairing 统一处理）

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

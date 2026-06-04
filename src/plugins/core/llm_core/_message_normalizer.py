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

# 增量扫描缓存：记录上次验证完成时各 provider 的消息数量
# 使用 (provider, name, pipeline_id) 作为 key，避免不同管道共享缓存
_pairing_validated_len: dict[str, int] = {}


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


def _is_valid_tool_call_id(tc_id: str | None) -> bool:
    """检查 tool_call_id 是否符合系统标准格式 call_<hex>。

    系统内统一使用 call_<24位hex> 格式（如 call_b3982bf711c648b297524fe6）。
    部分模型（如 MiniMax）可能返回 call_function_<base62>_<n> 等非标准格式，
    MiniMax API 在回传时会拒绝这些非标准 id。

    Args:
        tc_id: 待检查的 tool_call_id

    Returns:
        是否符合标准格式
    """
    if not tc_id or not isinstance(tc_id, str):
        return False
    # 标准格式: call_ + 仅含十六进制字符（至少1位）
    # 拒绝 call_function_xxx_1 等含非hex字符或下划线的格式
    if not tc_id.startswith("call_") or len(tc_id) < 6:
        return False
    hex_part = tc_id[5:]  # "call_" 之后的部分
    return bool(re.fullmatch(r"[0-9a-f]+", hex_part))


def _normalize_tool_calls_in_messages(messages: list[dict[str, Any]]) -> None:
    """确保 assistant 消息中的 tool_calls 使用统一的内部格式。

    执行两项修正：
    1. 结构格式：确保 tool_calls 使用 OpenAI API 格式
        内部 raw: {"id": "...", "name": "...", "arguments": "..."}
        标准格式: {"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}
    2. ID 格式：确保所有 tool_call_id 符合系统标准 call_<hex> 格式。
        部分模型返回非标准 id（如 call_function_xxx_1），统一替换为标准格式，
        同时同步修正对应 tool 消息的 tool_call_id 以保持配对一致。

    缺少 type 字段会导致智谱AI等 API 报"工具类型不能为空"。
    非标准 tool_call_id 会导致 MiniMax API 报 "invalid params, tool call id is invalid"。
    """
    # id_remap: 记录非标准 id -> 新标准 id 的映射，用于同步修正 tool 消息
    id_remap: dict[str, str] = {}

    for msg_idx, msg in enumerate(messages):
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
                normalized.append({
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": tc.get("args", tc.get("arguments", "{}")),
                    },
                })
            msg["tool_calls"] = normalized
            raw_tcs = normalized

        # ── 修正 2: tool_call_id 格式统一 ──
        for tc in raw_tcs:
            if not isinstance(tc, dict):
                continue
            old_id = tc.get("id")
            if old_id and not _is_valid_tool_call_id(old_id):
                new_id = f"call_{uuid.uuid4().hex[:24]}"
                tc["id"] = new_id
                id_remap[old_id] = new_id
                logger.info(
                    "tool_call_id 格式修正: %s → %s", old_id, new_id,
                )

    # 同步修正 tool 消息中对应的 tool_call_id，保持 assistant↔tool 配对一致
    if id_remap:
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            tc_id = msg.get("tool_call_id")
            if tc_id and tc_id in id_remap:
                msg["tool_call_id"] = id_remap[tc_id]


def _validate_tool_call_pairing(
    messages: list[dict[str, Any]],
    provider: str,
    name: str,
) -> list[dict[str, Any]]:
    """增量验证 tool_calls 和 tool result 的配对完整性。

    DeepSeek 和 MiniMax 严格要求每条 assistant(tool_calls) 后面必须跟齐
    所有 tool_call_id 对应的 tool 消息。消息历史在压缩/截断/执行记录恢复
    等场景下可能产生不配对的消息，此函数负责清理和补全。

    采用增量扫描：通过模块级缓存 _pairing_validated_len 记录上次验证完成时
    的消息数量，下次只扫描新增部分，避免每次对整个消息列表做完整遍历。

    Phase A: 移除孤立的 tool result（前面没有 assistant(tool_calls) 的）
    Phase B: 清理不完整的 assistant(tool_calls)（后面缺少 tool result 的）

    Args:
        messages: 消息列表
        provider: 提供商标识
        name: 插件名称

    Returns:
        修正后的消息列表
    """
    cache_key = f"{provider}:{name}"
    cached_len = _pairing_validated_len.get(cache_key, 0)
    msg_count = len(messages)

    # 消息被截断/重建（数量比缓存少），重置缓存做全量扫描
    if msg_count < cached_len:
        cached_len = 0

    # 没有新增消息，直接返回
    if cached_len > 0 and cached_len == msg_count:
        return messages

    scan_start = cached_len

    # 确定安全起点：向前回溯到最近一条 assistant(tool_calls)
    # 确保新增的 tool result 能匹配到之前的 tool_calls
    safety_start = 0
    for idx in range(scan_start - 1, -1, -1):
        if messages[idx].get("role") == "assistant" and messages[idx].get("tool_calls"):
            safety_start = idx
            break

    if scan_start > 0:
        logger.debug(
            "[%s] %s tool_call pairing: incremental scan "
            "safety_start=%d, scan_start=%d, total=%d",
            name, provider, safety_start, scan_start, msg_count,
        )

    # ── Phase A: 移除孤立的 tool result ──
    # 从 safety_start 到 scan_start 重放消息，仅追踪 expecting 状态
    expecting_tool_ids: set[str] = set()
    expecting_tool_ids_ordered: list[str] = []
    for msg in messages[safety_start:scan_start]:
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
        elif msg.get("role") == "tool":
            if expecting_tool_ids:
                tc_id = msg.get("tool_call_id")
                if tc_id in expecting_tool_ids:
                    expecting_tool_ids.discard(tc_id)
                    if tc_id in expecting_tool_ids_ordered:
                        expecting_tool_ids_ordered.remove(tc_id)
                elif expecting_tool_ids_ordered:
                    matched_id = expecting_tool_ids_ordered.pop(0)
                    expecting_tool_ids.discard(matched_id)
        else:
            expecting_tool_ids = set()
            expecting_tool_ids_ordered = []

    # scan_start 之前的消息直接加入 validated（已验证，不做检查）
    validated: list[dict[str, Any]] = list(messages[:scan_start])
    dropped_count = 0
    positional_match_count = 0

    # 对新增消息（scan_start 之后）执行 Phase A 检查
    for msg in messages[scan_start:]:
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

    # ── Phase B: 清理不完整的 assistant(tool_calls) 消息 ──
    # 修复：当 safety_start < scan_start 时，说明 validated 区域内有 assistant(tool_calls)
    # 可能在之前的扫描中被误判为完整（或从未被 Phase B 检查过）。
    # 必须从 safety_start 开始检查，而非 scan_start，确保所有未完整配对的
    # assistant(tool_calls) 都能被清理。
    phase_b_start = safety_start if safety_start < scan_start else scan_start
    final: list[dict[str, Any]] = list(validated[:phase_b_start])
    removed_count = 0
    i = phase_b_start
    while i < len(validated):
        msg = validated[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            required_ids = {
                tc.get("id")
                for tc in msg["tool_calls"]
                if tc.get("id")
            }
            j = i + 1
            while j < len(validated) and validated[j].get("role") == "tool":
                tc_id = validated[j].get("tool_call_id")
                required_ids.discard(tc_id)
                j += 1
            if required_ids:
                # 有不完整的 tool_call → 删除整条 assistant 消息及已匹配的 tool 结果
                removed_count += 1
                logger.warning(
                    "[%s] %s tool_call pairing: removing incomplete assistant message "
                    "(missing tool results: %s)",
                    name, provider, required_ids,
                )
                i = j  # 跳过后续已匹配的 tool 结果
            else:
                # 完整配对 → 保留
                final.append(msg)
                while i + 1 < j:
                    i += 1
                    final.append(validated[i])
                i = j
        else:
            final.append(msg)
            i += 1
    if removed_count:
        logger.warning(
            "[%s] %s tool_call pairing: removed %d incomplete assistant messages "
            "(tool execution was interrupted, no result available)",
            name, provider, removed_count,
        )

    # 更新缓存：记录本次验证完成时的消息数量
    # 注意：final 可能比 msg_count 更短（Phase B 可能移除了不完整的消息），
    # 所以缓存 final 的实际长度，而不是原始 msg_count
    _pairing_validated_len[cache_key] = len(final)

    return final


def reset_pairing_cache(provider: str = "", name: str = "") -> None:
    """重置 tool_call 配对验证缓存。

    当 LLM API 返回 tool_call 相关错误（如 tool call id invalid）时调用，
    强制下一次 normalize_messages_for_provider 执行全量扫描，而非增量扫描。

    Args:
        provider: 提供商名称，空字符串表示重置所有
        name: 插件名称，空字符串表示重置指定 provider 的所有
    """
    if not provider:
        _pairing_validated_len.clear()
        return
    if not name:
        to_remove = [k for k in _pairing_validated_len if k.startswith(f"{provider}:")]
        for k in to_remove:
            del _pairing_validated_len[k]
        return
    _pairing_validated_len.pop(f"{provider}:{name}", None)


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
    # 修复：验证 tool_call_id 匹配。Phase B 的增量扫描可能遗漏不完整的
    # assistant(tool_calls)，导致后续 assistant 的 tool results 被错误分配给
    # 前面的不完整 assistant。必须通过 tool_call_id 匹配来正确分组。
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(converted):
        msg = converted[i]
        result.append(msg)

        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # 收集此 assistant 期望的 tool_call_id 集合
            expected_ids: set[str] = {
                tc.get("id")
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

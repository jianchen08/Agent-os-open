"""消息格式规范化模块 -- 针对 LLM 提供商的消息格式修正。"""

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


def _is_valid_tool_call_id(tc_id: str | None) -> bool:
    """检查 tool_call_id 是否符合系统标准格式 call_<hex>。"""
    if not tc_id or not isinstance(tc_id, str):
        return False
    # 标准格式: call_ + 仅含十六进制字符（至少1位）
    # 拒绝 call_function_xxx_1 等含非hex字符或下划线的格式
    if not tc_id.startswith("call_") or len(tc_id) < 6:
        return False
    hex_part = tc_id[5:]  # "call_" 之后的部分
    return bool(re.fullmatch(r"[0-9a-f]+", hex_part))


def standardize_tool_calls_in_messages(messages: list[dict[str, Any]]) -> None:
    """标准化 tool_calls 为 OpenAI API 格式（公共入口）。"""
    _normalize_tool_calls_in_messages(messages)


def _normalize_tool_calls_in_messages(messages: list[dict[str, Any]]) -> None:  # noqa: PLR0912
    """确保 assistant 消息中的 tool_calls 使用统一的内部格式。"""
    # id_remap: 记录非标准 id -> 新标准 id 的映射，用于同步修正 tool 消息
    id_remap: dict[str, str] = {}

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
                    "tool_call_id 格式修正: %s → %s",
                    old_id,
                    new_id,
                )

    # 同步修正 tool 消息中对应的 tool_call_id，保持 assistant↔tool 配对一致
    if id_remap:
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            tc_id = msg.get("tool_call_id")
            if tc_id and tc_id in id_remap:
                msg["tool_call_id"] = id_remap[tc_id]


def _validate_tool_call_pairing(  # noqa: PLR0912,PLR0915
    messages: list[dict[str, Any]],
    provider: str,
    name: str,
    *,
    pipeline_id: str = "",
) -> list[dict[str, Any]]:
    """增量验证 tool_calls 和 tool result 的配对完整性。"""
    cache_key = f"{provider}:{name}:{pipeline_id}"
    cached_len = _pairing_validated_len.get(cache_key, 0)
    msg_count = len(messages)

    # MiniMax 对配对极度严格，增量缓存一旦过期就漏检，触发 2013 错误。
    # 每次全量扫描开销极小（线性遍历消息列表），但能根除缓存漂移风险。
    if provider == "minimax":
        cached_len = 0

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
            "[%s] %s tool_call pairing: incremental scan safety_start=%d, scan_start=%d, total=%d",
            name,
            provider,
            safety_start,
            scan_start,
            msg_count,
        )

    # ── Phase A: 移除孤立的 tool result ──
    # 从 safety_start 到 scan_start 重放消息，仅追踪 expecting 状态
    expecting_tool_ids: set[str] = set()
    expecting_tool_ids_ordered: list[str] = []
    for msg in messages[safety_start:scan_start]:
        if msg.get("role") == "assistant":
            if msg.get("tool_calls"):
                expecting_tool_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
                expecting_tool_ids_ordered = [tc.get("id") for tc in msg["tool_calls"] if tc.get("id")]
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
                expecting_tool_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
                expecting_tool_ids_ordered = [tc.get("id") for tc in msg["tool_calls"] if tc.get("id")]
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
                        "[%s] %s tool_call pairing: positional match tool_call_id %s → %s",
                        name,
                        provider,
                        tc_id,
                        matched_id,
                    )
                    patched_msg = dict(msg)
                    patched_msg["tool_call_id"] = matched_id
                    validated.append(patched_msg)
                else:
                    dropped_count += 1
                    logger.warning(
                        "[%s] %s tool_call pairing: dropping tool result with unexpected tool_call_id=%s (expected %s)",
                        name,
                        provider,
                        tc_id,
                        expecting_tool_ids,
                    )
            else:
                dropped_count += 1
                logger.warning(
                    "[%s] %s tool_call pairing: dropping orphaned tool "
                    "result (tool_call_id=%s, no preceding assistant)",
                    name,
                    provider,
                    msg.get("tool_call_id", "?"),
                )
        else:
            expecting_tool_ids = set()
            expecting_tool_ids_ordered = []
            validated.append(msg)
    if dropped_count:
        logger.warning(
            "[%s] %s tool_call pairing: dropped %d invalid tool results",
            name,
            provider,
            dropped_count,
        )
    if positional_match_count:
        logger.info(
            "[%s] %s tool_call pairing: positionally matched %d",
            name,
            provider,
            positional_match_count,
        )

    # ── Phase B: 清理不完整的 assistant(tool_calls) 消息 ──
    # 从 safety_start 开始检查（当 safety_start < scan_start 时，validated 区域内
    # 可能有 assistant(tool_calls) 在之前的扫描中被误判为完整或从未被 Phase B 检查过），
    # 确保所有未完整配对的 assistant(tool_calls) 都能被清理。
    phase_b_start = safety_start if safety_start < scan_start else scan_start
    final: list[dict[str, Any]] = list(validated[:phase_b_start])
    removed_count = 0
    i = phase_b_start
    while i < len(validated):
        msg = validated[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            required_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
            j = i + 1
            while j < len(validated) and validated[j].get("role") == "tool":
                tc_id = validated[j].get("tool_call_id")
                required_ids.discard(tc_id)
                j += 1
            if required_ids:
                # 有不完整的 tool_call → 删除整条 assistant 消息及已匹配的 tool 结果
                removed_count += 1
                logger.warning(
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
                    final.append(validated[i])
                i = j
        else:
            final.append(msg)
            i += 1
    if removed_count:
        logger.warning(
            "[%s] %s tool_call pairing: removed %d incomplete assistant messages "
            "(tool execution was interrupted, no result available)",
            name,
            provider,
            removed_count,
        )

    # 更新缓存：记录本次验证完成时的消息数量
    # 注意：final 可能比 msg_count 更短（Phase B 可能移除了不完整的消息），
    # 所以缓存 final 的实际长度，而不是原始 msg_count
    _pairing_validated_len[cache_key] = len(final)

    return final


def reset_pairing_cache(
    provider: str = "",
    name: str = "",
    *,
    pipeline_id: str = "",
) -> None:
    """重置 tool_call 配对验证缓存。"""
    if not provider:
        _pairing_validated_len.clear()
        return
    if not name:
        prefix = f"{provider}:"
        to_remove = [k for k in _pairing_validated_len if k.startswith(prefix)]
        for k in to_remove:
            del _pairing_validated_len[k]
        return
    if not pipeline_id:
        # pipeline_id 为空：清空该 provider:name 下所有管道（含历史无 pipeline_id 的 key）
        prefix = f"{provider}:{name}:"
        to_remove = [k for k in _pairing_validated_len if k.startswith(prefix)]
        for k in to_remove:
            del _pairing_validated_len[k]
        return
    _pairing_validated_len.pop(f"{provider}:{name}:{pipeline_id}", None)


def normalize_messages_for_provider(  # noqa: PLR0912,PLR0915
    messages: list[dict[str, Any]],
    *,
    provider: str,
    name: str,
    pipeline_id: str = "",
) -> list[dict[str, Any]]:
    """针对特定 LLM 提供商的消息格式修正。"""
    # 通用修正：确保 tool_calls 是 OpenAI API 格式
    _normalize_tool_calls_in_messages(messages)

    # ── 统一 tool_calls/tool 配对校验（所有模型）──
    # 确保每条 assistant(tool_calls) 后面跟齐所有 tool_call_id 对应的 tool 消息。
    # 消息历史在压缩/截断/执行记录恢复等场景下可能产生不配对消息，
    # 不同模型对此的容忍度不同，统一修复避免换模型时踩坑。
    messages = _validate_tool_call_pairing(
        messages,
        provider,
        name,
        pipeline_id=pipeline_id,
    )

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

    # Phase 2: 重定位 assistant(tool_calls) 和 tool 之间的非法消息
    # 通过 tool_call_id 匹配来正确分组：Phase B 的增量扫描可能遗漏不完整的
    # assistant(tool_calls)，导致后续 assistant 的 tool results 被错误分配给
    # 前面的不完整 assistant。
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(converted):
        msg = converted[i]
        result.append(msg)

        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # 收集此 assistant 期望的 tool_call_id 集合
            expected_ids: set[str] = {tc.get("id") for tc in msg["tool_calls"] if isinstance(tc, dict) and tc.get("id")}
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

    # Phase 5: 终极安全网 — 确保所有非首位 system 消息都已转换
    # 根因：StreamRepetitionGuard、ThinkingTruncationGuard 等管道组件
    # 会注入 system 消息，Phase 1-4 的复杂重定位在极端边界可能遗漏。
    # 此阶段做最终扫描，确保 MiniMax 永远不会收到非法 system 消息。
    final_fix_count = 0
    for _i, _m in enumerate(result):
        if _i > 0 and _m.get("role") == "system":
            logger.warning(
                "[%s] MiniMax Phase 5 安全网: 非首位 system→user idx=%d, content=%s",
                name,
                _i,
                str(_m.get("content", ""))[:200],
            )
            _m["role"] = "user"
            _m.pop("name", None)
            final_fix_count += 1
    if final_fix_count:
        logger.warning(
            "[%s] MiniMax Phase 5 安全网修复了 %d 条遗漏的 system 消息",
            name,
            final_fix_count,
        )

    return result

"""系统工具——YAML 校验 + 执行详情读取。

[来源: src/tools/builtin/yaml_validate/tool.py, read_execution_detail/tool.py]
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import yaml

logger = logging.getLogger(__name__)

# ── YAML 校验 ──────────────────────────────────────────

YAML_VALIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "YAML 内容字符串"},
        "file_path": {"type": "string", "description": "YAML 文件路径"},
        "schema_type": {
            "type": "string",
            "enum": ["agent", "workflow", "ui_scene", "generic"],
            "default": "generic",
        },
        "required_fields": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

_CONTENT_MAX_LEN = 500


async def yaml_validate(
    content: str = "",
    file_path: str = "",
    schema_type: str = "generic",
    required_fields: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """验证 YAML 配置文件。"""
    if required_fields is None:
        required_fields = []

    if content:
        yaml_content = content
    elif file_path:
        path = Path(file_path)
        if not path.exists():
            return {"valid": False, "error": f"文件不存在: {file_path}"}
        try:
            yaml_content = path.read_text(encoding="utf-8")
        except Exception as e:
            return {"valid": False, "error": f"读取文件失败: {e}"}
    else:
        return {"valid": False, "error": "必须提供 content 或 file_path"}

    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return {"valid": False, "error": f"YAML 语法错误: {e}"}

    if not isinstance(parsed, dict):
        return {"valid": False, "error": "YAML 内容必须是对象/字典类型"}

    errors: list[str] = []
    warnings: list[str] = []

    for field in required_fields:
        if field not in parsed:
            errors.append(f"缺少必需字段: {field}")

    if schema_type == "ui_scene":
        for field in ("scene_id", "display_name"):
            if field not in parsed:
                errors.append(f"UI 场景缺少必需字段: {field}")
    elif schema_type == "agent":
        if "name" not in parsed:
            errors.append("Agent 缺少必需字段: name")
    elif schema_type == "workflow":
        if "name" not in parsed:
            errors.append("工作流缺少必需字段: name")

    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings}
    return {"valid": True, "errors": [], "warnings": warnings, "parsed": parsed}


# ── 执行详情读取 ──────────────────────────────────────────

READ_EXECUTION_DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_run_id": {"type": "string", "description": "管道运行 ID"},
        "level": {
            "type": "string",
            "enum": ["skeleton", "L1", "L0"],
            "description": "skeleton=骨架, L1=压缩块, L0=原始记录",
        },
        "iteration": {"type": "integer", "description": "目标轮次"},
        "fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "L0层专用字段过滤",
        },
    },
    "required": ["pipeline_run_id", "level"],
}

# service-registry 能力调用句柄：async fn (method, params) -> Any。
# 由 server.py / 插件宿主在加载时注入（指向 service-registry 能力句柄的 call 方法）。
# 未注入时 read_execution_detail 优雅降级返回错误 dict，不崩溃。
# [来源: docs/tasks Step 5b 复盘系统读内核 trace，替代旧的 ExecutionRecordStorage]
_capability_caller: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None


def set_capability_caller(fn: Callable[[str, dict[str, Any]], Awaitable[Any]] | None) -> None:
    """注入 service-registry 能力调用句柄。

    生产环境由 simple/server.py 在插件加载时把 service-registry 能力句柄的
    call 方法注入进来；测试环境传 AsyncMock。传 None 可重置为未注入（降级）。
    """
    global _capability_caller
    _capability_caller = fn


async def read_execution_detail(
    pipeline_run_id: str,
    level: str,
    iteration: int | None = None,
    fields: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """查看管道执行的详细记录（从内核 trace/message 表读取）。

    经 service-registry 能力调用内核 ``messages.list`` / ``traces.list`` 读取
    真实对话消息与插件轨迹。

    Args:
        pipeline_run_id: 管道运行 ID（映射到内核 messages.pipeline_id 主键）。
        level: 抽象层级：
            - ``skeleton``：骨架——每条消息一行（``[seq N] role: preview``），
              附插件 step 轨迹（若 traces 可查）。
            - ``L1``：按对话轮次（turn）压缩——每遇到一条 role=user 开新轮次，
              把后续 assistant/tool 聚合进同一轮。
            - ``L0``：原始记录——返回完整 content_preview + tool_calls_json 等。
        iteration: 旧参数（按 iteration 过滤）。新版内核消息表无 iteration 字段，
            只有 seq_in_branch；为向后兼容保留，传入时映射为「第 iteration 个轮次」
            （1-based），用以框定 L1/L0 的轮次范围。None 表示不按轮次过滤。
        fields: L0 层字段过滤（保留兼容；当前实现返回全字段）。
    """
    if _capability_caller is None:
        return {"error": "capability 未注入，无法查询内核执行记录"}

    if not pipeline_run_id:
        return {"error": "pipeline_run_id 不能为空"}
    if not level:
        return {"error": "level 不能为空"}

    # thread_id: traces 按 thread_id 查(复盘 agent 传入,缺省用 pipeline_run_id)
    thread_id = kwargs.get("thread_id") or pipeline_run_id

    if level == "skeleton":
        # skeleton = 轨迹流程(traces)为主 + messages 轮次骨架为辅
        traces = await _fetch_traces(thread_id)
        messages = await _fetch_messages(pipeline_run_id)
        if isinstance(messages, dict) and "error" in messages:
            messages = []
        return _render_skeleton(pipeline_run_id, traces, cast(list[dict[str, Any]], messages))

    if level == "L1":
        # L1 = 压缩摘要(Hindsight 压缩块);无压缩块时降级用 messages 轮次摘要
        chunks = await _fetch_compression_chunks(pipeline_run_id)
        if chunks:
            return _render_l1_from_chunks(pipeline_run_id, chunks)
        # 降级:无压缩块,用 messages 轮次摘要
        messages = await _fetch_messages(pipeline_run_id)
        if isinstance(messages, dict) and "error" in messages:
            return messages
        turns = _group_turns(cast(list[dict[str, Any]], messages))
        if iteration is not None:
            if iteration < 1 or iteration > len(turns):
                return {"error": f"未找到 iteration={iteration} 的对话轮次（共 {len(turns)} 轮）"}
            turns = [turns[iteration - 1]]
        return _render_l1(pipeline_run_id, turns)

    if level == "L0":
        # L0 = 穿透到原文(messages + tool_calls),按需加载
        messages = await _fetch_messages(pipeline_run_id)
        if isinstance(messages, dict) and "error" in messages:
            return messages
        records = _select_l0_records(cast(list[dict[str, Any]], messages), iteration)
        if isinstance(records, dict) and "error" in records:
            return records
        return _render_l0(pipeline_run_id, cast(list[dict[str, Any]], records))

    return {"error": f"不支持的 level: {level}"}


async def _fetch_messages(pipeline_run_id: str) -> list[dict[str, Any]] | dict[str, Any]:
    """经 service-registry 调用 messages.list，返回内核消息记录列表。

    返回的每条记录字段对齐 kernel/crates/core/src/types.rs MessageRecord：
    message_id / run_id / branch_id / seq_in_branch / role / content_preview /
    tool_calls_json / tool_call_id / reasoning_content / created_at / pipeline_id。
    能力调用失败时返回降级错误 dict（不抛异常）。
    """
    assert _capability_caller is not None  # 由调用方前置校验保证
    try:
        result = await _capability_caller(
            "messages.list",
            {"pipeline_id": pipeline_run_id, "limit": 500},
        )
    except Exception as exc:  # noqa: BLE001 — 内核调用失败统一降级，不崩 read_execution_detail
        logger.warning(
            "[read_execution_detail] messages.list 调用失败 pipeline=%s: %s",
            pipeline_run_id,
            exc,
        )
        return {"error": f"内核 messages.list 调用失败: {exc}"}
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and isinstance(result.get("messages"), list):
        return result["messages"]
    return []


async def _fetch_traces(thread_id: str) -> list[dict[str, Any]]:
    """经 service-registry 调用 traces.list，返回插件步骤轨迹(state 变更 patch)。

    每条轨迹含 plugin_id / patch_type / patch_data(JSON state_updates) /
    seq_in_branch / created_at。这是复盘的「轨迹流程」主线——看每个插件
    这步做了什么(state 怎么变、路由走向、错误),不含对话原文。

    Args:
        thread_id: 会话 ID(traces 按 thread_id 查询)

    Returns:
        轨迹列表；调用失败返回空列表(降级,不崩)。
    """
    assert _capability_caller is not None
    try:
        result = await _capability_caller(
            "traces.list",
            {"thread_id": thread_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[read_execution_detail] traces.list 调用失败 thread=%s: %s", thread_id, exc)
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and isinstance(result.get("traces"), list):
        return result["traces"]
    return []


async def _fetch_compression_chunks(pipeline_id: str) -> list[dict[str, Any]]:
    """经 tool-executor 调 hindsight.recall 读压缩块(L1/L2 摘要)。

    压缩块是压缩器产出的结构化摘要(过程/决策/结果/状态快照),按 pipeline_id
    过滤。这是复盘的「摘要层」——看对话要点而非原文。

    调用失败或无压缩块时返回空列表(降级到 messages 轮次摘要)。

    Args:
        pipeline_id: 管道 ID(压缩块按 pipeline_id 关联)

    Returns:
        压缩块列表(每条含 content/metadata);失败返回 []。
    """
    assert _capability_caller is not None
    try:
        result = await _capability_caller(
            "tool-executor.invoke",
            {
                "tool_name": "hindsight.recall",
                "args": {
                    "bank_id": pipeline_id,
                    "query": "",
                    "memory_type": "chunk",
                    "top_k": 20,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[read_execution_detail] hindsight.recall 压缩块失败: %s", exc)
        return []
    if isinstance(result, dict):
        return result.get("results", [])
    return []


def _render_skeleton(
    pipeline_run_id: str,
    traces: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """渲染 skeleton 层：轨迹流程(traces)为主 + messages 轮次骨架为辅。

    轨迹主线：每个插件步骤一行,展示插件名 + state 变更的关键字段(路由/token/错误)。
    对话骨架：每条消息一行(附在 trace_steps 后),供快速定位轮次。

    设计理由:复盘优先看「流程是否正常」(哪个插件跑了/state 怎么变/路由走向),
    而非直接灌对话原文——具体内容按需通过 L0 穿透。
    """
    # ── 轨迹主线:每个插件步骤 ──
    trace_steps: list[dict[str, Any]] = []
    for tr in traces:
        plugin_id = tr.get("plugin_id", "?")
        seq = tr.get("seq_in_branch")
        patch_data = tr.get("patch_data")
        # 解析 state_updates,提取复盘关心的关键字段
        state_changes: dict[str, Any] = {}
        if isinstance(patch_data, str):
            try:
                state_changes = json.loads(patch_data)
            except (json.JSONDecodeError, ValueError):
                state_changes = {"_raw": patch_data[:100]}
        elif isinstance(patch_data, dict):
            state_changes = patch_data

        # 提取复盘关键字段(路由/token/错误/状态)
        key_fields: dict[str, Any] = {}
        for k in (
            "core_type", "execution_status", "raw_error",
            "router.stop_reason", "router.last_tool_call",
            "llm_usage", "track.total_tokens",
            "stuck_detected", "stuck_reason",
            "task_complete",
        ):
            if k in state_changes:
                key_fields[k] = state_changes[k]

        trace_steps.append({
            "seq": seq,
            "plugin": plugin_id,
            "state_keys": list(state_changes.keys())[:8],
            "key_changes": key_fields if key_fields else None,
        })

    # ── 对话骨架:每条消息一行(轮次定位用)──
    message_lines: list[str] = []
    for msg in messages:
        seq = msg.get("seq_in_branch")
        role = msg.get("role", "?")
        preview = _safe_content_to_str(msg.get("content_preview")).replace("\n", " ").strip()
        preview = preview[:60]
        if preview:
            message_lines.append(f"[seq {seq}] {role}: {preview}")
        else:
            message_lines.append(f"[seq {seq}] {role}")

    return {
        "pipeline_run_id": pipeline_run_id,
        "level": "skeleton",
        "trace_steps": trace_steps,
        "trace_count": len(trace_steps),
        "message_lines": message_lines,
        "message_count": len(messages),
        "hint": "trace_steps=插件流程(状态变更), message_lines=对话骨架(定位轮次), L1=压缩摘要, L0=穿透原文",
    }


def _render_l1_from_chunks(
    pipeline_run_id: str, chunks: list[dict[str, Any]]
) -> dict[str, Any]:
    """从 Hindsight 压缩块渲染 L1 层:展示压缩摘要(L1 过程/L2 三元组/state_snapshot)。

    压缩块是压缩器产出的结构化摘要,按 layer 和 seq 排序。
    每块展示 content(摘要文本) + metadata(layer/keywords/sequence)。
    """
    rendered: list[dict[str, Any]] = []
    for chunk in chunks:
        content = chunk.get("content", "")
        meta = chunk.get("metadata") or {}
        # 尝试解析 content 为 JSON(压缩块存的是结构化 JSON)
        parsed: dict[str, Any] | None = None
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            pass

        item: dict[str, Any] = {
            "content_preview": content[:500] if not parsed else None,
            "layer": meta.get("layer") or _extract_tag(meta.get("tags", []), "layer"),
            "keywords": meta.get("keywords", []),
            "seq_range": _extract_tag(meta.get("tags", []), "seq"),
        }
        if parsed:
            # 结构化压缩块:展示 L1/L2 字段
            if "l1" in parsed:
                item["l1"] = parsed["l1"]
            if "l2" in parsed:
                item["l2"] = parsed["l2"]
            if "state_snapshot" in parsed:
                item["state_snapshot"] = parsed["state_snapshot"]
        rendered.append(item)

    return {
        "pipeline_run_id": pipeline_run_id,
        "level": "L1",
        "source": "compression_chunks",
        "chunk_count": len(rendered),
        "chunks": rendered,
        "hint": "压缩摘要(L1过程/L2三元组/state_snapshot)。需要具体原文用 L0 穿透。",
    }


def _extract_tag(tags: list[Any], prefix: str) -> str:
    """从 tags 列表提取 prefix:xxx 格式的值。"""
    for tag in tags:
        s = str(tag)
        if s.startswith(f"{prefix}:"):
            return s[len(prefix) + 1:]
    return ""


def _group_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """把消息序列按对话轮次分组。

    规则：每条 role=user 消息开启一个新轮次；其后的 assistant/tool 消息归入该轮，
    直到下一条 user 消息。首条非 user 消息（无前置 user）独占一轮。
    """
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] | None = None
    for msg in messages:
        role = msg.get("role")
        if role == "user" or current is None:
            current = []
            turns.append(current)
        current.append(msg)
    return turns


def _render_l1(
    pipeline_run_id: str, turns: list[list[dict[str, Any]]]
) -> dict[str, Any]:
    """渲染 L1 层：按轮次压缩摘要。

    每轮摘要含 turn 序号（1-based）、起止 seq、user 输入预览、assistant 回复预览、
    tool 调用计数。
    """
    rendered = []
    for idx, turn in enumerate(turns, start=1):
        # 过滤条件与元素是两次独立 m.get 调用，类型上仍含 None；运行时 None 已被滤除。
        seqs: list[Any] = [m.get("seq_in_branch") for m in turn if m.get("seq_in_branch") is not None]
        user_msgs = [m for m in turn if m.get("role") == "user"]
        ai_msgs = [m for m in turn if m.get("role") == "assistant"]
        tool_msgs = [m for m in turn if m.get("role") == "tool"]
        rendered.append({
            "turn": idx,
            "seq_range": [min(seqs), max(seqs)] if seqs else [],
            "user_inputs": [
                {"content_preview": _safe_content_to_str(m.get("content_preview"))[:300]}
                for m in user_msgs
            ],
            "ai_actions": [
                {
                    "content_preview": _safe_content_to_str(m.get("content_preview"))[:200],
                    "reasoning_preview": _safe_content_to_str(m.get("reasoning_content"))[:200],
                }
                for m in ai_msgs
            ],
            "tool_calls_count": len(tool_msgs),
        })
    return {
        "pipeline_run_id": pipeline_run_id,
        "level": "L1",
        "turn_count": len(rendered),
        "turns": rendered,
    }


def _select_l0_records(
    messages: list[dict[str, Any]], iteration: int | None
) -> list[dict[str, Any]] | dict[str, Any]:
    """按 iteration（轮次，1-based）筛选 L0 原始记录；None 表示返回全部。"""
    if iteration is None:
        return messages
    turns = _group_turns(messages)
    if iteration < 1 or iteration > len(turns):
        return {"error": f"未找到 iteration={iteration} 的对话轮次（共 {len(turns)} 轮）"}
    return turns[iteration - 1]


def _render_l0(
    pipeline_run_id: str, records: list[dict[str, Any]]
) -> dict[str, Any]:
    """渲染 L0 层：返回完整 content_preview + tool_calls_json 等原始字段。"""
    filtered: list[dict[str, Any]] = []
    for msg in records:
        item: dict[str, Any] = {
            "message_id": msg.get("message_id"),
            "run_id": msg.get("run_id"),
            "branch_id": msg.get("branch_id"),
            "seq_in_branch": msg.get("seq_in_branch"),
            "role": msg.get("role"),
            "content": _truncate_text(
                _safe_content_to_str(msg.get("content_preview")), _CONTENT_MAX_LEN
            ),
            "created_at": msg.get("created_at"),
        }
        tool_calls_json = msg.get("tool_calls_json")
        if tool_calls_json:
            item["tool_calls_json"] = tool_calls_json
        tool_call_id = msg.get("tool_call_id")
        if tool_call_id:
            item["tool_call_id"] = tool_call_id
        reasoning = msg.get("reasoning_content")
        if reasoning:
            item["reasoning_content"] = reasoning
        filtered.append(item)

    return {
        "pipeline_run_id": pipeline_run_id,
        "level": "L0",
        "record_count": len(filtered),
        "records": filtered,
    }


def _truncate_text(text: str | None, max_len: int) -> str | None:
    if not text:
        return text
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"...(truncated, total {len(text)} chars)"


def _safe_content_to_str(content: Any) -> str:
    if not content:
        return ""
    if isinstance(content, str):
        return content
    import json  # noqa: PLC0415

    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)

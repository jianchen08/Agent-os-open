"""系统工具——YAML 校验 + 执行详情读取 + 资源注册。

[来源: src/tools/builtin/yaml_validate/tool.py, read_execution_detail/tool.py, register_resource/tool.py]
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

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

    Step 5b 改造：不再依赖已删除的 ExecutionRecordStorage（YAML），改为经
    service-registry 能力调用内核 ``messages.list`` / ``traces.list`` 读取
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

    messages = await _fetch_messages(pipeline_run_id)
    if isinstance(messages, dict) and "error" in messages:
        return messages

    if level == "skeleton":
        return _render_skeleton(pipeline_run_id, messages)

    if level == "L1":
        turns = _group_turns(messages)
        if iteration is not None:
            # 1-based：iteration=1 → 第 1 轮；越界返回错误
            if iteration < 1 or iteration > len(turns):
                return {"error": f"未找到 iteration={iteration} 的对话轮次（共 {len(turns)} 轮）"}
            turns = [turns[iteration - 1]]
        return _render_l1(pipeline_run_id, turns)

    if level == "L0":
        records = _select_l0_records(messages, iteration)
        if isinstance(records, dict) and "error" in records:
            return records
        return _render_l0(pipeline_run_id, records)

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


def _render_skeleton(
    pipeline_run_id: str, messages: list[dict[str, Any]]
) -> dict[str, Any]:
    """渲染 skeleton 层：每条消息一行骨架。

    行格式 ``[seq N] role: preview``，preview 取 content_preview（截断 80 字符、
    折叠换行）。空消息仅输出 ``[seq N] role``。
    """
    lines: list[str] = []
    for msg in messages:
        seq = msg.get("seq_in_branch")
        role = msg.get("role", "?")
        preview = _safe_content_to_str(msg.get("content_preview")).replace("\n", " ").strip()
        preview = preview[:80]
        if preview:
            lines.append(f"[seq {seq}] {role}: {preview}")
        else:
            lines.append(f"[seq {seq}] {role}")

    return {
        "pipeline_run_id": pipeline_run_id,
        "level": "skeleton",
        "total_records": len(messages),
        "lines": lines,
    }


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
        seqs = [m.get("seq_in_branch") for m in turn if m.get("seq_in_branch") is not None]
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


# ── 资源注册 ──────────────────────────────────────────

REGISTER_RESOURCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resource_type": {
            "type": "string",
            "enum": ["agent", "tool", "template", "pipeline_config"],
            "description": "资源类型",
        },
        "resource_id": {"type": "string", "description": "资源唯一标识"},
        "config": {"type": "object", "description": "资源配置数据"},
        "overwrite": {"type": "boolean", "default": False},
    },
    "required": ["resource_type", "resource_id", "config"],
}

_service_cache: dict[str, Any] = {}


def set_service(service_name: str, service: Any) -> None:
    """设置服务实例到模块级缓存。"""
    _service_cache[service_name] = service


def _get_service(service_name: str) -> Any:
    return _service_cache.get(service_name)


async def register_resource(
    resource_type: str,
    resource_id: str,
    config: dict[str, Any],
    overwrite: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """执行资源注册。"""
    if not resource_type:
        return {"success": False, "error": "必须提供 resource_type"}
    if not resource_id:
        return {"success": False, "error": "必须提供 resource_id"}
    if not config:
        return {"success": False, "error": "必须提供 config"}

    dispatchers = {
        "agent": _register_agent,
        "tool": _register_tool,
        "template": _register_template,
        "pipeline_config": _register_pipeline_config,
    }

    dispatcher = dispatchers.get(resource_type)
    if dispatcher is None:
        return {"success": False, "error": f"不支持的资源类型: {resource_type}"}

    return dispatcher(resource_id, config, overwrite)


def _register_agent(resource_id: str, config: dict[str, Any], overwrite: bool) -> dict[str, Any]:
    try:
        registry = _get_service("agent_registry")
        if registry is None:
            return {"success": False, "error": "AgentRegistry 未注入"}

        if not overwrite and registry.get(resource_id) is not None:
            return {"success": False, "error": f"Agent '{resource_id}' 已存在"}

        registry.register(config)
        return {"success": True, "resource_type": "agent", "resource_id": resource_id}
    except Exception as exc:
        logger.error("Agent 注册失败: %s", exc)
        return {"success": False, "error": f"Agent 注册失败: {exc}"}


def _register_tool(resource_id: str, config: dict[str, Any], overwrite: bool) -> dict[str, Any]:
    try:
        registry = _get_service("tool_registry")
        if registry is None:
            return {"success": False, "error": "ToolRegistry 未注入"}

        if not overwrite and registry.has(resource_id):
            return {"success": False, "error": f"工具 '{resource_id}' 已存在"}

        registry.register(
            name=resource_id,
            func=config.get("func"),
            schema=config.get("schema", {}),
            description=config.get("description", resource_id),
        )
        return {"success": True, "resource_type": "tool", "resource_id": resource_id}
    except Exception as exc:
        logger.error("工具注册失败: %s", exc)
        return {"success": False, "error": f"工具注册失败: {exc}"}


def _register_template(resource_id: str, config: dict[str, Any], overwrite: bool) -> dict[str, Any]:
    try:
        registry = _get_service("template_registry")
        if registry is None:
            return {"success": False, "error": "TemplateRegistry 未注入"}

        if not overwrite and registry.get(resource_id) is not None:
            return {"success": False, "error": f"模板 '{resource_id}' 已存在"}

        registry.register(config)
        return {"success": True, "resource_type": "template", "resource_id": resource_id}
    except Exception as exc:
        logger.error("模板注册失败: %s", exc)
        return {"success": False, "error": f"模板注册失败: {exc}"}


def _register_pipeline_config(
    resource_id: str, config: dict[str, Any], overwrite: bool
) -> dict[str, Any]:
    try:
        registry = _get_service("pipeline_config_store")
        if registry is None:
            return {"success": False, "error": "PipelineConfigStore 未注入"}

        if not overwrite and registry.get(resource_id) is not None:
            return {"success": False, "error": f"管道配置 '{resource_id}' 已存在"}

        registry.register(resource_id, config)
        return {
            "success": True,
            "resource_type": "pipeline_config",
            "resource_id": resource_id,
        }
    except Exception as exc:
        logger.error("管道配置注册失败: %s", exc)
        return {"success": False, "error": f"管道配置注册失败: {exc}"}

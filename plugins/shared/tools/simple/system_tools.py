"""系统工具——YAML 校验 + 执行详情读取 + 资源注册。

[来源: src/tools/builtin/yaml_validate/tool.py, read_execution_detail/tool.py, register_resource/tool.py]
"""

from __future__ import annotations

import logging
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

_storage: Any = None


def set_storage(storage: Any) -> None:
    """注入 ExecutionRecordStorage 实例。"""
    global _storage
    _storage = storage


async def read_execution_detail(
    pipeline_run_id: str,
    level: str,
    iteration: int | None = None,
    fields: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """查看管道执行的详细记录。"""
    if _storage is None:
        return {"error": "ExecutionRecordStorage 未注入，无法查询执行记录"}

    if not pipeline_run_id:
        return {"error": "pipeline_run_id 不能为空"}
    if not level:
        return {"error": "level 不能为空"}

    _storage._ensure_loaded(pipeline_run_id)

    if level == "skeleton":
        records = _storage.list_by_pipeline(pipeline_run_id)[0]
        if not records:
            return {"error": f"未找到管道 {pipeline_run_id} 的执行记录"}

        lines: list[str] = []
        for record in records:
            parts = [f"[iter {record.iteration}]", record.type]
            if record.name:
                parts.append(record.name)
            line_head = " ".join(parts)
            if record.type == "user":
                content_str = _safe_content_to_str(record.content).replace("\n", " ").strip()
                line = f"{line_head}: {content_str}" if content_str else line_head
            else:
                line = line_head
                preview = _safe_content_to_str(record.content)[:50].replace("\n", " ").strip()
                if preview:
                    line += f" {preview}"
                if record.error:
                    line += f" -> error: {record.error[:50]}"
            lines.append(line)

        return {
            "pipeline_run_id": pipeline_run_id,
            "level": "skeleton",
            "total_records": len(records),
            "iterations": sorted({r.iteration for r in records}),
            "lines": lines,
        }

    if level in ("L1", "L0"):
        if iteration is None:
            return {"error": f"{level} 层需要指定 iteration 参数"}

        all_records = _storage.list_by_pipeline(pipeline_run_id)[0]
        target_records = [r for r in all_records if r.iteration == iteration]
        if not target_records:
            return {"error": f"未找到 iteration={iteration} 的执行记录"}

        if level == "L1":
            ai_records = [r for r in target_records if r.type == "ai"]
            tool_records = [r for r in target_records if r.type == "tool"]
            user_records = [r for r in target_records if r.type in ("user", "human")]
            error_records = [r for r in target_records if r.error]

            summary = {
                "iteration": iteration,
                "user_inputs": [
                    {"type": r.type, "content_preview": (r.content or "")[:300]}
                    for r in user_records
                ],
                "ai_actions": [
                    {
                        "type": r.type,
                        "content_preview": (r.content or "")[:200],
                        "thinking_preview": (r.thinking_content or "")[:200],
                    }
                    for r in ai_records
                ],
                "tool_calls_summary": [
                    {"name": r.name, "result_preview": (r.content or "")[:200]}
                    for r in tool_records
                ],
                "errors": [
                    {"name": r.name, "error": (r.error or "")[:200]} for r in error_records
                ],
            }
            return {
                "pipeline_run_id": pipeline_run_id,
                "level": "L1",
                "iteration": iteration,
                "record_count": len(target_records),
                "summary": summary,
            }

        # L0
        if fields is None:
            fields = ["all"]
        if "all" in fields:
            fields = ["all"]

        filtered_records = []
        for record in target_records:
            filtered = {
                "record_id": record.record_id,
                "iteration": record.iteration,
                "sequence": record.sequence,
                "type": record.type,
                "name": record.name,
            }
            if "all" in fields:
                filtered["role"] = record.role
                filtered["content"] = _truncate_text(record.content, _CONTENT_MAX_LEN)
                filtered["thinking_content"] = record.thinking_content
                filtered["error"] = record.error
            else:
                if "thinking" in fields and record.thinking_content:
                    filtered["thinking_content"] = record.thinking_content
                if "content" in fields and record.content:
                    filtered["content"] = _truncate_text(record.content, _CONTENT_MAX_LEN)
                if "error" in fields and record.error:
                    filtered["error"] = record.error
            filtered_records.append(filtered)

        return {
            "pipeline_run_id": pipeline_run_id,
            "level": "L0",
            "iteration": iteration,
            "record_count": len(filtered_records),
            "records": filtered_records,
        }

    return {"error": f"不支持的 level: {level}"}


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

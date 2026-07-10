"""资源注册工具 — 动态注册 Agent/工具/模板到系统。

让 Agent 可以将新创建的资源注册到系统中，实现"创建→评估→注册"闭环。
支持注册类型：agent、tool、template、pipeline_config。

暴露接口：
- register_resource_schema: 工具参数 JSON Schema
- register_resource_func: 工具执行函数
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 工具参数 Schema（OpenAI Function Calling 格式）
register_resource_schema: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resource_type": {
            "type": "string",
            "enum": ["agent", "tool", "template", "pipeline_config"],
            "description": "资源类型",
        },
        "resource_id": {
            "type": "string",
            "description": "资源唯一标识（agent 的 config_id / 工具名 / 模板 ID / 管道 ID）",
        },
        "config": {
            "type": "object",
            "description": "资源配置数据。agent 类型需要 name/level/agent_type 等字段；tool 类型需要 name/schema/func_module/func_name；template 类型需要 template_id 和 raw_content；pipeline_config 类型需要 pipeline_id 和 name",
        },
        "overwrite": {
            "type": "boolean",
            "description": "是否覆盖已存在的同名资源，默认 false",
            "default": False,
        },
    },
    "required": ["resource_type", "resource_id", "config"],
}

REGISTER_RESOURCE_DESCRIPTION = (
    "资源注册工具。将新创建的 Agent、工具、模板或管道配置注册到系统中，"
    "使其可被其他 Agent 发现和使用。注册前应确保资源已通过评估。"
)


def register_resource_func(params: dict[str, Any]) -> dict[str, Any]:
    """执行资源注册。

    根据 resource_type 将配置注册到对应的 Registry：
    - agent → AgentRegistry
    - tool → ToolRegistry
    - template → TemplateRegistry
    - pipeline_config → PipelineConfigStore

    Args:
        params: 工具参数，含 resource_type、resource_id、config 等

    Returns:
        包含 success 和注册结果的字典
    """
    resource_type = params.get("resource_type")
    resource_id = params.get("resource_id")
    config = params.get("config", {})
    overwrite = params.get("overwrite", False)

    if not resource_type:
        return {
            "success": False,
            "error": "必须提供 resource_type",
            "error_code": "MISSING_RESOURCE_TYPE",
        }

    if not resource_id:
        return {
            "success": False,
            "error": "必须提供 resource_id",
            "error_code": "MISSING_RESOURCE_ID",
        }

    if not config:
        return {
            "success": False,
            "error": "必须提供 config",
            "error_code": "MISSING_CONFIG",
        }

    dispatchers = {
        "agent": _register_agent,
        "tool": _register_tool,
        "template": _register_template,
        "pipeline_config": _register_pipeline_config,
    }

    dispatcher = dispatchers.get(resource_type)
    if dispatcher is None:
        return {
            "success": False,
            "error": f"不支持的资源类型: {resource_type}",
            "error_code": "INVALID_RESOURCE_TYPE",
        }

    return dispatcher(resource_id, config, overwrite)


def _register_agent(resource_id: str, config: dict[str, Any], overwrite: bool) -> dict[str, Any]:
    """注册 Agent 配置到 AgentRegistry。

    Args:
        resource_id: Agent 的 config_id
        config: Agent 配置数据
        overwrite: 是否覆盖已存在的配置

    Returns:
        注册结果字典
    """
    try:
        from agents.types import AgentConfig, AgentLevel, AgentType  # noqa: PLC0415

        # 获取或创建 AgentRegistry（通过服务定位器，回退到全局单例）
        registry = _get_service("agent_registry")
        if registry is None:
            from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415

            registry = get_global_agent_registry_sync()

        # 检查是否已存在
        if not overwrite and registry.get(resource_id) is not None:
            return {
                "success": False,
                "error": f"Agent '{resource_id}' 已存在，设置 overwrite=true 可覆盖",
                "error_code": "RESOURCE_EXISTS",
            }

        # 构建 AgentConfig
        level_str = config.get("level", "L1")
        try:
            level = AgentLevel(level_str)
        except ValueError:
            level = AgentLevel.L1_MAIN

        type_str = config.get("agent_type", "specialized")
        try:
            agent_type = AgentType(type_str)
        except ValueError:
            agent_type = AgentType.SPECIALIZED

        agent_config = AgentConfig(
            config_id=resource_id,
            name=config.get("name", resource_id),
            description=config.get("description", ""),
            level=level,
            agent_type=agent_type,
            system_prompt=config.get("system_prompt", ""),
            tool_ids=config.get("tool_ids", []),
            tags=config.get("tags", []),
            category=config.get("category", ""),
        )

        registry.register(agent_config)
        logger.info("[register_resource] Agent 注册成功: %s", resource_id)

        return {
            "success": True,
            "resource_type": "agent",
            "resource_id": resource_id,
            "message": f"Agent '{resource_id}' 注册成功",
        }

    except Exception as exc:
        logger.error("[register_resource] Agent 注册失败: %s", exc)
        return {
            "success": False,
            "error": f"Agent 注册失败: {exc}",
            "error_code": "REGISTRATION_FAILED",
        }


def _register_tool(resource_id: str, config: dict[str, Any], overwrite: bool) -> dict[str, Any]:
    """注册工具到 ToolRegistry。

    工具注册需要提供 func_module 和 func_name，或者直接提供 func 引用。
    对于通过 LLM 创建的工具，通常注册为一个通用的执行器。

    Args:
        resource_id: 工具名称
        config: 工具配置数据
        overwrite: 是否覆盖已存在的工具

    Returns:
        注册结果字典
    """
    try:
        from tools.registry import ToolRegistry  # noqa: PLC0415

        registry = _get_service("tool_registry")
        if registry is None:
            registry = ToolRegistry()

        # 检查是否已存在
        if not overwrite and registry.has(resource_id):
            return {
                "success": False,
                "error": f"工具 '{resource_id}' 已存在，设置 overwrite=true 可覆盖",
                "error_code": "RESOURCE_EXISTS",
            }

        # 构建工具注册参数
        tool_name = resource_id
        description = config.get("description", config.get("name", resource_id))
        schema = config.get("schema", config.get("input_schema", {"type": "object", "properties": {}}))

        # 动态加载工具函数
        func_module = config.get("func_module")
        func_name = config.get("func_name")

        if func_module and func_name:
            # 从模块路径加载
            import importlib  # noqa: PLC0415

            module = importlib.import_module(func_module)
            func = getattr(module, func_name)
        else:
            # 注册一个占位函数，返回配置中的固定响应
            _config = dict(config)

            def placeholder_func(params: dict[str, Any]) -> dict[str, Any]:
                """动态注册工具的占位执行函数。"""
                return {
                    "success": True,
                    "tool": tool_name,
                    "message": _config.get("response_template", f"工具 {tool_name} 已执行"),
                    "params": params,
                }

            func = placeholder_func

        registry.register(
            name=tool_name,
            func=func,
            schema=schema,
            description=description,
        )
        logger.info("[register_resource] 工具注册成功: %s", resource_id)

        return {
            "success": True,
            "resource_type": "tool",
            "resource_id": resource_id,
            "message": f"工具 '{resource_id}' 注册成功",
        }

    except Exception as exc:
        logger.error("[register_resource] 工具注册失败: %s", exc)
        return {
            "success": False,
            "error": f"工具注册失败: {exc}",
            "error_code": "REGISTRATION_FAILED",
        }


def _register_template(resource_id: str, config: dict[str, Any], overwrite: bool) -> dict[str, Any]:
    """注册模板到 TemplateRegistry。

    Args:
        resource_id: 模板 ID
        config: 模板配置数据
        overwrite: 是否覆盖已存在的模板

    Returns:
        注册结果字典
    """
    try:
        from templates.registry import TemplateRegistry  # noqa: PLC0415

        registry = _get_service("template_registry")
        if registry is None:
            registry = TemplateRegistry()

        # 检查是否已存在
        if not overwrite and registry.get(resource_id) is not None:
            return {
                "success": False,
                "error": f"模板 '{resource_id}' 已存在，设置 overwrite=true 可覆盖",
                "error_code": "RESOURCE_EXISTS",
            }

        # 如果提供了 raw_content，尝试用 TemplateLoader 解析
        raw_content = config.get("raw_content", "")
        if raw_content:
            from templates.loader import TemplateLoader  # noqa: PLC0415

            loader = TemplateLoader()
            try:
                spec = loader._parse_template(raw_content, f"{resource_id}_template.md")
                spec.template_id = resource_id
                registry.register(spec)
            except Exception:
                # 解析失败，构建基本 spec
                spec = _build_template_spec(resource_id, config)
                registry.register(spec)
        else:
            spec = _build_template_spec(resource_id, config)
            registry.register(spec)

        logger.info("[register_resource] 模板注册成功: %s", resource_id)

        return {
            "success": True,
            "resource_type": "template",
            "resource_id": resource_id,
            "message": f"模板 '{resource_id}' 注册成功",
        }

    except Exception as exc:
        logger.error("[register_resource] 模板注册失败: %s", exc)
        return {
            "success": False,
            "error": f"模板注册失败: {exc}",
            "error_code": "REGISTRATION_FAILED",
        }


def _register_pipeline_config(resource_id: str, config: dict[str, Any], overwrite: bool) -> dict[str, Any]:
    """注册管道配置到 PipelineConfigStore。

    Args:
        resource_id: 管道 ID
        config: 管道配置数据
        overwrite: 是否覆盖已存在的配置

    Returns:
        注册结果字典
    """
    try:
        from tools.tool_context import PipelineConfig, PipelineConfigStore  # noqa: PLC0415

        registry = _get_service("pipeline_config_store")
        if registry is None:
            registry = PipelineConfigStore()

        # 检查是否已存在
        if not overwrite and registry.get(resource_id) is not None:
            return {
                "success": False,
                "error": f"管道配置 '{resource_id}' 已存在，设置 overwrite=true 可覆盖",
                "error_code": "RESOURCE_EXISTS",
            }

        pipeline_config = PipelineConfig(
            pipeline_id=resource_id,
            name=config.get("name", resource_id),
            input_routes=config.get("input_routes", []),
            output_routes=config.get("output_routes", []),
            plugins=config.get("plugins", []),
            core_plugins=config.get("core_plugins", {}),
            max_iterations=config.get("max_iterations", 100),
        )

        registry.register(resource_id, pipeline_config)
        logger.info("[register_resource] 管道配置注册成功: %s", resource_id)

        return {
            "success": True,
            "resource_type": "pipeline_config",
            "resource_id": resource_id,
            "message": f"管道配置 '{resource_id}' 注册成功",
        }

    except Exception as exc:
        logger.error("[register_resource] 管道配置注册失败: %s", exc)
        return {
            "success": False,
            "error": f"管道配置注册失败: {exc}",
            "error_code": "REGISTRATION_FAILED",
        }


def _build_template_spec(resource_id: str, config: dict[str, Any]) -> TemplateSpec:  # noqa: F821
    """从配置数据构建 TemplateSpec。

    当 raw_content 不可用或解析失败时使用。

    Args:
        resource_id: 模板 ID
        config: 模板配置数据

    Returns:
        TemplateSpec 实例
    """
    from templates.types import (  # noqa: PLC0415
        EvaluationDimension,
        TemplateSpec,
        TemplateType,
    )

    # 解析评估维度
    eval_dims: list[EvaluationDimension] = []
    for dim_data in config.get("evaluation_dimensions", []):
        eval_dims.append(
            EvaluationDimension(
                name=dim_data.get("name", ""),
                check_content=dim_data.get("check_content", ""),
                required=dim_data.get("required", True),
                pass_criteria=dim_data.get("pass_criteria", ""),
            )
        )

    template_type_str = config.get("template_type", "B")
    template_type = TemplateType.DELIVERABLE if template_type_str == "B" else TemplateType.CONSUMABLE

    return TemplateSpec(
        template_id=resource_id,
        name=config.get("name", resource_id),
        template_type=template_type,
        description=config.get("description", ""),
        purpose=config.get("purpose", []),
        usage=config.get("usage", []),
        scenarios=config.get("scenarios", []),
        evaluation_dimensions=eval_dims,
        raw_content=config.get("raw_content", ""),
    )


def _get_service(service_name: str) -> Any:
    """获取已注册的服务实例。

    尝试通过全局服务定位器获取服务。
    如果服务不可用，尝试使用模块级缓存。

    Args:
        service_name: 服务名称

    Returns:
        服务实例，不可用时返回 None
    """
    # 1. 尝试从 CLI 主程序获取服务
    try:
        from channels.cli.cli_main import CLIMain  # noqa: PLC0415

        app = CLIMain.get_instance()
        if app is not None:
            service = app._services.get(service_name)
            if service is not None:
                return service
    except Exception:
        pass

    # 2. 尝试从模块级缓存获取
    return _service_cache.get(service_name)


# 模块级服务缓存 — 在独立运行（非 CLI）时使用
_service_cache: dict[str, Any] = {}


def set_service(service_name: str, service: Any) -> None:
    """设置服务实例到模块级缓存。

    供测试和独立运行时注入服务。

    Args:
        service_name: 服务名称
        service: 服务实例
    """
    _service_cache[service_name] = service

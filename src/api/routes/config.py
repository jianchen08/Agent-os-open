"""
配置 API 路由

提供系统配置的查询和管理接口
"""

import logging
import os
import sys
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.routes.auth import get_current_user
from src.config.llm_config import get_llm_config
from src.tools.registry import ToolRegistry
from src.tools.types import ToolSource

logger = logging.getLogger(__name__)

# 添加项目根目录到路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config.settings import get_settings

settings = get_settings()

router = APIRouter()

# 全局工具注册表实例
_tool_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """获取工具注册表实例"""
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
    return _tool_registry


def set_tool_registry(registry: ToolRegistry) -> None:
    """设置工具注册表实例（用于依赖注入）"""
    global _tool_registry
    _tool_registry = registry


# ============================================================================
# 上下文窗口配置模型
# ============================================================================


class ContextWindowConfig(BaseModel):
    """上下文窗口配置（基于百分比）"""

    max_tokens_ratio: float = Field(
        default=0.8,
        ge=0.1,
        le=1.0,
        description="最大 Token 使用比例（相对于模型上下文窗口）",
    )
    reserved_tokens_ratio: float = Field(
        default=0.15,
        ge=0.05,
        le=0.5,
        description="预留 Token 比例（用于响应生成）",
    )
    truncation_strategy: str = Field(
        default="tail", description="截断策略: head/tail/middle"
    )


class ContextWindowResponse(BaseModel):
    """上下文窗口配置响应"""

    config: ContextWindowConfig
    model: str = Field(..., description="当前模型")
    model_max_tokens: int = Field(..., description="模型最大 Token 数")
    effective_max_tokens: int = Field(..., description="实际可用最大 Token 数")
    effective_reserved_tokens: int = Field(..., description="实际预留 Token 数")


# 不再使用内存配置，改为读写 YAML 文件


@router.get("/llm", summary="获取 LLM 配置")
async def get_llm_config_api() -> dict[str, Any]:
    """
    获取 LLM 配置信息

    Returns:
        包含模型、提供商、默认配置的字典
    """
    config_manager = get_llm_config()

    # 获取所有模型配置
    models = {}
    for alias in config_manager.list_models():
        model_config = config_manager.get_model(alias)
        # 隐藏 API 密钥
        models[alias] = {
            "provider": model_config.provider,
            "model_name": model_config.model_name,
            "display_name": model_config.display_name,
            "api_base": model_config.api_base,
            "default_params": model_config.default_params or {},
        }

    # 获取提供商配置（隐藏密钥）
    providers = {}
    for name in config_manager.list_providers():
        provider_config = config_manager.get_provider(name)
        providers[name] = {
            "api_key": "***" if provider_config.api_key else "",
            "api_base": provider_config.api_base,
            "extra": provider_config.extra or {},
        }

    # 获取默认配置
    defaults = config_manager._defaults

    return {
        "models": models,
        "providers": providers,
        "defaults": {
            "chat": defaults.chat,
            "reasoning": defaults.reasoning,
            "embedding": defaults.embedding,
            "fallback": defaults.fallback,
        },
    }


@router.get("/llm/providers", summary="获取提供商列表")
async def get_providers() -> dict[str, Any]:
    """
    获取所有提供商信息

    Returns:
        提供商列表
    """
    config_manager = get_llm_config()

    providers = {}
    for name in config_manager.list_providers():
        provider_config = config_manager.get_provider(name)
        providers[name] = {
            "api_base": provider_config.api_base,
            "has_key": bool(provider_config.api_key),
        }

    return {"providers": providers}


@router.get("/llm/models", summary="获取模型列表")
async def get_models() -> dict[str, Any]:
    """
    获取所有可用模型

    Returns:
        模型列表
    """
    config_manager = get_llm_config()

    models = {}
    for alias in config_manager.list_models():
        model_config = config_manager.get_model(alias)
        models[alias] = {
            "provider": model_config.provider,
            "model_name": model_config.model_name,
            "display_name": model_config.display_name,
            "api_base": model_config.api_base,
        }

    return {"models": models}


@router.get("/llm/defaults", summary="获取默认配置")
async def get_defaults() -> dict[str, Any]:
    """
    获取默认模型配置

    Returns:
        默认配置
    """
    config_manager = get_llm_config()
    defaults = config_manager._defaults

    return {
        "chat": defaults.chat,
        "reasoning": defaults.reasoning,
        "embedding": defaults.embedding,
        "fallback": defaults.fallback,
    }


@router.put("/llm/defaults", summary="更新默认配置")
async def update_defaults(
    defaults: dict[str, str],
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    更新默认模型配置

    Args:
        defaults: 默认配置字典

    Returns:
        更新后的默认配置
    """
    config_manager = get_llm_config()

    # 验证并更新默认配置
    valid_keys = ["chat", "reasoning", "embedding", "fallback"]
    for key, value in defaults.items():
        if key not in valid_keys:
            raise HTTPException(status_code=400, detail=f"无效的配置键: {key}")
        # 验证模型是否存在
        if value and value not in config_manager.list_models():
            raise HTTPException(status_code=400, detail=f"模型 '{value}' 不存在")

    # 更新配置
    if "chat" in defaults:
        config_manager._defaults.chat = defaults["chat"]
    if "reasoning" in defaults:
        config_manager._defaults.reasoning = defaults["reasoning"]
    if "embedding" in defaults:
        config_manager._defaults.embedding = defaults["embedding"]
    if "fallback" in defaults:
        config_manager._defaults.fallback = defaults["fallback"]

    return await get_defaults()


class ModelConfigRequest(BaseModel):
    """模型配置请求"""

    provider: str = Field(..., description="提供商")
    model_name: str = Field(..., description="模型名称")
    display_name: str = Field(..., description="显示名称")
    api_base: str | None = Field(None, description="API 基础 URL")
    default_params: dict[str, Any] | None = Field(None, description="默认参数")


@router.post("/llm/models", summary="添加模型")
async def add_model(
    models: dict[str, ModelConfigRequest],
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    添加新模型配置

    Args:
        models: 模型配置字典，键为模型 ID

    Returns:
        更新后的模型列表
    """
    config_manager = get_llm_config()

    for model_id, config in models.items():
        # 检查模型是否已存在
        if model_id in config_manager.list_models():
            raise HTTPException(status_code=400, detail=f"模型 '{model_id}' 已存在")

        # 验证提供商是否存在
        if config.provider not in config_manager.list_providers():
            raise HTTPException(
                status_code=400,
                detail=f"提供商 '{config.provider}' 不存在",
            )

        # 添加模型配置
        config_manager.add_model(
            alias=model_id,
            provider=config.provider,
            model_name=config.model_name,
            display_name=config.display_name,
            api_base=config.api_base,
            default_params=config.default_params,
        )

    # 返回更新后的模型列表
    return (await get_models())["models"]


@router.delete("/llm/models/{model_id}", summary="删除模型")
async def delete_model(
    model_id: str,
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    删除模型配置

    Args:
        model_id: 模型 ID

    Returns:
        更新后的模型列表
    """
    config_manager = get_llm_config()

    # 检查模型是否存在
    if model_id not in config_manager.list_models():
        raise HTTPException(status_code=404, detail=f"模型 '{model_id}' 不存在")

    # 检查是否为默认模型
    defaults = config_manager._defaults
    if model_id in [
        defaults.chat,
        defaults.reasoning,
        defaults.embedding,
        defaults.fallback,
    ]:
        raise HTTPException(
            status_code=400,
            detail=f"无法删除默认模型 '{model_id}'，请先更改默认配置",
        )

    # 删除模型
    config_manager.remove_model(model_id)

    # 持久化配置到文件
    config_manager.save_to_file()

    # 返回更新后的模型列表
    return (await get_models())["models"]


@router.put("/llm/providers/{provider_id}", summary="更新提供商配置")
async def update_provider(
    provider_id: str,
    config: dict[str, Any],
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    更新提供商配置

    Args:
        provider_id: 提供商 ID
        config: 提供商配置

    Returns:
        更新后的提供商列表
    """
    config_manager = get_llm_config()

    # 检查提供商是否存在
    if provider_id not in config_manager.list_providers():
        raise HTTPException(status_code=404, detail=f"提供商 '{provider_id}' 不存在")

    # 更新提供商配置
    provider_config = config_manager.get_provider(provider_id)

    if "api_key" in config and config["api_key"] != "***":
        provider_config.api_key = config["api_key"]
    if "api_base" in config:
        provider_config.api_base = config["api_base"]
    if "extra" in config:
        provider_config.extra = config["extra"]

    # 返回更新后的提供商列表
    return (await get_providers())["providers"]


@router.get("/context-window", summary="获取上下文窗口配置")
async def get_context_window(
    current_user=Depends(get_current_user),
) -> ContextWindowResponse:
    """
    获取上下文窗口配置

    Returns:
        上下文窗口配置信息
    """
    from src.config.system_config import get_system_config_manager

    config_manager = get_llm_config()
    defaults = config_manager._defaults

    # 获取当前默认模型
    current_model = defaults.chat or "glm-4.7"

    # 从模型配置中获取实际的上下文窗口大小
    try:
        model_config = config_manager.get_model(current_model)
        model_max_tokens = model_config.context_window
    except Exception as exc:
        # 如果获取失败，使用默认值并记录警告
        logger.warning(
            f"获取模型 {current_model} 的上下文窗口大小失败: {exc}，使用默认值 128000"
        )
        model_max_tokens = 128000

    # 从 YAML 加载配置
    system_config = get_system_config_manager()
    yaml_config = system_config.load_context_window_config()

    # 从 YAML 提取比例配置
    budgets = yaml_config.get("budgets", {})
    # 支持 system_prompt 和 prompt 两种键名（向后兼容）
    system_prompt_budget = budgets.get("system_prompt", budgets.get("prompt", 0.10))
    context_config = ContextWindowConfig(
        max_tokens_ratio=system_prompt_budget
        + budgets.get("recent", 0.10)
        + budgets.get("l1", 0.09)
        + budgets.get("l2", 0.03)
        + budgets.get("l3", 0.01)
        + budgets.get("retrieval", 0.05),
        reserved_tokens_ratio=budgets.get("response_reserve", 0.20),
        truncation_strategy="tail",  # YAML 中没有此配置，使用默认值
    )

    # 计算实际 token 数
    effective_max = int(model_max_tokens * context_config.max_tokens_ratio)
    effective_reserved = int(model_max_tokens * context_config.reserved_tokens_ratio)

    return ContextWindowResponse(
        config=context_config,
        model=current_model,
        model_max_tokens=model_max_tokens,
        effective_max_tokens=effective_max,
        effective_reserved_tokens=effective_reserved,
    )


@router.put("/context-window", summary="更新上下文窗口配置")
async def update_context_window(
    config: ContextWindowConfig,
    current_user=Depends(get_current_user),
) -> ContextWindowResponse:
    """
    更新上下文窗口配置

    Args:
        config: 新的上下文窗口配置

    Returns:
        更新后的配置信息
    """
    from src.config.system_config import get_system_config_manager

    # 验证配置
    if config.max_tokens_ratio < config.reserved_tokens_ratio:
        raise HTTPException(
            status_code=400,
            detail="max_tokens_ratio 必须大于 reserved_tokens_ratio",
        )

    if config.truncation_strategy not in ["head", "tail", "middle"]:
        raise HTTPException(
            status_code=400, detail="truncation_strategy 必须是 head/tail/middle 之一"
        )

    # 加载现有 YAML 配置
    system_config = get_system_config_manager()
    yaml_config = system_config.load_context_window_config()

    # 更新 budgets（保持原有结构，只更新比例）
    # 注意：前端的 max_tokens_ratio 是总使用比例，需要分配到各个部分
    # 这里简化处理：保持各部分的相对比例，按总比例缩放
    budgets = yaml_config.get("budgets", {})
    current_total = (
        budgets.get("system_prompt", budgets.get("prompt", 0.10))
        + budgets.get("recent", 0.10)
        + budgets.get("l1", 0.09)
        + budgets.get("l2", 0.03)
        + budgets.get("l3", 0.01)
        + budgets.get("retrieval", 0.05)
    )

    # 计算缩放因子
    if current_total > 0:
        scale_factor = config.max_tokens_ratio / current_total
        budgets["system_prompt"] = budgets.get("system_prompt", budgets.get("prompt", 0.10)) * scale_factor
        budgets["recent"] = budgets.get("recent", 0.10) * scale_factor
        budgets["l1"] = budgets.get("l1", 0.09) * scale_factor
        budgets["l2"] = budgets.get("l2", 0.03) * scale_factor
        budgets["l3"] = budgets.get("l3", 0.01) * scale_factor
        budgets["retrieval"] = budgets.get("retrieval", 0.05) * scale_factor

    # 更新预留比例
    budgets["response_reserve"] = config.reserved_tokens_ratio

    yaml_config["budgets"] = budgets

    # 保存到 YAML
    system_config.save_context_window_config(yaml_config)

    # 返回更新后的配置
    return await get_context_window(current_user)


# ============================================================================
# API 配置模型
# ============================================================================


class EndpointConfig(BaseModel):
    """API 端点配置"""

    base_url: str = Field(default=settings.api_base_url, description="基础 URL")
    version: str = Field(default="v1", description="API 版本")
    timeout: int = Field(default=30, description="超时时间（秒）")


class RateLimitConfig(BaseModel):
    """限流配置"""

    global_limit: str = Field(default="100/minute", description="全局限流")
    auth: str = Field(default="10/minute", description="认证限流")
    tasks: str = Field(default="50/minute", description="任务限流")
    websocket: str = Field(default="30/minute", description="WebSocket 限流")


class APIConfigModel(BaseModel):
    """API 配置"""

    endpoint: EndpointConfig = Field(
        default_factory=EndpointConfig, description="端点配置"
    )
    rate_limit: RateLimitConfig = Field(
        default_factory=RateLimitConfig, description="限流配置"
    )
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:5173"],
        description="CORS 允许的源",
    )


# 内存中的 API 配置
_api_config = APIConfigModel()


@router.get("/api", summary="获取 API 配置")
async def get_api_config(
    current_user=Depends(get_current_user),
) -> APIConfigModel:
    """
    获取 API 配置

    Returns:
        API 配置信息
    """
    return _api_config


@router.put("/api", summary="更新 API 配置")
async def update_api_config(
    config: APIConfigModel,
    current_user=Depends(get_current_user),
) -> APIConfigModel:
    """
    更新 API 配置

    Args:
        config: 新的 API 配置

    Returns:
        更新后的配置信息
    """
    global _api_config

    # 验证超时时间
    if config.endpoint.timeout < 1:
        raise HTTPException(status_code=400, detail="timeout 必须大于 0")

    # 更新配置
    _api_config = config

    return _api_config


# ============================================================================
# 工具配置模型
# ============================================================================


class ToolConfigResponse(BaseModel):
    """工具配置响应"""

    id: str = Field(..., description="工具 ID")
    name: str = Field(..., description="工具名称")
    description: str = Field(..., description="工具描述")
    enabled: bool = Field(True, description="是否启用")
    type: Literal["builtin", "mcp", "custom"] = Field(..., description="工具类型")


class ToggleToolRequest(BaseModel):
    """切换工具启用状态请求"""

    enabled: bool = Field(..., description="是否启用")


class SaveToolsRequest(BaseModel):
    """批量保存工具配置请求"""

    tools: list[ToolConfigResponse] = Field(..., description="工具配置列表")


# 内存中的工具启用状态（工具 ID -> 是否启用）
_tool_enabled_status: dict[str, bool] = {}


def _source_to_type(source: ToolSource) -> Literal["builtin", "mcp", "custom"]:
    """将工具来源转换为前端类型"""
    if source == ToolSource.CODE:
        return "builtin"
    elif source == ToolSource.MCP:
        return "mcp"
    else:
        return "custom"


@router.get("/tools", summary="获取工具列表")
async def get_tools(
    current_user=Depends(get_current_user),
) -> list[ToolConfigResponse]:
    """
    获取所有工具配置

    Returns:
        工具配置列表
    """
    registry = get_tool_registry()
    tools = registry.list_all()

    result = []
    for tool in tools:
        # 获取启用状态，默认为启用
        enabled = _tool_enabled_status.get(tool.name, True)

        result.append(
            ToolConfigResponse(
                id=tool.name,
                name=tool.name,
                description=tool.description,
                enabled=enabled,
                type=_source_to_type(tool.source),
            )
        )

    return result


@router.put("/tools/{tool_id}", summary="切换工具启用状态")
async def toggle_tool_enabled(
    tool_id: str,
    request: ToggleToolRequest,
    current_user=Depends(get_current_user),
) -> ToolConfigResponse:
    """
    切换工具启用状态

    Args:
        tool_id: 工具 ID
        request: 包含启用状态的请求

    Returns:
        更新后的工具配置
    """
    registry = get_tool_registry()
    tool = registry.get_optional(tool_id)

    if tool is None:
        raise HTTPException(status_code=404, detail=f"工具 '{tool_id}' 不存在")

    # 更新启用状态
    _tool_enabled_status[tool_id] = request.enabled

    return ToolConfigResponse(
        id=tool.name,
        name=tool.name,
        description=tool.description,
        enabled=request.enabled,
        type=_source_to_type(tool.source),
    )


@router.put("/tools", summary="批量更新工具配置")
async def save_tools_config(
    request: SaveToolsRequest,
    current_user=Depends(get_current_user),
) -> list[ToolConfigResponse]:
    """
    批量更新工具配置

    Args:
        request: 包含工具配置列表的请求

    Returns:
        更新后的工具配置列表
    """
    # 更新所有工具的启用状态
    for tool_config in request.tools:
        _tool_enabled_status[tool_config.id] = tool_config.enabled

    # 返回更新后的配置
    return await get_tools(current_user)

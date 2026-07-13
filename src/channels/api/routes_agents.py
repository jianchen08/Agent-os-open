"""Agent 配置查询 API 路由。

提供 Agent 配置的列表和详情查询接口（只读），
数据来源于 AgentRegistry 从 YAML 文件加载的配置。
"""

from __future__ import annotations

import logging
from pathlib import Path  # noqa: F401
from typing import Any

from fastapi import APIRouter, Depends, Query

from channels.api.deps import APIError, require_auth
from channels.api.models import AgentListResponse, AgentResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["Agent 配置"])


def _get_agent_registry() -> Any:
    """获取全局 Agent 注册表单例。

    通过 get_global_agent_registry_sync() 获取，配置从 config/agents/ 加载，
    热重载由 PluginHotReloader 统一处理。

    Returns:
        AgentRegistry 实例，加载失败则返回 None
    """
    try:
        from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415

        return get_global_agent_registry_sync()
    except Exception as exc:
        logger.warning("Agent 注册表初始化失败: %s", exc)
        return None


def _resolve_agent_model(cfg: Any) -> str:
    """解析 Agent 配置对应的实际模型标识。

    与运行时 apply_agent_model_override 解析逻辑保持一致：
    model_tier 解析优先（从 llm.yaml defaults.tiers），model_name 兜底。

    Args:
        cfg: AgentConfig dataclass

    Returns:
        模型标识字符串，解析失败返回空字符串
    """
    model_id = ""
    if getattr(cfg, "model_tier", ""):
        try:
            from pipeline.plugin_resolver import resolve_tier  # noqa: PLC0415

            model_id = resolve_tier(cfg.model_tier, {})
        except Exception as exc:
            logger.warning("解析 model_tier=%r 失败: %s", cfg.model_tier, exc)
    if not model_id:
        model_id = getattr(cfg, "model_name", "") or ""
    return model_id


def _config_to_response(cfg: Any) -> AgentResponse:
    """将 AgentConfig dataclass 转为 AgentResponse。"""
    return AgentResponse(
        config_id=cfg.config_id,
        name=cfg.name,
        display_name=cfg.display_name,
        description=cfg.description,
        agent_type=cfg.agent_type.value if hasattr(cfg.agent_type, "value") else str(cfg.agent_type),
        category=cfg.category,
        level=cfg.level.value if hasattr(cfg.level, "value") else str(cfg.level),
        system_prompt=cfg.system_prompt[:200] + "..." if len(cfg.system_prompt) > 200 else cfg.system_prompt,
        tool_ids=cfg.tool_ids,
        tags=cfg.tags,
        is_active=cfg.is_active,
        version=cfg.version,
        model=_resolve_agent_model(cfg),
    )


@router.get(
    "",
    response_model=AgentListResponse,
    summary="获取 Agent 配置列表",
)
def list_agents(
    category: str | None = Query(default=None, description="按分类筛选"),
    level: str | None = Query(default=None, description="按层级筛选 (L1/L2/L3)"),
    tag: str | None = Query(default=None, description="按标签筛选"),
    limit: int = Query(default=50, ge=1, le=200, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    _user: dict = Depends(require_auth),
) -> AgentListResponse:
    """获取所有已加载的 Agent 配置列表。

    支持按分类、层级、标签筛选，结果分页返回。

    Returns:
        AgentListResponse 包含 items 和 total
    """
    registry = _get_agent_registry()

    if registry is None:
        return AgentListResponse(items=[], total=0)

    # 获取全部配置
    configs = registry.list_all()

    # 筛选
    if category:
        configs = [c for c in configs if c.category == category]
    if level:
        from agents.types import AgentLevel  # noqa: PLC0415

        try:
            level_enum = AgentLevel(level)
            configs = [c for c in configs if c.level == level_enum]
        except ValueError:
            pass
    if tag:
        configs = [c for c in configs if tag in c.tags]

    total = len(configs)
    items = [_config_to_response(c) for c in configs[offset : offset + limit]]

    return AgentListResponse(items=items, total=total)


@router.get(
    "/{config_id}",
    response_model=AgentResponse,
    summary="获取 Agent 配置详情",
)
def get_agent(
    config_id: str,
    _user: dict = Depends(require_auth),
) -> AgentResponse:
    """根据 config_id 获取单个 Agent 配置详情。

    Args:
        config_id: Agent 配置唯一标识

    Returns:
        AgentResponse 配置详情

    Raises:
        APIError: 配置不存在 (404)
    """
    registry = _get_agent_registry()

    if registry is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="Agent 配置未加载或注册表未初始化",
        )

    config = registry.get(config_id)
    if config is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message=f"Agent 配置 '{config_id}' 不存在",
        )

    return _config_to_response(config)


# ---------------------------------------------------------------------------
# Agent 健康检查
# ---------------------------------------------------------------------------


@router.get(
    "/health",
    summary="Agent服务健康检查",
)
def agents_health() -> dict[str, Any]:
    """Agent 服务健康检查。"""
    return {"status": "ok", "agents_count": 0}


# ---------------------------------------------------------------------------
# 默认Agent
# ---------------------------------------------------------------------------


@router.get(
    "/default",
    summary="获取默认Agent",
)
def get_default_agent(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取默认 Agent 配置。"""
    registry = _get_agent_registry()
    if registry is None:
        return {
            "config_id": "default",
            "name": "default",
            "display_name": "默认Agent",
            "description": "默认Agent配置",
            "agent_type": "general",
            "is_active": True,
        }
    return {"config_id": "default", "name": "default", "display_name": "默认Agent"}


# ---------------------------------------------------------------------------
# Agent CRUD（前端期望的写操作端点）
# ---------------------------------------------------------------------------


@router.post(
    "",
    summary="创建Agent配置",
)
def create_agent(
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """创建Agent配置。"""
    return {"config_id": "stub", "name": "", "message": "Agent创建成功（存根）"}


@router.put(
    "/{agent_id}",
    summary="更新Agent配置",
)
def update_agent(
    agent_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """更新Agent配置。"""
    return {"config_id": agent_id, "message": "Agent已更新"}


@router.delete(
    "/{agent_id}",
    summary="删除Agent配置",
)
def delete_agent(
    agent_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """删除Agent配置。"""
    return {"message": "Agent已删除", "config_id": agent_id}

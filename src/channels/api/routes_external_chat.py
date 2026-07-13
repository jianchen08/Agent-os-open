"""外部系统 Agent 管道执行端点。

提供轻量级 HTTP POST 接口，让外部系统通过一次调用触发 Agent 管道执行并同步返回结果。
无状态设计：不创建 thread、不做会话持久化。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from channels.api.deps import APIError, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/external",
    tags=["外部系统"],
    dependencies=[Depends(require_auth)],
)


# ============================================================
# 请求 / 响应模型
# ============================================================


class ExternalChatRequest(BaseModel):
    """外部聊天请求模型。"""

    agent_id: str = Field(description="目标 Agent 配置 ID")
    message: str = Field(description="用户输入文本")


class ExternalChatResponse(BaseModel):
    """外部聊天响应模型。"""

    agent_id: str
    reply: str


# ============================================================
# 依赖获取
# ============================================================


def _get_agent_registry() -> Any:
    """从 ServiceProvider 获取全局 AgentRegistry 实例。"""
    from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

    return get_service_provider().get("agent_registry")


# ============================================================
# 端点
# ============================================================


@router.post(
    "/chat",
    response_model=ExternalChatResponse,
    summary="外部系统调用 Agent 管道",
)
async def external_chat(
    body: ExternalChatRequest,
    _user: dict = Depends(require_auth),
) -> ExternalChatResponse:
    """接收外部系统的消息，同步执行 Agent 管道并返回结果。

    Args:
        body: 包含 agent_id 和 message 的请求体
        _user: JWT 鉴权后的用户信息

    Returns:
        ExternalChatResponse 包含 agent_id 和 reply

    Raises:
        APIError: agent_id 不存在 (404) 或引擎执行失败 (500)
    """
    # 1. 获取 AgentRegistry，查找 agent 配置
    agent_registry = _get_agent_registry()
    if agent_registry is None:
        raise APIError(
            status_code=503,
            error_code="EXT_CHAT_001",
            message="AgentRegistry 服务不可用",
        )

    agent_config = agent_registry.get(body.agent_id)
    if agent_config is None:
        raise APIError(
            status_code=404,
            error_code="EXT_CHAT_002",
            message=f"Agent '{body.agent_id}' 不存在",
        )

    # 2. 持有者注册管道（API 入口是这次执行的持有者），再 run_once 执行拿结果。
    # 外部全程不持有 engine 引用：register 写 entry，run_once 内部经 entry 访问。
    from infrastructure.service_provider import get_service_provider  # noqa: PLC0415
    from pipeline.message_bus import run_once  # noqa: PLC0415
    from pipeline.message_types import MessageType, PipelineMessage  # noqa: PLC0415
    from pipeline.registry import get_engine_registry  # noqa: PLC0415

    provider = get_service_provider()
    entry = get_engine_registry().register_pipeline(
        tags={"agent_id": body.agent_id, "source": "external_chat"},
        input_route_table=provider.get("input_route_table"),
        output_route_table=provider.get("output_route_table"),
        plugin_registry=provider.get("plugin_registry"),
        services=provider.get_all_services(),
    )
    if entry is None:
        raise APIError(
            status_code=503,
            error_code="EXT_CHAT_003",
            message="管道注册失败（路由表/插件注册表不可用）",
        )

    try:
        inject_result, state = await run_once(
            PipelineMessage(
                type=MessageType.CHAT,
                content=body.message,
                pipeline_id=entry.engine.pipeline_id,
            ),
        )
        if not inject_result.success:
            raise APIError(
                status_code=500,
                error_code="EXT_CHAT_005",
                message=f"管道执行失败: {inject_result.error}",
            )
    except APIError:
        raise
    except Exception as exc:
        logger.error("管道执行失败 (agent=%s): %s", body.agent_id, exc)
        raise APIError(
            status_code=500,
            error_code="EXT_CHAT_005",
            message="Agent 管道执行失败",
        ) from exc

    # 3. 提取结果
    raw_result = state.get("raw_result", "")
    reply = str(raw_result) if raw_result is not None else ""

    return ExternalChatResponse(agent_id=body.agent_id, reply=reply)

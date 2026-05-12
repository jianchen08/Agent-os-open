"""
思考模式 API 路由

提供思考模式切换和管理的 REST API 接口
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.llm.base import Message
from src.llm.services.thinking_mode import get_thinking_mode_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/thinking-mode", tags=["思考模式"])


# === 请求模型 ===


class ThinkingModeRequest(BaseModel):
    """思考模式请求"""

    model_name: str = Field(..., description="模型名称")
    messages: list[dict[str, str]] = Field(..., description="消息列表")
    enable_thinking: bool = Field(True, description="是否启用思考模式")
    task_type: str | None = Field("general", description="任务类型")
    complexity: str | None = Field("medium", description="复杂度")
    extra_params: dict[str, Any] | None = Field(default=None, description="额外参数")


class ThinkingModeSwitchRequest(BaseModel):
    """思考模式切换请求"""

    current_model: str = Field(..., description="当前模型名称")
    enable_thinking: bool = Field(..., description="是否启用思考模式")


class ThinkingModeRecommendationRequest(BaseModel):
    """思考模式推荐请求"""

    task_type: str = Field("general", description="任务类型")
    complexity: str = Field("medium", description="复杂度")


# === 响应模型 ===


class ThinkingModeInfo(BaseModel):
    """思考模式信息"""

    model_name: str
    thinking_type: str
    display_name: str
    base_model: str
    thinking_model: str
    is_same_model: bool
    switch_description: str
    thinking_params: dict[str, Any]
    normal_params: dict[str, Any]


class ThinkingModelInfo(BaseModel):
    """思考模型信息"""

    model_name: str
    display_name: str
    thinking_type: str
    base_model: str
    thinking_model: str
    is_same_model: bool
    supports_reasoning_effort: bool
    description: str


class ThinkingModeResponse(BaseModel):
    """思考模式响应"""

    content: str
    model_used: str
    thinking_enabled: bool
    thinking_type: str | None = None
    usage: dict[str, Any] | None = None


class ThinkingModeSwitchResponse(BaseModel):
    """思考模式切换响应"""

    target_model: str
    params: dict[str, Any]
    switch_type: str
    description: str


class ThinkingModeRecommendation(BaseModel):
    """思考模式推荐"""

    model_name: str
    display_name: str
    thinking_type: str
    suitability_score: float
    optimal_params: dict[str, Any]
    best_for: list[str]
    tips: list[str]
    cost_estimate: str


# === API 路由 ===


@router.get("/test")
async def test_endpoint() -> dict:
    """测试端点"""
    return {"message": "思考模式路由工作正常"}


@router.get("/models", response_model=list[ThinkingModelInfo])
async def get_thinking_models() -> list[ThinkingModelInfo]:
    """获取所有支持思考模式的模型"""
    try:
        service = get_thinking_mode_service()
        models = service.get_available_thinking_models()

        return [
            ThinkingModelInfo(
                model_name=model["model_name"],
                display_name=model["display_name"],
                thinking_type=model["thinking_type"],
                base_model=model["base_model"],
                thinking_model=model["thinking_model"],
                is_same_model=model["is_same_model"],
                supports_reasoning_effort=model["supports_reasoning_effort"],
                description=model["description"],
            )
            for model in models
        ]
    except Exception as e:
        logger.error(f"获取思考模型列表失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取思考模型列表失败: {str(e)}")


@router.get("/models/{model_name}", response_model=ThinkingModeInfo)
async def get_thinking_mode_info(model_name: str) -> ThinkingModeInfo:
    """获取指定模型的思考模式信息"""
    try:
        service = get_thinking_mode_service()
        info = service.get_thinking_mode_info(model_name)

        if not info:
            raise HTTPException(
                status_code=404, detail=f"模型 {model_name} 不支持思考模式"
            )

        return ThinkingModeInfo(**info)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取思考模式信息失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取思考模式信息失败: {str(e)}")


@router.post("/generate", response_model=ThinkingModeResponse)
async def generate_with_thinking_mode(
    request: ThinkingModeRequest,
) -> ThinkingModeResponse:
    """使用思考模式生成响应"""
    try:
        service = get_thinking_mode_service()

        # 转换消息格式
        messages = [
            Message(role=msg["role"], content=msg["content"])  # type: ignore
            for msg in request.messages
        ]

        # 生成响应
        extra_params = request.extra_params or {}
        response = await service.generate_with_thinking_mode(
            model_name=request.model_name,
            messages=messages,
            enable_thinking=request.enable_thinking,
            **extra_params,
        )

        # 获取思考模式信息
        thinking_info = service.get_thinking_mode_info(request.model_name)
        thinking_type = thinking_info.get("thinking_type") if thinking_info else None

        return ThinkingModeResponse(
            content=response.content or "",
            model_used=response.model,
            thinking_enabled=request.enable_thinking,
            thinking_type=thinking_type,
            usage={
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
        )
    except Exception as e:
        logger.error(f"思考模式生成失败: {e}")
        raise HTTPException(status_code=500, detail=f"思考模式生成失败: {str(e)}")


@router.post("/switch", response_model=ThinkingModeSwitchResponse)
async def switch_thinking_mode(
    request: ThinkingModeSwitchRequest,
) -> ThinkingModeSwitchResponse:
    """切换思考模式"""
    try:
        service = get_thinking_mode_service()

        target_model, params = service.switch_thinking_mode(
            current_model=request.current_model,
            enable_thinking=request.enable_thinking,
        )

        # 获取切换信息
        thinking_info = service.get_thinking_mode_info(request.current_model)
        if not thinking_info:
            raise HTTPException(
                status_code=400,
                detail=f"模型 {request.current_model} 不支持思考模式",
            )

        switch_type = thinking_info["thinking_type"]
        description = thinking_info["switch_description"]

        return ThinkingModeSwitchResponse(
            target_model=target_model,
            params=params,
            switch_type=switch_type,
            description=description,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"思考模式切换失败: {e}")
        raise HTTPException(status_code=500, detail=f"思考模式切换失败: {str(e)}")


@router.post("/recommendations", response_model=list[ThinkingModeRecommendation])
async def get_thinking_mode_recommendations(
    request: ThinkingModeRecommendationRequest,
) -> list[ThinkingModeRecommendation]:
    """获取思考模式推荐"""
    try:
        service = get_thinking_mode_service()

        recommendations = service.get_thinking_mode_recommendations(
            task_type=request.task_type, complexity=request.complexity
        )

        return [ThinkingModeRecommendation(**rec) for rec in recommendations]
    except Exception as e:
        logger.error(f"获取思考模式推荐失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取思考模式推荐失败: {str(e)}")


@router.get("/check/{model_name}")
async def check_thinking_mode_support(model_name: str) -> dict:
    """检查模型是否支持思考模式"""
    try:
        service = get_thinking_mode_service()

        supports_thinking = service.can_enable_thinking_mode(model_name)

        result = {"model_name": model_name, "supports_thinking": supports_thinking}

        if supports_thinking:
            info = service.get_thinking_mode_info(model_name)
            if info:
                result.update(
                    {
                        "thinking_type": info["thinking_type"],
                        "display_name": info["display_name"],
                        "switch_description": info["switch_description"],
                    }
                )

        return result
    except Exception as e:
        logger.error(f"检查思考模式支持失败: {e}")
        raise HTTPException(status_code=500, detail=f"检查思考模式支持失败: {str(e)}")


# === 健康检查 ===


@router.get("/health")
async def health_check() -> dict:
    """思考模式服务健康检查"""
    try:
        service = get_thinking_mode_service()
        models = service.get_available_thinking_models()

        return {
            "status": "healthy",
            "available_models": len(models),
            "service": "thinking_mode",
        }
    except Exception as e:
        logger.error(f"思考模式服务健康检查失败: {e}")
        raise HTTPException(status_code=503, detail=f"服务不可用: {str(e)}")

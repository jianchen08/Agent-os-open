"""
Token 计算相关 API 路由

提供基于 tiktoken 的精确 Token 计算功能
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.core.tokenizer import get_token_counter

router = APIRouter(prefix="/tokens", tags=["Token计算"])


class TokenCountRequest(BaseModel):
    """Token 计算请求"""

    text: str = Field(..., description="要计算的文本")
    model: str = Field(default="gpt-4", description="模型名称，用于选择编码器")


class TokenCountResponse(BaseModel):
    """Token 计算响应"""

    token_count: int = Field(..., description="Token 数量")
    text_length: int = Field(..., description="文本字符数")
    model: str = Field(..., description="使用的模型")


class BatchTokenCountRequest(BaseModel):
    """批量 Token 计算请求"""

    texts: list[str] = Field(..., description="要计算的文本列表")
    model: str = Field(default="gpt-4", description="模型名称")


class BatchTokenCountResponse(BaseModel):
    """批量 Token 计算响应"""

    token_counts: list[int] = Field(..., description="Token 数量列表")
    total_tokens: int = Field(..., description="总 Token 数")
    model: str = Field(..., description="使用的模型")


class MessageTokenCountRequest(BaseModel):
    """消息 Token 计算请求"""

    role: str = Field(..., description="消息角色")
    content: str = Field(..., description="消息内容")
    model: str = Field(default="gpt-4", description="模型名称")


class MessagesTokenCountRequest(BaseModel):
    """多条消息 Token 计算请求"""

    messages: list[MessageTokenCountRequest] = Field(..., description="消息列表")
    model: str = Field(default="gpt-4", description="模型名称")


@router.post("/count", response_model=TokenCountResponse)
async def count_tokens(request: TokenCountRequest) -> TokenCountResponse:
    """
    计算文本的 Token 数量

    使用 tiktoken 进行精确的 Token 计算，支持多种模型编码器。
    """
    try:
        token_counter = get_token_counter()
        # 使用 count_text 方法，支持模型参数
        token_count = token_counter.count_text(request.text, request.model)

        return TokenCountResponse(
            token_count=token_count,
            text_length=len(request.text),
            model=request.model,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Token 计算失败: {str(e)}")


@router.post("/count/batch", response_model=BatchTokenCountResponse)
async def count_tokens_batch(
    request: BatchTokenCountRequest,
) -> BatchTokenCountResponse:
    """
    批量计算多个文本的 Token 数量
    """
    try:
        token_counter = get_token_counter()
        token_counts = [
            token_counter.count_text(text, request.model) for text in request.texts
        ]

        return BatchTokenCountResponse(
            token_counts=token_counts,
            total_tokens=sum(token_counts),
            model=request.model,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量 Token 计算失败: {str(e)}")


@router.post("/count/messages")
async def count_messages_tokens(request: MessagesTokenCountRequest):
    """
    计算多条消息的总 Token 数量

    考虑消息格式开销（每条消息约 4 tokens 的格式开销）
    """
    try:
        token_counter = get_token_counter()

        # 转换为字典格式
        messages = [
            {"role": msg.role, "content": msg.content} for msg in request.messages
        ]

        token_count = token_counter.count_messages(messages, request.model)

        return {
            "token_count": token_count,
            "message_count": len(messages),
            "model": request.model,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"消息 Token 计算失败: {str(e)}")

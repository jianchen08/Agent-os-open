"""
知识库路由

提供知识库管理相关的 API 端点
"""

import logging
import os
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import generate_trace_id
from src.api.errors import create_error_response
from src.api.routes.auth import get_current_user
from src.db.connection import get_async_session
from src.db.models import KnowledgeBase

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# 数据模型
# ============================================================================


class KnowledgeBaseItem(BaseModel):
    """知识库项"""

    id: str = Field(..., description="知识库 ID")
    name: str = Field(..., description="知识库名称")
    description: str | None = Field(None, description="描述")
    type: str = Field(..., description="类型")
    status: str = Field(..., description="状态")
    doc_count: int = Field(default=0, description="文档数量")
    tags: list[str] | None = Field(None, description="标签")
    created_at: str = Field(..., description="创建时间")
    updated_at: str | None = Field(None, description="更新时间")


class KnowledgeBaseListResponse(BaseModel):
    """知识库列表响应"""

    items: list[KnowledgeBaseItem] = Field(..., description="知识库列表")
    total: int = Field(..., ge=0, description="总数量")


class KnowledgeBaseStatsResponse(BaseModel):
    """知识库统计响应"""

    total: int = Field(..., ge=0, description="知识库总数")
    completed: int = Field(..., ge=0, description="已完成数量")
    processing: int = Field(..., ge=0, description="处理中数量")
    error: int = Field(..., ge=0, description="错误数量")
    total_docs: int = Field(..., ge=0, description="文档总数")


class KnowledgeBaseCreateRequest(BaseModel):
    """知识库创建请求"""

    name: str = Field(..., min_length=1, max_length=255, description="知识库名称")
    description: str | None = Field(None, description="描述")
    type: str = Field(default="document", description="类型")
    tags: list[str] | None = Field(None, description="标签")


class KnowledgeBaseUpdateRequest(BaseModel):
    """知识库更新请求"""

    name: str | None = Field(None, min_length=1, max_length=255, description="知识库名称")
    description: str | None = Field(None, description="描述")
    tags: list[str] | None = Field(None, description="标签")


# ============================================================================
# 辅助函数
# ============================================================================


def kb_to_item(kb: KnowledgeBase) -> KnowledgeBaseItem:
    """
    将数据库模型转换为响应模型

    Args:
        kb: 知识库数据库模型

    Returns:
        知识库响应模型
    """
    return KnowledgeBaseItem(
        id=str(kb.id),
        name=kb.name,
        description=kb.description,
        type=kb.type,
        status=kb.status,
        doc_count=kb.doc_count,
        tags=kb.tags or [],
        created_at=kb.created_at.isoformat() if kb.created_at else "",
        updated_at=kb.updated_at.isoformat() if kb.updated_at else None,
    )


# ============================================================================
# 路由
# ============================================================================


@router.get(
    "",
    response_model=KnowledgeBaseListResponse,
    summary="获取知识库列表",
    description="获取当前用户的知识库列表",
)
async def list_knowledge_bases(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """获取知识库列表"""
    try:
        query = (
            select(KnowledgeBase)
            .where(KnowledgeBase.user_id == str(current_user.id))
            .order_by(KnowledgeBase.created_at.desc())
        )

        result = await session.execute(query)
        knowledge_bases = result.scalars().all()

        items = [kb_to_item(kb) for kb in knowledge_bases]

        return KnowledgeBaseListResponse(items=items, total=len(items))
    except Exception as exc:
        logger.warning(f"获取知识库列表失败: {exc}", exc_info=True)
        return KnowledgeBaseListResponse(items=[], total=0)


@router.get(
    "/stats",
    response_model=KnowledgeBaseStatsResponse,
    summary="获取知识库统计",
    description="获取当前用户的知识库统计数据",
)
async def get_knowledge_base_stats(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """获取知识库统计"""
    try:
        user_id = str(current_user.id)

        # 总数
        total_query = select(func.count(KnowledgeBase.id)).where(
            KnowledgeBase.user_id == user_id
        )
        total_result = await session.execute(total_query)
        total = total_result.scalar() or 0

        # 已完成
        completed_query = select(func.count(KnowledgeBase.id)).where(
            KnowledgeBase.user_id == user_id, KnowledgeBase.status == "completed"
        )
        completed_result = await session.execute(completed_query)
        completed = completed_result.scalar() or 0

        # 处理中
        processing_query = select(func.count(KnowledgeBase.id)).where(
            KnowledgeBase.user_id == user_id, KnowledgeBase.status == "processing"
        )
        processing_result = await session.execute(processing_query)
        processing = processing_result.scalar() or 0

        # 错误
        error_query = select(func.count(KnowledgeBase.id)).where(
            KnowledgeBase.user_id == user_id, KnowledgeBase.status == "error"
        )
        error_result = await session.execute(error_query)
        error = error_result.scalar() or 0

        # 文档总数
        docs_query = select(func.sum(KnowledgeBase.doc_count)).where(
            KnowledgeBase.user_id == user_id
        )
        docs_result = await session.execute(docs_query)
        total_docs = docs_result.scalar() or 0

        return KnowledgeBaseStatsResponse(
            total=total,
            completed=completed,
            processing=processing,
            error=error,
            total_docs=total_docs,
        )
    except Exception as exc:
        logger.warning(f"获取知识库统计失败: {exc}", exc_info=True)
        return KnowledgeBaseStatsResponse(
            total=0, completed=0, processing=0, error=0, total_docs=0
        )


@router.post(
    "",
    response_model=KnowledgeBaseItem,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库",
    description="创建新的知识库",
)
async def create_knowledge_base(
    request: KnowledgeBaseCreateRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> KnowledgeBaseItem:
    """创建知识库"""
    kb = KnowledgeBase(
        id=str(uuid.uuid4()),
        user_id=str(current_user.id),
        name=request.name,
        description=request.description,
        type=request.type,
        status="completed",
        tags=request.tags or [],
    )

    session.add(kb)
    await session.flush()
    await session.refresh(kb)

    return kb_to_item(kb)


@router.post(
    "/upload",
    response_model=KnowledgeBaseItem,
    status_code=status.HTTP_201_CREATED,
    summary="上传文件到知识库",
    description="上传文件并创建知识库",
)
async def upload_knowledge_base(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> KnowledgeBaseItem:
    """上传文件到知识库"""
    # 验证文件类型
    allowed_extensions = [".pdf", ".txt", ".md", ".docx"]
    file_ext = os.path.splitext(file.filename or "unknown")[1].lower()

    if file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件格式: {file_ext}。支持的格式: {', '.join(allowed_extensions)}",
        )

    # 获取文件大小
    file_size = 0
    content = await file.read()
    file_size = len(content)

    # 确定文件类型
    file_type_map = {
        ".pdf": "pdf",
        ".txt": "txt",
        ".md": "md",
        ".docx": "docx",
    }
    file_type = file_type_map.get(file_ext, "document")

    # 创建知识库记录
    kb = KnowledgeBase(
        id=str(uuid.uuid4()),
        user_id=str(current_user.id),
        name=file.filename or "未命名文件",
        description=f"文件大小: {file_size} 字节",
        type=file_type,
        status="processing",
        doc_count=1,
        tags=[file_type],
    )

    session.add(kb)
    await session.flush()
    await session.refresh(kb)

    # 模拟异步处理（实际项目中应该使用后台任务）
    # 这里我们直接将状态改为 completed
    kb.status = "completed"
    await session.flush()
    await session.refresh(kb)

    logger.info(f"用户 {current_user.id} 上传文件 {file.filename} 成功")

    return kb_to_item(kb)


@router.get(
    "/{kb_id}",
    response_model=KnowledgeBaseItem,
    summary="获取知识库",
    description="获取指定的知识库",
)
async def get_knowledge_base(
    kb_id: str,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> KnowledgeBaseItem:
    """获取知识库"""
    kb = await session.get(KnowledgeBase, kb_id)

    if not kb:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="KB_001",
            trace_id=trace_id,
            path=f"/api/v1/knowledge-base/{kb_id}",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    if str(kb.user_id) != str(current_user.id):
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="KB_002",
            trace_id=trace_id,
            path=f"/api/v1/knowledge-base/{kb_id}",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=error.model_dump(mode="json")
        )

    return kb_to_item(kb)


@router.patch(
    "/{kb_id}",
    response_model=KnowledgeBaseItem,
    summary="更新知识库",
    description="更新知识库信息",
)
async def update_knowledge_base(
    kb_id: str,
    request: KnowledgeBaseUpdateRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> KnowledgeBaseItem:
    """更新知识库"""
    kb = await session.get(KnowledgeBase, kb_id)

    if not kb:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="KB_001",
            trace_id=trace_id,
            path=f"/api/v1/knowledge-base/{kb_id}",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    if str(kb.user_id) != str(current_user.id):
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="KB_002",
            trace_id=trace_id,
            path=f"/api/v1/knowledge-base/{kb_id}",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=error.model_dump(mode="json")
        )

    # 更新字段
    if request.name is not None:
        kb.name = request.name
    if request.description is not None:
        kb.description = request.description
    if request.tags is not None:
        kb.tags = request.tags

    await session.flush()
    await session.refresh(kb)

    return kb_to_item(kb)


@router.delete(
    "/{kb_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除知识库",
    description="删除指定的知识库",
)
async def delete_knowledge_base(
    kb_id: str,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """删除知识库"""
    kb = await session.get(KnowledgeBase, kb_id)

    if not kb:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="KB_001",
            trace_id=trace_id,
            path=f"/api/v1/knowledge-base/{kb_id}",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    if str(kb.user_id) != str(current_user.id):
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="KB_002",
            trace_id=trace_id,
            path=f"/api/v1/knowledge-base/{kb_id}",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=error.model_dump(mode="json")
        )

    await session.delete(kb)
    await session.flush()

    logger.info(f"用户 {current_user.id} 删除知识库 {kb_id} 成功")


@router.post(
    "/check",
    summary="检查知识库",
    description="检查知识库状态",
)
async def check_knowledge_bases(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """检查知识库"""
    try:
        user_id = str(current_user.id)

        # 查询处理中的知识库
        processing_query = select(KnowledgeBase).where(
            KnowledgeBase.user_id == user_id, KnowledgeBase.status == "processing"
        )
        result = await session.execute(processing_query)
        processing_kbs = result.scalars().all()

        # 将处理中的知识库标记为完成（模拟处理完成）
        for kb in processing_kbs:
            kb.status = "completed"

        if processing_kbs:
            await session.flush()

        return {
            "success": True,
            "message": "知识库检查完成",
            "processed_count": len(processing_kbs),
        }
    except Exception as exc:
        logger.error(f"知识库检查失败: {exc}", exc_info=True)
        return {"success": False, "message": "知识库检查失败", "processed_count": 0}

"""审批视图路由器。

根据制品（Artifact）类型将审批请求路由到对应的前端视图模式。
后端在创建审批请求时调用 resolve_view_mode() 确定视图模式，
前端 ApprovalRouter 组件据此渲染对应的子视图。

支持的视图模式（与前端 ApprovalRouter.tsx 中 ViewMode 对齐）：
    - text_diff : 文本差异对比视图（默认）
    - image_annotation : 图片标注视图
    - media_timeline : 媒体时间轴视图

路由规则（按优先级）：
    1. 显式指定 view_mode → 直接使用（若有效）
    2. 单一制品 → 根据制品类型自动推断
    3. 多制品 → 根据 first_artifact_type 推断，降级为 text_diff

暴露接口：
- ViewMode : 视图模式枚举
- resolve_view_mode : 根据制品信息解析视图模式
- get_artifact_view_hints : 获取制品视图提示（供前端使用）
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ViewMode(str, Enum):
    """审批视图模式枚举。

    与前端 ApprovalRouter.tsx 中的 ViewMode 类型保持一致：
        text_diff | image_annotation | media_timeline
    """

    TEXT_DIFF = "text_diff"
    IMAGE_ANNOTATION = "image_annotation"
    MEDIA_TIMELINE = "media_timeline"


# 制品类型到视图模式的映射
_ARTIFACT_TYPE_TO_VIEW_MODE: dict[str, ViewMode] = {
    # 文本类制品 → 文本差异视图
    "text": ViewMode.TEXT_DIFF,
    "file": ViewMode.TEXT_DIFF,
    # 图片类制品 → 图片标注视图
    "image": ViewMode.IMAGE_ANNOTATION,
    "screenshot": ViewMode.IMAGE_ANNOTATION,
    # 视频/音频制品 → 媒体时间轴视图
    "video": ViewMode.MEDIA_TIMELINE,
    "audio": ViewMode.MEDIA_TIMELINE,
}

# 默认视图模式
_DEFAULT_VIEW_MODE: ViewMode = ViewMode.TEXT_DIFF

# 有效视图模式集合
_VALID_VIEW_MODES: set[str] = {m.value for m in ViewMode}


def resolve_view_mode(
    *,
    explicit_mode: str | None = None,
    artifact_types: list[str] | None = None,
    first_artifact_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """根据制品信息解析视图模式。

    解析优先级：
    1. explicit_mode 非空且有效 → 直接使用
    2. artifact_types 非空 → 根据第一个制品类型推断
    3. first_artifact_type 非空 → 根据该类型推断
    4. metadata 中有 view_mode 且有效 → 使用
    5. 以上均无 → 返回默认 text_diff

    Args:
        explicit_mode: 显式指定的视图模式
        artifact_types: 制品类型列表
        first_artifact_type: 第一个制品的类型（简化参数）
        metadata: 审批请求的元数据字典

    Returns:
        视图模式字符串（与 ViewMode 枚举值一致）
    """
    # 1. 显式指定
    if explicit_mode and explicit_mode in _VALID_VIEW_MODES:
        return explicit_mode

    # 2. 根据制品类型推断
    target_type: str | None = None
    if artifact_types is not None and len(artifact_types) > 0:
        target_type = artifact_types[0]
    elif first_artifact_type:
        target_type = first_artifact_type

    if target_type:
        view_mode = _ARTIFACT_TYPE_TO_VIEW_MODE.get(target_type)
        if view_mode is not None:
            return view_mode.value

    # 3. 从 metadata 中提取
    if metadata and isinstance(metadata, dict):
        meta_mode = metadata.get("view_mode")
        if meta_mode and isinstance(meta_mode, str) and meta_mode in _VALID_VIEW_MODES:
            return meta_mode

    # 4. 默认降级
    logger.debug(
        "[ViewRouter] 无法确定视图模式，使用默认 text_diff | explicit=%s types=%s first=%s",
        explicit_mode,
        artifact_types,
        first_artifact_type,
    )
    return _DEFAULT_VIEW_MODE.value


def get_artifact_view_hints(
    artifact_type: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取制品视图提示信息。

    供前端在渲染审批界面时使用，包含视图模式和必要的渲染参数提示。

    Args:
        artifact_type: 制品类型
        metadata: 制品元数据

    Returns:
        视图提示字典，包含：
        - view_mode: 推荐的视图模式
        - supports_annotations: 是否支持标注
        - supports_timeline: 是否支持时间轴
    """
    view_mode = _ARTIFACT_TYPE_TO_VIEW_MODE.get(
        artifact_type,
        _DEFAULT_VIEW_MODE,
    )

    hints: dict[str, Any] = {
        "view_mode": view_mode.value,
        "supports_annotations": view_mode
        in (
            ViewMode.IMAGE_ANNOTATION,
            ViewMode.MEDIA_TIMELINE,
        ),
        "supports_timeline": view_mode == ViewMode.MEDIA_TIMELINE,
    }

    # 从元数据补充信息
    if metadata:
        if view_mode == ViewMode.MEDIA_TIMELINE:
            # 媒体时间轴模式需要时长信息
            duration = metadata.get("duration") or metadata.get("duration_seconds")
            if duration is not None:
                hints["duration"] = float(duration)
            media_type = metadata.get("media_type", "video")
            hints["media_type"] = media_type if media_type in ("video", "audio") else "video"

        elif view_mode == ViewMode.IMAGE_ANNOTATION:
            # 图片标注模式需要图片尺寸
            width = metadata.get("width")
            height = metadata.get("height")
            if width is not None and height is not None:
                hints["image_dimensions"] = {"width": int(width), "height": int(height)}

    return hints

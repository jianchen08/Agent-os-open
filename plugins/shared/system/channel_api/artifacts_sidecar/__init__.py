"""批注（Annotation）与制品（Artifact）sidecar 自包含模块。

本包完全自包含：纯内存存储、无 DB、无 LLM、无外部依赖。

包含：
- models: Annotation/AnnotationStatus/AnnotationTarget/Artifact/ArtifactType 数据类
- annotation_service: 批注 CRUD + 状态管理（全局单例）
- artifact_service: 制品 CRUD + 版本追踪（全局单例）

被 routes_artifacts.py 通过 ``from artifacts_sidecar.X import Y`` 引用。
"""

from artifacts_sidecar.models import (
    Annotation,
    AnnotationStatus,
    AnnotationTarget,
    Artifact,
    ArtifactType,
)

__all__ = [
    "Annotation",
    "AnnotationStatus",
    "AnnotationTarget",
    "Artifact",
    "ArtifactType",
]

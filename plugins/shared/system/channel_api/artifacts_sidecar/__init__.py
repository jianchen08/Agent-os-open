"""批注（Annotation）与制品（Artifact）sidecar 兼容薄壳（C2 合流 2026-08-20）。

[0.2 C2] 原三份实现体（models/annotation_service/artifact_service，~590 行，
与 system/artifacts/ 主包仅 import 路径不同）已删除，本包退化为对主包的
薄 re-export——单一事实源见 plugins/shared/system/artifacts/。

兼容范围：`from artifacts_sidecar import Artifact, ...`（包级属性访问）。
子模块级引用（`from artifacts_sidecar.models import ...`）已随重复实现体删除；
渠道内消费方 routes_artifacts.py 已改为直接从主包
``from artifacts.annotation_service / artifacts.artifact_service import ...``。

注意：单例语义随合流统一——主包的 get_artifact_service/get_annotation_service
全局单例在 channel_api sidecar 进程内与 workspace_service 等兄弟消费方共享
（合流前两份独立单例并存于同一进程，属重复实现的不一致，合流即收敛）。
"""

from artifacts.models import (
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

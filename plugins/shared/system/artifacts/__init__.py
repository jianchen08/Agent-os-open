"""制品与批注 sidecar 模块（0.2 P1-3）。

纯内存实现，无 DB / 无 LLM / 无外部依赖，sidecar 直接导入本包（不依赖 src/）。
包名 ``artifacts``，跨 sidecar 导入沿用 ``from artifacts.X`` 风格
（与 workspace/ tasks/ 约定一致）。
"""

from artifacts.models import Annotation, AnnotationTarget, Artifact, ArtifactType

__all__ = [
    "Artifact",
    "ArtifactType",
    "Annotation",
    "AnnotationTarget",
]

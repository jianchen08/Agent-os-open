"""制品与批注 sidecar 模块（0.2 P1-3）。

从 0.1 参考代码 src/artifacts/ 搬迁而来，纯内存实现，无 DB / 无 LLM / 无外部依赖，
使 channel_api sidecar 脱离对 src/ 的导入。包名与 src/artifacts/ 保持一致（``artifacts``），
跨 sidecar 导入沿用 ``from artifacts.X`` 风格（与 workspace/ tasks/ 约定一致）。
"""

from artifacts.models import Annotation, AnnotationTarget, Artifact, ArtifactType

__all__ = [
    "Artifact",
    "ArtifactType",
    "Annotation",
    "AnnotationTarget",
]

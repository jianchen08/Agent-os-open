"""制品与批注模块。

提供制品（Artifact）的结构化管理、版本追踪，
以及批注（Annotation）的创建和管理能力。
"""

from artifacts.models import Annotation, AnnotationTarget, Artifact, ArtifactType

__all__ = [
    "Artifact",
    "ArtifactType",
    "Annotation",
    "AnnotationTarget",
]

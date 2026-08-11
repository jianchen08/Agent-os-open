"""工作空间模块。

提供容器任务级别的工作空间管理，聚合展示文档目录和制品。
"""

from workspace.models import FileTreeNode, Workspace

__all__ = [
    "Workspace",
    "FileTreeNode",
]

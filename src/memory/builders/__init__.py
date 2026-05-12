"""
构建器模块

提供上下文构建器，用于按 layer_order 顺序拼接各层内容
"""

from .context_builder import ContextBuilder

__all__ = [
    "ContextBuilder",
]

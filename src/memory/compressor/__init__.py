"""
上下文压缩器模块

支持分层递进压缩：L0(原文) → L1(八段) → L2(三元组) → L3(关键词)

核心职责：
- 压缩逻辑（L0→L1→L2→L3）
- 压缩数据模型定义
"""

from .config import CompressionConfig, ContextBudget, load_context_window_config
from .models import (
    ChunkMetadata,
    ChunkStatus,
    CompressionReport,
    CompressionResult,
    ContentRef,
)


def __getattr__(name):
    """延迟导入子模块，避免 eager import 触发外部依赖链"""
    _lazy = {
        "ContextCompressor": ".core",
        "normalize_layer_name": ".core",
        "LAYER_NAME_MAP": ".core",
        "LAYER_NAME_MAP_REVERSE": ".core",
    }
    if name in _lazy:
        import importlib
        module = importlib.import_module(_lazy[name], __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CompressionConfig",
    "ContextBudget",
    "load_context_window_config",
    "ContextCompressor",
    "normalize_layer_name",
    "LAYER_NAME_MAP",
    "LAYER_NAME_MAP_REVERSE",
    "ChunkMetadata",
    "ChunkStatus",
    "ContentRef",
    "CompressionResult",
    "CompressionReport",
]

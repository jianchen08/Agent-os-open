"""
上下文压缩器模块

支持分层递进压缩：L0(原文) → L1(八段) → L2(三元组) → L3(关键词)

核心职责：
- 压缩逻辑（L0→L1→L2→L3）
- 读取压缩块和消息

注意：
- 向量检索、加载器、构建器等非核心功能在子模块中提供
- 四层架构组装由上层代码处理
"""

# 直接导入：无外部依赖问题的模块（仅依赖标准库）
from .config import CompressionConfig, ContextBudget, load_context_window_config
from .models import (
    ChunkMetadata,
    ChunkStatus,
    CompressionReport,
    CompressionResult,
    ContentRef,
    MemoryExtraction,
    PreservedZone,
)


def __getattr__(name):
    """延迟导入子模块，避免 eager import 触发外部依赖链"""
    _lazy = {
        # core
        "ContextCompressor": ".core",
        "normalize_layer_name": ".core",
        "LAYER_NAME_MAP": ".core",
        "LAYER_NAME_MAP_REVERSE": ".core",
        # db
        "MemoryChunkDB": ".db",
        # metadata_store
        "ChunkMetadataStore": ".metadata_store",
        # reader
        "ContextReader": ".reader",
        # store
        "LayeredContextStore": ".store",
        "create_layered_store_for_model": ".store",
        # structured
        "StructuredCompressor": ".structured",
        # writer
        "ContextWriter": ".writer",
    }
    if name in _lazy:
        import importlib
        module = importlib.import_module(_lazy[name], __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    # 配置
    "CompressionConfig",
    "ContextBudget",
    "load_context_window_config",

    # 核心压缩
    "ContextCompressor",
    "normalize_layer_name",
    "LAYER_NAME_MAP",
    "LAYER_NAME_MAP_REVERSE",

    # 结构化压缩
    "StructuredCompressor",

    # 分层存储
    "LayeredContextStore",
    "create_layered_store_for_model",

    # 数据库
    "MemoryChunkDB",

    # 新的存取分离架构
    "ChunkMetadata",
    "ChunkStatus",
    "ContentRef",
    "CompressionResult",
    "CompressionReport",
    "ChunkMetadataStore",
    "PreservedZone",
    "MemoryExtraction",
    "ContextWriter",
    "ContextReader",
]

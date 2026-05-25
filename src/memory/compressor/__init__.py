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

from .config import CompressionConfig, ContextBudget, load_context_window_config
from .core import (
    LAYER_NAME_MAP,
    LAYER_NAME_MAP_REVERSE,
    ContextCompressor,
    normalize_layer_name,
)
from .db import MemoryChunkDB
from .metadata_store import ChunkMetadataStore

# 新的存取分离架构
from .models import (
    ChunkMetadata,
    ChunkStatus,
    CompressionReport,
    CompressionResult,
    ContentRef,
    MemoryExtraction,
    PreservedZone,
)
from .reader import ContextReader
from .store import LayeredContextStore, create_layered_store_for_model
from .structured import StructuredCompressor
from .writer import ContextWriter

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

"""
数据模型定义

定义压缩系统使用的数据模型
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ChunkStatus(Enum):
    """块状态"""
    ACTIVE = "active"           # 正常存在
    COMPRESSED = "compressed"   # 已被压缩到下一层
    DISCARDED = "discarded"     # 已被丢弃（遗忘）


@dataclass
class ContentRef:
    """数据库内容引用"""
    table: str                  # "execution_records" 或 "memory_chunks"
    record_id: str              # 主键
    column: str = "content"     # 列名


@dataclass
class ChunkMetadata:
    """块元数据（内存中存储）"""
    chunk_id: str
    session_id: str
    layer: str                  # L0/L1/L2/L3
    token_count: int
    message_count: int          # L0/L1 有效
    created_at: datetime
    content_ref: ContentRef     # 数据库引用
    status: ChunkStatus = ChunkStatus.ACTIVE
    executor_id: str | None = None
    executor_type: str | None = None


@dataclass
class CompressionResult:
    """压缩结果"""
    source_layer: str
    target_layer: str
    source_chunk_ids: list[str]     # 被压缩的 chunk_ids
    new_chunk: ChunkMetadata | None = None  # 新生成的块
    tokens_saved: int = 0


@dataclass
class CompressionReport:
    """压缩报告"""
    iterations: int = 0
    compressed_chunks: list[tuple[str, str]] = field(default_factory=list)  # [(chunk_id, layer), ...]
    new_chunks: list[ChunkMetadata] = field(default_factory=list)
    tokens_saved: int = 0

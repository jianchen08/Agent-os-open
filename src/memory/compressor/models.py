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
    review_status: str = "pending"       # "pending" 或 "reviewed"
    execution_anchors: list[dict] | None = None  # 执行过程锚点
    sequence_start: int | None = None    # 覆盖的 sequence 范围
    sequence_end: int | None = None


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


@dataclass
class PreservedZone:
    """保留区：从完整上下文中重新识别提取的关键信息。

    每轮从完整上下文（旧保留区 + 压缩块 + 当前对话）中重新识别提取，
    内容刷新到最新状态。保留区独立于压缩块存储，避免压缩块拼接时产生冲突。
    """

    user_requirements: str = ""  # 用户原始需求/指令
    key_decisions: str = ""  # 关键决策记录
    execution_plan: str = ""  # 当前执行计划（只保留最新版）
    constraints: str = ""  # 活跃约束条件
    pending_tasks: str = ""  # 未完成任务状态


@dataclass
class MemoryExtraction:
    """长期记忆提取：从对话中提取可长期保存的记忆项。

    有值就填，没值就空，不做去重判断。
    调用方负责将提取结果写入 memory 工具存储。
    """

    user_profile_updates: str = ""  # 写入 memory(tags=["user_profile"])
    project_knowledge_updates: str = ""  # 写入 memory(tags=["project_knowledge"])
    experience_updates: str = ""  # 写入 memory(tags=["experience"])

"""记忆模块。

提供记忆存储、检索和上下文管理功能。
从旧代码 src/memory/ 搬迁而来，移除 ORM 硬依赖，
改为 dataclass + 接口注入 + JSON 文件存储（MVP 默认）。

核心组件：
- types: 数据模型（dataclass）
- ports: 存储和检索接口
- service: 记忆服务门面
- episode_service: 情景记忆服务
- knowledge_service: 知识服务
- tag_network: Tag 网络检索
- context_compressor: 上下文压缩器
- memory_context_service: 记忆上下文服务
- history_buffer: 对话历史缓冲区
- variable_priority: 变量优先级
- maintenance: 记忆维护服务（过期清理、相似合并等）
- storage: 存储后端（JSON / pgvector）
- plugins: 管道插件
"""

from memory.types import (
    ChunkData,
    Context,
    ContextRequest,
    ContextType,
    CooccurrenceEntry,
    Episode,
    InjectType,
    Knowledge,
    MemoryType,
    RetrievalConfig,
    RetrievalMethod,
    SearchResult,
    TagBoostResult,
    TagInfo,
    ToolInfo,
)
from memory.ports import (
    IEpisodeStorage,
    IMemoryStore,
    IRetriever,
    ISemanticStorage,
    EpisodeNotFoundError,
    KnowledgeNotFoundError,
    StorageConnectionError,
    StorageError,
)
from memory.service import MemoryService
from memory.episode_service import EpisodeService
from memory.knowledge_service import KnowledgeService
from memory.tag_network import (
    TagCooccurrenceMatrix,
    TagNetworkConfig,
    TagNetworkRetriever,
)
from memory.context_compressor import (
    CompressionConfig,
    ContextCompressor,
)
from memory.memory_context_service import MemoryContextService
from memory.history_buffer import (
    ConversationHistory,
    HistoryBuffer,
    MessageEntry,
)
from memory.variable_priority import VariablePriority
from memory.maintenance import MemoryMaintenanceService

__all__ = [
    # types
    "ChunkData",
    "Episode",
    "Knowledge",
    "ToolInfo",
    "ContextRequest",
    "Context",
    "MemoryType",
    "ContextType",
    "InjectType",
    "RetrievalMethod",
    "SearchResult",
    "RetrievalConfig",
    "TagInfo",
    "CooccurrenceEntry",
    "TagBoostResult",
    # ports
    "IMemoryStore",
    "IRetriever",
    "IEpisodeStorage",
    "ISemanticStorage",
    "StorageError",
    "EpisodeNotFoundError",
    "KnowledgeNotFoundError",
    "StorageConnectionError",
    # services
    "MemoryService",
    "EpisodeService",
    "KnowledgeService",
    "MemoryContextService",
    # maintenance
    "MemoryMaintenanceService",
    # tag network
    "TagNetworkConfig",
    "TagCooccurrenceMatrix",
    "TagNetworkRetriever",
    # compressor
    "CompressionConfig",
    "ContextCompressor",
    # history
    "HistoryBuffer",
    "ConversationHistory",
    "MessageEntry",
    # priority
    "VariablePriority",
]

"""记忆模块常量定义。

从旧代码 src/memory/constants.py 搬迁，保持常量值不变。

暴露接口：
- TokenBudget: Token 预算管理常量
- Retrieval: 记忆检索相关常量
- MemoryTypeConst: 记忆类型常量
- Compression: 记忆压缩相关常量
- ContextManagement: 上下文管理相关常量
- Storage: 记忆存储相关常量
- Similarity: 相似度计算常量
- Priority: 记忆优先级常量
- Lifecycle: 记忆生命周期常量
- ErrorMessages: 错误消息常量
- VectorDB: 向量数据库常量
- ImportExport: 导入导出常量
"""

from __future__ import annotations


class TokenBudget:
    """Token 预算管理常量。"""

    CRITICAL_THRESHOLD = 0.90
    WARNING_THRESHOLD = 0.80
    DEFAULT_BUDGET = 100000


class Retrieval:
    """记忆检索相关常量。"""

    MIN_SCORE = 0.50
    SCORE_THRESHOLD = 0.80
    DEFAULT_TOP_K = 10
    MAX_RESULTS = 100
    NORM_EPSILON = 1e-8


class MemoryTypeConst:
    """记忆类型常量。"""

    EPISODE = "episode"
    SEMANTIC = "semantic"
    WORKING = "working"
    PROCEDURAL = "procedural"


class Compression:
    """记忆压缩相关常量。"""

    OPTIMAL_THRESHOLD = 0.50
    DEFAULT_COMPRESSOR = "ratio"
    MIN_COMPRESS_SIZE = 1000


class ContextManagement:
    """上下文管理相关常量。"""

    DEFAULT_MAX_TOKENS = 128000
    MIN_TOKENS = 1000
    KEEP_RECENT_RATIO = 0.7
    KEEP_IMPORTANT_RATIO = 0.3


class Storage:
    """记忆存储相关常量。"""

    DEFAULT_BATCH_SIZE = 100
    MAX_BATCH_SIZE = 1000
    DEFAULT_CACHE_SIZE = 1000
    CACHE_TTL = 3600


class Similarity:
    """相似度计算常量。"""

    MIN_COSINE = 0.0
    MAX_COSINE = 1.0
    DEFAULT_THRESHOLD = 0.5
    DEFAULT_EMBEDDING_DIM = 1536


class Priority:
    """记忆优先级常量。"""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4
    DEFAULT = MEDIUM


class Lifecycle:
    """记忆生命周期常量。"""

    EPISODE_RETENTION = 30 * 24 * 3600
    SEMANTIC_RETENTION = 365 * 24 * 3600
    WORKING_RETENTION = 1 * 24 * 3600
    ACCESS_DECAY = 7


class ErrorMessages:
    """记忆模块错误消息常量。"""

    MEMORY_NOT_FOUND = "记忆不存在"
    STORAGE_ERROR = "存储失败"
    RETRIEVAL_ERROR = "检索失败"
    COMPRESSION_ERROR = "压缩失败"
    INVALID_QUERY = "无效的查询"
    BUDGET_EXCEEDED = "Token预算已超限"


class VectorDB:
    """向量数据库相关常量。"""

    INDEX_TYPE = "HNSW"
    M_PARAMETER = 16
    EF_CONSTRUCTION = 200
    DEFAULT_EF = 50


class ImportExport:
    """记忆导入导出常量。"""

    JSON_FORMAT = "json"
    CSV_FORMAT = "csv"
    MAX_IMPORT_SIZE = 10 * 1024 * 1024
    MAX_EXPORT_SIZE = 50 * 1024 * 1024


class HistoryConfig:
    """对话历史缓冲区配置常量。

    默认容量从 1000 优化为 100，减少多会话并发时的内存压力。
    所有默认值均可通过环境变量覆盖。

    环境变量:
        HISTORY_BUFFER_MAX_SIZE: HistoryBuffer 最大消息数
        HISTORY_BUFFER_MAX_MESSAGES: ConversationHistory 最大消息数
        HISTORY_BUFFER_MAX_TOKENS: ConversationHistory 最大 token 数
    """

    DEFAULT_MAX_SIZE: int = 100
    DEFAULT_MAX_MESSAGES: int = 100
    DEFAULT_MAX_TOKENS: int = 128000

    ENV_KEY_MAX_SIZE: str = "HISTORY_BUFFER_MAX_SIZE"
    ENV_KEY_MAX_MESSAGES: str = "HISTORY_BUFFER_MAX_MESSAGES"
    ENV_KEY_MAX_TOKENS: str = "HISTORY_BUFFER_MAX_TOKENS"

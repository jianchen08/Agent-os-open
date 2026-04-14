"""
核心常量定义

暴露接口：
- to_int(cls, priority: str) -> int：to_int功能
- to_str(cls, priority: int) -> str：to_str功能
- MessageType：MessageType类
- RecordStatus：RecordStatus类
- TokenThresholds：TokenThresholds类
- Pagination：Pagination类
- Timeout：Timeout类
- Retry：Retry类
- CostControl：CostControl类
- Evaluation：Evaluation类
- WebSocketEvent：WebSocketEvent类
- HTTPStatus：HTTPStatus类
- QueryLimits：QueryLimits类
- FileSystem：FileSystem类
- LogLevel：LogLevel类
- ToolLevelReference：ToolLevelReference类
- ToolLimits：ToolLimits类
- TaskPriority：TaskPriority类
"""

# =============================================================================
# 应用程序常量
# =============================================================================

APPLICATION_NAME = "AI Agent System"
DEFAULT_AGENT_NAME = "执行规划专家"  # 默认AI助手名称
FALLBACK_AGENT_NAME = "回滚管理助手"  # 备用助手名称

# =============================================================================
# 会话和线程常量
# =============================================================================

DEFAULT_SESSION_TITLE = "新会话"
DEFAULT_THREAD_STATE = "idle"

# =============================================================================
# 消息类型常量
# =============================================================================


class MessageType:
    """消息类型常量"""

    USER_INPUT = "user_input"
    AI_RESPONSE = "ai_response"
    AGENT_THINK = "agent_think"
    ASSISTANT = "assistant"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    SYSTEM = "system"
    UNKNOWN = "unknown"


# 可重试的AI消息类型
RETRYABLE_MESSAGE_TYPES = [
    MessageType.AI_RESPONSE,
    MessageType.AGENT_THINK,
    MessageType.ASSISTANT,
    MessageType.LLM_RESPONSE,
]

# =============================================================================
# 记录状态常量
# =============================================================================


class RecordStatus:
    """执行记录状态常量"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ERROR = "error"
    UNKNOWN = "unknown"


# =============================================================================
# Token 和内存管理常量
# =============================================================================


class TokenThresholds:
    """Token 预算阈值常量"""

    CRITICAL = 0.90  # 90% - 严重阈值，触发立即清理
    WARNING = 0.80  # 80% - 警告阈值
    COMPRESS = 0.50  # 50% - 压缩触发阈值
    MIN_SCORE = 0.50  # 最小相关得分
    RETRIEVAL_SCORE_THRESHOLD = 0.80  # 检索得分阈值（用于过滤低质量结果）


# =============================================================================
# 分页常量
# =============================================================================


class Pagination:
    """分页相关常量"""

    DEFAULT_PAGE = 1
    DEFAULT_PAGE_SIZE = 20
    MIN_PAGE_SIZE = 1
    MAX_PAGE_SIZE_SMALL = 100  # 小列表最大值
    MAX_PAGE_SIZE_LARGE = 500  # 大列表最大值
    DEFAULT_SKIP = 0
    DEFAULT_LIMIT = 100


# =============================================================================
# 超时和重试常量
# =============================================================================


class Timeout:
    """超时相关常量（秒）"""

    HEALTH_CHECK = 5
    SERVICE_STARTUP = 5
    PROCESS_WAIT = 10
    API_REQUEST = 30.0
    MESSAGE_READY_WAIT = 1.0
    DEFAULT_RETRY_DELAY = 1.0
    DEFAULT_AGENT_TIMEOUT = 300  # 默认Agent超时（5分钟）
    BASH_TOOL_DEFAULT = 60  # Bash工具默认超时
    WEB_TOOL_DEFAULT = 30  # Web工具默认超时
    API_EVALUATOR_DEFAULT = 30  # API评估器默认超时
    TRIGGER_ACTION_DEFAULT = 30  # 触发器动作默认超时
    TASK_SUBMIT_DEFAULT = 300  # 任务提交默认超时
    POLLING_TIMEOUT = 300  # 轮询超时
    TEST_RUNNER_DEFAULT = 300  # 测试运行器默认超时


class Retry:
    """重试相关常量"""

    MAX_RETRIES = 3
    DEFAULT_DELAY = 1.0  # 秒


# =============================================================================
# 成本控制常量
# =============================================================================


class CostControl:
    """成本控制相关常量"""

    WARNING_THRESHOLD = 0.80  # 80% - 警告阈值
    CRITICAL_THRESHOLD = 0.90  # 90% - 严重阈值
    EXHAUSTED_THRESHOLD = 1.0  # 100% - 耗尽阈值
    DAILY_TOKEN_LIMIT = 10**12  # 每日Token限制（无限制）
    MONTHLY_TOKEN_LIMIT = 10**15  # 每月Token限制（无限制）


# =============================================================================
# 版本控制常量
# =============================================================================

DEFAULT_VERSION = "1.0"
DEFAULT_WORKFLOW_VERSION = "1.0.0"

# =============================================================================
# 工作流评估常量
# =============================================================================


class Evaluation:
    """工作流评估相关常量"""

    MIN_SCORE = 0.0
    MAX_SCORE = 100.0
    PASS_THRESHOLD = 70.0  # 通过阈值（百分制）
    DEFAULT_WEIGHT = 1.0
    PROGRESS_MIN = 0.0
    PROGRESS_MAX = 1.0
    THRESHOLD_70_PERCENT = 0.7  # 70% 阈值（用于评估）
    THRESHOLD_50_PERCENT = 0.5  # 50% 阈值（用于评估）
    DEFAULT_THRESHOLD = 60.0  # 默认阈值（百分制）


# =============================================================================
# WebSocket 事件类型常量
# =============================================================================


class WebSocketEvent:
    """WebSocket 事件类型常量"""

    MESSAGE_DELETED = "message_deleted"
    STREAM_START = "stream_start"
    STREAM_CHUNK = "stream_chunk"
    STREAM_END = "stream_end"
    MESSAGE_UPDATE = "message_update"


# =============================================================================
# HTTP 状态码常量
# =============================================================================


class HTTPStatus:
    """HTTP 状态码常量（补充 fastapi.status）"""

    OK = 200
    CREATED = 201
    BAD_REQUEST = 400
    NOT_FOUND = 404
    INTERNAL_SERVER_ERROR = 500


# =============================================================================
# 数据库查询常量
# =============================================================================


class QueryLimits:
    """数据库查询限制常量"""

    DEFAULT_SAMPLE_LIMIT = 5  # 默认采样数量
    MESSAGE_SEARCH_LIMIT = 10  # 消息搜索限制
    EPISODE_SEARCH_LIMIT = 10  # 情景记忆搜索限制
    AUDIT_QUERY_LIMIT = 1000  # 审计日志查询限制
    NOTIFICATION_QUERY_LIMIT = 1000  # 通知查询限制
    SSE_NOTIFICATION_LIMIT = 10  # SSE通知限制
    CONTEXT_SAMPLE_SMALL = 3  # 小样本数量
    CONTEXT_SAMPLE_MEDIUM = 5  # 中样本数量
    CONTEXT_SAMPLE_LARGE = 10  # 大样本数量
    CONTEXT_SAMPLE_MINIMAL = 2  # 最小样本数量


# =============================================================================
# 文件系统常量
# =============================================================================


class FileSystem:
    """文件系统相关常量"""

    LOG_DIR = "logs"
    CONFIG_DIR = "config"
    TEMP_DIR = "temp"


# =============================================================================
# 日志常量
# =============================================================================


class LogLevel:
    """日志级别常量"""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# =============================================================================
# 工具级别常量（参考）
# =============================================================================
# 注意：实际的工具级别应该使用枚举类 src.tools.base.ToolLevel


class ToolLevelReference:
    """工具级别参考常量（用于文档参考）"""

    SYSTEM = "SYSTEM"
    USER = "USER"
    BUILTIN = "BUILTIN"
    CUSTOM = "CUSTOM"


# =============================================================================
# 工具相关常量
# =============================================================================


class ToolLimits:
    """工具限制常量"""

    MEMORY_SEARCH_DEFAULT = 10  # 记忆搜索默认限制
    MEMORY_VIEW_DEFAULT = 20  # 记忆查看默认限制
    TASK_LIST_DEFAULT = 50  # 任务列表默认限制
    RESOURCE_SEARCH_DEFAULT = 20  # 资源搜索默认限制
    WEB_SEARCH_MULTIPLIER = 2  # Web搜索结果倍数（用于去重）
    MAX_RECENT_TURNS_MULTIPLIER = 2  # 最大最近轮次倍数


# =============================================================================
# 任务优先级常量
# =============================================================================


class TaskPriority:
    """任务优先级常量"""

    # 字符串到整数的映射
    LOW = 1
    MEDIUM = 5
    HIGH = 9

    # 映射字典
    STR_TO_INT = {"low": LOW, "medium": MEDIUM, "high": HIGH}
    INT_TO_STR = {LOW: "low", MEDIUM: "medium", HIGH: "high"}

    @classmethod
    def to_int(cls, priority: str) -> int:
        """将字符串优先级转换为整数"""
        return cls.STR_TO_INT.get(priority.lower(), cls.MEDIUM)

    @classmethod
    def to_str(cls, priority: int) -> str:
        """将整数优先级转换为字符串"""
        return cls.INT_TO_STR.get(priority, "medium")

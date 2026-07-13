"""
核心常量定义

暴露接口：
- to_int(cls, priority: str) -> int：to_int功能
- to_str(cls, priority: int) -> str：to_str功能
- Timeout：Timeout类
- Retry：Retry类
- CostControl：CostControl类
- Evaluation：Evaluation类
- QueryLimits：QueryLimits类
- ToolLimits：ToolLimits类
- TaskPriority：TaskPriority类
"""

# =============================================================================
# 应用程序常量
# =============================================================================

APPLICATION_NAME = "AI Agent System"
DEFAULT_AGENT_NAME = "执行规划专家"
FALLBACK_AGENT_NAME = "回滚管理助手"

# =============================================================================
# 会话和线程常量
# =============================================================================

DEFAULT_SESSION_TITLE = "新会话"
DEFAULT_THREAD_STATE = "idle"

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
    DEFAULT_AGENT_TIMEOUT = 300
    BASH_TOOL_DEFAULT = 60
    WEB_TOOL_DEFAULT = 30
    API_EVALUATOR_DEFAULT = 30
    TRIGGER_ACTION_DEFAULT = 30
    TASK_SUBMIT_DEFAULT = 300
    POLLING_TIMEOUT = 300
    TEST_RUNNER_DEFAULT = 300


class Retry:
    """重试相关常量"""

    MAX_RETRIES = 3
    DEFAULT_DELAY = 1.0


# =============================================================================
# 成本控制常量
# =============================================================================


class CostControl:
    """成本控制相关常量"""

    WARNING_THRESHOLD = 0.80
    CRITICAL_THRESHOLD = 0.90
    EXHAUSTED_THRESHOLD = 1.0
    DAILY_TOKEN_LIMIT = 10**12
    MONTHLY_TOKEN_LIMIT = 10**15


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
    PASS_THRESHOLD = 70.0
    DEFAULT_WEIGHT = 1.0
    PROGRESS_MIN = 0.0
    PROGRESS_MAX = 1.0
    THRESHOLD_70_PERCENT = 0.7
    THRESHOLD_50_PERCENT = 0.5
    DEFAULT_THRESHOLD = 60.0


# =============================================================================
# 数据库查询常量
# =============================================================================


class QueryLimits:
    """数据库查询限制常量"""

    DEFAULT_SAMPLE_LIMIT = 5
    MESSAGE_SEARCH_LIMIT = 10
    EPISODE_SEARCH_LIMIT = 10
    AUDIT_QUERY_LIMIT = 1000
    NOTIFICATION_QUERY_LIMIT = 1000
    SSE_NOTIFICATION_LIMIT = 10
    CONTEXT_SAMPLE_SMALL = 3
    CONTEXT_SAMPLE_MEDIUM = 5
    CONTEXT_SAMPLE_LARGE = 10
    CONTEXT_SAMPLE_MINIMAL = 2


# =============================================================================
# 工具相关常量
# =============================================================================


class ToolLimits:
    """工具限制常量"""

    MEMORY_SEARCH_DEFAULT = 10
    MEMORY_VIEW_DEFAULT = 20
    TASK_LIST_DEFAULT = 50
    RESOURCE_SEARCH_DEFAULT = 20
    WEB_SEARCH_MULTIPLIER = 2
    MAX_RECENT_TURNS_MULTIPLIER = 2


# =============================================================================
# 任务优先级常量
# =============================================================================


class TaskPriority:
    """任务优先级常量"""

    LOW = 1
    MEDIUM = 5
    HIGH = 9

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

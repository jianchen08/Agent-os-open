"""
API 模块常量定义

集中管理 API 路由、响应、验证等相关的常量。
"""

# =============================================================================
# 分页常量
# =============================================================================


class Pagination:
    """API 分页常量"""

    DEFAULT_PAGE = 1
    DEFAULT_PAGE_SIZE = 20
    MIN_PAGE_SIZE = 1
    MAX_PAGE_SIZE = 100
    MAX_PAGE_SIZE_EXTENDED = 500  # 用于大型列表
    DEFAULT_SKIP = 0
    DEFAULT_LIMIT = 100
    MIN_LIMIT = 1
    MAX_LIMIT = 500


# =============================================================================
# 线程/会话相关常量
# =============================================================================


class Thread:
    """线程相关常量"""

    DEFAULT_STATE = "idle"
    DEFAULT_TITLE = "新会话"
    STATUS_ACTIVE = "active"
    STATUS_ARCHIVED = "archived"


# =============================================================================
# 消息相关常量
# =============================================================================


class Message:
    """消息相关常量"""

    DEFAULT_TYPE = "unknown"
    SERIALIZATION_FAILURE_CONTENT = "[序列化失败]"
    REGENERATION_ERROR_MESSAGE = "抱歉，无法重新生成：未找到对话上下文"
    MAX_CONTENT_LENGTH = 100000  # 最大消息内容长度


# =============================================================================
# WebSocket 相关常量
# =============================================================================


class WebSocket:
    """WebSocket 相关常量"""

    # 消息类型
    MESSAGE_DELETED = "message_deleted"
    STREAM_START = "stream_start"
    STREAM_CHUNK = "stream_chunk"
    STREAM_END = "stream_end"
    MESSAGE_UPDATE = "message_update"

    # 超时
    CONNECTION_TIMEOUT = 30  # 连接超时（秒）
    HEARTBEAT_INTERVAL = 30  # 心跳间隔（秒）
    MESSAGE_TIMEOUT = 60  # 消息超时（秒）


# =============================================================================
# 响应格式常量
# =============================================================================


class ResponseFormat:
    """API 响应格式常量"""

    # 成功响应
    SUCCESS_FIELD = "success"
    MESSAGE_FIELD = "message"
    DATA_FIELD = "data"

    # 列表响应
    ITEMS_FIELD = "items"  # 或 "threads", "messages" 等
    TOTAL_FIELD = "total"
    PAGE_FIELD = "page"
    PAGE_SIZE_FIELD = "page_size"

    # 时间格式
    DATETIME_FORMAT = "iso"  # ISO 8601 格式


# =============================================================================
# 错误消息常量
# =============================================================================


class ErrorMessages:
    """API 错误消息常量"""

    # 通用错误
    INTERNAL_ERROR = "内部服务器错误"
    INVALID_REQUEST = "无效的请求"
    NOT_FOUND = "资源不存在"
    UNAUTHORIZED = "未授权"
    FORBIDDEN = "禁止访问"

    # 线程相关
    THREAD_NOT_FOUND = "线程不存在"
    THREAD_CREATE_FAILED = "创建线程失败"
    THREAD_UPDATE_FAILED = "更新线程失败"
    THREAD_DELETE_FAILED = "删除线程失败"

    # 消息相关
    MESSAGE_NOT_FOUND = "消息不存在"
    MESSAGE_DELETE_FAILED = "删除消息失败"
    MESSAGE_EDIT_FAILED = "编辑消息失败"
    MESSAGE_RETRY_FAILED = "重试消息失败"
    MESSAGE_VERSIONS_FAILED = "获取消息版本失败"
    MESSAGE_NOT_READY = "消息正在保存中，请稍后重试"
    EMPTY_CONTENT = "消息内容不能为空"

    # 记录相关
    RECORD_NOT_FOUND = "记录不存在"
    RECORD_CHILDREN_FAILED = "获取子节点记录失败"
    TOOL_CALL_FAILED = "获取工具调用失败"

    # Agent 相关
    AGENT_NOT_FOUND = "Agent不存在"
    INVALID_AGENT_ID = "无效的 Agent ID 格式"
    AGENT_UPDATE_FAILED = "更新会话 Agent 失败"


# =============================================================================
# HTTP 状态码常量
# =============================================================================


class HTTPCode:
    """HTTP 状态码常量"""

    OK = 200
    CREATED = 201
    NO_CONTENT = 204
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    METHOD_NOT_ALLOWED = 405
    CONFLICT = 409
    INTERNAL_SERVER_ERROR = 500
    SERVICE_UNAVAILABLE = 503


# =============================================================================
# 请求验证常量
# =============================================================================


class Validation:
    """请求验证常量"""

    # ID 格式
    UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

    # 字符串长度
    MIN_TITLE_LENGTH = 1
    MAX_TITLE_LENGTH = 200
    MIN_CONTENT_LENGTH = 1
    MAX_CONTENT_LENGTH = 100000

    # 数值范围
    MIN_PAGE = 1
    MAX_PAGE_SIZE = 1000


# =============================================================================
# API 路由前缀常量
# =============================================================================


class RoutePrefix:
    """API 路由前缀常量"""

    API_V1 = "/api/v1"
    THREADS = "/threads"
    MESSAGES = "/messages"
    AGENTS = "/agents"
    TOOLS = "/tools"
    WORKFLOWS = "/workflows"
    TASKS = "/tasks"
    MEMORY = "/memory"


# =============================================================================
# 查询参数常量
# =============================================================================


class QueryParams:
    """查询参数常量"""

    PAGE = "page"
    PAGE_SIZE = "page_size"
    SKIP = "skip"
    LIMIT = "limit"
    SORT = "sort"
    ORDER = "order"
    FILTER = "filter"
    SEARCH = "search"
    AGENT_ID = "agent_id"


# =============================================================================
# 版本管理常量
# =============================================================================


class Versioning:
    """消息版本管理常量"""

    # 版本字段
    IS_CURRENT = "is_current"
    IS_HISTORY = "is_history"
    IS_DELETED = "is_deleted"
    VERSION = "version"
    HAS_HISTORY = "has_history"

    # 默认值
    INITIAL_VERSION = 1


# =============================================================================
# 缓存常量
# =============================================================================


class Cache:
    """API 缓存常量"""

    # 缓存时间（秒）
    SHORT_TTL = 60  # 1分钟
    MEDIUM_TTL = 300  # 5分钟
    LONG_TTL = 3600  # 1小时

    # 缓存键前缀
    THREAD_LIST_PREFIX = "thread_list"
    MESSAGE_CACHE_PREFIX = "message_cache"
    AGENT_CACHE_PREFIX = "agent_cache"


# =============================================================================
# 流式传输常量
# =============================================================================


class Streaming:
    """流式传输相关常量"""

    # 流式事件类型
    STREAM_START = "stream_start"
    STREAM_CHUNK = "stream_chunk"
    STREAM_END = "stream_end"
    STREAM_ERROR = "stream_error"

    # 流式传输配置
    CHUNK_SIZE = 1024  # 默认块大小（字节）
    MAX_BUFFER_SIZE = 10240  # 最大缓冲区大小（字节）


# =============================================================================
# 日志消息常量
# =============================================================================


class LogMessages:
    """API 日志消息常量"""

    # 线程操作
    THREAD_LIST_QUERY = "开始查询线程列表"
    THREAD_CREATE = "创建线程"
    THREAD_UPDATE = "更新线程"
    THREAD_DELETE = "删除线程"
    THREAD_NOT_FOUND = "线程不存在"

    # 消息操作
    MESSAGE_DELETE = "删除消息"
    MESSAGE_RETRY = "重试消息"
    MESSAGE_EDIT = "编辑消息"
    MESSAGE_VERSIONS = "获取消息版本"
    MESSAGE_NOT_FOUND = "消息不存在"

    # 查询操作
    QUERY_START = "开始查询"
    QUERY_COMPLETE = "查询完成"
    QUERY_ERROR = "查询失败"

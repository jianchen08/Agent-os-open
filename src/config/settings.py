"""
统一配置管理

提供项目的统一配置管理，支持环境变量覆盖
"""

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 已知不安全的 JWT secret 占位符（代码默认值 + 常见占位串）
_INSECURE_JWT_SECRETS = frozenset({
    "dev-insecure-key-do-not-use-in-production",
    "development-secret-key-change-in-production",
    "change-me",
    "your-secret-key",
})


class Settings(BaseSettings):
    """项目配置"""

    # 服务器配置
    api_host: str = Field(default="localhost", validation_alias="API_HOST")
    api_port: int = Field(default=8988, validation_alias="API_PORT")
    frontend_port: int = Field(default=5188, validation_alias="FRONTEND_PORT")

    # 应用配置
    debug: bool = Field(default=False, validation_alias="APP_DEBUG")
    environment: str = Field(default="development", validation_alias="APP_ENVIRONMENT")

    # 数据库配置
    database_url: str = Field(
        default="postgresql+asyncpg://user:password@localhost/agent_db",
        validation_alias=AliasChoices("DATABASE_URL", "APP_DATABASE_URL"),
    )

    # 数据库连接池配置
    db_pool_size: int = Field(default=20, validation_alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=30, validation_alias="DB_MAX_OVERFLOW")
    db_pool_timeout: int = Field(default=60, validation_alias="DB_POOL_TIMEOUT")
    db_pool_recycle: int = Field(default=3600, validation_alias="DB_POOL_RECYCLE")

    # Redis 配置
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="REDIS_URL")
    redis_db: int = Field(default=0, validation_alias="REDIS_DB")
    redis_pool_size: int = Field(default=10, validation_alias="REDIS_POOL_SIZE")
    redis_decode_responses: bool = Field(default=True, validation_alias="REDIS_DECODE_RESPONSES")

    # API 密钥
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    zhipu_api_key: str | None = Field(default=None, validation_alias="APP_ZHIPU_API_KEY")

    # 日志配置
    log_level: str = Field(default="INFO", validation_alias="APP_LOG_LEVEL")
    log_format: str = Field(default="text", validation_alias="APP_LOG_FORMAT")

    # 数据库日志配置
    db_echo: bool = Field(default=False, validation_alias="APP_DB_ECHO")

    # JWT 配置
    jwt_secret_key: str = Field(
        default="dev-insecure-key-do-not-use-in-production",
        validation_alias="APP_JWT_SECRET_KEY",
    )
    jwt_algorithm: str = Field(default="HS256", validation_alias="APP_JWT_ALGORITHM")
    jwt_expire_minutes: int = Field(default=30, validation_alias="JWT_EXPIRE_MINUTES")
    access_token_expire_minutes: int = Field(default=30, validation_alias="APP_ACCESS_TOKEN_EXPIRE_MINUTES")
    refresh_token_expire_days: int = Field(default=7, validation_alias="APP_REFRESH_TOKEN_EXPIRE_DAYS")

    # 是否开放公开注册（默认关闭，需管理员预创建账号或显式开启）
    allow_public_register: bool = Field(default=False, validation_alias="APP_ALLOW_PUBLIC_REGISTER")

    # 文件上传配置
    max_file_size: int = Field(default=10 * 1024 * 1024, validation_alias="MAX_FILE_SIZE")  # 10MB
    upload_dir: str = Field(default="uploads", validation_alias="UPLOAD_DIR")

    # 配置目录
    config_dir: str = Field(default="config", validation_alias="CONFIG_DIR")

    # 注入 LLM 的当前时间所用时区（IANA 时区名，如 Asia/Shanghai、UTC、Asia/Tokyo）
    # 传给 LLM 的时间会带上时区标注，格式如：2026-07-02 11:24:00 (UTC+8, Asia/Shanghai)
    timezone: str = Field(default="Asia/Shanghai", validation_alias="APP_TIMEZONE")

    # WebSocket 配置
    ws_heartbeat_interval: int = Field(default=30, validation_alias="WS_HEARTBEAT_INTERVAL")
    ws_max_connections: int = Field(default=1000, validation_alias="WS_MAX_CONNECTIONS")

    # 任务配置
    max_concurrent_tasks: int = Field(default=10, validation_alias="MAX_CONCURRENT_TASKS")
    task_timeout: int = Field(default=300, validation_alias="TASK_TIMEOUT")  # 5分钟
    ac_max_retries: int = Field(default=5, validation_alias="AC_MAX_RETRIES")  # AC 最大重试次数
    task_max_retries: int = Field(default=6, validation_alias="TASK_MAX_RETRIES")  # 任务最大重试次数

    # 任务存储配置
    task_storage_type: str = Field(
        default="database", validation_alias="TASK_STORAGE_TYPE"
    )  # 存储类型: file | database
    task_storage_path: str = Field(
        default="data/tasks", validation_alias="TASK_STORAGE_PATH"
    )  # 文件存储路径（仅 file 类型使用）

    # 事件总线配置
    event_bus_type: str = Field(
        default="memory", validation_alias="EVENT_BUS_TYPE"
    )  # 事件总线类型: memory | redis_streams

    # LLM 并发控制配置（按提供商）
    llm_max_concurrent: int = Field(
        default=2, validation_alias="LLM_MAX_CONCURRENT"
    )  # LLM API 最大并发数（向后兼容，已弃用）
    llm_rate_limit_per_minute: int = Field(
        default=60, validation_alias="LLM_RATE_LIMIT_PER_MINUTE"
    )  # LLM API 每分钟最大请求数（已弃用）

    # 各提供商的并发配置
    llm_zhipu_max_concurrent: int = Field(default=2, validation_alias="LLM_ZHIPU_MAX_CONCURRENT")  # 智谱 AI 最大并发数
    llm_openai_max_concurrent: int = Field(
        default=10, validation_alias="LLM_OPENAI_MAX_CONCURRENT"
    )  # OpenAI 最大并发数
    llm_anthropic_max_concurrent: int = Field(
        default=5, validation_alias="LLM_ANTHROPIC_MAX_CONCURRENT"
    )  # Anthropic 最大并发数
    llm_default_max_concurrent: int = Field(
        default=2, validation_alias="LLM_DEFAULT_MAX_CONCURRENT"
    )  # 默认最大并发数（未配置的提供商使用）

    # 性能优化配置
    enable_query_cache: bool = Field(default=True, validation_alias="ENABLE_QUERY_CACHE")
    query_cache_ttl: int = Field(default=300, validation_alias="QUERY_CACHE_TTL")  # 查询缓存5分钟
    enable_redis_cache: bool = Field(default=True, validation_alias="ENABLE_REDIS_CACHE")

    # API性能配置
    api_response_cache_ttl: int = Field(default=60, validation_alias="API_RESPONSE_CACHE_TTL")  # API响应缓存1分钟
    enable_compression: bool = Field(default=True, validation_alias="ENABLE_COMPRESSION")  # 启用响应压缩

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # 忽略额外字段
    )

    @model_validator(mode="after")
    def _validate_jwt_secret_strength(self) -> "Settings":
        """非 development 环境强制校验 JWT secret 强度（fail-closed）。

        生产/预发环境若仍使用代码默认值或已知占位串或过短的 secret，
        直接拒启动，防止攻击者伪造任意用户 token。
        """
        if self.environment == "development":
            return self
        secret = self.jwt_secret_key
        if secret in _INSECURE_JWT_SECRETS or len(secret) < 32:
            raise ValueError(
                "JWT secret 不安全：非 development 环境（当前 "
                f"{self.environment!r}）必须配置强随机 secret（>=32 字节、"
                "非占位符）。请运行 `openssl rand -hex 32` 生成并写入"
                " APP_JWT_SECRET_KEY 环境变量后重启。"
            )
        return self

    @property
    def api_base_url(self) -> str:
        """API 基础 URL"""
        return f"http://{self.api_host}:{self.api_port}"

    @property
    def frontend_base_url(self) -> str:
        """前端基础 URL"""
        return f"http://{self.api_host}:{self.frontend_port}"

    @property
    def ws_base_url(self) -> str:
        """WebSocket 基础 URL"""
        return f"ws://{self.api_host}:{self.api_port}/ws"


# 全局配置实例
settings = Settings()


def get_settings() -> Settings:
    """获取配置实例"""
    return settings


def reset_settings() -> None:
    """重置配置实例（主要用于测试）"""
    global settings  # noqa: PLW0603
    settings = Settings()


def get_api_base_url() -> str:
    """获取 API 基础 URL"""
    return settings.api_base_url


def get_frontend_base_url() -> str:
    """获取前端基础 URL"""
    return settings.frontend_base_url


def get_ws_base_url() -> str:
    """获取 WebSocket 基础 URL"""
    return settings.ws_base_url

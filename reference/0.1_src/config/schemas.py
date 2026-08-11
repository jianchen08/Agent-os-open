"""
配置数据模型定义
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class MultimodalCapabilityConfig(BaseModel):
    """模型多模态能力配置（对应 llm.yaml 的 multimodal 子节点）"""

    supports_image: bool = False
    supports_audio: bool = False
    supports_video: bool = False
    supports_document: bool = False
    supported_image_types: list[str] = Field(default_factory=list)
    supported_audio_types: list[str] = Field(default_factory=list)
    supported_video_types: list[str] = Field(default_factory=list)
    max_image_size: int = 20 * 1024 * 1024
    max_audio_size: int = 25 * 1024 * 1024
    max_video_size: int = 100 * 1024 * 1024
    max_document_size: int = 10 * 1024 * 1024


class ModelConfig(BaseModel):
    """单个模型配置"""

    provider: str  # openai/anthropic/ollama/openai_compatible
    model_name: str  # 实际 API 模型名
    display_name: str  # 显示名称
    api_base: str | None = None  # 自定义端点
    api_key: str | None = None  # 模型级别 API 密钥（可选）
    context_window: int = 128000  # 上下文窗口大小（tokens）
    reasoning_model: bool | None = None  # 是否为推理模型
    dimension: int | None = None  # 嵌入维度（仅嵌入模型）
    default_params: dict[str, Any] = Field(default_factory=dict)
    multimodal: MultimodalCapabilityConfig | None = None  # 多模态能力（仅对话模型）


class ProviderConfig(BaseModel):
    """提供商配置"""

    api_key: str | None = None
    api_base: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class EmbeddingConfig(BaseModel):
    """嵌入模型配置"""

    provider: str
    model_name: str
    dimension: int = 1536


class LLMDefaults(BaseModel):
    """LLM 默认配置"""

    chat: str = "gpt4o"
    embedding: str = "openai"
    fallback: str = "claude-haiku"
    tiers: dict[str, str] = Field(default_factory=dict)


class EndpointConfig(BaseModel):
    """API 端点配置"""

    base_url: str
    version: str = "v1"
    timeout: int = 30
    auth: dict[str, Any] | None = None


class RateLimitConfig(BaseModel):
    """限流配置"""

    global_limit: str = "60/minute"
    auth: str = "10/minute"
    tasks: str = "30/minute"
    websocket: str = "5/minute"


class CORSConfig(BaseModel):
    """CORS 配置"""

    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    allow_credentials: bool = True
    allowed_methods: list[str] = Field(default_factory=lambda: ["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    allowed_headers: list[str] = Field(default_factory=lambda: ["*"])


class AppConfig(BaseModel):
    """应用配置"""

    name: str = "元思考 Agent 系统"
    version: str = "1.0.0"
    debug: bool = False
    environment: Literal["development", "staging", "production"] = "development"


class ServerConfig(BaseModel):
    """服务器配置"""

    # 默认绑定localhost以提高安全性，生产环境可通过环境变量配置
    host: str = "127.0.0.1"  # 改为localhost，避免绑定所有接口
    port: int = 8000
    workers: int = 4
    reload: bool = False


class DatabaseConfig(BaseModel):
    """数据库配置"""

    url: str
    pool_size: int = 20
    max_overflow: int = 10
    pool_timeout: int = 30
    pool_recycle: int = 3600
    echo: bool = False


class CacheConfig(BaseModel):
    """缓存配置"""

    backend: str = "redis"
    url: str = "redis://localhost:6379/0"
    ttl: int = 3600
    max_connections: int = 50


class AuthConfig(BaseModel):
    """认证配置"""

    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7


class MemoryConfig(BaseModel):
    """记忆模块配置"""

    class CompressionConfig(BaseModel):
        enabled: bool = True
        hot_days: int = 7
        warm_days: int = 90
        warm_ratio: float = 0.5
        cold_ratio: float = 0.75

    class RetrievalConfig(BaseModel):
        top_k: int = 5
        similarity_threshold: float = 0.7

    compression: CompressionConfig = Field(default_factory=CompressionConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)


class LoggingConfig(BaseModel):
    """日志配置"""

    level: str = "INFO"
    format: Literal["json", "text"] = "json"
    file: str | None = None
    rotation: str = "daily"
    retention_days: int = 30

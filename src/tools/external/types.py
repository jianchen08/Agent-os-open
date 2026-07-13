"""外部工具核心类型定义。

暴露接口：
- ExternalToolState：外部工具连接状态枚举
- ExternalToolConfig：外部工具连接配置
- ExternalToolCapability：工具能力描述
- ExternalToolInfo：工具元信息
- RetryPolicy：重试策略配置
- AuthConfig：认证配置
- SandboxResourceLimits：沙箱资源限制
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExternalToolState(str, Enum):
    """外部工具连接状态。

    状态机转换：
        DISCONNECTED → CONNECTING → CONNECTED → DISCONNECTED
        CONNECTING → ERROR → RECONNECTING → CONNECTED
        CONNECTED → RECONNECTING → CONNECTED
        任意状态 → DISCONNECTED（主动断开）
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class AuthType(str, Enum):
    """认证类型。"""

    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    BASIC = "basic"
    OAUTH2 = "oauth2"
    CUSTOM = "custom"


class ProtocolType(str, Enum):
    """通信协议类型。"""

    HTTP = "http"
    WEBSOCKET = "websocket"


@dataclass
class RetryPolicy:
    """重试策略配置。

    Attributes:
        max_retries: 最大重试次数
        base_delay: 基础延迟（秒）
        max_delay: 最大延迟（秒）
        exponential_base: 指数退避基数
        jitter: 是否添加随机抖动
    """

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True


@dataclass
class AuthConfig:
    """认证配置。

    Attributes:
        auth_type: 认证类型
        secret_key: 密钥存储的引用键名（不直接存储密钥值）
        headers: 额外的认证头
        params: 额外的认证参数
    """

    auth_type: AuthType = AuthType.NONE
    secret_key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class ExternalToolConfig:
    """外部工具连接配置。

    Attributes:
        name: 工具唯一名称
        display_name: 显示名称
        description: 工具描述
        protocol: 通信协议
        endpoint: 连接端点地址
        connect_timeout: 连接超时（秒）
        read_timeout: 读取超时（秒）
        execute_timeout: 执行超时（秒）
        retry_policy: 重试策略
        auth: 认证配置
        max_connections: 最大连接数
        idle_timeout: 空闲连接超时（秒）
        heartbeat_interval: 心跳间隔（秒）
        enable_sandbox: 是否启用沙箱执行
        sandbox_image: 沙箱 Docker 镜像
        extra: 扩展配置
    """

    name: str = ""
    display_name: str = ""
    description: str = ""
    protocol: ProtocolType = ProtocolType.HTTP
    endpoint: str = ""
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    execute_timeout: float = 60.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    auth: AuthConfig = field(default_factory=AuthConfig)
    max_connections: int = 5
    idle_timeout: float = 300.0
    heartbeat_interval: float = 30.0
    enable_sandbox: bool = False
    sandbox_image: str = "python:3.11-slim"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExternalToolCapability:
    """工具能力描述。

    Attributes:
        name: 能力名称（即操作名称）
        description: 能力描述
        input_schema: 输入 JSON Schema
        output_schema: 输出 JSON Schema
        requires_sandbox: 是否需要沙箱执行
        timeout_override: 覆盖默认超时（秒）
        dangerous: 是否为危险操作
    """

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    requires_sandbox: bool = False
    timeout_override: float | None = None
    dangerous: bool = False


@dataclass
class ExternalToolInfo:
    """外部工具元信息。

    Attributes:
        name: 工具名称
        version: 版本号
        display_name: 显示名称
        description: 描述
        capabilities: 能力列表
        state: 当前连接状态
        config: 工具配置
        metadata: 扩展元数据
    """

    name: str = ""
    version: str = "1.0.0"
    display_name: str = ""
    description: str = ""
    capabilities: list[ExternalToolCapability] = field(default_factory=list)
    state: ExternalToolState = ExternalToolState.DISCONNECTED
    config: ExternalToolConfig | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SandboxResourceLimits:
    """沙箱资源限制。

    Attributes:
        cpu_limit: CPU 限制（核数）
        memory_limit_mb: 内存限制（MB）
        disk_limit_mb: 磁盘限制（MB）
        network_whitelist: 网络白名单
        max_processes: 最大进程数
        timeout_seconds: 执行超时（秒）
    """

    cpu_limit: float = 1.0
    memory_limit_mb: int = 512
    disk_limit_mb: int = 1024
    network_whitelist: list[str] = field(default_factory=list)
    max_processes: int = 10
    timeout_seconds: float = 60.0

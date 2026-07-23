"""外部工具连接机制（模块六）。

提供外部工具的标准接入框架，支持：
- HTTP/WebSocket 双协议连接
- 标准适配器模式（定义 Schema → 验证 → 执行 → 错误处理）
- 沙箱隔离执行（复用 isolation 模块）
- 密钥安全存储和轮换
- 配置热更新
- 生命周期管理

使用方式：
    from tools.external import ExternalToolLifecycle, ExternalToolConfigManager
"""

from __future__ import annotations

# 适配器
from tools.external.adapter import ExternalToolAdapter

# 配置管理
from tools.external.config import ExternalToolConfigManager

# 连接管理
from tools.external.connection import ExternalToolConnection

# 异常
from tools.external.exceptions import (
    ConfigError,
    ConnectionError,
    ExecutionError,
    ExternalTimeoutError,
    ExternalToolException,
    SandboxError,
    SecretError,
)

# 接口
from tools.external.interfaces import (
    IExternalToolAdapter,
    IExternalToolConnection,
    IExternalToolSandbox,
    ISecretManager,
)

# 生命周期
from tools.external.lifecycle import ExternalToolLifecycle

# 注册表
from tools.external.registry import ExternalToolRegistry

# 沙箱执行
from tools.external.sandbox import ExternalToolSandbox

# 密钥管理
from tools.external.secrets import ExternalToolSecretManager

# 核心类型
from tools.external.types import (
    AuthConfig,
    AuthType,
    ExternalToolCapability,
    ExternalToolConfig,
    ExternalToolInfo,
    ExternalToolState,
    ProtocolType,
    RetryPolicy,
    SandboxResourceLimits,
)

__all__ = [
    # 核心类型
    "AuthConfig",
    "AuthType",
    "ExternalToolCapability",
    "ExternalToolConfig",
    "ExternalToolInfo",
    "ExternalToolState",
    "ProtocolType",
    "RetryPolicy",
    "SandboxResourceLimits",
    # 接口
    "IExternalToolAdapter",
    "IExternalToolConnection",
    "IExternalToolSandbox",
    "ISecretManager",
    # 适配器
    "ExternalToolAdapter",
    # 连接管理
    "ExternalToolConnection",
    # 配置管理
    "ExternalToolConfigManager",
    # 密钥管理
    "ExternalToolSecretManager",
    # 沙箱执行
    "ExternalToolSandbox",
    # 注册表
    "ExternalToolRegistry",
    # 生命周期
    "ExternalToolLifecycle",
    # 异常
    "ConfigError",
    "ConnectionError",
    "ExecutionError",
    "ExternalTimeoutError",
    "ExternalToolException",
    "SandboxError",
    "SecretError",
]

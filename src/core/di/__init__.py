"""
依赖注入容器

提供完整的依赖注入支持，包括：
- 服务生命周期管理（singleton, transient, scoped）
- 服务注册与解析
- 依赖注入
- 生命周期钩子
"""

from src.core.di.container import Container
from src.core.di.decorators import inject, inject_method, scoped, singleton, transient
from src.core.di.global_container import (
    dispose_global_container,
    get_global_container,
    get_service,
    reset_global_container,
    set_global_container,
)
from src.core.di.lifetime import ServiceLifetime
from src.core.exceptions import (
    CircularDependencyError,
    DIException as DIError,
    ServiceAlreadyRegisteredError,
    ServiceNotFoundError,
)

__all__ = [
    "Container",
    "ServiceLifetime",
    "get_global_container",
    "set_global_container",
    "reset_global_container",
    "dispose_global_container",
    "get_service",
    "DIError",
    "ServiceNotFoundError",
    "ServiceAlreadyRegisteredError",
    "CircularDependencyError",
    "inject",
    "singleton",
    "transient",
    "scoped",
    "inject_method",
]

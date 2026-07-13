"""
依赖注入装饰器

提供便捷的装饰器用于标记服务生命周期和自动注入
"""

import functools
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def inject(**dependencies: str):
    """
    依赖注入装饰器

    自动从容器中注入依赖到函数参数

    Args:
        **dependencies: 参数名到服务名的映射

    Example:
        @inject(tool_registry="tool_registry", llm_factory="llm_factory")
        def my_function(tool_registry, llm_factory):
            pass
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 获取容器（如果有）
            container = kwargs.get("container")
            if not container:
                # 尝试从全局容器获取
                from src.core.di import get_global_container  # noqa: PLC0415

                container = get_global_container()

            # 注入依赖
            for param_name, service_name in dependencies.items():
                if param_name not in kwargs and container and container.has(service_name):
                    kwargs[param_name] = container.get(service_name)

            return func(*args, **kwargs)

        return wrapper

    return decorator


def singleton(service_name: str | None = None):
    """
    单例服务装饰器

    Args:
        service_name: 服务名称（可选，默认使用类名）

    Example:
        @singleton()
        class MyService:
            pass
    """

    def decorator(cls: type) -> type:
        # 注册到全局容器
        from src.core.di import get_global_container  # noqa: PLC0415

        container = get_global_container()
        name = service_name or cls.__name__

        if not container.has(name):
            container.register_singleton(name, cls, factory=cls)

        return cls

    return decorator


def transient(service_name: str | None = None):
    """
    瞬态服务装饰器

    Args:
        service_name: 服务名称（可选，默认使用类名）

    Example:
        @transient()
        class MyService:
            pass
    """

    def decorator(cls: type) -> type:
        # 注册到全局容器
        from src.core.di import get_global_container  # noqa: PLC0415

        container = get_global_container()
        name = service_name or cls.__name__

        if not container.has(name):
            container.register_transient(name, cls, factory=cls)

        return cls

    return decorator


def scoped(service_name: str | None = None):
    """
    作用域服务装饰器

    Args:
        service_name: 服务名称（可选，默认使用类名）

    Example:
        @scoped()
        class MyService:
            pass
    """

    def decorator(cls: type) -> type:
        # 注册到全局容器
        from src.core.di import get_global_container  # noqa: PLC0415

        container = get_global_container()
        name = service_name or cls.__name__

        if not container.has(name):
            container.register_scoped(name, cls, factory=cls)

        return cls

    return decorator


def inject_method(**dependencies: str):
    """
    方法依赖注入装饰器

    用于类方法中自动注入依赖

    Args:
        **dependencies: 参数名到服务名的映射

    Example:
        class MyClass:
            @inject_method(tool_registry="tool_registry")
            def my_method(self, tool_registry):
                pass
    """

    def decorator(method: Callable) -> Callable:
        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            # 获取容器（如果有）
            container = kwargs.get("container")
            if not container:
                # 尝试从 self.container 获取
                if hasattr(self, "container"):
                    container = self.container
                else:
                    # 尝试从全局容器获取
                    from src.core.di import get_global_container  # noqa: PLC0415

                    container = get_global_container()

            # 注入依赖
            for param_name, service_name in dependencies.items():
                if param_name not in kwargs and container and container.has(service_name):
                    kwargs[param_name] = container.get(service_name)

            return method(self, *args, **kwargs)

        return wrapper

    return decorator

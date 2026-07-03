"""
依赖注入容器

提供完整的依赖注入支持，包括：
- 服务生命周期管理（singleton, transient, scoped）
- 服务注册与解析
- 依赖注入
- 生命周期钩子
"""

import inspect
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from src.core.di.lifetime import ServiceLifetime
from src.core.exceptions.di import (
    CircularDependencyError,
    InvalidServiceFactoryError,
    ServiceAlreadyRegisteredError,
    ServiceNotFoundError,
    ServiceValidationError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ServiceDescriptor:
    """
    服务描述符

    描述一个服务的注册信息
    """

    def __init__(
        self,
        service_type: type,
        lifetime: ServiceLifetime = ServiceLifetime.SINGLETON,
        factory: Callable | None = None,
        instance: Any | None = None,
    ):
        """
        初始化服务描述符

        Args:
            service_type: 服务类型
            lifetime: 生命周期
            factory: 工厂函数（可选）
            instance: 已存在的实例（可选）
        """
        self.service_type = service_type
        self.lifetime = lifetime
        self.factory = factory
        self.instance = instance
        self.is_initialized = False

    def validate(self):
        """验证服务描述符"""
        if self.lifetime == ServiceLifetime.SINGLETON and self.instance is None:  # noqa: SIM102
            # 单例服务必须有工厂或实例
            if self.factory is None:
                raise InvalidServiceFactoryError(
                    f"Singleton service {self.service_type.__name__} must have factory or instance"
                )


class Container:
    """
    依赖注入容器

    支持多种服务生命周期：
    - SINGLETON: 单例，整个容器生命周期内只创建一次
    - TRANSIENT: 瞬态，每次请求都创建新实例
    - SCOPED: 作用域，在同一个作用域内使用同一实例
    """

    def __init__(self):
        """初始化容器"""
        # 服务注册表: service_name -> ServiceDescriptor
        self._services: dict[str, ServiceDescriptor] = {}

        # 单例实例缓存: service_name -> instance
        self._singletons: dict[str, Any] = {}

        # 作用域实例缓存: scope_id -> (service_name -> instance)
        self._scopes: dict[str, dict[str, Any]] = {}

        # 当前作用域 ID
        self._current_scope: str | None = None

        # 服务初始化钩子: service_name -> callable
        self._init_hooks: dict[str, Callable] = {}

        # 服务销毁钩子: service_name -> callable
        self._dispose_hooks: dict[str, Callable] = {}

        # 依赖解析栈（用于检测循环依赖）
        self._resolution_stack: list = []

        # 作用域计数器
        self._scope_counter = 0

    def register(
        self,
        service_name: str,
        service_type: type,
        lifetime: ServiceLifetime = ServiceLifetime.SINGLETON,
        factory: Callable | None = None,
        instance: Any | None = None,
    ) -> "Container":
        """
        注册服务

        Args:
            service_name: 服务名称
            service_type: 服务类型
            lifetime: 生命周期
            factory: 工厂函数（可选）
            instance: 已存在的实例（可选）

        Returns:
            容器实例（支持链式调用）

        Raises:
            ServiceAlreadyRegisteredError: 服务已注册
        """
        if service_name in self._services:
            raise ServiceAlreadyRegisteredError(service_name)

        descriptor = ServiceDescriptor(service_type, lifetime, factory, instance)
        descriptor.validate()

        self._services[service_name] = descriptor
        logger.debug(f"Service registered: {service_name} (lifetime={lifetime.value})")

        return self

    def register_instance(self, service_name: str, instance: Any) -> "Container":
        """
        注册已存在的实例（作为单例）

        Args:
            service_name: 服务名称
            instance: 服务实例

        Returns:
            容器实例
        """
        return self.register(
            service_name,
            type(instance),
            ServiceLifetime.SINGLETON,
            instance=instance,
        )

    def update_instance(self, service_name: str, instance: Any) -> "Container":
        """
        更新已存在的实例（用于测试）

        Args:
            service_name: 服务名称
            instance: 服务实例

        Returns:
            容器实例
        """
        if service_name in self._services:
            self._services[service_name].instance = instance
            self._singletons[service_name] = instance
        else:
            self.register_instance(service_name, instance)
        return self

    def register_singleton(
        self,
        service_name: str,
        service_type: type,
        factory: Callable | None = None,
    ) -> "Container":
        """
        注册单例服务

        Args:
            service_name: 服务名称
            service_type: 服务类型
            factory: 工厂函数（可选）

        Returns:
            容器实例
        """
        return self.register(service_name, service_type, ServiceLifetime.SINGLETON, factory)

    def register_transient(
        self,
        service_name: str,
        service_type: type,
        factory: Callable | None = None,
    ) -> "Container":
        """
        注册瞬态服务

        Args:
            service_name: 服务名称
            service_type: 服务类型
            factory: 工厂函数（可选）

        Returns:
            容器实例
        """
        return self.register(service_name, service_type, ServiceLifetime.TRANSIENT, factory)

    def register_scoped(
        self,
        service_name: str,
        service_type: type,
        factory: Callable | None = None,
    ) -> "Container":
        """
        注册作用域服务

        Args:
            service_name: 服务名称
            service_type: 服务类型
            factory: 工厂函数（可选）

        Returns:
            容器实例
        """
        return self.register(service_name, service_type, ServiceLifetime.SCOPED, factory)

    def get(self, service_name: str) -> Any:
        """
        获取服务实例

        Args:
            service_name: 服务名称

        Returns:
            服务实例

        Raises:
            ServiceNotFoundError: 服务未找到
            CircularDependencyError: 循环依赖
        """
        # 检查循环依赖
        if service_name in self._resolution_stack:
            raise CircularDependencyError(self._resolution_stack + [service_name])

        # 查找服务描述符
        descriptor = self._services.get(service_name)
        if not descriptor:
            raise ServiceNotFoundError(service_name)

        # 如果已有实例，直接返回
        if descriptor.instance is not None:
            return descriptor.instance

        # 根据生命周期创建实例
        self._resolution_stack.append(service_name)

        try:
            if descriptor.lifetime == ServiceLifetime.SINGLETON:
                instance = self._get_singleton(service_name, descriptor)
            elif descriptor.lifetime == ServiceLifetime.TRANSIENT:
                instance = self._create_instance(descriptor)
            elif descriptor.lifetime == ServiceLifetime.SCOPED:
                instance = self._get_scoped(service_name, descriptor)
            else:
                raise ServiceValidationError(f"Unknown lifetime: {descriptor.lifetime}")

            return instance
        finally:
            self._resolution_stack.pop()

    def _get_singleton(self, service_name: str, descriptor: ServiceDescriptor) -> Any:
        """获取单例实例"""
        if service_name not in self._singletons:
            instance = self._create_instance(descriptor)
            self._singletons[service_name] = instance
            descriptor.instance = instance

            # 执行初始化钩子
            self._run_init_hook(service_name, instance)

        return self._singletons[service_name]

    def _get_scoped(self, service_name: str, descriptor: ServiceDescriptor) -> Any:
        """获取作用域实例"""
        if self._current_scope is None:
            raise ServiceValidationError("Cannot resolve scoped service outside of a scope")

        scope_cache = self._scopes.get(self._current_scope, {})

        if service_name not in scope_cache:
            instance = self._create_instance(descriptor)
            scope_cache[service_name] = instance

            # 执行初始化钩子
            self._run_init_hook(service_name, instance)

        return scope_cache[service_name]

    def _create_instance(self, descriptor: ServiceDescriptor) -> Any:
        """
        创建服务实例

        Args:
            descriptor: 服务描述符

        Returns:
            服务实例
        """
        # 使用工厂函数创建
        if descriptor.factory:
            return self._invoke_factory(descriptor.factory)

        # 使用类型自动创建（带依赖注入）
        return self._auto_create(descriptor.service_type)

    def _invoke_factory(self, factory: Callable) -> Any:
        """
        调用工厂函数创建实例

        Args:
            factory: 工厂函数

        Returns:
            服务实例
        """
        # 检查是否需要容器参数
        sig = inspect.signature(factory)
        params = sig.parameters

        kwargs = {}
        for param_name, param in params.items():
            # 尝试从容器中解析依赖
            if param_name != "container":
                # 检查类型注解
                param_type = param.annotation
                if param_type != inspect.Parameter.empty:
                    # 尝试通过类型查找服务
                    service = self._try_get_by_type(param_type)
                    if service:
                        kwargs[param_name] = service

        # 如果需要容器，传递容器（支持 'container' 或 'c' 作为参数名）
        if "container" in params:
            kwargs["container"] = self
        elif "c" in params:
            kwargs["c"] = self

        return factory(**kwargs)

    def _auto_create(self, service_type: type) -> Any:
        """
        自动创建服务实例（通过构造函数注入）

        Args:
            service_type: 服务类型

        Returns:
            服务实例
        """
        # 获取构造函数
        constructor = service_type.__init__

        # 解析构造参数
        sig = inspect.signature(constructor)
        params = [p for p in sig.parameters.values() if p.name != "self" and p.kind != inspect.Parameter.VAR_KEYWORD]

        kwargs = {}
        for param in params:
            # 跳过没有类型注解的参数
            if param.annotation == inspect.Parameter.empty:
                continue

            # 尝试通过类型解析依赖
            dependency = self._try_get_by_type(param.annotation)
            if dependency is not None:
                kwargs[param.name] = dependency
            elif param.default == inspect.Parameter.empty:
                # 必需参数但无法解析
                logger.warning(f"Cannot resolve dependency {param.name} for {service_type.__name__}")

        return service_type(**kwargs)

    def _try_get_by_type(self, service_type: type) -> Any | None:
        """
        尝试通过类型获取服务

        Args:
            service_type: 服务类型

        Returns:
            服务实例或 None
        """
        # 查找匹配的服务
        for name, descriptor in self._services.items():
            if descriptor.service_type == service_type or issubclass(descriptor.service_type, service_type):
                return self.get(name)

        return None

    def has(self, service_name: str) -> bool:
        """
        检查服务是否已注册

        Args:
            service_name: 服务名称

        Returns:
            是否已注册
        """
        return service_name in self._services

    def is_registered(self, service_name: str) -> bool:
        """
        检查服务是否已注册（别名）

        Args:
            service_name: 服务名称

        Returns:
            是否已注册
        """
        return self.has(service_name)

    @asynccontextmanager
    async def create_scope(self):
        """
        创建作用域

        在作用域内，scoped 服务会共享同一个实例

        Usage:
            async with container.create_scope():
                instance1 = container.get("my_service")
                instance2 = container.get("my_service")
                # instance1 和 instance2 是同一个实例
        """
        # 创建新的作用域
        self._scope_counter += 1
        scope_id = f"scope_{self._scope_counter}"
        self._scopes[scope_id] = {}

        # 保存之前的作用域
        previous_scope = self._current_scope
        self._current_scope = scope_id

        try:
            yield self
        finally:
            # 销毁作用域中的服务
            await self._dispose_scope(scope_id)

            # 恢复之前的作用域
            self._current_scope = previous_scope

    async def _dispose_scope(self, scope_id: str):
        """
        销毁作用域

        Args:
            scope_id: 作用域 ID
        """
        if scope_id in self._scopes:
            scope_cache = self._scopes[scope_id]

            # 执行销毁钩子
            for service_name, instance in scope_cache.items():
                await self._run_dispose_hook(service_name, instance)

            # 清除作用域缓存
            del self._scopes[scope_id]

    def add_init_hook(self, service_name: str, hook: Callable):
        """
        添加初始化钩子

        Args:
            service_name: 服务名称
            hook: 钩子函数
        """
        self._init_hooks[service_name] = hook

    def add_dispose_hook(self, service_name: str, hook: Callable):
        """
        添加销毁钩子

        Args:
            service_name: 服务名称
            hook: 钩子函数
        """
        self._dispose_hooks[service_name] = hook

    def _run_init_hook(self, service_name: str, instance: Any):
        """运行初始化钩子"""
        if service_name in self._init_hooks:
            try:
                hook = self._init_hooks[service_name]
                if inspect.iscoroutinefunction(hook):
                    # 异步钩子需要特殊处理
                    logger.warning(f"Init hook for {service_name} is async but called synchronously")
                else:
                    hook(instance)
            except Exception as e:
                logger.error(f"Error running init hook for {service_name}: {e}")

    async def _run_dispose_hook(self, service_name: str, instance: Any):
        """运行销毁钩子"""
        if service_name in self._dispose_hooks:
            try:
                hook = self._dispose_hooks[service_name]
                if inspect.iscoroutinefunction(hook):
                    await hook(instance)
                else:
                    hook(instance)
            except Exception as e:
                logger.error(f"Error running dispose hook for {service_name}: {e}")

    async def dispose(self):
        """销毁容器，释放所有资源"""
        # 销毁所有作用域
        for scope_id in list(self._scopes.keys()):
            await self._dispose_scope(scope_id)

        # 销毁所有单例
        for service_name, instance in self._singletons.items():
            await self._run_dispose_hook(service_name, instance)

        # 清空缓存
        self._singletons.clear()
        self._scopes.clear()
        self._services.clear()

        logger.info("Container disposed")

    def list_services(self) -> dict[str, str]:
        """
        列出所有已注册的服务

        Returns:
            服务名称到类型名称的映射
        """
        return {name: desc.service_type.__name__ for name, desc in self._services.items()}

"""
注册表和工厂基类模块

暴露接口：
- get_instance(cls) -> SingletonMixin：get_instance功能
- reset_instance(cls) -> None：reset_instance功能
- has_instance(cls) -> bool：has_instance功能
- register(self, value: V, key: K | None, overwrite: bool) -> K：register功能
- unregister(self, key: K) -> V：unregister功能
- get(self, key: K) -> V：get功能
- get_optional(self, key: K) -> V | None：get_optional功能
- has(self, key: K) -> bool：has功能
- list_all(self) -> list[V]：list_all功能
- list_keys(self) -> list[K]：list_keys功能
- count(self) -> int：count功能
- clear(self) -> None：clear功能
- update(self, key: K, value: V) -> None：update功能
- get(self, key: K, use_cache: bool) -> V：get功能
- create(self, key: K) -> V：create功能
- clear_cache(self) -> None：clear_cache功能
- remove_from_cache(self, key: K) -> bool：remove_from_cache功能
- list_cached(self) -> list[K]：list_cached功能
- is_cached(self, key: K) -> bool：is_cached功能
- list_available_types(self) -> list[K]：list_available_types功能
- enable_cache(self) -> None：enable_cache功能
- disable_cache(self) -> None：disable_cache功能
- SingletonMixin：SingletonMixin类
- BaseRegistry：BaseRegistry类
- SimpleRegistry：SimpleRegistry类
- CachedFactory：CachedFactory类
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


# =============================================================================
# 单例模式基类
# =============================================================================


class SingletonMixin:
    """
    线程安全的单例模式混入类

    使用方式:
        class MyClass(SingletonMixin):
            def __init__(self, arg1=None):
                # 注意: __init__ 每次获取实例时都会被调用
                # 如果需要只执行一次的初始化，使用 _singleton_init
                pass

            def _singleton_init(self, arg1=None):
                # 这里只会在第一次创建实例时执行
                self.arg1 = arg1

        # 获取实例
        instance1 = MyClass.get_instance("value1")
        instance2 = MyClass.get_instance("value2")  # 返回相同实例，但 arg1="value2"

    特性:
    - 线程安全：使用锁确保多线程环境下只有一个实例
    - 支持参数更新：每次 get_instance 都会调用 __init__，可以更新参数
    - 可选一次性初始化：重写 _singleton_init 实现只执行一次的初始化
    """

    _instance: SingletonMixin | None = None
    _lock: threading.Lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls, *args: Any, **kwargs: Any) -> SingletonMixin:  # noqa: ARG004
        """确保只有一个实例被创建"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls, *args: Any, **kwargs: Any) -> SingletonMixin:
        """获取单例实例"""
        instance = cls.__new__(cls, *args, **kwargs)
        instance.__init__(*args, **kwargs)
        return instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例实例（主要用于测试）"""
        with cls._lock:
            cls._instance = None
            cls._initialized = False
        logger.debug(f"{cls.__name__} 单例已重置")

    @classmethod
    def has_instance(cls) -> bool:
        """检查是否已创建实例"""
        return cls._instance is not None


# =============================================================================
# 注册表基类
# =============================================================================


class BaseRegistry(ABC, Generic[K, V]):
    """
    抽象注册表基类

    提供通用的注册表功能：
    - 注册/注销条目
    - 查询条目
    - 列出所有条目

    泛型参数:
        K: 键的类型（如 str）
        V: 值的类型

    使用方式:
        class MyRegistry(BaseRegistry[str, MyClass]):
            def _create_key(self, value: MyClass) -> str:
                return value.name

            def _on_register(self, key: str, value: MyClass) -> None:
                logger.info(f"Registered: {key}")

        registry = MyRegistry()
        registry.register(my_obj)
        obj = registry.get("key")
    """

    def __init__(self) -> None:
        """初始化注册表"""
        self._items: dict[K, V] = {}
        self._lock = threading.RLock()
        logger.debug(f"{self.__class__.__name__} 初始化完成")

    @abstractmethod
    def _create_key(self, value: V) -> K:
        """从值创建键"""
        pass

    def _on_register(self, key: K, value: V) -> None:
        """注册后的钩子方法"""
        pass

    def _on_unregister(self, key: K, value: V) -> None:
        """注销后的钩子方法"""
        pass

    def register(self, value: V, key: K | None = None, overwrite: bool = False) -> K:
        """注册条目"""
        if key is None:
            key = self._create_key(value)

        with self._lock:
            if key in self._items and not overwrite:
                raise KeyError(f"键 '{key}' 已存在，设置 overwrite=True 以覆盖")

            self._items[key] = value
            self._on_register(key, value)

        logger.debug(f"已注册: {key}")
        return key

    def unregister(self, key: K) -> V:
        """注销条目"""
        with self._lock:
            if key not in self._items:
                raise KeyError(f"键 '{key}' 不存在")

            value = self._items.pop(key)
            self._on_unregister(key, value)

        logger.debug(f"已注销: {key}")
        return value

    def get(self, key: K) -> V:
        """获取条目（无锁读取 — CPython dict 读操作是原子的）"""
        if key not in self._items:
            raise KeyError(f"键 '{key}' 不存在")
        return self._items[key]

    def get_optional(self, key: K) -> V | None:
        """可选获取条目（无锁读取）"""
        return self._items.get(key)

    def has(self, key: K) -> bool:
        """检查键是否存在（无锁读取）"""
        return key in self._items

    def list_all(self) -> list[V]:
        """列出所有条目（无锁读取 — 返回快照）"""
        return list(self._items.values())

    def list_keys(self) -> list[K]:
        """列出所有键（无锁读取 — 返回快照）"""
        return list(self._items.keys())

    def count(self) -> int:
        """获取条目数量（无锁读取）"""
        return len(self._items)

    def clear(self) -> None:
        """清空注册表"""
        with self._lock:
            items = list(self._items.items())
            self._items.clear()

            # 调用注销钩子
            for key, value in items:
                self._on_unregister(key, value)

        logger.debug(f"{self.__class__.__name__} 已清空")

    def update(self, key: K, value: V) -> None:
        """更新条目（如果不存在则注册）"""
        self.register(value, key, overwrite=True)


class SimpleRegistry(BaseRegistry[K, V]):
    """
    简单注册表实现

    使用显式键的注册表，不需要从值中提取键

    使用方式:
        registry = SimpleRegistry[str, MyClass]()
        registry.register(my_obj, key="my_key")
        obj = registry.get("my_key")
    """

    def _create_key(self, value: V) -> K:
        """简单注册表需要显式提供键"""
        raise NotImplementedError("SimpleRegistry 需要显式提供键，使用 register(value, key='xxx')")


# =============================================================================
# 带缓存的工厂基类
# =============================================================================


class CachedFactory(ABC, Generic[K, V]):
    """
    带缓存的工厂基类

    提供类型映射和实例缓存功能：
    - 类型到实现类的映射
    - 实例缓存（避免重复创建）
    - 类型验证

    泛型参数:
        K: 键的类型（如 str）
        V: 值的类型

    使用方式:
        class MyFactory(CachedFactory[str, MyClient]):
            TYPE_MAPPING = {
                "type_a": ClientA,
                "type_b": ClientB,
            }

            def _create_instance(self, key: str, **kwargs) -> MyClient:
                client_class = self.TYPE_MAPPING[key]
                return client_class(**kwargs)

        factory = MyFactory()
        client = factory.get("type_a", option1="value1")
    """

    # 子类必须定义类型映射
    TYPE_MAPPING: dict[K, type[V]] = {}

    def __init__(self) -> None:
        """初始化工厂"""
        self._cache: dict[K, V] = {}
        self._lock = threading.RLock()
        self._cache_enabled = True
        logger.debug(f"{self.__class__.__name__} 初始化完成")

    @abstractmethod
    def _create_instance(self, key: K, **kwargs: Any) -> V:
        """创建实例"""
        pass

    def _validate_key(self, key: K) -> None:
        """验证键是否有效"""
        if key not in self.TYPE_MAPPING:
            available = list(self.TYPE_MAPPING.keys())
            raise ValueError(f"未知的类型: {key}。可用类型: {available}")

    def _get_cache_key(self, key: K, **kwargs: Any) -> K:
        """获取缓存键"""
        return key

    def _on_create(self, key: K, instance: V, **kwargs: Any) -> None:
        """创建实例后的钩子"""
        pass

    def get(self, key: K, use_cache: bool = True, **kwargs: Any) -> V:
        """获取实例（带缓存）"""
        self._validate_key(key)

        cache_key = self._get_cache_key(key, **kwargs)

        # 检查缓存
        if use_cache and self._cache_enabled and cache_key in self._cache:
            logger.debug(f"使用缓存实例: {cache_key}")
            return self._cache[cache_key]

        # 创建实例
        with self._lock:
            # 双重检查
            if use_cache and self._cache_enabled and cache_key in self._cache:
                return self._cache[cache_key]

            instance = self._create_instance(key, **kwargs)
            self._on_create(key, instance, **kwargs)

            # 缓存实例
            if use_cache and self._cache_enabled:
                self._cache[cache_key] = instance

        logger.debug(f"创建新实例: {cache_key}")
        return instance

    def create(self, key: K, **kwargs: Any) -> V:
        """创建新实例（不使用缓存）"""
        return self.get(key, use_cache=False, **kwargs)

    def clear_cache(self) -> None:
        """清除缓存"""
        with self._lock:
            self._cache.clear()
        logger.debug(f"{self.__class__.__name__} 缓存已清除")

    def remove_from_cache(self, key: K) -> bool:
        """从缓存中移除"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def list_cached(self) -> list[K]:
        """列出已缓存的键（无锁读取 — 返回快照）"""
        return list(self._cache.keys())

    def is_cached(self, key: K) -> bool:
        """检查是否已缓存（无锁读取）"""
        return key in self._cache

    def list_available_types(self) -> list[K]:
        """列出所有可用类型"""
        return list(self.TYPE_MAPPING.keys())

    def enable_cache(self) -> None:
        """启用缓存"""
        self._cache_enabled = True

    def disable_cache(self) -> None:
        """禁用缓存"""
        self._cache_enabled = False


# =============================================================================
# 单例注册表组合
# =============================================================================


class SingletonRegistry(SingletonMixin, BaseRegistry[K, V]):
    """
    单例注册表

    组合了单例模式和注册表功能的基类

    使用方式:
        class MyRegistry(SingletonRegistry[str, MyClass]):
            def _create_key(self, value: MyClass) -> str:
                return value.name

        # 获取单例实例
        registry = MyRegistry.get_instance()
        registry.register(my_obj)
    """

    def __init__(self) -> None:
        """初始化"""
        # 确保只初始化一次
        if not self._initialized:
            BaseRegistry.__init__(self)
            self._singleton_init()
            self.__class__._initialized = True

    def _singleton_init(self) -> None:
        """
        单例初始化钩子

        子类重写此方法执行一次性初始化
        """
        pass


class SingletonCachedFactory(SingletonMixin, CachedFactory[K, V]):
    """
    单例缓存工厂

    组合了单例模式和缓存工厂功能的基类

    使用方式:
        class MyFactory(SingletonCachedFactory[str, MyClient]):
            TYPE_MAPPING = {"a": ClientA, "b": ClientB}

            def _create_instance(self, key: str, **kwargs) -> MyClient:
                return self.TYPE_MAPPING[key](**kwargs)

        # 获取单例实例
        factory = MyFactory.get_instance()
        client = factory.get("a")
    """

    def __init__(self) -> None:
        """初始化"""
        # 确保只初始化一次
        if not self._initialized:
            CachedFactory.__init__(self)
            self._singleton_init()
            self.__class__._initialized = True

    def _singleton_init(self) -> None:
        """
        单例初始化钩子

        子类重写此方法执行一次性初始化
        """
        pass

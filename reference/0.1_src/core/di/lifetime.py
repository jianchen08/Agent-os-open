"""
服务生命周期枚举
"""

from enum import Enum


class ServiceLifetime(Enum):
    """
    服务生命周期

    - SINGLETON: 单例，整个容器生命周期内只创建一次
    - TRANSIENT: 瞬态，每次请求都创建新实例
    - SCOPED: 作用域，在同一个作用域内使用同一实例
    """

    SINGLETON = "singleton"
    TRANSIENT = "transient"
    SCOPED = "scoped"

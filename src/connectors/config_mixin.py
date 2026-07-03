"""ConfigCenter 配置订阅混入类。

为通道适配器和连接器提供统一的 ConfigCenter 集成能力，
支持配置变更后自动通知子类执行重载逻辑。

使用方式：
    1. 子类继承 ConfigSubscriberMixin（放在 MRO 中 ABC 之前）
    2. 在 start()/connect() 时调用 subscribe_config()
    3. 重写 _on_config_changed() 处理配置变更
    4. 在 stop()/disconnect() 时调用 unsubscribe_config()

暴露接口：
- ConfigSubscriberMixin: 配置订阅混入类
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config.config_center import ConfigCenter

logger = logging.getLogger(__name__)


class ConfigSubscriberMixin:
    """ConfigCenter 配置订阅混入类。

    为通道适配器和连接器提供配置热加载能力。
    使用懒初始化模式，不需要在 __init__ 中调用 super()。

    子类需重写 _on_config_changed() 以处理具体的配置重载逻辑。

    Example::

        class MyConnector(BaseConnector, ConfigSubscriberMixin):
            async def connect(self) -> None:
                ...
                self.subscribe_config(config_center, "capability_adapters.yaml")

            def _on_config_changed(self, event_type, file_path, context):
                # 重新加载配置
                ...

            async def disconnect(self) -> None:
                self.unsubscribe_config()
                ...
    """

    def _ensure_config_state(self) -> None:
        """确保配置订阅状态已初始化（懒初始化）。"""
        if not hasattr(self, "_config_center"):
            self._config_center: ConfigCenter | None = None
            self._config_path_prefix: str = ""
            self._config_callback: Any = None

    def subscribe_config(
        self,
        config_center: ConfigCenter,
        path_prefix: str,
    ) -> None:
        """订阅 ConfigCenter 配置变更。

        注册回调到 ConfigCenter，当 path_prefix 下的配置文件变更时，
        自动调用 _on_config_changed()。

        Args:
            config_center: ConfigCenter 实例
            path_prefix: 监听的路径前缀，如 "capability_adapters.yaml"
        """
        self._ensure_config_state()

        # 先取消旧订阅
        if self._config_center is not None:
            self.unsubscribe_config()

        self._config_center = config_center
        self._config_path_prefix = path_prefix

        # 创建绑定到 self 的回调
        self._config_callback = _create_callback(self)
        config_center.watch(path_prefix, self._config_callback)

        adapter_name = (
            getattr(self, "connector_type", None) or getattr(self, "channel_type", None) or self.__class__.__name__
        )
        logger.info(
            "配置订阅已注册: %s -> prefix=%s",
            adapter_name,
            path_prefix,
        )

    def unsubscribe_config(self) -> None:
        """取消 ConfigCenter 配置订阅。"""
        self._ensure_config_state()

        if self._config_center is not None and self._config_callback is not None:
            self._config_center.unwatch(
                self._config_path_prefix,
                self._config_callback,
            )
            adapter_name = (
                getattr(self, "connector_type", None) or getattr(self, "channel_type", None) or self.__class__.__name__
            )
            logger.info(
                "配置订阅已取消: %s -> prefix=%s",
                adapter_name,
                self._config_path_prefix,
            )

        self._config_center = None
        self._config_path_prefix = ""
        self._config_callback = None

    def _on_config_changed(
        self,
        event_type: str,
        file_path: str,
        context: dict[str, Any],
    ) -> None:
        """配置变更回调。

        子类重写此方法以处理配置热加载逻辑。
        默认实现仅记录日志。

        Args:
            event_type: 事件类型（created/modified/deleted/manual_reload）
            file_path: 变更的文件绝对路径
            context: 变更上下文，包含 config_type 等元信息
        """
        adapter_name = (
            getattr(self, "connector_type", None) or getattr(self, "channel_type", None) or self.__class__.__name__
        )
        logger.info(
            "配置变更通知: %s, event=%s, path=%s",
            adapter_name,
            event_type,
            file_path,
        )


def _create_callback(subscriber: ConfigSubscriberMixin) -> Any:
    """为订阅者创建 ConfigCenter 回调函数。

    Args:
        subscriber: 配置订阅者实例

    Returns:
        符合 ConfigCenter.watch() 签名的回调函数
    """

    def callback(event_type: str, file_path: str, context: dict[str, Any]) -> None:
        subscriber._on_config_changed(event_type, file_path, context)

    return callback

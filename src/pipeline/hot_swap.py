"""插件热替换管理器。

提供安全的插件替换流程：预检查 → 快照 → 替换 → 健康检查 → 失败回滚。
支持运行时动态替换插件实例，替换失败时自动恢复旧插件。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.plugin import IPlugin

from pipeline.registry import PluginRegistry

logger = logging.getLogger(__name__)


@dataclass
class SwapSnapshot:
    """替换前快照。

    保存插件替换前的旧插件实例，供回滚使用。

    Attributes:
        plugin_name: 被替换的插件名称
        old_plugin: 替换前的旧插件实例，不存在时为 None
        timestamp: 快照时间戳
        swap_id: 替换操作唯一标识
    """

    plugin_name: str
    old_plugin: IPlugin | None = None
    timestamp: float = field(default_factory=time.monotonic)
    swap_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class SwapResult:
    """替换结果。

    Attributes:
        success: 替换是否成功
        swap_id: 替换操作唯一标识
        error: 错误信息，成功时为 None
        rolled_back: 是否已自动回滚
    """

    success: bool = False
    swap_id: str = ""
    error: str | None = None
    rolled_back: bool = False


class HotSwapManager:
    """插件热替换管理器。

    提供安全的插件替换流程：预检查 → 快照 → 替换 → 健康检查 → 失败回滚。

    流程：
    1. 预检查：验证新插件兼容性（同类型、同接口）
    2. 快照：保存旧插件实例
    3. 替换：从 registry 注销旧插件，注册新插件
    4. 健康检查：执行新插件的简易检查
    5. 如果健康检查失败 → 自动回滚

    Attributes:
        _registry: 插件注册表实例
        _snapshots: swap_id → SwapSnapshot 的映射，保存历史替换快照
    """

    def __init__(self, registry: PluginRegistry) -> None:
        """初始化热替换管理器。

        Args:
            registry: 插件注册表实例
        """
        self._registry = registry
        self._snapshots: dict[str, SwapSnapshot] = {}

    async def swap_plugin(
        self,
        plugin_name: str,
        new_plugin: IPlugin,
        *,
        health_check: bool = True,
    ) -> SwapResult:
        """替换插件。

        执行完整的替换流程：预检查 → 快照 → 替换 → 健康检查 → 失败回滚。

        Args:
            plugin_name: 要替换的插件名称
            new_plugin: 新插件实例
            health_check: 是否执行健康检查

        Returns:
            SwapResult 包含是否成功、swap_id、回滚状态
        """
        # 1. 获取旧插件
        old_plugin = self._registry.get(plugin_name)

        # 2. 预检查
        warnings = self._pre_check(old_plugin, new_plugin)
        if warnings:
            logger.warning(
                "Hot swap pre-check warnings for '%s': %s",
                plugin_name,
                "; ".join(warnings),
            )

        # 3. 保存快照
        snapshot = SwapSnapshot(
            plugin_name=plugin_name,
            old_plugin=old_plugin,
        )
        self._snapshots[snapshot.swap_id] = snapshot

        # 4. 执行替换
        try:
            self._registry.replace(plugin_name, new_plugin)
        except Exception as exc:
            logger.error("Hot swap failed during replace: %s", exc)
            del self._snapshots[snapshot.swap_id]
            return SwapResult(
                success=False,
                swap_id=snapshot.swap_id,
                error=f"替换失败: {exc}",
            )

        # 5. 健康检查
        if health_check:
            healthy = await self._health_check(new_plugin)
            if not healthy:
                # 自动回滚
                rolled_back = await self.rollback(snapshot.swap_id)
                return SwapResult(
                    success=False,
                    swap_id=snapshot.swap_id,
                    error="健康检查失败",
                    rolled_back=rolled_back,
                )

        logger.info(
            "Hot swap succeeded: '%s' → '%s' (swap_id=%s)",
            plugin_name,
            new_plugin.name,
            snapshot.swap_id,
        )
        return SwapResult(
            success=True,
            swap_id=snapshot.swap_id,
        )

    async def rollback(self, swap_id: str) -> bool:
        """回滚到替换前的状态。

        根据 swap_id 查找快照，恢复旧插件实例。

        Args:
            swap_id: 替换操作的 ID

        Returns:
            是否回滚成功
        """
        snapshot = self._snapshots.get(swap_id)
        if snapshot is None:
            logger.warning("Rollback failed: swap_id '%s' not found", swap_id)
            return False

        try:
            # 先移除当前插件（可能是新插件也可能已经被其他替换）
            current = self._registry.get(snapshot.plugin_name)

            if snapshot.old_plugin is not None:
                # 恢复旧插件
                self._registry.replace(snapshot.plugin_name, snapshot.old_plugin)
                logger.info(
                    "Rollback succeeded: restored '%s' (swap_id=%s)",
                    snapshot.plugin_name,
                    swap_id,
                )
            else:
                # 旧插件不存在（原始是新增），移除当前插件
                if current is not None:
                    self._registry._plugins.pop(snapshot.plugin_name, None)
                    if hasattr(self._registry, "_core_plugins"):
                        self._registry._core_plugins.pop(snapshot.plugin_name, None)
                logger.info(
                    "Rollback succeeded: removed '%s' (was new, swap_id=%s)",
                    snapshot.plugin_name,
                    swap_id,
                )

            # 清理快照
            del self._snapshots[swap_id]
            return True

        except Exception as exc:
            logger.error("Rollback failed: %s", exc)
            return False

    def _pre_check(self, old_plugin: IPlugin | None, new_plugin: IPlugin) -> list[str]:
        """预检查新插件兼容性。

        检查新旧插件的类型是否一致。不一致时发出警告（不阻止替换）。

        Args:
            old_plugin: 旧插件实例，不存在时为 None
            new_plugin: 新插件实例

        Returns:
            警告信息列表，空列表表示无警告
        """
        warnings: list[str] = []

        if old_plugin is not None:
            # 检查插件接口类型是否一致
            from pipeline.plugin import ICorePlugin, IInputPlugin, IOutputPlugin  # noqa: PLC0415

            old_interfaces: set[type] = set()
            new_interfaces: set[type] = set()

            for iface in (IInputPlugin, ICorePlugin, IOutputPlugin):
                if isinstance(old_plugin, iface):
                    old_interfaces.add(iface)
                if isinstance(new_plugin, iface):
                    new_interfaces.add(iface)

            if old_interfaces != new_interfaces:
                old_names = [i.__name__ for i in old_interfaces]
                new_names = [i.__name__ for i in new_interfaces]
                warnings.append(f"插件接口类型不同: 旧={old_names}, 新={new_names}")

        return warnings

    async def _health_check(self, plugin: IPlugin) -> bool:
        """简易健康检查。

        尝试执行插件的基本操作（访问属性 + 简单 execute），
        确保实例可用。对于核心插件只检查属性。

        Args:
            plugin: 待检查的插件实例

        Returns:
            插件是否健康
        """
        try:
            # 检查基本属性
            _ = plugin.name
            _ = plugin.priority

            # 对于 Input/Output 插件，尝试一次空 execute
            from pipeline.plugin import ICorePlugin  # noqa: PLC0415

            if not isinstance(plugin, ICorePlugin):
                # 构造一个最小化的 PluginContext 用于检查
                from pipeline.plugin import PluginContext  # noqa: PLC0415

                mock_ctx = PluginContext(state={}, _services={})
                await plugin.execute(mock_ctx)

            return True
        except Exception as exc:
            logger.warning("Health check failed for plugin '%s': %s", plugin, exc)
            return False

    def get_snapshot(self, swap_id: str) -> SwapSnapshot | None:
        """获取替换快照。

        Args:
            swap_id: 替换操作 ID

        Returns:
            快照实例，不存在时返回 None
        """
        return self._snapshots.get(swap_id)

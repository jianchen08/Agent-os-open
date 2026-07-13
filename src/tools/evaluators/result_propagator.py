"""
评估结果传播器

负责将评估结果传播到调用方、存储系统等
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from src.tools.evaluators.result_wrapper import EvaluationResult, EvaluationSummary

logger = logging.getLogger(__name__)


class ResultPropagationCallback:
    """结果传播回调"""

    def __init__(
        self,
        name: str,
        callback: Callable[[EvaluationResult], Any],
        enabled: bool = True,
    ):
        """
        初始化回调

        Args:
            name: 回调名称
            callback: 回调函数
            enabled: 是否启用
        """
        self.name = name
        self.callback = callback
        self.enabled = enabled

    async def invoke(self, result: EvaluationResult) -> Any:
        """
        调用回调

        Args:
            result: 评估结果

        Returns:
            回调返回值
        """
        if not self.enabled:
            return None

        try:
            return await self.callback(result)
        except Exception as e:
            logger.error(f"回调 {self.name} 执行失败: {e}")
            return None


class ResultPropagator:
    """
    评估结果传播器

    负责将评估结果传播到各个地方：
    - 调用方（通过回调）
    - 存储系统（数据库）
    - 日志系统
    - 监控系统
    """

    def __init__(self):
        """初始化传播器"""
        self._callbacks: list[ResultPropagationCallback] = []
        self._storage_backend: Callable | None = None

    def register_callback(
        self,
        name: str,
        callback: Callable[[EvaluationResult], Any],
        enabled: bool = True,
    ) -> None:
        """
        注册回调

        Args:
            name: 回调名称
            callback: 回调函数
            enabled: 是否启用
        """
        self._callbacks.append(ResultPropagationCallback(name=name, callback=callback, enabled=enabled))
        logger.info(f"已注册评估结果回调: {name}")

    def unregister_callback(self, name: str) -> bool:
        """
        取消注册回调

        Args:
            name: 回调名称

        Returns:
            是否成功取消
        """
        original_length = len(self._callbacks)
        self._callbacks = [cb for cb in self._callbacks if cb.name != name]
        removed = len(self._callbacks) < original_length

        if removed:
            logger.info(f"已取消评估结果回调: {name}")

        return removed

    def enable_callback(self, name: str) -> bool:
        """
        启用回调

        Args:
            name: 回调名称

        Returns:
            是否成功启用
        """
        for cb in self._callbacks:
            if cb.name == name:
                cb.enabled = True
                logger.info(f"已启用评估结果回调: {name}")
                return True
        return False

    def disable_callback(self, name: str) -> bool:
        """
        禁用回调

        Args:
            name: 回调名称

        Returns:
            是否成功禁用
        """
        for cb in self._callbacks:
            if cb.name == name:
                cb.enabled = False
                logger.info(f"已禁用评估结果回调: {name}")
                return True
        return False

    def set_storage_backend(self, backend: Callable) -> None:
        """
        设置存储后端

        Args:
            backend: 存储后端函数
        """
        self._storage_backend = backend
        logger.info("已设置评估结果存储后端")

    async def propagate(
        self,
        result: EvaluationResult,
        propagate_to_callbacks: bool = True,
        propagate_to_storage: bool = True,
    ) -> dict[str, Any]:
        """
        传播评估结果

        Args:
            result: 评估结果
            propagate_to_callbacks: 是否传播到回调
            propagate_to_storage: 是否传播到存储

        Returns:
            传播结果摘要
        """
        propagation_summary = {
            "callback_results": [],
            "storage_result": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # 传播到回调
        if propagate_to_callbacks:
            for callback in self._callbacks:
                if callback.enabled:
                    cb_result = await callback.invoke(result)
                    propagation_summary["callback_results"].append(
                        {
                            "callback": callback.name,
                            "result": str(cb_result),
                        }
                    )

        # 传播到存储
        if propagate_to_storage and self._storage_backend:
            try:
                storage_result = await self._storage_backend(result)
                propagation_summary["storage_result"] = str(storage_result)
            except Exception as e:
                logger.error(f"存储评估结果失败: {e}")
                propagation_summary["storage_result"] = f"error: {e}"

        return propagation_summary

    async def propagate_summary(
        self,
        summary: EvaluationSummary,
        propagate_to_callbacks: bool = True,
        propagate_to_storage: bool = True,
    ) -> dict[str, Any]:
        """
        传播评估摘要

        Args:
            summary: 评估摘要
            propagate_to_callbacks: 是否传播到回调
            propagate_to_storage: 是否传播到存储

        Returns:
            传播结果摘要
        """
        propagation_summary = {
            "callback_results": [],
            "storage_result": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # 传播每个结果
        for result in summary.results:
            result_summary = await self.propagate(
                result,
                propagate_to_callbacks=propagate_to_callbacks,
                propagate_to_storage=propagate_to_storage,
            )
            propagation_summary["callback_results"].extend(result_summary["callback_results"])

        return propagation_summary

    def clear_callbacks(self) -> None:
        """清除所有回调"""
        self._callbacks.clear()
        logger.info("已清除所有评估结果回调")


# 全局传播器实例
_global_propagator: ResultPropagator | None = None


def get_global_propagator() -> ResultPropagator:
    """
    获取全局传播器实例

    Returns:
        全局传播器
    """
    global _global_propagator  # noqa: PLW0603
    if _global_propagator is None:
        _global_propagator = ResultPropagator()
    return _global_propagator


def set_global_propagator(propagator: ResultPropagator) -> None:
    """
    设置全局传播器

    Args:
        propagator: 传播器实例
    """
    global _global_propagator  # noqa: PLW0603
    _global_propagator = propagator

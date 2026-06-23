"""
评估指标文件加载器

从 config/evaluation_metrics/ 目录加载 YAML 格式的评估指标配置。
提供与原 EvaluationMetricService 兼容的接口。

核心变更：
- 字段名保持不变（evaluator_id、default_config）
- 新增 expect 字段（断言规则）
- 新增 expected_input 字段（预期输入定义）
- 新增 expected_output 字段（预期输出定义）
- default_config 支持 {{变量}} 占位符

使用示例:
    >>> from src.evaluation.metric_loader import get_metric_loader
    >>>
    >>> loader = get_metric_loader()
    >>> metric = await loader.get_metric("file_check")
    >>> metrics = await loader.get_metrics_by_ids(["file_check", "bash_check"])
    >>> expected_input = loader.get_expected_input("file_check")
    >>> expected_output = loader.get_expected_output("file_check")
"""

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class MetricLoader:
    """
    评估指标文件加载器

    职责：
    1. 从 config/evaluation_metrics/ 目录加载 YAML 文件
    2. 提供内存缓存
    3. 支持按 ID/名称查询
    4. 提供与原 EvaluationMetricService 兼容的接口

    字段变更：
    - 字段名保持不变
    - 新增 expect 字段
    - 新增 expected_input 字段（预期输入定义）
    - 新增 expected_output 字段（预期输出定义）
    """

    def __init__(self, config_dir: str = "config/evaluation_metrics"):
        """
        初始化评估指标加载器

        Args:
            config_dir: 配置文件目录路径
        """
        self._config_dir = Path(config_dir)
        self._cache: dict[str, dict[str, Any]] = {}
        self._name_to_id: dict[str, str] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """
        确保配置已加载

        使用懒加载模式，首次访问时才加载所有配置。
        """
        if self._loaded:
            return
        self._load_all()
        self._loaded = True

    def _load_all(self) -> None:
        """
        加载所有评估指标配置文件

        遍历配置目录，加载所有 YAML 文件到内存缓存。
        """
        if not self._config_dir.exists():
            logger.warning(f"配置目录不存在: {self._config_dir}")
            return

        for file_path in self._config_dir.glob("*.yaml"):
            try:
                with open(file_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)

                if data and "id" in data:
                    metric_id = data["id"]
                    self._cache[metric_id] = data

                    # 建立名称到 ID 的映射
                    if "name" in data:
                        self._name_to_id[data["name"]] = metric_id

                    logger.debug(f"加载评估指标: {metric_id}")

            except Exception as e:
                logger.error(f"加载配置文件失败: {file_path} - {e}")

        logger.info(f"已加载 {len(self._cache)} 个评估指标")

    def reload(self) -> None:
        """
        重新加载配置文件

        清空缓存并重新加载所有配置文件。
        用于配置文件更新后的热重载。
        """
        self._cache.clear()
        self._name_to_id.clear()
        self._loaded = False
        self._ensure_loaded()
        logger.info("评估指标配置已重新加载")

    async def get_metric(self, metric_id: str) -> dict[str, Any] | None:
        """
        按 ID 获取评估指标

        Args:
            metric_id: 指标 ID

        Returns:
            指标配置字典，不存在返回 None
        """
        self._ensure_loaded()
        return self._cache.get(metric_id)

    async def get_metric_by_name(self, name: str) -> dict[str, Any] | None:
        """
        按名称获取评估指标

        Args:
            name: 指标名称

        Returns:
            指标配置字典，不存在返回 None
        """
        self._ensure_loaded()
        metric_id = self._name_to_id.get(name)
        if metric_id:
            return self._cache.get(metric_id)
        return None

    async def get_metrics_by_ids(self, metric_ids: list[str]) -> list[dict[str, Any]]:
        """
        批量获取评估指标

        Args:
            metric_ids: 指标 ID 列表

        Returns:
            指标配置字典列表（只返回存在的指标）
        """
        self._ensure_loaded()
        return [self._cache[mid] for mid in metric_ids if mid in self._cache]

    async def list_metrics(
        self,
        category: str | None = None,
        status: str = "active",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        列出评估指标

        Args:
            category: 按分类过滤（可选）
            status: 按状态过滤（默认 active）
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            指标配置字典列表
        """
        self._ensure_loaded()
        metrics = list(self._cache.values())

        # 按状态过滤
        metrics = [m for m in metrics if m.get("status", "active") == status]

        # 按分类过滤
        if category:
            metrics = [m for m in metrics if m.get("category") == category]

        return metrics[offset : offset + limit]

    async def get_categories(self) -> list[str]:
        """
        获取所有指标分类

        Returns:
            分类列表（去重后）
        """
        self._ensure_loaded()
        categories = set()
        for metric in self._cache.values():
            if "category" in metric:
                categories.add(metric["category"])
        return sorted(categories)

    def render_config(
        self,
        metric: dict[str, Any],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """
        渲染配置参数

        将 default_config 中的 {{变量}} 占位符替换为实际参数值。

        Args:
            metric: 指标配置
            params: 参数值字典

        Returns:
            渲染后的配置字典
        """
        default_config = metric.get("default_config", {})
        rendered = dict(default_config)

        # 合并用户提供的参数
        rendered.update(params)

        # 渲染占位符
        for key, value in rendered.items():
            if isinstance(value, str):
                rendered[key] = self._render_placeholders(value, params)

        return rendered

    def _render_placeholders(self, template: str, params: dict[str, Any]) -> str:
        """
        渲染字符串中的占位符

        Args:
            template: 包含 {{变量}} 占位符的模板字符串
            params: 参数值字典

        Returns:
            渲染后的字符串
        """
        pattern = r"\{\{(\w+)\}\}"

        def replace(match: re.Match) -> str:
            var_name = match.group(1)
            if var_name in params:
                return str(params[var_name])
            return match.group(0)  # 保留未找到的占位符

        return re.sub(pattern, replace, template)

    def get_expected_input(self, metric_id: str) -> dict[str, Any] | None:
        """
        获取预期输入配置

        Args:
            metric_id: 指标 ID

        Returns:
            预期输入配置字典，不存在返回 None

        Example:
            >>> loader = get_metric_loader()
            >>> expected_input = loader.get_expected_input("file_check")
            >>> print(expected_input["params"]["path"]["type"])  # string
        """
        self._ensure_loaded()
        metric = self._cache.get(metric_id)
        if metric:
            return metric.get("expected_input")
        return None

    def get_expected_output(self, metric_id: str) -> dict[str, Any] | None:
        """
        获取预期输出配置

        Args:
            metric_id: 指标 ID

        Returns:
            预期输出配置字典，不存在返回 None

        Example:
            >>> loader = get_metric_loader()
            >>> expected_output = loader.get_expected_output("file_check")
            >>> print(expected_output["params"]["success"]["type"])  # boolean
        """
        self._ensure_loaded()
        metric = self._cache.get(metric_id)
        if metric:
            return metric.get("expected_output")
        return None

    def validate_expect_conditions(self, metric: dict[str, Any]) -> list[str]:
        """
        验证条件配置的有效性

        Args:
            metric: 指标配置

        Returns:
            错误消息列表（空列表表示验证通过）

        Example:
            >>> loader = get_metric_loader()
            >>> metric = await loader.get_metric("file_check")
            >>> errors = loader.validate_expect_conditions(metric)
            >>> if errors:
            ...     print(f"配置错误: {errors}")
        """
        from src.evaluation.expect_evaluator import ExpectConditionEvaluator  # noqa: PLC0415

        errors = []
        expect = metric.get("expect", {})

        # 验证条件配置
        condition_errors = ExpectConditionEvaluator.validate_expect(expect)
        errors.extend(condition_errors)

        return errors


# 全局单例
_metric_loader: MetricLoader | None = None


def get_metric_loader() -> MetricLoader:
    """
    获取评估指标加载器单例

    Returns:
        MetricLoader 实例
    """
    global _metric_loader  # noqa: PLW0603
    if _metric_loader is None:
        _metric_loader = MetricLoader()
    return _metric_loader


def reset_metric_loader() -> None:
    """
    重置评估指标加载器单例

    主要用于测试场景。
    """
    global _metric_loader  # noqa: PLW0603
    _metric_loader = None

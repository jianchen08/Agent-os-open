"""YAML 指标文件加载器。

从 config/evaluation_metrics/ 目录加载所有评估指标 YAML 文件，
解析为 MetricDefinition 实例并注册到指标注册表。

精简原则：
- 保留核心字段（id/name/description/evaluator_type/expect/default_config）
- 去掉过时字段（expected_input/expected_output 保留描述，去掉完整 schema 定义）
- input_schema 保留供运行时参数校验
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from evaluation.types import (
    ExpectCondition,
    ExpectSpec,
    MetricDefinition,
    MetricType,
)

logger = logging.getLogger(__name__)

# 指标 YAML 中的 evaluator_type 到 MetricType 的映射
_EVALUATOR_TYPE_MAP: dict[str, MetricType] = {
    "tool": MetricType.TOOL,
    "agent": MetricType.AGENT,
    "human": MetricType.HUMAN,
}


class MetricLoader:
    """YAML 指标文件加载器。

    从指定目录加载评估指标 YAML 文件，解析为 MetricDefinition 实例。

    Attributes:
        _metrics: 已加载的指标字典（key=metric_id, value=MetricDefinition）
        _metrics_dir: 指标文件目录路径
    """

    def __init__(self, metrics_dir: str | Path | None = None) -> None:
        """初始化指标加载器。

        Args:
            metrics_dir: 指标 YAML 文件目录路径，
                         None 时使用默认路径 config/evaluation_metrics/
        """
        if metrics_dir is None:
            self._metrics_dir = Path.cwd() / "config" / "evaluation_metrics"
        else:
            self._metrics_dir = Path(metrics_dir)
        self._metrics: dict[str, MetricDefinition] = {}

    @property
    def metrics(self) -> dict[str, MetricDefinition]:
        """获取已加载的指标字典。"""
        return self._metrics

    def load_all(self) -> dict[str, MetricDefinition]:
        """加载目录下所有 YAML 指标文件。

        Returns:
            加载后的指标字典（key=metric_id, value=MetricDefinition）

        Raises:
            FileNotFoundError: 指标目录不存在
        """
        if not self._metrics_dir.exists():
            raise FileNotFoundError(f"Metrics directory not found: {self._metrics_dir}")

        yaml_files = sorted(self._metrics_dir.glob("*.yaml"))
        if not yaml_files:
            logger.warning("No YAML files found in %s", self._metrics_dir)
            return self._metrics

        for yaml_file in yaml_files:
            try:
                definition = self._load_file(yaml_file)
                self._metrics[definition.id] = definition
                logger.debug("Loaded metric: %s from %s", definition.id, yaml_file.name)
            except Exception as e:
                logger.error("Failed to load metric from %s: %s", yaml_file.name, e)

        logger.info("Loaded %d metrics from %s", len(self._metrics), self._metrics_dir)
        return self._metrics

    def load_one(self, metric_id: str) -> MetricDefinition | None:
        """加载单个指标文件。

        Args:
            metric_id: 指标 ID（对应文件名如 format_valid → format_valid.yaml）

        Returns:
            加载后的 MetricDefinition，文件不存在时返回 None
        """
        yaml_file = self._metrics_dir / f"{metric_id}.yaml"
        if not yaml_file.exists():
            logger.warning("Metric file not found: %s", yaml_file)
            return None

        try:
            definition = self._load_file(yaml_file)
            self._metrics[definition.id] = definition
            return definition
        except Exception as e:
            logger.error("Failed to load metric %s: %s", metric_id, e)
            return None

    def get(self, metric_id: str) -> MetricDefinition | None:
        """获取已加载的指标定义。

        Args:
            metric_id: 指标 ID

        Returns:
            MetricDefinition，不存在时返回 None
        """
        return self._metrics.get(metric_id)

    def add_metric(self, metric: MetricDefinition) -> None:
        """动态添加指标定义。

        用于运行时注入动态生成的指标（如模板评估维度转换而来的指标），
        无需通过 YAML 文件加载。

        Args:
            metric: 指标定义对象
        """
        self._metrics[metric.id] = metric
        logger.debug("Dynamically added metric: %s", metric.id)

    def list_metrics(self) -> list[str]:
        """列出所有已加载的指标 ID。

        Returns:
            指标 ID 列表
        """
        return list(self._metrics.keys())

    def _load_file(self, path: Path) -> MetricDefinition:
        """解析单个 YAML 文件为 MetricDefinition。

        Args:
            path: YAML 文件路径

        Returns:
            解析后的 MetricDefinition

        Raises:
            yaml.YAMLError: YAML 解析错误
            KeyError: 缺少必需字段
        """
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or "id" not in data:
            raise KeyError(f"Missing required field 'id' in {path}")

        # 解析 evaluator_type → MetricType
        evaluator_type_str = data.get("evaluator_type", "tool")
        metric_type = _EVALUATOR_TYPE_MAP.get(evaluator_type_str, MetricType.TOOL)

        # 解析 expect 条件
        expect = self._parse_expect(data.get("expect", {}))

        return MetricDefinition(
            id=data["id"],
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            metric_type=metric_type,
            evaluator_id=data.get("evaluator_id", ""),
            default_config=data.get("default_config", {}),
            expect=expect,
            input_schema=data.get("input_schema", {}),
            input_mapping=data.get("input_mapping", {}),
            is_red_line=data.get("is_red_line", False),
            default_weight=data.get("default_weight", 1.0),
            level=data.get("level", 1),
            includes=data.get("includes", []),
            requires=data.get("requires", []),
            tags=data.get("tags", []),
            status=data.get("status", "active"),
        )

    def _parse_expect(self, data: dict[str, Any]) -> ExpectSpec:
        """解析 expect 段。

        Args:
            data: expect 段的原始数据

        Returns:
            ExpectSpec 实例
        """
        conditions: list[ExpectCondition] = []
        for cond_data in data.get("conditions", []):
            conditions.append(
                ExpectCondition(
                    field=cond_data.get("field", ""),
                    operator=cond_data.get("operator", "is_true"),
                    value=cond_data.get("value"),
                )
            )

        return ExpectSpec(
            conditions=conditions,
            logic=data.get("logic", "and"),
            pass_message=data.get("pass_message", "评估通过"),
            fail_message=data.get("fail_message", "评估未通过"),
        )

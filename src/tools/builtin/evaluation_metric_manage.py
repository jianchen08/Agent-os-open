"""
评估指标管理工具

提供评估指标的查询功能

注意：评估指标已迁移到文件存储，不再支持动态创建、更新和删除。
"""

from typing import Any

from src.core.results import ToolExecutionResult
from src.evaluation.metric_loader import get_metric_loader
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class EvaluationMetricManageTool:
    """
    评估指标管理工具

    提供：
    - 查询评估指标
    - 列出评估指标

    注意：评估指标已迁移到文件存储，不再支持动态创建、更新和删除。
    """

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="evaluation_metric_manage",
            description="""
评估指标管理工具（只读）

提供评估指标的查询和列表功能。

注意：评估指标已迁移到文件存储，不再支持动态创建、更新和删除。
如需添加新指标，请在 config/evaluation_metrics/ 目录下创建 YAML 配置文件。

支持的操作：
- query: 查询指标详情（按ID或名称）
- list: 列出指标列表（支持按分类过滤）
""".strip(),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["query", "list"],
                        "description": "操作类型：query-查询指标详情，list-列出指标列表",
                    },
                    "metric_id": {
                        "type": "string",
                        "description": "指标ID，query操作时使用",
                    },
                    "metric_name": {
                        "type": "string",
                        "description": "指标名称，query操作时可用名称查询",
                    },
                    "filters": {
                        "type": "object",
                        "description": "过滤条件，list操作时使用",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "按分类过滤",
                            },
                            "status": {
                                "type": "string",
                                "description": "按状态过滤（默认 active）",
                            },
                        },
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.EVALUATION,
            level=ToolLevel.ALL,
            requires_approval=False,
            dangerous_operations=[],
            tags=["evaluation", "metric", "query"],
        )

    async def execute(
        self,
        action: str,
        metric_id: str | None = None,
        metric_name: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> ToolExecutionResult:
        """
        执行工具

        Args:
            action: 操作类型（query/list）
            metric_id: 指标ID
            metric_name: 指标名称
            filters: 过滤条件

        Returns:
            工具执行结果
        """
        metric_loader = get_metric_loader()

        if action == "query":
            return await self._query_metric(metric_loader, metric_id, metric_name)
        elif action == "list":
            return await self._list_metrics(metric_loader, filters)
        else:
            return create_failure_result(
                error=f"不支持的操作类型: {action}。支持的操作：query, list",
                error_code="INVALID_ACTION",
            )

    async def _query_metric(
        self,
        metric_loader,
        metric_id: str | None,
        metric_name: str | None,
    ) -> ToolResult:
        """
        查询评估指标

        Args:
            metric_loader: 指标加载器
            metric_id: 指标ID
            metric_name: 指标名称

        Returns:
            工具结果
        """
        if not metric_id and not metric_name:
            return create_failure_result(
                error="必须提供 metric_id 或 metric_name",
                error_code="MISSING_IDENTIFIER",
            )

        if metric_id:
            metric = await metric_loader.get_metric(metric_id)
        else:
            metric = await metric_loader.get_metric_by_name(metric_name)

        if not metric:
            return create_failure_result(
                error=f"指标不存在: {metric_id or metric_name}",
                error_code="NOT_FOUND",
            )

        return create_success_result(
            data={
                "metric": {
                    "id": metric.get("id"),
                    "name": metric.get("name", ""),
                    "description": metric.get("description", ""),
                    "category": metric.get("category", ""),
                    "evaluator_type": metric.get("evaluator_type", "tool"),
                    "evaluator_id": metric.get("evaluator_id", ""),
                    "default_config": metric.get("default_config", {}),
                    "input_schema": metric.get("input_schema", {}),
                    "includes": metric.get("includes", []),
                    "requires": metric.get("requires", []),
                    "level": metric.get("level", 1),
                    "is_red_line": metric.get("is_red_line", False),
                    "default_weight": metric.get("default_weight", 1.0),
                    "source": metric.get("source", "builtin"),
                    "status": metric.get("status", "active"),
                    "tags": metric.get("tags", []),
                    "when_to_use": metric.get("when_to_use", []),
                    "when_not_to_use": metric.get("when_not_to_use", []),
                    "examples": metric.get("examples", []),
                    "caveats": metric.get("caveats", []),
                },
            }
        )

    async def _list_metrics(
        self,
        metric_loader,
        filters: dict[str, Any] | None,
    ) -> ToolResult:
        """
        列出评估指标

        Args:
            metric_loader: 指标加载器
            filters: 过滤条件

        Returns:
            工具结果
        """
        category = None
        status = "active"

        if filters:
            category = filters.get("category")
            status = filters.get("status", "active")

        metrics = await metric_loader.list_metrics(
            category=category,
            status=status,
            limit=100,
        )

        return create_success_result(
            data={
                "metrics": [
                    {
                        "id": m.get("id"),
                        "name": m.get("name", ""),
                        "description": m.get("description", ""),
                        "category": m.get("category", ""),
                        "evaluator_type": m.get("evaluator_type", "tool"),
                        "status": m.get("status", "active"),
                    }
                    for m in metrics
                ],
                "count": len(metrics),
            }
        )

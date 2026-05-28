"""MemoryMaintenanceService - 内存维护服务，负责触发复盘流程。"""
from __future__ import annotations

from typing import Any

from src.memory.maintenance.review_engine import Pipeline, ReviewEngine


class MemoryMaintenanceService:
    """内存维护服务。

    职责：封装复盘引擎，提供高层接口供外部调用。
    注意：此服务与 ReviewEngine 之间存在接口不匹配问题。
    """

    def __init__(self) -> None:
        self._engine = ReviewEngine()

    def trigger_review(self, pipeline_configs: list[dict[str, Any]]) -> dict[str, Any]:
        """触发复盘流程。

        Args:
            pipeline_configs: pipeline 配置列表，每项包含 pipeline_id 和 errors。

        Returns:
            复盘结果摘要。
        """
        result: dict[str, Any] = {
            "phase": "MemoryMaintenanceService",
            "interface_check": None,
            "review_result": None,
        }

        # 检测接口不匹配问题
        interface_issues = self._check_interface_compatibility()
        result["interface_check"] = interface_issues

        # 尝试通过 service 层调用 ReviewEngine
        try:
            pipelines = self._build_pipelines(pipeline_configs)
            # 接口不匹配：service 层期望 run_batch_review 方法，但 ReviewEngine 只有 run_review
            self._engine.register_pipelines(pipelines)
            review_result = self._engine.run_review()
            result["review_result"] = review_result
        except AttributeError as e:
            result["review_result"] = {
                "error": f"接口不匹配: {e}",
                "status": "interface_mismatch",
            }
        except Exception as e:
            result["review_result"] = {
                "error": f"未知异常: {e}",
                "status": "failed",
            }

        return result

    def _check_interface_compatibility(self) -> list[dict[str, str]]:
        """检查 service 与 ReviewEngine 的接口兼容性。"""
        issues: list[dict[str, str]] = []

        # 检查 ReviewEngine 是否有 service 期望的方法
        expected_methods = ["run_batch_review", "get_summary", "reset"]
        for method_name in expected_methods:
            if not hasattr(self._engine, method_name):
                issues.append({
                    "missing_method": method_name,
                    "description": f"ReviewEngine 缺少 {method_name} 方法，MemoryMaintenanceService 期望调用此方法",
                    "severity": "high" if method_name == "run_batch_review" else "medium",
                })

        return issues

    def _build_pipelines(self, configs: list[dict[str, Any]]) -> list[Pipeline]:
        """从配置构建 Pipeline 对象。"""
        from src.memory.maintenance.review_engine import ErrorRecord

        pipelines: list[Pipeline] = []
        for config in configs:
            errors = [
                ErrorRecord(
                    error_id=e.get("error_id", f"err-{i}"),
                    error_type=e.get("error_type", "unknown"),
                    message=e.get("message", ""),
                    timestamp=e.get("timestamp", ""),
                )
                for i, e in enumerate(config.get("errors", []))
            ]
            pipeline = Pipeline(
                pipeline_id=config["pipeline_id"],
                errors=errors,
            )
            pipelines.append(pipeline)
        return pipelines

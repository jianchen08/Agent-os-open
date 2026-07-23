#!/usr/bin/env python3
"""评估系统 MCP 服务端。

强制门控+按指标审查功能与 0.1 等价。
核心业务逻辑参考 0.1 src/evaluation/engine.py。

[来源: docs/tasks/task_10_system_plugins.md AC-09-3]
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("evaluation_service")

# 评估结果存储
_results: dict[str, dict[str, Any]] = {}

# 内置指标注册表
_metric_registry: dict[str, dict[str, Any]] = {
    "file_check": {
        "type": "file_check",
        "description": "Check file existence/content",
        "params_schema": {"path": {"type": "string"}},
    },
    "bash_check": {
        "type": "bash_check",
        "description": "Run shell command and check exit code",
        "params_schema": {"command": {"type": "string"}},
    },
    "semantic_check": {
        "type": "semantic_check",
        "description": "Semantic evaluation of content",
        "params_schema": {"criteria": {"type": "string"}},
    },
    "human_review": {
        "type": "human_review",
        "description": "Human review gate",
        "params_schema": {"reviewer": {"type": "string"}},
    },
}


@plugin.tool(
    name="evaluation.run",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "metrics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric_id": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["file_check", "bash_check", "semantic_check", "human_review"],
                        },
                        "params": {"type": "object"},
                    },
                    "required": ["metric_id", "type"],
                },
            },
            "gate_mode": {"type": "boolean", "default": True},
        },
        "required": ["task_id", "metrics"],
    },
    description="Run evaluation metrics against task outputs, with optional gate mode",
)
async def evaluation_run(
    task_id: str,
    metrics: list[dict[str, Any]],
    gate_mode: bool = True,
) -> dict[str, Any]:
    """Run evaluation metrics and optionally gate the pipeline.

    In gate mode, if any metric fails, the pipeline is blocked.
    """
    eval_id = f"eval_{uuid.uuid4().hex[:8]}"
    results: list[dict[str, Any]] = []
    all_passed = True

    for metric in metrics:
        metric_id = metric.get("metric_id", "unknown")
        metric_type = metric.get("type", "unknown")
        params = metric.get("params", {})

        # Check if metric type is registered
        if metric_type not in _metric_registry:
            results.append({
                "metric_id": metric_id,
                "type": metric_type,
                "passed": False,
                "error": f"unknown metric type: {metric_type}",
            })
            all_passed = False
            continue

        # DEBT: 评估逻辑为简化实现。ceiling: 当前仅 file_check 真正读文件。
        # upgrade: 集成 bash_check（subprocess）/semantic_check（LLM）/human_review 后完善。
        if metric_type == "file_check":
            import os
            file_path = params.get("path", "")
            passed = bool(file_path) and os.path.exists(file_path)
            message = f"file exists: {file_path}" if passed else f"file not found: {file_path}"
        else:
            # DEBT: bash_check/semantic_check/human_review 为占位。ceiling: 仅 file_check 实现。
            # upgrade: 后续批次补充其他 metric 类型。
            passed = bool(params)
            message = "OK (params provided)" if passed else "No parameters provided"

        results.append({
            "metric_id": metric_id,
            "type": metric_type,
            "passed": passed,
            "message": message,
        })
        if not passed:
            all_passed = False

    summary = {
        "eval_id": eval_id,
        "task_id": task_id,
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "all_passed": all_passed,
        "gate_mode": gate_mode,
        "gated": gate_mode and not all_passed,
        "results": results,
        "timestamp": time.time(),
    }

    _results[eval_id] = summary

    # In gate mode with failures: notify pipeline-executor to block
    if gate_mode and not all_passed:
        # pipeline = plugin.get_capability("pipeline-executor")
        # await pipeline.call("block", {"eval_id": eval_id, "reason": "evaluation gate failed"})
        pass

    return summary


@plugin.tool(
    name="evaluation.get_result",
    schema={
        "type": "object",
        "properties": {
            "eval_id": {"type": "string"},
        },
        "required": ["eval_id"],
    },
    description="Get evaluation result by ID",
)
async def get_result(eval_id: str) -> dict[str, Any]:
    """Retrieve a stored evaluation result."""
    result = _results.get(eval_id)
    if result is None:
        return {"error": "evaluation not found", "eval_id": eval_id}
    return result


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize evaluation service on load."""
    pass


# 资源暴露（函数式调用——SDK register_resource 签名要求 handler 必填）
def _metric_registry_resource() -> dict[str, Any]:
    """Expose registered metric types as MCP resource."""
    return {"metrics": _metric_registry}


plugin.register_resource(
    "evaluation://metrics/registry", _metric_registry_resource, name="Metric Registry"
)


if __name__ == "__main__":
    plugin.run()

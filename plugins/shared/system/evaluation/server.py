#!/usr/bin/env python3
"""评估系统 MCP 服务端。

强制门控+按指标审查功能与 0.1 等价。
核心业务逻辑参考 0.1 src/evaluation/engine.py。

[来源: docs/tasks/task_10_system_plugins.md AC-09-3]
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any

import yaml

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("evaluation_service")

# http.handle 响应封装（内核 HttpHandleResponse/ToolExecutionResult 样板）：
# 公共实现 plugins/shared/http_json.py，经共享层自举裸名导入。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from http_json import (  # noqa: E402
    error as _error,
    json_response as _json_response,
    ok as _ok,
)

# ── HTTP 端点（http.handle）—— 前端 /ext/evaluation_service/metrics 入口 ──────
# 内核 http_dispatcher 透传：dispatcher 把 HttpHandleRequest（method/path/raw_body/
# headers/query/plugin_id）整体作为 arguments 传给本工具。本工具按 path 分发，
# 读 config_files 声明的汇总 yaml（config/evaluation/evaluation_metrics.yaml），
# 返回 ToolExecutionResult{success,data}。
# data 必须是 HttpHandleResponse{status,headers,body,body_encoding}，body 需 base64。
# 字段形状对齐 frontend/src/services/api/evaluationMetrics.ts 的 EvaluationMetric 类型。


# 指标汇总文件：与 manifest 的 config_files.evaluation_metrics.path 一致（唯一真相源）。
_METRICS_YAML = os.path.join("config", "evaluation", "evaluation_metrics.yaml")


def _project_root() -> str:
    """定位项目根：优先 AGENTOS_PROJECT_ROOT，回退相对路径上溯。"""
    root = os.environ.get("AGENTOS_PROJECT_ROOT")
    if root and os.path.isdir(root):
        return root
    # sidecar 工作目录通常是项目根；上溯最多 6 层找 config/ 目录兜底
    cur = os.getcwd()
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "config")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.getcwd()


def _load_metrics() -> list[dict[str, Any]]:
    """读汇总 yaml 的 metrics 数组。

    文件不存在/不可读/解析失败 → 抛出（配置损坏 ≠ 无指标，由
    :func:`_load_metrics_checked` 转 5xx 错误信封）；解析成功但内容为空
    （空文件/无 metrics 键）→ 真实空注册表，返回 []。
    """
    yaml_path = os.path.join(_project_root(), _METRICS_YAML)
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    metrics = data.get("metrics", []) if isinstance(data, dict) else []
    return [m for m in metrics if isinstance(m, dict)]


def _load_metrics_checked() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """读指标表并把读失败翻译成 HTTP 错误信封。

    Returns:
        (metrics, None) = 读成功；( [], error_envelope) = 读失败（500）。
    """
    try:
        return _load_metrics(), None
    except (OSError, yaml.YAMLError) as exc:
        return [], _error(f"评估指标配置读取失败: {exc}", 500)


def _metric_to_response(raw: dict[str, Any]) -> dict[str, Any]:
    """把汇总 yaml 的单条 metric 补齐成前端 EvaluationMetric 类型要求的字段。

    汇总 yaml 已含 name/description/category/evaluator_type/evaluator_id/level/
    includes/requires/input_schema/tags；补齐 yaml 没有但前端类型要求的字段。
    """
    return {
        "id": str(raw.get("name", raw.get("id", ""))),
        "name": str(raw.get("name", "")),
        "description": str(raw.get("description", "")),
        "category": str(raw.get("category", raw.get("metric_type", ""))),
        "evaluator_type": str(raw.get("evaluator_type", "")),
        "evaluator_id": str(raw.get("evaluator_id", "")),
        "default_config": raw.get("default_config"),
        "input_schema": raw.get("input_schema"),
        "default_pass_threshold": raw.get("default_pass_threshold"),
        "includes": list(raw.get("includes", []) or []),
        "requires": list(raw.get("requires", []) or []),
        "level": int(raw.get("level", 0)) if raw.get("level") is not None else 0,
        "is_red_line": bool(raw.get("is_red_line", False)),
        "default_weight": float(raw.get("default_weight", 1.0)) if raw.get("default_weight") is not None else 1.0,
        "source": str(raw.get("source", "builtin")),
        "status": str(raw.get("status", "active")),
        "tags": list(raw.get("tags", []) or []),
        "usage_count": 0,
        "success_count": 0,
        "avg_execution_time": None,
        "created_at": "",
        "updated_at": None,
    }

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

        # 本端点**不是执行面权威**：评估执行由 task_evaluate 插件的
        # PipelineEvaluationExecutor 真实装配（tool 型本地执行 + agent 型派
        # 评估子管道继承任务工作区，见
        # plugins/shared/tools/task_evaluate/_executor.py）。本插件保留指标
        # 注册表 + metrics HTTP 读面；evaluation.run 维持诚实 stub 语义（未实现
        # 类型一律判失败），调用方应改走 task_evaluate 工具。
        if metric_type == "file_check":
            import os
            file_path = params.get("path", "")
            passed = bool(file_path) and os.path.exists(file_path)
            message = f"file exists: {file_path}" if passed else f"file not found: {file_path}"
            error = None
        else:
            # 显式 stub（诚实语义）：bash_check/semantic_check/human_review 在本
            # 端点未实现，一律判失败并说明原因——执行面走 task_evaluate 的
            # PipelineEvaluationExecutor。
            passed = False
            message = None
            error = f"metric type not implemented: {metric_type}"

        result_entry: dict[str, Any] = {
            "metric_id": metric_id,
            "type": metric_type,
            "passed": passed,
        }
        if message is not None:
            result_entry["message"] = message
        if error is not None:
            result_entry["error"] = error
        results.append(result_entry)
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
        # gate 未实现：pipeline-executor block 通道未接线，gated 恒 False——
        # 如实报告"门控未生效"，不返回假 gated:true 让调用方误以为已拦截。
        "gated": False,
        "gate_enforced": False,
        "note": "gate 未实现",
        "results": results,
        "timestamp": time.time(),
    }

    _results[eval_id] = summary

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


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/evaluation_service/metrics (evaluation metrics registry)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到评估指标端点。

    签名覆盖 HttpHandleRequest 全部字段（SDK 的 td.handler(**arguments) 展开）。
    读汇总 yaml（config_files 声明的唯一真相源），返回前端 EvaluationMetric 形状。
    """
    q = query or {}

    # GET /ext/evaluation_service/metrics —— 列表（支持 category/status/limit/skip 过滤）
    if path == "/ext/evaluation_service/metrics" and method == "GET":
        raw_metrics, read_err = _load_metrics_checked()
        if read_err is not None:
            return read_err
        metrics = [_metric_to_response(m) for m in raw_metrics]
        # 过滤
        category = q.get("category")
        if category:
            metrics = [m for m in metrics if m["category"] == category]
        status_f = q.get("status")
        if status_f:
            metrics = [m for m in metrics if m["status"] == status_f]
        metric_type = q.get("metric_type")
        if metric_type:
            metrics = [m for m in metrics if m["evaluator_type"] == metric_type]
        # 分页
        total = len(metrics)
        try:
            skip = int(q.get("skip", 0))
            limit = int(q.get("limit", total))
        except ValueError:
            skip, limit = 0, total
        page = metrics[skip : skip + limit] if limit > 0 else metrics
        return _ok(_json_response({"metrics": page, "total": total}))

    # GET /ext/evaluation_service/metrics/{metric_id} —— 单项
    prefix = "/ext/evaluation_service/metrics/"
    if path.startswith(prefix) and method == "GET":
        metric_id = path[len(prefix) :]
        raw_metrics, read_err = _load_metrics_checked()
        if read_err is not None:
            return read_err
        for m in raw_metrics:
            if str(m.get("name", m.get("id", ""))) == metric_id:
                return _ok(_json_response(_metric_to_response(m)))
        return _error(f"评估指标 '{metric_id}' 不存在", 404)

    # DELETE /ext/evaluation_service/metrics/{metric_id} —— 内置指标只读，拒绝
    if path.startswith(prefix) and method == "DELETE":
        metric_id = path[len(prefix) :]
        # 诚实语义：内置指标定义在 config_files（只读资源），不可删除。
        # 前端 deleteEvaluationMetric 已 try/catch 吞错返回 false，行为一致。
        return _error(
            f"内置指标 '{metric_id}' 不可删除（config_files 只读资源）",
            405,
        )

    # 未匹配的 path
    return _ok(_json_response({"error": "not found", "path": path}, 404))


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

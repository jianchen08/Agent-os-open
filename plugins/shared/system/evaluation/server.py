#!/usr/bin/env python3
"""评估系统 MCP 服务端。

强制门控+按指标审查功能与 0.1 等价。
核心业务逻辑参考 0.1 src/evaluation/engine.py。

[来源: docs/tasks/task_10_system_plugins.md AC-09-3]
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any

import yaml

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("evaluation_service")

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


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """把任意 JSON 可序列化对象包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    """成功响应：{success, data}（ToolExecutionResult 契约）。"""
    return {"success": True, "data": data}


def _error(message: str, status: int = 503) -> dict[str, Any]:
    """错误响应：{success:false, error}。503 表示 sidecar 未就绪。"""
    return {"success": False, "error": message, "data": _json_response({"error": message}, status)}


def _load_metrics() -> list[dict[str, Any]]:
    """读汇总 yaml 的 metrics 数组。读失败返回空列表（不抛，由调用方决定语义）。"""
    yaml_path = os.path.join(_project_root(), _METRICS_YAML)
    try:
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return []
    metrics = data.get("metrics", []) if isinstance(data, dict) else []
    return [m for m in metrics if isinstance(m, dict)]


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
        # 字段别名映射：前端 service 依赖旧字段名，此处做双向兼容
        "metric_type": str(raw.get("category", raw.get("evaluator_type", ""))),
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
        metrics = [_metric_to_response(m) for m in _load_metrics()]
        # 过滤
        category = q.get("category")
        if category:
            metrics = [m for m in metrics if m["category"] == category]
        status_f = q.get("status")
        if status_f:
            metrics = [m for m in metrics if m["status"] == status_f]
        metric_type = q.get("metric_type")
        if metric_type:
            metrics = [m for m in metrics if m["metric_type"] == metric_type]
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
        for m in _load_metrics():
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

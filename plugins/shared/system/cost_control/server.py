#!/usr/bin/env python3
"""成本控制 MCP 服务端——纯接口适配层。

老代码从 0.1 src/cost_control/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

channel_api 退役批次 1（config/cost-control 域 → cost_control 插件）：
新增 /ext/cost_control/config/cost-control GET/PUT —— 成本控制 **YAML 配置
全文**读写（config/system/cost_control.yaml，前端 services/api/config.ts 的
CostControlConfigResponse 嵌套形态消费），与既有 /ext/cost_control/config
（展平形态，前端 costControl.ts 消费）并存，语义逐项对齐 channel_api
routes_config.py 的 cost-control 段（_DEFAULT_COST_CONTROL 兜底 + 全文覆写）。

[来源: docs/working/module_migration_plan.md §5.1]
"""
from __future__ import annotations

import base64
import copy
import datetime
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(__file__))  # 让同目录老代码的导入可用

from budget_manager import (
    BudgetAlert,
    BudgetAlertAction,
    BudgetAlertLevel,
    BudgetManager,
    BudgetStatus,
    get_budget_manager,
    reset_budget_manager,
)
from exceptions import BudgetExceededException, QuotaExhaustedException

from agentos_plugin_sdk import AgentOSPlugin
from config import CostControlConfig, get_cost_control_config

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("cost_control")

_budget_manager: BudgetManager | None = None


def _serialize_alert(alert: BudgetAlert | None) -> dict[str, Any] | None:
    """将 BudgetAlert 序列化为可 JSON 化的 dict。"""
    if alert is None:
        return None
    data = asdict(alert)
    data["level"] = alert.level.value
    data["timestamp"] = alert.timestamp.isoformat()
    if alert.action_taken is not None:
        data["action_taken"] = alert.action_taken.value
    return data


def _serialize_status(status: BudgetStatus) -> dict[str, Any]:
    """将 BudgetStatus 序列化为可 JSON 化的 dict。"""
    data = asdict(status)
    data["alert_level"] = status.alert_level.value
    return data


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """插件加载时初始化预算管理器。"""
    global _budget_manager
    config = plugin.get_config()
    cost_config = get_cost_control_config()
    _budget_manager = BudgetManager(config=cost_config)
    logger.info("cost_control service initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """插件卸载时清理。"""
    global _budget_manager
    _budget_manager = None
    reset_budget_manager()


@plugin.tool(
    name="cost_control.check_budget",
    schema={
        "type": "object",
        "properties": {
            "estimated_tokens": {"type": "integer", "minimum": 0, "description": "预估 Token 数"},
            "user_id": {"type": "string", "description": "用户 ID（可选）"},
            "task_id": {"type": "string", "description": "任务 ID（可选）"},
            "session_id": {"type": "string", "description": "会话 ID（可选）"},
        },
        "required": ["estimated_tokens"],
    },
    description="Check if budget allows the estimated token usage",
)
async def cost_control_check_budget(
    estimated_tokens: int,
    user_id: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """检查预算是否允许执行。

    Raises:
        BudgetExceededException: 预算超限（任务/会话级别）
        QuotaExhaustedException: 配额耗尽（全局级别）
    """
    try:
        result = await _budget_manager.check_budget(
            estimated_tokens=estimated_tokens,
            user_id=user_id,
            task_id=task_id,
            session_id=session_id,
        )
        return {"allowed": result}
    except BudgetExceededException as e:
        return {"allowed": False, "error": e.to_dict()}
    except QuotaExhaustedException as e:
        return {"allowed": False, "error": e.to_dict()}


@plugin.tool(
    name="cost_control.record_usage",
    schema={
        "type": "object",
        "properties": {
            "tokens": {"type": "integer", "minimum": 0, "description": "使用的 Token 数"},
            "model": {"type": "string", "description": "模型名称"},
            "user_id": {"type": "string", "description": "用户 ID（可选）"},
            "task_id": {"type": "string", "description": "任务 ID（可选）"},
            "session_id": {"type": "string", "description": "会话 ID（可选）"},
        },
        "required": ["tokens", "model"],
    },
    description="Record token usage and check for budget alerts",
)
async def cost_control_record_usage(
    tokens: int,
    model: str,
    user_id: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """记录 Token 使用量，返回告警信息（如果有）。"""
    alert = await _budget_manager.record_usage(
        tokens=tokens,
        model=model,
        user_id=user_id,
        task_id=task_id,
        session_id=session_id,
    )
    return {"recorded": True, "alert": _serialize_alert(alert)}


@plugin.tool(
    name="cost_control.get_status",
    schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "用户 ID（可选）"},
            "task_id": {"type": "string", "description": "任务 ID（可选）"},
            "session_id": {"type": "string", "description": "会话 ID（可选）"},
        },
    },
    description="Get current budget status for a scope",
)
async def cost_control_get_status(
    user_id: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """获取预算状态。"""
    status = _budget_manager.get_budget_status(
        user_id=user_id,
        task_id=task_id,
        session_id=session_id,
    )
    return _serialize_status(status)


@plugin.tool(
    name="cost_control.get_statistics",
    schema={
        "type": "object",
        "properties": {},
    },
    description="Get global usage statistics",
)
async def cost_control_get_statistics() -> dict[str, Any]:
    """获取全局使用统计。"""
    return _budget_manager.get_usage_statistics()


@plugin.tool(
    name="cost_control.reset_task_budget",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
        },
        "required": ["task_id"],
    },
    description="Reset budget tracking for a specific task",
)
async def cost_control_reset_task_budget(task_id: str) -> dict[str, Any]:
    """重置任务预算。"""
    await _budget_manager.reset_task_budget(task_id)
    return {"reset": True, "task_id": task_id}


@plugin.tool(
    name="cost_control.reset_session_budget",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "会话 ID"},
        },
        "required": ["session_id"],
    },
    description="Reset budget tracking for a specific session",
)
async def cost_control_reset_session_budget(session_id: str) -> dict[str, Any]:
    """重置会话预算。"""
    await _budget_manager.reset_session_budget(session_id)
    return {"reset": True, "session_id": session_id}


# ── HTTP 端点（http.handle）—— 前端 /ext/cost_control/** 入口 ──────────────
# 内核 http_dispatcher 透传：dispatcher 把 HttpHandleRequest（method/path/raw_body/
# headers/query/plugin_id）整体作为 arguments 传给本工具。本工具按 path 分发到 5 个
# 子端点，调 BudgetManager 真实业务，返回 ToolExecutionResult{success,data}。
# data 必须是 HttpHandleResponse{status,headers,body,body_encoding}，body 需 base64。
# 字段形状严格对齐 frontend/src/services/api/costControl.ts 的 TS 类型。


# ── 成本控制 YAML 配置全文读写（批次1 迁入，源 channel_api routes_config.py）──
# 注意与插件内 CostControlConfig（config/cost_control.yaml via config_center，
# global_budget 字段）不同：本组端点读写 **config/system/cost_control.yaml**
# （channel_api 原路径不变，global_config 字段），是前端 settings 页的配置编辑面。


def _resolve_project_root() -> Path:
    """向上查找项目根（含 config/ + config/models/ 的目录）。

    与 channel_api routes_config._resolve_project_root 同构：按目录特征探测，
    不硬编码 parent×N（模块相对项目根的深度随布局变化不可靠）。
    """
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "config").is_dir() and (candidate / "config" / "models").is_dir():
            return candidate
    # 兜底：仓库布局内必有 config/ 探测命中（与 channel_api 同构）
    return Path(__file__).resolve().parent.parent.parent.parent  # pragma: no cover


_COST_CONTROL_YAML = _resolve_project_root() / "config" / "system" / "cost_control.yaml"

_DEFAULT_COST_CONTROL: dict[str, Any] = {
    "enabled": True,
    "global_config": {
        "daily_token_limit": 1000000,
        "monthly_token_limit": 30000000,
        "per_task_token_limit": 200000,
        "per_session_token_limit": 500000,
    },
    "alerts": {
        "warning_threshold": 70,
        "critical_threshold": 90,
        "exhausted_threshold": 100,
    },
    "protection": {
        "auto_save_at_warning": True,
        "auto_pause_at_critical": True,
        "auto_stop_at_exhausted": True,
    },
}


def _read_cost_control_yaml() -> dict[str, Any]:
    """读成本控制 YAML；文件不存在时返回默认值（对齐 channel_api 语义）。"""
    if _COST_CONTROL_YAML.exists():
        with open(_COST_CONTROL_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            return data
    return copy.deepcopy(_DEFAULT_COST_CONTROL)


def _write_cost_control_yaml(data: dict[str, Any]) -> None:
    """全文覆写成本控制 YAML（通道与 channel_api 原 _write_yaml 一致）。"""
    _COST_CONTROL_YAML.parent.mkdir(parents=True, exist_ok=True)
    with open(_COST_CONTROL_YAML, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


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


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 → JSON dict；空 body 返回 {}）。"""
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        candidate = base64.b64decode(raw_body).decode("utf-8")
        if candidate.lstrip().startswith(("{", "[")):
            decoded = candidate
    except Exception:  # noqa: BLE001 —— 非 base64 明文 body 直接按 JSON 解
        pass
    try:
        parsed = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


def _reshape_usage_statistics(raw: dict[str, Any]) -> dict[str, Any]:
    """把 BudgetManager.get_usage_statistics() 的返回重塑成前端期望形状。

    差异：插件用 ``global`` / ``tasks(dict)`` / ``sessions(dict)`` / 无 updated_at；
    前端期望 ``global_stats`` / ``tasks(array)`` / ``sessions(array)`` / 有 updated_at。
    """
    return {
        "global_stats": raw.get("global", {}),
        "tasks": [
            {"task_id": tid, **stats}
            for tid, stats in raw.get("tasks", {}).items()
        ],
        "sessions": [
            {"session_id": sid, **stats}
            for sid, stats in raw.get("sessions", {}).items()
        ],
        "recent_records": raw.get("recent_records", []),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def _flatten_cost_config(cost_config: CostControlConfig) -> dict[str, Any]:
    """把嵌套 CostControlConfig 拍平成前端 CostConfigResponse 扁平结构。"""
    gb = cost_config.global_budget
    al = cost_config.alerts
    pr = cost_config.protection
    return {
        "daily_token_limit": gb.daily_token_limit,
        "monthly_token_limit": gb.monthly_token_limit,
        "per_task_token_limit": gb.per_task_token_limit,
        "per_session_token_limit": gb.per_session_token_limit,
        "warning_threshold": al.warning_threshold,
        "critical_threshold": al.critical_threshold,
        "auto_save_at_warning": pr.auto_save_at_warning,
        "auto_pause_at_critical": pr.auto_pause_at_critical,
        "auto_stop_at_exhausted": pr.auto_stop_at_exhausted,
    }


def _build_cost_report(stats_raw: dict[str, Any], period: str) -> dict[str, Any]:
    """从 usage_statistics 派生 cost report（BudgetManager 无现成 report 方法）。

    对齐前端 CostReportResponse：period/start_date/end_date/total_tokens/total_cost/
    by_model/by_task/daily_breakdown。
    """
    g = stats_raw.get("global", {})
    today = datetime.date.today()
    if period == "weekly":
        start = today - datetime.timedelta(days=today.weekday())
    elif period == "monthly":
        start = today.replace(day=1)
    else:
        start = today
    records = stats_raw.get("recent_records", [])
    by_model: dict[str, dict[str, Any]] = {}
    for r in records:
        m = r.get("model", "unknown")
        bucket = by_model.setdefault(m, {"tokens": 0, "cost": 0.0, "count": 0})
        bucket["tokens"] += r.get("tokens", 0)
        bucket["cost"] += r.get("cost", 0.0)
        bucket["count"] += 1
    return {
        "period": period,
        "start_date": start.isoformat(),
        "end_date": today.isoformat(),
        "total_tokens": g.get("daily_tokens", 0),
        "total_cost": g.get("estimated_daily_cost", 0.0),
        "by_model": by_model,
        "by_task": stats_raw.get("tasks", {}),
        "daily_breakdown": [],
        "items": records,
    }


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
    description="HTTP endpoint handler for /ext/cost_control/** (cost control business REST)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 7 个子端点（5 既有 + 2 批次1 迁入的 YAML 配置读写）。

    签名覆盖 HttpHandleRequest 全部字段（SDK 的 td.handler(**arguments) 展开）。
    BudgetManager 未初始化时返回 503（不崩，前端 axios 会重试/降级）——
    但 YAML 配置读写不依赖 BudgetManager，先于初始化守卫分发。
    """
    # ── 成本控制 YAML 配置全文读写（批次1 迁入；不依赖 BudgetManager）──
    if path == "/ext/cost_control/config/cost-control" and method == "GET":
        return _ok(_json_response(_read_cost_control_yaml()))

    if path == "/ext/cost_control/config/cost-control" and method == "PUT":
        try:
            body = _decode_body(raw_body)
        except ValueError as exc:
            return _ok(_json_response({"error": str(exc)}, 400))
        if not body:
            return _ok(_json_response({"error": "配置不能为空"}, 400))
        _write_cost_control_yaml(body)
        logger.info("cost-control YAML 配置已更新: %s", _COST_CONTROL_YAML)
        return _ok(_json_response(body))

    global _budget_manager
    if _budget_manager is None:
        logger.warning("http.handle called but BudgetManager not initialized (on_load pending)")
        return _error("cost_control service not initialized (sidecar warming up)", 503)

    bm = _budget_manager

    # 分发：按 path 精确匹配 5 个子端点
    if path == "/ext/cost_control/budget/status" and method == "GET":
        status = bm.get_budget_status()
        return _ok(_json_response(_serialize_status(status)))

    if path == "/ext/cost_control/usage/statistics" and method == "GET":
        raw = bm.get_usage_statistics()
        return _ok(_json_response(_reshape_usage_statistics(raw)))

    if path == "/ext/cost_control/config" and method == "GET":
        cost_config = get_cost_control_config()
        return _ok(_json_response(_flatten_cost_config(cost_config)))

    if path == "/ext/cost_control/report" and method == "GET":
        period = (query or {}).get("period", "daily")
        raw = bm.get_usage_statistics()
        report = _build_cost_report(raw, period)
        return _ok(_json_response(report))

    if path == "/ext/cost_control/budget/reset" and method == "POST":
        # BudgetManager 无全局 reset，重置内部 usage 累计
        bm._global_daily_usage = 0
        bm._global_monthly_usage = 0
        bm._task_usage.clear()
        bm._session_usage.clear()
        bm._usage_records.clear()
        logger.info("global budget reset via /ext/cost_control/budget/reset")
        return _ok(_json_response({"success": True, "message": "预算已重置"}))

    # 未匹配的 path
    logger.warning("http.handle: no route for path=%s method=%s", path, method)
    return _ok(_json_response({"error": "not found", "path": path}, 404))


if __name__ == "__main__":
    plugin.run()

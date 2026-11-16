#!/usr/bin/env python3
"""成本控制 MCP 服务端——纯接口适配层。

本目录为实现模块（平铺），本文件只做接口适配：
调用同目录实现模块，通过 MCP SDK 暴露为工具。

config/cost-control 域：
新增 /ext/cost_control/config/cost-control GET/PUT —— 成本控制 **YAML 配置
全文**读写（config/plugins/cost_control/cost_control.yaml，前端 services/api/config.ts 的
CostControlConfigResponse 嵌套形态消费），与既有 /ext/cost_control/config
（展平形态，前端 costControl.ts 消费）并存，语义对齐 channel_api
routes_config.py 的 cost-control 段（_DEFAULT_COST_CONTROL 兜底 + 全文覆写）。

[来源: docs/working/module_migration_plan.md §5.1]
"""
# config-write-surface-exempt: 真债:cost_control.yaml应走内核单一面(前端已可用PluginConfigEditor);迁移快照欠账,见方案§2审计表
from __future__ import annotations

import copy
import datetime
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from agentos_plugin_sdk import (
    DbReadQueryError as _DbReadQueryError,
    db_read_query as _db_read_query,
)
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

_paths = bootstrap_plugin(__file__)  # 插件目录（budget_manager 等平铺模块）+ plugins/shared 根（http_json）入 sys.path

from _root_api import (  # noqa: E402 — HTTP 响应封装公共实现，调用点零改名
    decode_body as _decode_body,
    error as _error,
    json_response as _json_response,
    ok as _ok,
    resolve_project_root,  # noqa: E402 — 项目根解析单源（F-2）
)
from budget_manager import (  # noqa: E402
    BudgetAlert,
    BudgetManager,
    BudgetStatus,
    reset_budget_manager,
)
from exceptions import BudgetExceededException, QuotaExhaustedException  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from config import (  # noqa: E402
    CostControlConfig,
    get_cost_control_config,
    load_cost_control_config,
)

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


def _header_value(headers: dict[str, str] | None, name: str) -> str:
    """取入站请求头值（内核 header key 已小写，大小写防御）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == name and v:
            return v
    return ""


def _request_tenant(headers: dict[str, str] | None) -> str:
    """请求方租户身份（内核对 /ext 已认证分发注入的 X-AgentOS-Tenant 头）。

    用量页是租户数据面：tenant_id 必须来自该头（已认证租户），缺失/为空 =
    身份不可得，读面 fail-closed 全 0——绝不全租户兜底（否则成本页退化为
    跨租户消耗泄露）。
    """
    return _header_value(headers, "x-agentos-tenant")


def _request_authorization(headers: dict[str, str] | None) -> str:
    """入站 Authorization 原头值（db-read 契约身份链：内核复权后内建收窄）。"""
    return _header_value(headers, "authorization")


async def _trace_stats_dict(
    tenant_id: str, authorization: str = ""
) -> dict[str, Any]:
    """traces 用量读面单点（F10 收敛）：统计/报表/展示共用一处聚合。

    经 db-read 契约取数（traces 数据面 tenant 档，自由只读 SELECT，契约层按
    内核复权的请求方身份强制收窄——用量页为**本租户口径**；traces 自带
    tenant_id 列，内核 TRACES_DDL）：daily/monthly 走共享 UTC 窗口求和（与
    monitoring token-usage 同源同量），total/by_model/records 由明细行累计。
    tenant_id 缺失 = 请求方身份不可得 → fail-closed 全 0（不跨租户兜底）；
    契约失败（无凭证 401/库缺失 503 等）降级全 0 + warn（读面契约，不抛）；
    manager 内存账本不参与本面（其只持预算限额与费率派生基准，见
    _reshape_usage_statistics）。SELECT 尾列投影真实租户列供契约收窄。
    """
    stats: dict[str, Any] = {
        "daily": 0,
        "monthly": 0,
        "total": 0,
        "by_model": {},
        "records": [],
    }
    if not tenant_id:
        return stats
    try:
        # 聚合形状纪律：必须 GROUP BY 租户列后由契约包装收窄——无 GROUP BY 的
        # 裸聚合会把全部租户聚成一行（租户列成任意值），包装无法正确收窄
        #（71dca0fff 实证教训，随契约迁移纪律保持）
        daily = await _db_read_query(
            plugin,
            "traces",
            "SELECT COALESCE(SUM(json_extract(patch_data,"
            " '$.llm_usage.total_tokens')), 0), tenant_id"
            " FROM traces"
            " WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL"
            "   AND substr(created_at, 1, 10) = date('now')"
            " GROUP BY tenant_id",
            authorization=authorization,
        )
        stats["daily"] = int(daily[0][0]) if daily else 0
        monthly = await _db_read_query(
            plugin,
            "traces",
            "SELECT COALESCE(SUM(json_extract(patch_data,"
            " '$.llm_usage.total_tokens')), 0), tenant_id"
            " FROM traces"
            " WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL"
            "   AND substr(created_at, 1, 7) = strftime('%Y-%m', 'now')"
            " GROUP BY tenant_id",
            authorization=authorization,
        )
        stats["monthly"] = int(monthly[0][0]) if monthly else 0
        for row in await _db_read_query(
            plugin,
            "traces",
            "SELECT json_extract(patch_data, '$.llm_usage.input_tokens'),"
            "       json_extract(patch_data, '$.llm_usage.output_tokens'),"
            "       json_extract(patch_data, '$.llm_usage.total_tokens'),"
            "       json_extract(patch_data, '$.llm_model'),"
            "       created_at, tenant_id"
            " FROM traces WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL",
            authorization=authorization,
        ):
            _inp, _out, raw_total, raw_model, created_at, _tenant = row
            total = int(raw_total) if raw_total is not None else 0
            stats["total"] += total
            model = str(raw_model or "unknown")
            stats["by_model"][model] = stats["by_model"].get(model, 0) + total
            stats["records"].append(
                {
                    "tokens": total,
                    "model": model,
                    "timestamp": str(created_at or ""),
                }
            )
    except _DbReadQueryError as exc:  # noqa: BLE001 — 契约失败降级全 0（读面契约不抛）
        logger.warning("[cost_control] traces 用量聚合失败（降级 0）: %s", exc)
    return stats


def _injected_cost_config() -> dict[str, Any] | None:
    """内核注入配置里的 cost 命名空间（manifest config_files[id=cost]）。

    注入形态按 config_files[].id 命名空间嵌套（内核 build_injected_config）：
    ``{"cost": <设置页 yaml 内容>}``；内核未携带/形态非 dict → None（执法面
    回落对齐后的默认表）。
    """
    cfg = plugin.get_config()
    value = cfg.get("cost") if isinstance(cfg, dict) else None
    return value if isinstance(value, dict) else None


@plugin.on_config_changed
def _on_config_changed(config: dict[str, Any]) -> None:
    """内核注入视图变化（设置页 PUT → 下次调用携带新值）→ 执法面随之。

    只换 ``_budget_manager.config``（限额/阈值对象原位替换），不重建
    manager——重建会清空当日用量账本，跨 PUT 的预算执法被归零。内核按
    mtime 现算每调用注入（isolation 式 mtime 轮询不需要）。
    """
    new_config = load_cost_control_config(config.get("cost") if isinstance(config, dict) else None)
    if _budget_manager is not None:
        _budget_manager.config = new_config
        logger.info(
            "[cost_control] 执法面配置已随注入视图更新: daily=%s monthly=%s",
            new_config.global_budget.daily_token_limit,
            new_config.global_budget.monthly_token_limit,
        )


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """插件加载时初始化预算管理器（配置 = manifest 注入的 cost 命名空间）。"""
    global _budget_manager
    cost_config = load_cost_control_config(_injected_cost_config())
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
    assert _budget_manager is not None, "on_load 已保证 _budget_manager 初始化"
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
    assert _budget_manager is not None, "on_load 已保证 _budget_manager 初始化"
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
    assert _budget_manager is not None, "on_load 已保证 _budget_manager 初始化"
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
    assert _budget_manager is not None, "on_load 已保证 _budget_manager 初始化"
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
    assert _budget_manager is not None, "on_load 已保证 _budget_manager 初始化"
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
    assert _budget_manager is not None, "on_load 已保证 _budget_manager 初始化"
    await _budget_manager.reset_session_budget(session_id)
    return {"reset": True, "session_id": session_id}


# ── HTTP 端点（http.handle）—— 前端 /ext/cost_control/** 入口 ──────────────
# 内核 http_dispatcher 透传：dispatcher 把 HttpHandleRequest（method/path/raw_body/
# headers/query/plugin_id）整体作为 arguments 传给本工具。本工具按 path 分发到 5 个
# 子端点，调 BudgetManager 真实业务，返回 ToolExecutionResult{success,data}。
# data 必须是 HttpHandleResponse{status,headers,body,body_encoding}，body 需 base64。
# 字段形状严格对齐 frontend/src/services/api/costControl.ts 的 TS 类型。


# ── 成本控制 YAML 配置全文读写（源 channel_api routes_config.py）──
# 本组端点读写 **config/plugins/cost_control/cost_control.yaml**（global_config
# 字段，前端 settings 页/PluginConfigEditor 的配置编辑面，即 manifest
# config_files[id=cost] 真值文件）。执法面（BudgetManager）消费同一文件——
# 经内核 config_files 注入（见 _on_load / _on_config_changed），PUT 落盘后
# 内核下次调用携带新视图即生效；两端点数字同源一致（B2 收敛）。


def _resolve_project_root() -> Path:
    """项目根（含 config/ + config/kernel/ 的目录）。

    解析单源在 plugins/shared/project_root.py（env 优先→向上探测→缓存，
    F-2 收敛；探测锚定本文件位置，与收敛前一致）；探测不中时保持既有
    兜底（parent×4，仓库布局内不可达）。
    """
    here = Path(__file__).resolve().parent
    root = resolve_project_root("config", "config/kernel", start=here)
    if root is not None:
        return root
    return here.parent.parent.parent  # pragma: no cover


_COST_CONTROL_YAML = _resolve_project_root() / "config" / "plugins" / "cost_control" / "cost_control.yaml"

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
    """读成本控制 YAML；文件不存在时返回默认值。"""
    if _COST_CONTROL_YAML.exists():
        with open(_COST_CONTROL_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            return data
    return copy.deepcopy(_DEFAULT_COST_CONTROL)


def _write_cost_control_yaml(data: dict[str, Any]) -> None:
    """全文覆写成本控制 YAML（原子写：临时文件 + Path.replace，中断不留半文件）。"""
    _COST_CONTROL_YAML.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _COST_CONTROL_YAML.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp_path.replace(_COST_CONTROL_YAML)


async def _reshape_usage_statistics(
    raw: dict[str, Any], tenant_id: str, authorization: str = ""
) -> dict[str, Any]:
    """把 BudgetManager.get_usage_statistics() 的返回重塑成前端期望形状。

    差异：插件用 ``global`` / ``tasks(dict)`` / ``sessions(dict)`` / 无 updated_at；
    前端期望 ``global_stats`` / ``tasks(array)`` / ``sessions(array)`` / 有 updated_at。

    读面单源（F10 收敛）：消耗字段（daily/monthly）取 traces 聚合单点
    `_trace_stats_dict(tenant_id)`（本租户口径，与报表 total/by_model/records
    同源同量）；manager 内存账本只持预算限额（limit/percent 预算语义）与费率
    派生基准——消耗成本由账本费率在此单点换算，账本用量本身不再作消耗兜底
    （traces 不可用时消耗显示 0，聚合单点降级契约）。
    """
    raw_global = raw.get("global", {})
    trace_stats = await _trace_stats_dict(tenant_id, authorization)
    daily_tokens = trace_stats["daily"]
    monthly_tokens = trace_stats["monthly"]

    daily_limit = raw_global.get("daily_limit", 0)
    monthly_limit = raw_global.get("monthly_limit", 0)
    daily_percent = (daily_tokens / daily_limit * 100) if daily_limit > 0 else 0.0
    monthly_percent = (monthly_tokens / monthly_limit * 100) if monthly_limit > 0 else 0.0
    # 费率（$/千 token）单点派生：manager 账本成本与其用量之比，traces 消耗按此换算成本
    mgr_daily = raw_global.get("daily_tokens", 0)
    mgr_daily_cost = raw_global.get("estimated_daily_cost", 0.0)
    cost_rate = (mgr_daily_cost / mgr_daily * 1000) if mgr_daily > 0 else 0.0
    daily_cost = (daily_tokens / 1000) * cost_rate
    monthly_cost = (monthly_tokens / 1000) * cost_rate

    return {
        "global_stats": {
            "daily_tokens": daily_tokens,
            "monthly_tokens": monthly_tokens,
            "daily_limit": daily_limit,
            "monthly_limit": monthly_limit,
            "daily_usage_percent": daily_percent,
            "monthly_usage_percent": monthly_percent,
            "estimated_daily_cost": daily_cost,
            "estimated_monthly_cost": monthly_cost,
        },
        "tasks": [
            {"task_id": tid, **stats}
            for tid, stats in raw.get("tasks", {}).items()
        ],
        "sessions": [
            {"session_id": sid, **stats}
            for sid, stats in raw.get("sessions", {}).items()
        ],
        "recent_records": raw.get("recent_records", []),
        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
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


async def _build_cost_report(
    stats_raw: dict[str, Any], period: str, tenant_id: str, authorization: str = ""
) -> dict[str, Any]:
    """从 usage_statistics 派生 cost report（BudgetManager 无现成 report 方法）。

    对齐前端 CostReportResponse：period/start_date/end_date/total_tokens/total_cost/
    by_model/by_task/daily_breakdown。每日明细与按模型占比取 traces 聚合
    （G5 同源，本租户口径见 _trace_stats_dict），period 语义映射到 traces 的
    日/周/月窗。
    """
    g = stats_raw.get("global", {})
    today = datetime.date.today()
    if period == "weekly":
        start = today - datetime.timedelta(days=today.weekday())
    elif period == "monthly":
        start = today.replace(day=1)
    else:
        start = today

    # 消耗/按模型/明细取 traces 聚合（G5 同源，见 _trace_stats_dict）
    trace_stats = await _trace_stats_dict(tenant_id, authorization)
    total_tokens = trace_stats["total"]
    by_model = {
        model: {"tokens": tokens, "cost": 0.0, "count": 1}
        for model, tokens in trace_stats["by_model"].items()
    }
    # 消费记录：明细带 tokens/model/timestamp，供前端占比与列表
    records = trace_stats["records"]
    records.sort(key=lambda r: r["timestamp"], reverse=True)

    return {
        "period": period,
        "start_date": start.isoformat(),
        "end_date": today.isoformat(),
        "total_tokens": total_tokens,
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
    """按 path 分发到 7 个子端点（5 既有 + 2 YAML 配置读写）。

    签名覆盖 HttpHandleRequest 全部字段（SDK 的 td.handler(**arguments) 展开）。
    BudgetManager 未初始化时返回 503（不崩，前端 axios 会重试/降级）——
    但 YAML 配置读写不依赖 BudgetManager，先于初始化守卫分发。
    """
    # ── 成本控制 YAML 配置全文读写（不依赖 BudgetManager）──
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
        return _ok(_json_response(
            await _reshape_usage_statistics(raw, _request_tenant(headers), _request_authorization(headers))
        ))

    if path == "/ext/cost_control/config" and method == "GET":
        cost_config = get_cost_control_config()
        return _ok(_json_response(_flatten_cost_config(cost_config)))

    if path == "/ext/cost_control/report" and method == "GET":
        period = (query or {}).get("period", "daily")
        raw = bm.get_usage_statistics()
        report = await _build_cost_report(raw, period, _request_tenant(headers), _request_authorization(headers))
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

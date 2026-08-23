"""触发器管理 REST 面（channel_api triggers 域接真，2026-08-21 收尾）。

channel_api 的 /ext/channel_api/triggers/* 原为纯 stub（创建返回硬编码存根）。
本模块把管理面落到 trigger_setup_tool 插件自身：与 LLM 工具 trigger_setup
同进程共享 TriggerManager 单例（进程内注册表，不落盘——重启后由 agent
重新 setup）与 TriggerSetupTool（创建/更新/取消语义一致，校验相同）。

手动触发（/trigger）走 manager.fire_manually：立即注入消息并累计 fire_count，
与检查循环的到期触发互相独立。
"""
from __future__ import annotations

import base64
import json
from dataclasses import asdict
from typing import Any

from tool import TriggerSetupTool
from triggers.manager import get_trigger_manager
from triggers.types import TriggerConfig, TriggerStatus

PREFIX = "/ext/trigger_setup_tool/triggers"


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _error(message: str, status: int = 400) -> dict[str, Any]:
    return {"success": False, "error": message, "data": _json_response({"error": message}, status)}


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 或明文 JSON）为 dict。"""
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        attempt = base64.b64decode(raw_body).decode("utf-8")
        if attempt.lstrip().startswith(("{", "[")):
            decoded = attempt
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        parsed = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


def _serialize(cfg: TriggerConfig) -> dict[str, Any]:
    """TriggerConfig → JSON 安全 dict（Enum 转 value、datetime 转 ISO、空容器兜底）。"""
    d = asdict(cfg)
    d["trigger_type"] = cfg.trigger_type.value if hasattr(cfg.trigger_type, "value") else cfg.trigger_type
    d["status"] = cfg.status.value if hasattr(cfg.status, "value") else cfg.status
    if d.get("scheduled_at") is not None:
        d["scheduled_at"] = cfg.scheduled_at.isoformat()
    for key in ("event_filter", "action_params", "metadata"):
        if d.get(key) is None:
            d[key] = {}
    return d


def _get_or_404(trigger_id: str) -> tuple[TriggerConfig | None, dict[str, Any] | None]:
    """取触发器；不存在时返回 404 响应。"""
    cfg = get_trigger_manager().get(trigger_id)
    if cfg is None:
        return None, _error(f"trigger not found: {trigger_id}", 404)
    return cfg, None


# ── 9 个端点 handler ──────────────────────────────────────────────


async def list_triggers(query: dict[str, Any] | None) -> dict[str, Any]:
    """GET /triggers：全部触发器（可选 status 过滤）。"""
    mgr = get_trigger_manager()
    status_filter = (query or {}).get("status") or None
    items = []
    for cfg in mgr.list_all():
        if status_filter and cfg.status.value != status_filter:
            continue
        items.append(_serialize(cfg))
    return _ok(_json_response({"items": items, "total": len(items)}))


async def create_trigger(body: dict[str, Any]) -> dict[str, Any]:
    """POST /triggers：创建（与 LLM 工具 trigger_setup action=setup 同语义）。"""
    tool = TriggerSetupTool()
    result = await tool.execute({**body, "action": "setup"})
    if not result.success:
        return _error(result.error or "trigger setup failed", 400)
    cfg = get_trigger_manager().get(result.output.get("trigger_id", "")) if isinstance(result.output, dict) else None
    return _ok(_json_response({"trigger": _serialize(cfg) if cfg else result.output}))


async def get_trigger(trigger_id: str) -> dict[str, Any]:
    """GET /triggers/{id}：详情。"""
    cfg, err = _get_or_404(trigger_id)
    if err is not None:
        return err
    return _ok(_json_response({"trigger": _serialize(cfg)}))


async def update_trigger(trigger_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """PUT /triggers/{id}：更新（与工具 action=update 同语义）。"""
    if get_trigger_manager().get(trigger_id) is None:
        return _error(f"trigger not found: {trigger_id}", 404)
    tool = TriggerSetupTool()
    result = await tool.execute({**body, "action": "update", "trigger_id": trigger_id})
    if not result.success:
        return _error(result.error or "trigger update failed", 400)
    cfg = get_trigger_manager().get(trigger_id)
    return _ok(_json_response({"trigger": _serialize(cfg) if cfg else result.output}))


async def delete_trigger(trigger_id: str) -> dict[str, Any]:
    """DELETE /triggers/{id}：取消（manager.cancel，状态置 CANCELLED）。"""
    mgr = get_trigger_manager()
    if mgr.get(trigger_id) is None:
        return _error(f"trigger not found: {trigger_id}", 404)
    return _ok(_json_response({"deleted": mgr.cancel(trigger_id), "trigger_id": trigger_id}))


async def set_trigger_status(trigger_id: str, status: TriggerStatus) -> dict[str, Any]:
    """enable/disable 共用：改进程内配置状态（与取消/到期置态同存储）。"""
    cfg, err = _get_or_404(trigger_id)
    if err is not None:
        return err
    assert cfg is not None  # _get_or_404 约定：err 为 None 时 cfg 必非 None
    cfg.status = status
    return _ok(_json_response({"updated": True, "trigger": _serialize(cfg)}))


async def fire_trigger(trigger_id: str) -> dict[str, Any]:
    """POST /triggers/{id}/trigger：手动触发一次。"""
    if get_trigger_manager().get(trigger_id) is None:
        return _error(f"trigger not found: {trigger_id}", 404)
    return _ok(_json_response({"fired": get_trigger_manager().fire_manually(trigger_id), "trigger_id": trigger_id}))


async def trigger_stats() -> dict[str, Any]:
    """GET /triggers/stats：按类型/状态计数。"""
    mgr = get_trigger_manager()
    all_cfgs = mgr.list_all()
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for cfg in all_cfgs:
        by_type[cfg.trigger_type.value] = by_type.get(cfg.trigger_type.value, 0) + 1
        by_status[cfg.status.value] = by_status.get(cfg.status.value, 0) + 1
    return _ok(_json_response({
        "total": len(all_cfgs),
        "active": sum(1 for c in all_cfgs if c.status == TriggerStatus.ACTIVE),
        "by_type": by_type,
        "by_status": by_status,
    }))


# ── 分派 ───────────────────────────────────────────────────────────


async def handle_triggers_http(
    method: str,
    path: str,
    query: dict[str, Any] | None,
    raw_body: str = "",
) -> dict[str, Any]:
    """http.handle 按 path/method 分发（PREFIX 下 9 端点）。"""
    try:
        body = _decode_body(raw_body)
    except ValueError as exc:
        return _error(str(exc), 400)

    if method == "GET" and path == PREFIX:
        return await list_triggers(query)
    if method == "POST" and path == PREFIX:
        return await create_trigger(body)
    if method == "GET" and path == f"{PREFIX}/stats":
        return await trigger_stats()
    if path.startswith(f"{PREFIX}/"):
        rest = path[len(PREFIX) + 1:]
        if "/" in rest:
            trigger_id, action = rest.split("/", 1)
            if method == "POST" and action == "enable":
                return await set_trigger_status(trigger_id, TriggerStatus.ACTIVE)
            if method == "POST" and action == "disable":
                return await set_trigger_status(trigger_id, TriggerStatus.PENDING)
            if method == "POST" and action == "trigger":
                return await fire_trigger(trigger_id)
        else:
            trigger_id = rest
            if method == "GET":
                return await get_trigger(trigger_id)
            if method == "PUT":
                return await update_trigger(trigger_id, body)
            if method == "DELETE":
                return await delete_trigger(trigger_id)
    return _error(f"not found: {method} {path}", 404)


async def handle_http_dispatch(
    path: str,
    method: str,
    raw_body: str = "",
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """server.py http.handle 入口（仅本插件前缀，fail-closed 未知路径 404）。"""
    if path.startswith(PREFIX):
        return await handle_triggers_http(method, path, query, raw_body)
    return _error(f"not found: {method} {path}", 404)
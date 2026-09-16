"""触发器管理 REST 面。

channel_api 的 /ext/channel_api/triggers/* 原为纯 stub（创建返回硬编码存根）。
本模块把管理面落到 trigger_setup_tool 插件自身：与 LLM 工具 trigger_setup
同进程共享 TriggerManager 单例（进程内注册表，不落盘——重启后由 agent
重新 setup）与 TriggerSetupTool（创建/更新/取消语义一致，校验相同）。

手动触发（/trigger）走 manager.fire_manually：立即注入消息并累计 fire_count，
与检查循环的到期触发互相独立。

创建（POST /triggers）的目标管道由 body.pipeline_id 指定（缺省回退前端
FormWidget 注入的当前激活管道）；到期注入需要 user_id（chat.send_message
硬校验 tenant 反查），经 Authorization Bearer token 自持解析（路由鉴权
auth=user 已由内核完成，此处只取身份不重复鉴权）。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

# 共享层公共模块（kernel_token/http_json，plugins/shared 平铺）入 sys.path 后
# 裸名导入（先例：tasks/http_api.py、tenant_data）。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from http_json import (  # noqa: E402
    decode_body as _decode_body,
    error as _error,
    json_response as _json_response,
    ok as _ok,
    protocol_error as _protocol_error,
)
from kernel_token import decode_kernel_token  # noqa: E402

from tool import TriggerSetupTool
from triggers.manager import get_trigger_manager
from triggers.types import TriggerConfig, TriggerStatus

PREFIX = "/ext/trigger_setup_tool/triggers"
PIPELINES_PATH = "/ext/trigger_setup_tool/pipelines"


def _serialize(cfg: TriggerConfig) -> dict[str, Any]:
    """TriggerConfig → JSON 安全 dict（Enum 转 value、datetime 转 ISO、空容器兜底）。"""
    return cfg.to_state_dict()


def _get_or_404(trigger_id: str) -> tuple[TriggerConfig | None, dict[str, Any] | None]:
    """取触发器；不存在时返回 404 响应。"""
    cfg = get_trigger_manager().get(trigger_id)
    if cfg is None:
        return None, _error(f"trigger not found: {trigger_id}", 404)
    return cfg, None


def _decode_bearer_user(headers: dict[str, Any] | None) -> str:
    """从 Authorization Bearer token 解出 user_id；无效/缺失/过期返回空串。

    解码走公共单点 kernel_token.decode_kernel_token（自解析不验签，鉴权权威
    在内核 dispatcher；路由 auth=user 已由内核完成，此处只取身份不重复鉴权）。
    """
    authz = ""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and v:
            authz = str(v)
            break
    token = authz[7:] if authz.lower().startswith("bearer ") else ""
    if not token:
        return ""
    decoded = decode_kernel_token(token)
    if decoded is None:
        return ""
    user_id, _username, exp = decoded
    if int(time.time()) >= exp:
        return ""
    return user_id


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


async def list_pipeline_options() -> dict[str, Any]:
    """GET /pipelines：目标管道下拉选项（创建表单 pipeline_id 字段消费）。

    state 聚合行 → ``{options: [{label, value}]}``；label 取
    display_name/name/task.goal 首个非空（都缺回退 pipeline_id），
    value=pipeline_id。桥未接通抛错转 500（fail-visible，不静默空选项）。
    """
    try:
        rows = await get_trigger_manager().collect_state_rows()
    except RuntimeError as exc:
        return _error(str(exc), 500)
    options = []
    for row in rows:
        pid = str(row.get("pipeline_id") or "")
        if not pid:
            continue
        display = row.get("display_name") or row.get("name") or row.get("task.goal") or ""
        options.append({"label": f"{display}（{pid}）" if display else pid, "value": pid})
    return _ok(_json_response({"options": options}))


async def create_trigger(body: dict[str, Any], headers: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST /triggers：创建（与 LLM 工具 trigger_setup action=setup 同语义）。

    user_id 从 Authorization Bearer token 解出（chat.send_message 硬校验
    user_id 非空，缺失/无效即 401——注册一个到期必投递失败的触发器是静默债）。

    拒绝路径走协议错误信封（success:true + data 携真实 HTTP 状态）：内核
    SidecarHttpHandler 对 success:false 信封一律回 502，前端会按可重试 5xx
    重放请求并丢失结构化错误体，故 401/400 必须经 _protocol_error 到达前端。
    """
    user_id = _decode_bearer_user(headers)
    if not user_id:
        return _protocol_error(
            "无法识别调用者（缺少有效 Bearer 凭据）：触发器到期注入消息需要 user_id",
            401,
        )
    tool = TriggerSetupTool()
    result = await tool.execute({**body, "user_id": user_id, "action": "setup"})
    if not result.success:
        return _protocol_error(result.error or "trigger setup failed", 400)
    cfg = get_trigger_manager().get(result.output.get("trigger_id", "")) if isinstance(result.output, dict) else None
    payload = _serialize(cfg) if cfg is not None else result.output
    return _ok(_json_response({"trigger": payload}))


async def get_trigger(trigger_id: str) -> dict[str, Any]:
    """GET /triggers/{id}：详情。"""
    cfg, err = _get_or_404(trigger_id)
    if err is not None:
        return err
    assert cfg is not None  # _get_or_404 约定：err 为 None 时 cfg 必非 None
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
    payload = _serialize(cfg) if cfg is not None else result.output
    return _ok(_json_response({"trigger": payload}))


async def delete_trigger(trigger_id: str) -> dict[str, Any]:
    """DELETE /triggers/{id}：删除（manager.unregister，注册表移除 + 终态落写面）。

    删除语义 = 注销而非取消：列表重拉条目消失（清残留），单发已触发（FIRED）
    也可删；state 写面按 unregister 既有契约以 CANCELLED 终态落（重灌跳过终态）。
    """
    mgr = get_trigger_manager()
    if not mgr.unregister(trigger_id):
        return _error(f"trigger not found: {trigger_id}", 404)
    return _ok(_json_response({"deleted": True, "trigger_id": trigger_id}))


async def set_trigger_status(trigger_id: str, status: TriggerStatus) -> dict[str, Any]:
    """enable/disable 共用：改状态并同步落目标管道 state（与注册同存储）。"""
    cfg, err = _get_or_404(trigger_id)
    if err is not None:
        return err
    assert cfg is not None  # _get_or_404 约定：err 为 None 时 cfg 必非 None
    cfg.status = status
    get_trigger_manager().persist(cfg)
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
    headers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """http.handle 按 path/method 分发（PREFIX 下 9 端点）。"""
    try:
        body = _decode_body(raw_body)
    except ValueError as exc:
        return _error(str(exc), 400)

    if method == "GET" and path == PREFIX:
        return await list_triggers(query)
    if method == "POST" and path == PREFIX:
        return await create_trigger(body, headers)
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
    headers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """server.py http.handle 入口（本插件前缀，fail-closed 未知路径 404）。"""
    if method == "GET" and path == PIPELINES_PATH:
        return await list_pipeline_options()
    if path.startswith(PREFIX):
        return await handle_triggers_http(method, path, query, raw_body, headers)
    return _error(f"not found: {method} {path}", 404)

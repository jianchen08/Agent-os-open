"""触发器管理 REST 面。

channel_api 的 /ext/channel_api/triggers/* 原为纯 stub（创建返回硬编码存根）。
本模块把管理面落到 trigger_setup_tool 插件自身：与 LLM 工具 trigger_setup
同进程共享 TriggerManager 单例（进程内注册表，不落盘——重启后由 agent
重新 setup）与 TriggerSetupTool（创建/更新/取消语义一致，校验相同）。

手动触发（/trigger）走 manager.fire_manually：立即注入消息并累计 fire_count，
与检查循环的到期触发互相独立。

创建（POST /triggers）的目标管道由 body.pipeline_id 指定（缺省回退前端
FormWidget 注入的当前激活管道）；到期注入需要 user_id（chat.send_message
硬校验 tenant 反查），身份取内核权威注入的 x-agentos-user 头（路由鉴权
auth=user 已由内核完成；头缺失时回退 Authorization Bearer 自解析，仍无效
即 401），并写进触发器 metadata 属主字段（user_id + tenant_id）。

归属过滤（X6-F1 跨租户收口）：读面（list/get/stats）只覆盖调用方可见集
（admin 角色全见，其余仅属主本人，缺身份头空集 fail-closed）；写面
（update/delete/enable/disable/fire）非属主且非 admin 显式 403；归属键 =
触发器 metadata 属主字段（user_id/tenant_id），归属不可判（P-63 存量缺
user_id）对非 admin 不可见不可操作——宁可少显示不可越权。消费模式同源：
approval/server.py（403 写面 + admin 运维面）、hindsight_memory/
routes_memory.py（读面 fail-closed 归属过滤，X4-F1）。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

# 共享层公共模块（kernel_token/http_json，plugins/shared 平铺）入 sys.path 后
# 裸名导入（先例：tasks/http_api.py、tenant_data）。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
# 归一判重：他写入方可能以变体拼写（大小写/分隔符）注入同一根，裸字符串 in 会被
# 击穿产生重复条目——与消费方（测试 abspath 计数）同一归一口径。
if not any(os.path.abspath(p or ".") == _SHARED_ROOT for p in sys.path):
    sys.path.insert(0, _SHARED_ROOT)
from _root_api import (  # noqa: E402
    decode_body as _decode_body,
    decode_kernel_token,  # noqa: E402
    error as _error,
    json_response as _json_response,
    ok as _ok,
    protocol_error as _protocol_error,
)
from tool import TriggerSetupTool
from triggers.manager import get_trigger_manager
from triggers.types import TriggerConfig, TriggerStatus

PREFIX = "/ext/trigger_setup_tool/triggers"
PIPELINES_PATH = "/ext/trigger_setup_tool/pipelines"

# 内核在 /ext 已认证分发时权威注入的身份头（agentos-api http_dispatcher：匿名
# 请求剥离伪造头，handler 可见即可信）。归属校验只信这组头，不自行解析
# token 做授权（HMAC 密钥不出内核；Bearer 自解析仅 create 缺头时回退取身份）。
_TENANT_HEADER = "x-agentos-tenant"
_USER_HEADER = "x-agentos-user"
_ROLE_HEADER = "x-agentos-role"

# 写面越权拒绝统一文案（403 协议信封体）
_FORBIDDEN_MSG = "无权操作该触发器（非属主且非 admin 角色）"


def _serialize(cfg: TriggerConfig) -> dict[str, Any]:
    """TriggerConfig → JSON 安全 dict（Enum 转 value、datetime 转 ISO、空容器兜底）。

    P-30 三字段可见性：检查循环写在 ``metadata["last_fire_time"]`` 的最近
    触发时间平铺到条目顶层——前端 TableWidget 列取值 ``row[col.key]`` 不
    支持嵌套路径，深键不可声明为列。只增不删（既有键原样保留）。
    """
    d = cfg.to_state_dict()
    meta = d.get("metadata") or {}
    d["last_fire_time"] = meta.get("last_fire_time")
    return d


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


# ── 身份与归属（X6-F1 跨租户收口）─────────────────────────────────


def _header_value(headers: dict[str, Any] | None, name: str) -> str:
    """取入站请求头值（内核 header key 已小写，大小写防御）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == name and v:
            return str(v)
    return ""


def _caller_identity(headers: dict[str, Any] | None) -> tuple[str, str]:
    """(tenant_id, user_id)；缺任一 → 空串（fail-closed：无身份即无归属）。"""
    return _header_value(headers, _TENANT_HEADER), _header_value(headers, _USER_HEADER)


def _trigger_owner(cfg: TriggerConfig) -> tuple[str, str]:
    """触发器归属 (user_id, tenant_id)（metadata 属主字段，缺 → 空串）。"""
    meta = cfg.metadata or {}
    return str(meta.get("user_id") or ""), str(meta.get("tenant_id") or "")


def _may_access(cfg: TriggerConfig, headers: dict[str, Any] | None) -> bool:
    """触发器访问权判定（读面可见 + 写面可操作同一谓词）。

    - admin 角色（x-agentos-role）全通（运维面，对照 approval pending 端点
      同款设计）；
    - 属主本人：metadata.user_id 与身份头一致（一用户一租户，tenant_id 两边
      都非空时还须一致）；
    - 其余一律 False：非属主、缺身份头、P-63 存量（metadata 缺 user_id，
      归属不可判）——读面隐藏（404/空集），写面显式 403，绝不放行全量。
    """
    if _header_value(headers, _ROLE_HEADER) == "admin":
        return True
    tenant, user = _caller_identity(headers)
    if not tenant or not user:
        return False
    owner_user, owner_tenant = _trigger_owner(cfg)
    if not owner_user or owner_user != user:
        return False
    return not owner_tenant or owner_tenant == tenant


# ── 9 个端点 handler ──────────────────────────────────────────────


async def list_triggers(
    query: dict[str, Any] | None, headers: dict[str, Any] | None = None
) -> dict[str, Any]:
    """GET /triggers：调用方可见触发器（可选 status 过滤；X6-F1 归属过滤）。

    admin 角色全见（运维面）；其余已认证用户仅见属主本人的触发器；缺身份头
    → 空集（fail-closed，绝不返回全量）；P-63 存量（metadata 缺属主）对非
    admin 不可见。
    """
    mgr = get_trigger_manager()
    status_filter = (query or {}).get("status") or None
    items = []
    for cfg in mgr.list_all():
        if not _may_access(cfg, headers):
            continue
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

    身份源（X6-F1）：内核权威身份头优先（认证分发恒注入，token 伪造头已被
    剥离）；头缺失（非内核分发路径，如测试直调）回退 Bearer 自解析。调用者
    身份落 metadata 属主字段（user_id 由工具面写入，tenant_id 此处补章），
    读/写面归属过滤据此判定；body 伪造 user_id 不生效（inputs 展开顺序保证
    权威身份覆盖）。
    """
    tenant_id = _header_value(headers, _TENANT_HEADER)
    user_id = _header_value(headers, _USER_HEADER)
    if not user_id:
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
    if cfg is not None:
        # 属主字段落 metadata（归属过滤的判定键）：tenant_id 仅在身份头可证时
        # 落章（Bearer 回退路径无租户权威，留空——归属判定以 user_id 为主键）
        cfg.metadata["user_id"] = user_id
        if tenant_id:
            cfg.metadata["tenant_id"] = tenant_id
        get_trigger_manager().persist(cfg)
    payload = _serialize(cfg) if cfg is not None else result.output
    return _ok(_json_response({"trigger": payload}))


async def get_trigger(trigger_id: str, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET /triggers/{id}：详情（可见集外与不存在同响应——不泄露存在性）。"""
    cfg, err = _get_or_404(trigger_id)
    if err is not None:
        return err
    assert cfg is not None  # _get_or_404 约定：err 为 None 时 cfg 必非 None
    if not _may_access(cfg, headers):
        return _protocol_error(f"trigger not found: {trigger_id}", 404)
    return _ok(_json_response({"trigger": _serialize(cfg)}))


async def update_trigger(
    trigger_id: str, body: dict[str, Any], headers: dict[str, Any] | None = None
) -> dict[str, Any]:
    """PUT /triggers/{id}：更新（与工具 action=update 同语义；越权 403）。"""
    cfg = get_trigger_manager().get(trigger_id)
    if cfg is None:
        return _error(f"trigger not found: {trigger_id}", 404)
    if not _may_access(cfg, headers):
        return _protocol_error(_FORBIDDEN_MSG, 403)
    tool = TriggerSetupTool()
    result = await tool.execute({**body, "action": "update", "trigger_id": trigger_id})
    if not result.success:
        return _error(result.error or "trigger update failed", 400)
    payload_cfg = get_trigger_manager().get(trigger_id)
    payload = _serialize(payload_cfg) if payload_cfg is not None else result.output
    return _ok(_json_response({"trigger": payload}))


async def delete_trigger(trigger_id: str, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    """DELETE /triggers/{id}：删除（manager.unregister，注册表移除 + 终态落写面）。

    删除语义 = 注销而非取消：列表重拉条目消失（清残留），单发已触发（FIRED）
    也可删；state 写面按 unregister 既有契约以 CANCELLED 终态落（重灌跳过终态）。
    非属主且非 admin 显式 403（先于注销判定，越权零副作用）。
    """
    mgr = get_trigger_manager()
    cfg = mgr.get(trigger_id)
    if cfg is not None and not _may_access(cfg, headers):
        return _protocol_error(_FORBIDDEN_MSG, 403)
    if not mgr.unregister(trigger_id):
        return _error(f"trigger not found: {trigger_id}", 404)
    return _ok(_json_response({"deleted": True, "trigger_id": trigger_id}))


async def set_trigger_status(
    trigger_id: str, status: TriggerStatus, headers: dict[str, Any] | None = None
) -> dict[str, Any]:
    """enable/disable 共用：改状态并同步落目标管道 state（与注册同存储）。"""
    cfg, err = _get_or_404(trigger_id)
    if err is not None:
        return err
    assert cfg is not None  # _get_or_404 约定：err 为 None 时 cfg 必非 None
    if not _may_access(cfg, headers):
        return _protocol_error(_FORBIDDEN_MSG, 403)
    cfg.status = status
    get_trigger_manager().persist(cfg)
    return _ok(_json_response({"updated": True, "trigger": _serialize(cfg)}))


async def fire_trigger(trigger_id: str, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST /triggers/{id}/trigger：手动触发一次（越权 403 先于 fire 计数）。"""
    cfg = get_trigger_manager().get(trigger_id)
    if cfg is None:
        return _error(f"trigger not found: {trigger_id}", 404)
    if not _may_access(cfg, headers):
        return _protocol_error(_FORBIDDEN_MSG, 403)
    return _ok(_json_response({"fired": get_trigger_manager().fire_manually(trigger_id), "trigger_id": trigger_id}))


async def trigger_stats(headers: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET /triggers/stats：按类型/状态计数（只覆盖调用方可见集）。"""
    mgr = get_trigger_manager()
    visible_cfgs = [cfg for cfg in mgr.list_all() if _may_access(cfg, headers)]
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for cfg in visible_cfgs:
        by_type[cfg.trigger_type.value] = by_type.get(cfg.trigger_type.value, 0) + 1
        by_status[cfg.status.value] = by_status.get(cfg.status.value, 0) + 1
    return _ok(_json_response({
        "total": len(visible_cfgs),
        "active": sum(1 for c in visible_cfgs if c.status == TriggerStatus.ACTIVE),
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
        return await list_triggers(query, headers)
    if method == "POST" and path == PREFIX:
        return await create_trigger(body, headers)
    if method == "GET" and path == f"{PREFIX}/stats":
        return await trigger_stats(headers)
    if path.startswith(f"{PREFIX}/"):
        rest = path[len(PREFIX) + 1:]
        if "/" in rest:
            trigger_id, action = rest.split("/", 1)
            if method == "POST" and action == "enable":
                return await set_trigger_status(trigger_id, TriggerStatus.ACTIVE, headers)
            if method == "POST" and action == "disable":
                return await set_trigger_status(trigger_id, TriggerStatus.PENDING, headers)
            if method == "POST" and action == "trigger":
                return await fire_trigger(trigger_id, headers)
        else:
            trigger_id = rest
            if method == "GET":
                return await get_trigger(trigger_id, headers)
            if method == "PUT":
                return await update_trigger(trigger_id, body, headers)
            if method == "DELETE":
                return await delete_trigger(trigger_id, headers)
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

#!/usr/bin/env python3
"""审批系统 MCP 服务端（v0.2 审批闭环版）。

职责收窄为"管道挂起/恢复 + 审批语义"——交互请求的创建、前端通知、
用户响应等待统一委托给 human-interaction capability（交互工具插件）。
approval 不自建 request 结构；仅在创建审批后发一条 ``approval.created``
事件（fire-and-forget），驱动前端全屏审批浮层（SchemaFullscreenHost）。

能力依赖（由内核 initialize 注入）：
- human-interaction: create_choice/create_conversation/wait_for_choice——交互请求全权委托
- pipeline-executor: suspend/resume——挂起/恢复管道（approval 独有职责）
- event-bus: emit approval.created——通知前端全屏审批浮层
- logger: 结构化日志

HTTP 面（interaction 域 7 端点）：
- 源 routes_missing.py interaction_router 的端点在本插件 http.handle 的
  /ext/approval_service/interaction/**，经 human-interaction capability 桥
  （manifest provides 声明的命名空间路由）直达 human sidecar 真实单例的
  interaction.get_pending / interaction.respond / interaction.cancel。
- 鉴权：http_endpoints 声明 auth=user（声明性；内核 dispatcher 鉴权面与
  其余插件一致）。

闭环：
    创建审批 → human-interaction.create_choice（弹窗）+ pipeline-executor.suspend（挂起）
    → 用户选择 → human-interaction.wait_for_choice 返回
    → pipeline-executor.resume（恢复管道）

超时语义（F-APPROVAL-1）：审批等待超时/异常 = **拒绝**——不恢复管道、
记录拒绝状态，不再"超时即恢复放行"。默认超时调大（24h=86400s）且可配置。

[来源: docs/tasks/task_10_system_plugins.md AC-09-2; v0.2 审批闭环]
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # plugins/shared 根（http_json）入 sys.path

# http.handle 响应封装走公共实现（plugins/shared/http_json.py），调用点零改名。
from http_json import (  # noqa: E402
    decode_body as _decode_body,
    json_response as _json_response,
    ok as _ok,
)
from kernel_db import kernel_db_path as _kernel_db_path  # noqa: E402 — 内核库路径共享真值源

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("approval_service")

# 审批挂起句柄：approval_id → {suspend_handle, run_id, created_at}
# 仅存恢复管道所需的最小信息（交互状态由 human-interaction 插件管）。
_suspended: dict[str, dict[str, Any]] = {}

# 审批归属：request_id → {"tenant": 归属租户, "user": 创建者用户}
# 创建时记录（tenant 取内核 runs 表按 run_id 权威解析，user 取 param_inject
# 注入的创建者用户），approve/deny 时与内核注入的请求身份头比对——他租户或
# 同租户非创建者非 admin 一律 403。等待窗口结束（wait 收敛）即清除。
_ownership: dict[str, dict[str, str]] = {}

# 内核在 /ext 已认证分发时注入的身份头（agentos-api http_dispatcher），
# 插件侧归属校验只信这组头，不自行解析 token（HMAC 密钥不出内核）。
_TENANT_HEADER = "x-agentos-tenant"
_USER_HEADER = "x-agentos-user"
_ROLE_HEADER = "x-agentos-role"


def _header_value(headers: dict[str, str] | None, name: str) -> str:
    """取入站请求头值（内核 header key 已小写，大小写防御）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == name and v:
            return v
    return ""


def _lookup_run_tenant(run_id: str) -> str:
    """按 run_id 查内核 runs 表得到归属租户（权威锚，LLM 不可伪造）。

    库不可用 / run 不存在 → 空串（不可归因，approve/deny 侧按 admin-only 收口）。
    """
    if not run_id:
        return ""
    import sqlite3

    try:
        db_path = _kernel_db_path()
        if not db_path.is_file():
            return ""
        conn = sqlite3.connect(db_path)
        try:
            # runs 表退役（ADR 2026-09-18）：run_id 是 state 标量键，反查归属租户
            row = conn.execute(
                "SELECT tenant_id FROM pipeline_state WHERE field_key = 'run_id' AND value = ?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("[approval] state 租户查询失败 | run_id=%s | err=%s", run_id, exc)
        return ""
    return str(row[0]) if row else ""


def _record_ownership(request_id: str, run_id: str, user_id: str | None) -> None:
    """创建审批时记录归属：tenant 自 runs 表权威解析，user 取注入的创建者。"""
    _ownership[request_id] = {
        "tenant": _lookup_run_tenant(run_id or ""),
        "user": user_id or "",
    }


def _record_owner(request: dict[str, Any]) -> dict[str, str]:
    """从 human 侧请求记录归因归属：优先 _ownership（审批创建时记录），
    回退记录内创建者 user_id（一用户一租户：user_id 即归属租户）。"""
    rid = str(request.get("id") or request.get("request_id") or "")
    owner = _ownership.get(rid)
    if owner and owner.get("tenant"):
        return dict(owner)
    msg_data = request.get("message_data") or {}
    uid = str(msg_data.get("user_id") or "")
    if uid:
        return {"tenant": uid, "user": uid}
    return {"tenant": "", "user": ""}


async def _resolve_owner(service: Any, request_id: str) -> dict[str, str]:
    """归属解析：_ownership 优先，缺失时取 human 侧请求记录归因。

    human.create_choice 直建的交互（security_check/triggers_ext/LLM 工具）
    不经审批创建链路，_ownership 无记录——创建者 user_id 由创建方服务端
    注入记录（param_inject/state，LLM 不可伪造），归属校验据此放行本人。
    记录也取不到（请求已收敛/服务降级）→ 不可归因，决策面按 admin-only 收口。
    """
    owner = _ownership.get(request_id)
    if owner and owner.get("tenant"):
        return dict(owner)
    if service is not None:
        try:
            record = await service.get_request(request_id)
        except Exception:  # noqa: BLE001 —— 归因失败按不可归因处理
            record = None
        if isinstance(record, dict):
            return _record_owner(record)
    return {"tenant": "", "user": ""}


async def _ownership_denied(
    service: Any,
    request_id: str,
    headers: dict[str, str] | None,
) -> dict[str, Any] | None:
    """交互面守卫一步化：非 admin 先做记录归因回退再校验，admin 免归因直判。

    归因探测（get_request）是一次 human 侧能力往返，admin 无论归属与否都
    放行，省一次往返；记录归因仅供创建者本人的请求过闸。
    """
    fallback = None
    if _header_value(headers, _ROLE_HEADER) != "admin":
        fallback = await _resolve_owner(service, request_id)
    return _decision_denied(headers, request_id, fallback_owner=fallback)


def _decision_denied(
    headers: dict[str, str] | None,
    request_id: str,
    fallback_owner: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """交互面归属校验：放行返回 None，拒绝返回 403 响应（fail-closed）。

    适用于全部决策/响应类端点（approve/deny/response/cancel/viewed/detail）——
    它们与 approve/deny 走同一决策面，等权路径不得绕过守卫。

    规则：
    - 请求缺 X-AgentOS-Tenant 身份头（非内核已认证分发）→ 拒绝；
    - 归属租户已知：同租户且（创建者本人或 admin 角色方可决策；
    - 归属不可归因（无 run 锚/无记录/查询失败）：仅 admin 可决策（运维可恢复，
      避免审批永久卡死）。
    """
    requester_tenant = _header_value(headers, _TENANT_HEADER)
    if not requester_tenant:
        logger.warning(
            "[approval] 决策请求缺租户身份头，拒绝 | request_id=%s", request_id
        )
        return _ok(_json_response({"detail": "缺少租户身份，拒绝决策"}, 403))
    owner = _ownership.get(request_id)
    if not (owner and owner.get("tenant")) and fallback_owner:
        owner = fallback_owner
    owner_tenant = owner.get("tenant", "") if owner else ""
    owner_user = owner.get("user", "") if owner else ""
    attributed = bool(owner_tenant)
    requester_user = _header_value(headers, _USER_HEADER)
    requester_role = _header_value(headers, _ROLE_HEADER)
    # admin：同租户放行；归属不可归因（无 run 锚/查询失败）时放行——
    # 运维可恢复，避免审批永久卡死。
    if requester_role == "admin" and (not attributed or requester_tenant == owner_tenant):
        return None
    if not attributed:
        return _ok(
            _json_response({"detail": "审批归属不可归因，仅 admin 可决策"}, 403)
        )
    # 同租户且（创建者本人或 admin）：创建者未知（创建链路无 user_id）时
    # 同租户放行——一用户一租户模型下同租户即归属人。
    if requester_tenant == owner_tenant and (
        not owner_user or requester_user == owner_user or requester_role == "admin"
    ):
        return None
    logger.warning(
        "[approval] 决策越权拒绝 | request_id=%s | requester_tenant=%s | owner_tenant=%s",
        request_id,
        requester_tenant,
        owner_tenant,
    )
    return _ok(_json_response({"detail": "无权决策该审批"}, 403))

# 审批终态决断：request_id → {approved: bool, reason: str, ts: float}
# create_choice 的 wait 路径（超时/异常/正常）收敛后在此记录终态，
# 供后续 submit/查询返回明确结果（杜绝"超时后 submit 又尝试恢复"的歧义）。
_decisions: dict[str, dict[str, Any]] = {}

# 硬上限：异常灌写下兜底逐出最旧（TTL 之外的第二道界）。
_DECISIONS_MAX_ENTRIES = 4096


def _record_decision(request_id: str, decision: dict[str, Any]) -> None:
    """记录终态决策并顺带淘汰过期条目（S14：长驻 sidecar 内无界增长治理）。

    决策条目只服务"wait 收敛后 submit 重读终态"的短窗口，永不淘汰即
    随历史请求线性泄漏。每次写入做一次 TTL 清扫（条目量小，代价可忽略），
    清扫后仍超硬上限则按 ts 逐出最旧。
    """
    now = time.time()
    expired = [k for k, v in _decisions.items() if now - v.get("ts", 0) > _DECISIONS_TTL_SECONDS]
    for k in expired:
        _decisions.pop(k, None)
    while len(_decisions) >= _DECISIONS_MAX_ENTRIES:
        # 上限为正常量（4096），循环条件保证字典非空，min 必有值
        oldest = min(_decisions.items(), key=lambda kv: kv[1].get("ts", 0))
        _decisions.pop(oldest[0], None)
    entry = dict(decision)
    entry["ts"] = now
    _decisions[request_id] = entry


def _read_default_timeout() -> float:
    """读取默认审批超时（秒），可经 ``APPROVAL_DEFAULT_TIMEOUT_SECONDS`` 覆盖。

    产品决策（F-APPROVAL-1）：默认超时从 300s 调大为 24h=86400s，
    避免长审批被误判超时；非法/非正值的配置回退到 86400s。
    """
    raw = os.environ.get("APPROVAL_DEFAULT_TIMEOUT_SECONDS", "")
    try:
        val = float(raw) if raw else 86400.0
    except (TypeError, ValueError):
        return 86400.0
    return val if val > 0 else 86400.0


DEFAULT_TIMEOUT_SECONDS: float = _read_default_timeout()

# 终态决策重读窗口（秒）：等待窗口内 submit 必可重读终态；窗口外提交返回
# "无挂起审批"本就是正确语义。TTL = 2×默认审批超时（含配置放大量）。
_DECISIONS_TTL_SECONDS = max(86400.0, DEFAULT_TIMEOUT_SECONDS) * 2


def _get_cap(name: str) -> Any | None:
    """获取 capability handle，未注入返回 None。"""
    try:
        return plugin.get_capability(name)
    except KeyError:
        return None


async def _emit_approval_created(
    request_id: str, title: str, options: list[str], run_id: str | None = None
) -> None:
    """通知前端审批已创建（fire-and-forget，失败不影响审批主链路）。

    前端 SchemaFullscreenHost 订阅 ``approval.created`` 事件（ui_schema 声明
    ``trigger: "on_event:approval.created"`` + ``space: "fullscreen"``），
    据此打开全屏审批浮层。payload 与前端 ApprovalCreatedPayload 对齐。
    """
    bus = _get_cap("event-bus")
    if bus is None:
        logger.warning("[approval] event-bus not injected; skip approval.created")
        return
    try:
        await bus.notify("emit", {
            "event": "approval.created",
            "payload": {
                "request_id": request_id,
                "title": title,
                "options": options,
                "mode": "choice",
                "run_id": run_id or "",
            },
            "thread_id": run_id or "",
        })
    except Exception:
        logger.exception("[approval] emit approval.created failed")


async def _suspend_pipeline(run_id: str, approval_id: str) -> dict[str, Any] | None:
    """通过 pipeline-executor 挂起管道，返回 suspend_handle。"""
    pipeline = _get_cap("pipeline-executor")
    if pipeline is None:
        logger.warning("[approval] pipeline-executor not injected; skip suspend | id=%s", approval_id)
        return None
    try:
        result = await pipeline.call("suspend", {"run_id": run_id, "approval_id": approval_id})
    except Exception as exc:
        logger.warning("[approval] suspend failed | id=%s | err=%s", approval_id, exc)
        return None
    if isinstance(result, dict):
        return {
            "run_id": result.get("run_id", run_id),
            "branch_id": result.get("branch_id"),
            "seq": result.get("seq"),
        }
    return {"run_id": run_id, "branch_id": None, "seq": None}


async def _resume_pipeline(handle: dict[str, Any], approval_id: str, result: Any) -> bool:
    """通过 pipeline-executor 恢复管道。"""
    pipeline = _get_cap("pipeline-executor")
    if pipeline is None:
        logger.warning("[approval] pipeline-executor not injected; cannot resume | id=%s", approval_id)
        return False
    params: dict[str, Any] = {"approval_id": approval_id, "result": result}
    for key in ("run_id", "branch_id", "seq"):
        val = handle.get(key)
        if val is not None:
            params[key] = val
    try:
        await pipeline.call("resume", params)
        return True
    except Exception as exc:
        logger.warning("[approval] resume failed | id=%s | err=%s", approval_id, exc)
        return False


def _build_options(label_list: list[str]) -> list[dict[str, str]]:
    """把字符串选项列表转成 human-interaction 的 [{id, label}] 格式。"""
    return [{"id": str(i), "label": label} for i, label in enumerate(label_list)]


def _classify_reject(wait_res: dict[str, Any]) -> str:
    """从 human-interaction 的 error 返回推断拒绝原因。

    capability 层把 service 抛出的 InteractionTimeoutError/CancelledError/
    DeniedError 统一收敛为 ``{"error": ..., "error_code": ...}``：
    - INTERACTION_TIMEOUT → "timeout"
    - INTERACTION_CANCELLED → "cancelled"
    - 其余（含 denied / 异常）→ "rejected"
    """
    code = wait_res.get("error_code")
    if code == "INTERACTION_TIMEOUT":
        return "timeout"
    if code == "INTERACTION_CANCELLED":
        return "cancelled"
    return "rejected"


# ── 工具：create_choice（转调 human-interaction） ──────────────────


@plugin.tool(
    name="approval.create_choice",
    schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "run_id": {"type": "string", "description": "管道运行 ID（用于挂起管道）"},
            "user_id": {
                "type": "string",
                "description": "创建者用户（param_inject 服务端权威注入，LLM 无需填写）",
            },
            "timeout": {
                "type": "number",
                "default": DEFAULT_TIMEOUT_SECONDS,
                "description": "审批等待超时（秒）；超时 = 拒绝（不恢复管道）。"
                "默认 24h，可经 APPROVAL_DEFAULT_TIMEOUT_SECONDS 覆盖",
            },
        },
        "required": ["title", "options"],
    },
    description="Create a choice-mode approval（委托 human-interaction 弹窗 + 挂起管道）",
)
async def create_choice(
    title: str,
    options: list[str],
    run_id: str | None = None,
    user_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """创建选择审批——交互委托 human-interaction，approval 只管管道挂起/恢复。

    超时语义（F-APPROVAL-1）：审批等待超时/异常 = **拒绝**——
    不恢复管道、记录拒绝状态（``_decisions``），不再"超时即恢复放行"。
    无论正常/超时/异常，挂起句柄都在 finally 清理，杜绝句柄泄漏导致管道挂死。

    归属：创建时记录 (tenant, creator)——tenant 按 run_id 查内核 runs 表权威
    解析，creator 取 param_inject 注入的 user_id；approve/deny HTTP 端点据此
    做同租户 +（创建者本人或 admin）校验。等待窗口收敛即清除记录。

    恢复失败语义：resume 调用失败时管道仍挂起，返回 ``resume_failed``
    并保持挂起记录（不落 approved 决策），可经 approval.submit 重试。
    """
    hi = _get_cap("human-interaction")
    if hi is None:
        return {"error": "human-interaction capability not injected"}

    # 第一步：经 human-interaction 创建交互请求
    session_id = run_id or "approval"
    create_res = await hi.call("create_choice", {
        "session_id": session_id,
        "thread_id": session_id,
        "tab_id": session_id,
        "title": title,
        "description": "",
        "options": _build_options(options),
        "timeout_seconds": int(timeout),
    })
    if not isinstance(create_res, dict) or create_res.get("error"):
        return {"error": f"create_choice failed: {create_res}"}
    request_id = create_res.get("request_id", "")

    # 归属记录先行于等待窗口：approve/deny 在 wait 期间到达，需据此校验
    # （窗口收敛后随 _suspended 一并清除）。
    _record_ownership(request_id, run_id or "", user_id)

    # 通知前端全屏审批浮层（SchemaFullscreenHost）；fire-and-forget，失败不阻塞。
    await _emit_approval_created(request_id=request_id, title=title, options=options, run_id=run_id)

    # 第二步：挂起管道（approval 独有职责）
    suspend_handle = None
    if run_id:
        suspend_handle = await _suspend_pipeline(run_id, request_id)
        _suspended[request_id] = {
            "suspend_handle": suspend_handle,
            "run_id": run_id,
            "created_at": time.time(),
        }

    # 第三步：等待用户选择（阻塞，由 human-interaction 管 Event + 前端回路）
    # 超时/异常 = 拒绝：capability 层把 InteractionTimeoutError 等收敛为
    # {"error": ...}（也可能直接 raise）——两种形态都视为拒绝，绝不恢复管道。
    rejected_reason: str | None = None
    selected = ""
    try:
        wait_res = await hi.call("wait_for_choice", {"request_id": request_id, "timeout": timeout})
        if isinstance(wait_res, dict) and wait_res.get("error"):
            rejected_reason = _classify_reject(wait_res)
        elif isinstance(wait_res, dict):
            selected = wait_res.get("selected_option", "") or ""
        else:
            rejected_reason = "rejected"
    except Exception as exc:  # noqa: BLE001 —— wait 路径任何异常都收敛为拒绝，不让管道挂死
        logger.warning("[approval] wait_for_choice exception | id=%s | err=%s", request_id, exc)
        rejected_reason = "rejected"
    finally:
        # 杜绝句柄泄漏：无论成功/超时/异常，挂起句柄必须清理
        _suspended.pop(request_id, None)
        # 等待窗口收敛：approve/deny 决策面关闭，归属记录一并清除
        _ownership.pop(request_id, None)

    # 超时/异常 = 拒绝：记录拒绝状态，不恢复管道
    if rejected_reason is not None:
        _record_decision(request_id, {"approved": False, "reason": rejected_reason, "resumed": False})
        logger.info(
            "[approval] rejected (no resume) | id=%s | reason=%s", request_id, rejected_reason
        )
        return {
            "request_id": request_id,
            "status": "rejected",
            "reason": rejected_reason,
            "resumed": False,
        }

    # 第四步：正常路径——恢复管道。恢复失败不得记 approved+resolved（管道仍
    # 挂起）：重新武装挂起记录供 submit 重试，返回显式 resume_failed。
    if suspend_handle is None:
        # 未传 run_id：纯交互决断，无管道语义
        _record_decision(request_id, {"approved": True, "reason": "resolved", "resumed": False})
        return {
            "request_id": request_id,
            "status": "resolved",
            "selected_option": selected,
            "resumed": False,
        }

    resumed = await _resume_pipeline(suspend_handle, request_id, selected)
    if resumed:
        _record_decision(request_id, {"approved": True, "reason": "resolved", "resumed": True})
        return {
            "request_id": request_id,
            "status": "resolved",
            "selected_option": selected,
            "resumed": True,
        }

    _suspended[request_id] = {
        "suspend_handle": suspend_handle,
        "run_id": run_id,
        "created_at": time.time(),
    }
    logger.warning("[approval] resume failed after choice; kept suspended | id=%s", request_id)
    return {
        "request_id": request_id,
        "status": "resume_failed",
        "selected_option": selected,
        "resumed": False,
        "reason": "管道恢复失败，审批保持挂起（可经 submit 重试）",
    }


# ── 工具：submit（恢复挂起的管道） ──────────────────────────────


@plugin.tool(
    name="approval.submit",
    schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "result": {"type": "string"},
        },
        "required": ["request_id", "result"],
    },
    description="Submit approval result and resume the suspended pipeline",
)
async def submit(request_id: str, result: str) -> dict[str, Any]:
    """提交审批结果，恢复挂起的管道。

    注：create_choice 已内置 wait_for_choice + resume，多数场景不需要单独调 submit。
    本工具保留给"前端直接提交恢复"的路径（如 review 模式异步审批）。

    若该 request_id 已被 create_choice 的 wait 路径决断（超时拒绝/已恢复），
    直接返回已记录的终态，不再尝试重复恢复。
    """
    prior = _decisions.get(request_id)
    if prior is not None:
        return {
            "request_id": request_id,
            "status": "resolved" if prior["approved"] else "rejected",
            "approved": prior["approved"],
            "reason": prior.get("reason"),
            "resumed": prior["resumed"],
        }

    record = _suspended.get(request_id)
    if record is None:
        return {"error": "no suspended approval for this request_id", "request_id": request_id}

    handle = record.get("suspend_handle")
    if handle is None:
        # 无句柄：无管道可恢复，直接落已决断终态
        _suspended.pop(request_id, None)
        _record_decision(request_id, {"approved": True, "reason": "submitted", "resumed": False})
        logger.info("[approval] submitted (no handle) | id=%s", request_id)
        return {"request_id": request_id, "status": "resolved", "result": result, "resumed": False}

    # 恢复失败保持挂起态（不落 approved 决策），可再次 submit 重试
    resumed = await _resume_pipeline(handle, request_id, result)
    if not resumed:
        logger.warning("[approval] submit resume failed; kept suspended | id=%s", request_id)
        return {
            "request_id": request_id,
            "status": "resume_failed",
            "resumed": False,
            "reason": "管道恢复失败，审批保持挂起（可重新 submit 重试）",
        }

    _suspended.pop(request_id, None)
    _record_decision(request_id, {"approved": True, "reason": "submitted", "resumed": True})
    logger.info("[approval] submitted | id=%s | resumed=True", request_id)
    return {"request_id": request_id, "status": "resolved", "result": result, "resumed": True}


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    logger.info("approval_service loaded（委托 human-interaction 处理交互）")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    logger.info("approval_service unloaded | suspended=%d", len(_suspended))


# ── HTTP 端点（http.handle）：interaction 域 ─────────────────────────────
# 前端 /ext/approval_service/interaction/**（原 /ext/channel_api/interaction/**，
# 源 routes_missing.py interaction_router 7 路由）。经 human-interaction 桥
# 转发 human sidecar 真实单例。


class _HumanInteractionCapabilityProxy:
    """经 human-interaction capability 桥调 human sidecar 的真实服务实例。

    sidecar 进程隔离下 import human.service 拿到的是本进程全新空实例
    （_requests 恒空、Event 表为空）——真实交互数据在 human_interaction_tool
    进程。经 human-interaction capability（manifest provides 声明的命名空间桥，
    内核按声明路由到目标插件）转发到 interaction.get_pending/respond/cancel，
    作用于真实单例。
    """

    def __init__(self, hi: Any) -> None:
        self._hi = hi

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        # 方法名直通桥（tool_prefix=interaction 由 human manifest provides 声明，
        # 桥拼 interaction.<method> 调 sidecar）。统一显式传大传输超时：审批
        # 交互面请求不因 SDK 默认 30s 误断，实际时长由 human 服务 enforce
        # （与源 routes_missing 原语义一致）。
        res = await self._hi.call(method, params, timeout=86500.0)
        if isinstance(res, dict) and res.get("error"):
            raise RuntimeError(f"interaction.{method} 失败: {res['error']}")
        return res

    async def get_pending_requests(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        res = await self._call("get_pending", {"session_id": session_id, "limit": limit})
        return res.get("requests", [])

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        items = await self.get_pending_requests(limit=500)
        for it in items:
            if isinstance(it, dict) and (
                it.get("id") == request_id or it.get("request_id") == request_id
            ):
                return it
        return None

    async def respond(self, request_id: str, resp_data: dict[str, Any]) -> bool:
        # 兼容嵌套（body.response.*）与扁平（body 平铺）两种前端形状
        inner = resp_data.get("response", {}) if isinstance(resp_data, dict) else {}
        if not isinstance(inner, dict) or not inner:
            inner = resp_data if isinstance(resp_data, dict) else {}
        return await self.submit_response(
            request_id=request_id,
            response_type=inner.get("response_type", "answered"),
            selected_option=inner.get("selected_option"),
            answers=inner.get("answers"),
            feedback=inner.get("feedback"),
        )

    async def submit_response(
        self,
        request_id: str,
        response_type: str,
        selected_option: str | None = None,
        answers: list[str] | None = None,
        feedback: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        try:
            # response 载荷对齐 human 侧 interaction.respond 的 (request_id,
            # response) 透传形态：应答键由本侧组装，契约由 human 服务自持。
            res = await self._call("respond", {
                "request_id": request_id,
                "response": {
                    "response_type": response_type,
                    "selected_option": selected_option,
                    "answers": answers,
                    "feedback": feedback,
                },
            })
        except RuntimeError as exc:
            logger.warning("[approval] 交互响应转发失败 | request_id=%s | err=%s", request_id, exc)
            return False
        return bool(res.get("ok"))

    async def cancel_request(self, request_id: str, reason: str | None = None) -> bool:
        """取消交互请求（human sidecar 的 interaction.cancel 工具）。"""
        try:
            res = await self._call("cancel", {"request_id": request_id, "reason": reason})
        except RuntimeError as exc:
            logger.warning("[approval] 交互取消转发失败 | request_id=%s | err=%s", request_id, exc)
            return False
        return bool(res.get("ok"))

    async def mark_as_viewed(self, request_id: str) -> bool:
        """标记请求已查看——human sidecar 目前无 interaction.mark_viewed 工具
        （工具面仅 send_notification/create_choice/wait_for_choice/respond/cancel/
        get_pending），本端点保留为前端确认标记：成功应答、不落库——与提交类
        端点语义区分。
        """
        logger.warning(
            "[approval] viewed 端点确认应答（human sidecar 无 viewed 工具）| request_id=%s",
            request_id,
        )
        return True


def _get_human_interaction_service() -> Any | None:
    """经 human-interaction capability 桥转发到 human sidecar 真实实例；桥不可用降级 None。

    降级语义（对齐原空实例回退的可观察面）：pending 恒空、审批响应返回 False、
    详情 404——不崩 handler，前端轮询契约不破坏。
    """
    try:
        return _HumanInteractionCapabilityProxy(plugin.get_capability("human-interaction"))
    except (KeyError, AttributeError):
        logger.warning(
            "[approval] human-interaction capability 不可用，interaction HTTP 面降级"
            "（pending 恒空，审批响应不可用）"
        )
        return None


_INTERACTION_PREFIX = "/ext/approval_service/interaction"


def _interaction_not_found(path: str) -> dict[str, Any]:
    """404 响应信封（对齐源 routes_missing.interaction_router 的 {"detail":...} 口径外错误形态）。"""
    return _ok(_json_response({"error": "not found", "path": path}, 404))


async def _route_submit_response(
    service: Any,
    raw_body: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST /response：提交交互响应。

    body.response.* 嵌套或扁平两种形态均落 request_id 判定；服务未注入降级
    ``{"success": False}``。归属校验先行于响应副作用：/response 与 approve/deny
    走同一决策面（service.respond → submit_response），等权路径不得绕过守卫。
    """
    body = _decode_body(raw_body)
    if not body or "request_id" not in body:
        return _ok(_json_response({"detail": "缺少 request_id"}, 400))
    rid = str(body["request_id"])
    if service is None:
        return _ok(_json_response({"success": False}))
    denied = await _ownership_denied(service, rid, headers)
    if denied is not None:
        return denied
    result = await service.respond(rid, body)
    return _ok(_json_response({"success": result}))


async def _route_list_pending(
    service: Any,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """GET /pending：待处理请求列表（按请求者租户过滤）。

    过滤规则与决策面同源：admin 角色可见全部（运维面）；其余已认证用户只见
    归属本租户的请求；缺租户身份头一律空集（fail-closed——绝不全量兜底，
    否则列表退化为跨租户交互枚举）。
    """
    if service is None:
        return _ok(_json_response({"items": [], "total": 0}))
    requests = await service.get_pending_requests()
    requester_tenant = _header_value(headers, _TENANT_HEADER)
    requester_role = _header_value(headers, _ROLE_HEADER)
    if requester_role == "admin":
        visible = requests
    else:
        if not requester_tenant:
            return _ok(_json_response({"items": [], "total": 0}))
        visible = [
            r for r in requests
            if _header_tenant_matches(_record_owner(r), requester_tenant)
        ]
    return _ok(_json_response({"items": visible, "total": len(visible)}))


def _header_tenant_matches(owner: dict[str, str], requester_tenant: str) -> bool:
    """归属租户与请求者租户比对：不可归因一律 False（fail-closed）。"""
    return bool(owner.get("tenant")) and owner.get("tenant") == requester_tenant


def _match_request_route(sub: str) -> tuple[str, str] | None:
    """/{request_id} 系列路由解析：返回 (rid, action)；非该系列返回 None。

    action 取值："detail"（GET /{rid}）或 approve/deny/cancel/viewed
    （POST /{rid}/{action}）；method 归属校验由执行器负责。
    """
    if not (sub.startswith("/") and len(sub) > 1):
        return None
    rest = sub[1:]
    if "/" not in rest:
        return rest, "detail"
    rid, action = rest.split("/", 1)
    return rid, action


async def _dispatch_request_action(
    service: Any,
    rid: str,
    action: str,
    method: str,
    raw_body: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """执行 /{request_id} 系列端点；路由命中但方法不符或动作未知返回 None（调用方统一 404）。"""
    if action == "detail":
        # GET /{rid}：交互请求详情；不存在与服务未注入同形 404。
        # 归属校验先行于记录读取（_ownership 命中即可判，记录仅作归因回退）。
        if method != "GET":
            return None
        if service is None:
            return _ok(_json_response({"detail": "交互请求不存在"}, 404))
        denied = await _ownership_denied(service, rid, headers)
        if denied is not None:
            return denied
        record = await service.get_request(rid)
        if not record:
            return _ok(_json_response({"detail": "交互请求不存在"}, 404))
        return _ok(_json_response(record))

    if action in ("approve", "deny") and method == "POST":
        # 归属校验先行于任何决策副作用：同租户且（创建者本人或 admin），
        # 他租户/他人非 admin 一律 403（规则见 _decision_denied）。
        denied = _decision_denied(headers, rid)
        if denied is not None:
            return denied
        if service is None:
            return _ok(_json_response({
                "success": False,
                "request_id": rid,
                "status": "approved" if action == "approve" else "denied",
            }))
        body = _decode_body(raw_body)
        result = await service.submit_response(
            request_id=rid,
            response_type="approved" if action == "approve" else "denied",
            selected_option="approve" if action == "approve" else "reject",
            feedback=body.get("feedback") if body else None,
        )
        return _ok(_json_response({
            "success": result,
            "request_id": rid,
            "status": "approved" if action == "approve" else "denied",
        }))

    if action == "cancel" and method == "POST":
        # 归属校验：取消同样作用于他人可见的交互状态，与决策面同守卫
        if service is not None:
            denied = await _ownership_denied(service, rid, headers)
            if denied is not None:
                return denied
        if service is None:
            return _ok(_json_response({
                "success": False,
                "request_id": rid,
                "status": "cancelled",
            }))
        body = _decode_body(raw_body)
        result = await service.cancel_request(
            request_id=rid,
            reason=body.get("reason") if body else None,
        )
        return _ok(_json_response({
            "success": result,
            "request_id": rid,
            "status": "cancelled",
        }))

    if action == "viewed" and method == "POST":
        # viewed 不消费 body（标记已读无参数）；归属校验同决策面
        if service is not None:
            denied = await _ownership_denied(service, rid, headers)
            if denied is not None:
                return denied
        if service is None:
            return _ok(_json_response({
                "success": False,
                "request_id": rid,
                "viewed": True,
            }))
        result = await service.mark_as_viewed(rid)
        return _ok(_json_response({
            "success": result,
            "request_id": rid,
            "viewed": True,
        }))

    return None


async def _route_interaction_subroute(
    service: Any,
    sub: str,
    method: str,
    raw_body: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """按 sub 路径依次匹配 interaction 域 7 条路由；无一命中返回 None（调用方统一 404）。"""
    if sub == "/response" and method == "POST":
        return await _route_submit_response(service, raw_body, headers)
    if sub == "/pending" and method == "GET":
        return await _route_list_pending(service, headers)
    request_match = _match_request_route(sub)
    if request_match is not None:
        rid, action = request_match
        return await _dispatch_request_action(service, rid, action, method, raw_body, headers)
    return None


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
    description="HTTP endpoint handler for /ext/approval_service/interaction/**",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """interaction 域分发：/ext/approval_service/interaction/**（7 路由）。

    响应形态对齐源 routes_missing.interaction_router（success/request_id/status
    字段 + 404 {"detail": ...}）。human-interaction 桥不可用时降级空 pending/False 响应。
    headers 携带内核注入的身份头（X-AgentOS-Tenant/User/Role），approve/deny
    归属校验据此判定。
    """
    del plugin_id, query

    if not path.startswith(_INTERACTION_PREFIX):
        return _interaction_not_found(path)
    sub = path[len(_INTERACTION_PREFIX):]
    service = _get_human_interaction_service()

    try:
        routed = await _route_interaction_subroute(service, sub, method, raw_body, headers)
        if routed is not None:
            return routed
        logger.warning("http.handle: no route for sub=%s method=%s", sub, method)
        return _interaction_not_found(path)
    except ValueError as exc:
        return _ok(_json_response({"error": str(exc)}, 400))
    except Exception as exc:  # noqa: BLE001 —— 工具代理/服务异常统一 500
        logger.error("interaction http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


if __name__ == "__main__":
    plugin.run()

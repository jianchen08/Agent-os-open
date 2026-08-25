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
  /ext/approval_service/interaction/**，经 tool-executor 能力代理
  human_interaction_tool（granted_capabilities 声明 tool-executor）——
  代理（_HumanInteractionCapabilityProxy）直达 human sidecar
  真实单例的 interaction.get_pending / interaction.respond 工具。
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

import base64
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("approval_service")

# 审批挂起句柄：approval_id → {suspend_handle, run_id, created_at}
# 仅存恢复管道所需的最小信息（交互状态由 human-interaction 插件管）。
_suspended: dict[str, dict[str, Any]] = {}

# 审批终态决断：request_id → {approved: bool, reason: str}
# create_choice 的 wait 路径（超时/异常/正常）收敛后在此记录终态，
# 供后续 submit/查询返回明确结果（杜绝"超时后 submit 又尝试恢复"的歧义）。
_decisions: dict[str, dict[str, Any]] = {}


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
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """创建选择审批——交互委托 human-interaction，approval 只管管道挂起/恢复。

    超时语义（F-APPROVAL-1）：审批等待超时/异常 = **拒绝**——
    不恢复管道、记录拒绝状态（``_decisions``），不再"超时即恢复放行"。
    无论正常/超时/异常，挂起句柄都在 finally 清理，杜绝句柄泄漏导致管道挂死。
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

    # 超时/异常 = 拒绝：记录拒绝状态，不恢复管道
    if rejected_reason is not None:
        _decisions[request_id] = {"approved": False, "reason": rejected_reason}
        logger.info(
            "[approval] rejected (no resume) | id=%s | reason=%s", request_id, rejected_reason
        )
        return {
            "request_id": request_id,
            "status": "rejected",
            "reason": rejected_reason,
            "resumed": False,
        }

    # 第四步：正常路径——恢复管道（行为不变，回归护栏）
    resumed = False
    if suspend_handle is not None:
        resumed = await _resume_pipeline(suspend_handle, request_id, selected)

    _decisions[request_id] = {"approved": True, "reason": "resolved"}
    return {
        "request_id": request_id,
        "status": "resolved",
        "selected_option": selected,
        "resumed": resumed,
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
            "resumed": prior["approved"],
        }

    record = _suspended.get(request_id)
    if record is None:
        return {"error": "no suspended approval for this request_id", "request_id": request_id}

    handle = record.get("suspend_handle")
    resumed = False
    if handle is not None:
        resumed = await _resume_pipeline(handle, request_id, result)

    _suspended.pop(request_id, None)
    _decisions[request_id] = {"approved": True, "reason": "submitted"}
    logger.info("[approval] submitted | id=%s | resumed=%s", request_id, resumed)
    return {"request_id": request_id, "status": "resolved", "result": result, "resumed": resumed}


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    logger.info("approval_service loaded（委托 human-interaction 处理交互）")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    logger.info("approval_service unloaded | suspended=%d", len(_suspended))


# ── HTTP 端点（http.handle）：interaction 域 ─────────────────────────────
# 前端 /ext/approval_service/interaction/**（原 /ext/channel_api/interaction/**，
# 源 routes_missing.py interaction_router 7 路由）。经 tool-executor 代理
# human_interaction_tool 真实单例。


class _HumanInteractionCapabilityProxy:
    """经内核 tool-executor 调 human_interaction_tool sidecar 的真实服务实例。

    sidecar 进程隔离下 import human.service 拿到的是本进程全新空实例
    （_requests 恒空、Event 表为空）——真实交互数据在 human_interaction_tool
    进程。经标准能力 tool-executor.invoke 调用该插件的
    interaction.* 工具（作用于真实单例）。
    """

    _TOOL_METHODS = {
        "get_pending": "interaction.get_pending",
        "respond": "interaction.respond",
        "cancel": "interaction.cancel",
    }

    def __init__(self, executor_call: Callable[[str, dict[str, Any]], Any]) -> None:
        self._executor_call = executor_call

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        tool = self._TOOL_METHODS.get(method)
        if not tool:
            raise RuntimeError(f"human-interaction.{method} 无对应工具")
        res = await self._executor_call("invoke", {"tool_name": tool, "args": params})
        # invoke 返回形状自适应：工具结果可能直接平铺，也可能包在 data/result 里。
        # 注意候选顺序：先解包 data/result 信封再回退原值（源 routes_missing 原序
        # 是 res 先命中——任何 dict 信封都会被当作终值返回，包在 data/result 里的
        # 结果读不到；现序保证两种形态都正确，平铺结果零行为变化）。
        for candidate in (
            res.get("data") if isinstance(res, dict) and "data" in res else None,
            res.get("result") if isinstance(res, dict) and "result" in res else None,
            res,
        ):
            if isinstance(candidate, dict):
                if candidate.get("error"):
                    raise RuntimeError(f"{tool} 失败: {candidate['error']}")
                return candidate
        raise RuntimeError(f"{tool} 返回无法解析: {type(res).__name__}")

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
            res = await self._call("respond", {
                "request_id": request_id,
                "response_type": response_type,
                "selected_option": selected_option,
                "answers": answers,
                "feedback": feedback,
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
    """经内核 tool-executor 转发到 human sidecar 真实实例；通道不可用降级 None。

    降级语义（对齐原空实例回退的可观察面）：pending 恒空、审批响应返回 False、
    详情 404——不崩 handler，前端轮询契约不破坏。
    """
    try:
        executor = plugin.get_capability("tool-executor")

        async def _executor_call(method: str, params: dict[str, Any]) -> Any:
            # 审批等待（wait_for_choice 业务超时 86400）经 security_check →
            # human sidecar 全链长等待：SDK 默认 30s 会先于用户操作掐断；显式
            # 传大值，实际超时由 human 服务 enforce（与 route_missing 原语义一致）
            return await executor.call(method, params, timeout=86500.0)

        return _HumanInteractionCapabilityProxy(_executor_call)
    except (KeyError, AttributeError):
        logger.warning(
            "[approval] tool-executor 能力不可用，human-interaction HTTP 面降级"
            "（pending 恒空，审批响应不可用）"
        )
        return None


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """把 JSON 可序列化对象包成内核期望的 HttpHandleResponse（body base64）。"""
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


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 → JSON dict；空 body 返回 {}）。"""
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        candidate = base64.b64decode(raw_body).decode("utf-8")
        if candidate.lstrip().startswith(("{", "[")):
            decoded = candidate
    except Exception:  # noqa: BLE001  # pragma: no cover —— b64decode(validate=False) 对任意串几乎不抛
        pass
    try:
        parsed = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


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
    字段 + 404 {"detail": ...}）。tool-executor 未注入时降级空 pending/False 响应。
    """
    del plugin_id, headers, query

    prefix = "/ext/approval_service/interaction"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    sub = path[len(prefix):]
    service = _get_human_interaction_service()

    def _empty_pending() -> dict[str, Any]:
        return _ok(_json_response({"items": [], "total": 0}))

    try:
        # POST /response：提交交互响应（body.response.* 嵌套或扁平两种形态）
        if sub == "/response" and method == "POST":
            body = _decode_body(raw_body)
            if not body or "request_id" not in body:
                return _ok(_json_response({"detail": "缺少 request_id"}, 400))
            if service is None:
                return _ok(_json_response({"success": False}))
            result = await service.respond(body["request_id"], body)
            return _ok(_json_response({"success": result}))

        # GET /pending：待处理请求列表
        if sub == "/pending" and method == "GET":
            if service is None:
                return _empty_pending()
            requests = await service.get_pending_requests()
            return _ok(_json_response({"items": requests, "total": len(requests)}))

        # /{request_id} 系列
        if sub.startswith("/") and len(sub) > 1:
            rest = sub[1:]
            if "/" not in rest:
                rid = rest
                if method == "GET":
                    if service is None:
                        return _ok(_json_response({"detail": "交互请求不存在"}, 404))
                    record = await service.get_request(rid)
                    if not record:
                        return _ok(_json_response({"detail": "交互请求不存在"}, 404))
                    return _ok(_json_response(record))
            else:
                rid, action = rest.split("/", 1)
                if action in ("approve", "deny") and method == "POST":
                    if service is None:
                        success = False
                    else:
                        body = _decode_body(raw_body)
                        if action == "approve":
                            result = await service.submit_response(
                                request_id=rid,
                                response_type="approved",
                                selected_option="approve",
                                feedback=body.get("feedback") if body else None,
                            )
                        else:
                            result = await service.submit_response(
                                request_id=rid,
                                response_type="denied",
                                selected_option="reject",
                                feedback=body.get("feedback") if body else None,
                            )
                        success = result
                    return _ok(_json_response({
                        "success": success,
                        "request_id": rid,
                        "status": "approved" if action == "approve" else "denied",
                    }))
                if action == "cancel" and method == "POST":
                    if service is None:
                        success = False
                    else:
                        body = _decode_body(raw_body)
                        result = await service.cancel_request(
                            request_id=rid,
                            reason=body.get("reason") if body else None,
                        )
                        success = result
                    return _ok(_json_response({
                        "success": success,
                        "request_id": rid,
                        "status": "cancelled",
                    }))
                if action == "viewed" and method == "POST":
                    if service is None:
                        success = False
                    else:
                        result = await service.mark_as_viewed(rid)
                        success = result
                    return _ok(_json_response({
                        "success": success,
                        "request_id": rid,
                        "viewed": True,
                    }))

        logger.warning("http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except ValueError as exc:
        return _ok(_json_response({"error": str(exc)}, 400))
    except Exception as exc:  # noqa: BLE001 —— 工具代理/服务异常统一 500
        logger.error("interaction http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


if __name__ == "__main__":
    plugin.run()

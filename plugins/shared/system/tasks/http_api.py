"""tasks 插件自持 HTTP 面——tasks/projects 域。

来源：plugins/shared/system/channel_api/（只读参考，本文件为搬迁改造产物）：
- routes_tasks.py（21 端点：/tasks 域）
- routes_missing.py task_phase_router（task_phase/ac 9 端点）
- routes_missing.py projects_router（7 端点）

端点职责：
1. 插件内部经 ``from service_access import get_task_service`` 引用（无跨包借用）。
2. 任务列表/创建不再依赖 memory_store 数据兜底。
3. task_phase/ac 端点：phase 由任务状态映射，AC 保持未评估占位语义。
4. projects 域：projects = 容器任务（container task）。创建 project =
   建容器任务（task_scope=container，含 workspace 关联元数据 ws_meta），
   list/get/pause/resume/auto-execute/delete = 容器任务生命周期操作。
5. 能力访问（chat / pipeline-state / pipeline-executor 内核能力）经
   ``_capability()`` 统一取句柄（懒 import server.plugin；测试可 monkeypatch）。

协议：http.handle 工具按 path 分发（plugin.json http_endpoints 声明、内核
dispatcher 调度的标准插件 HTTP 面模式，与 agent_manager/task_form 同款）。
返回 ToolExecutionResult{success, data}，data 为 HttpHandleResponse
{status, headers, body(base64)}。
"""
from __future__ import annotations

import base64
import json
import logging
import sys
import time
from typing import Any

from enum_utils import safe_enum_value
from pydantic import BaseModel, Field
from service_access import get_task_service

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
# 请求/响应模型（channel_api/models.py Task* 子集搬入）
# ════════════════════════════════════════════════════════════


class TaskCreate(BaseModel):
    """创建任务请求模型。"""

    title: str
    description: str | None = None
    agent_id: str | None = None
    priority: int = 5
    tags: list[str] = Field(default_factory=list)
    input_data: dict[str, Any] = Field(default_factory=dict)


class TaskRootCreate(BaseModel):
    """手动创建根任务请求模型。"""

    title: str
    description: str = ""
    task_scope: str = "non_container"  # "container" | "non_container"
    target_id: str = ""  # 非容器必填（执行 agent）；容器为空
    workspace: str = ""
    isolation_level: str = ""  # plain/worktree/shared
    inherit: dict[str, Any] | None = None
    thread_id: str  # 复用当前会话 → 作 session_id
    parent_task_id: str | None = None  # 父容器任务 ID；有值则挂为子任务


class TaskUpdate(BaseModel):
    """更新任务请求模型。"""

    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    tags: list[str] | None = None


class TaskResponse(BaseModel):
    """任务响应模型。"""

    id: str
    title: str
    description: str | None = None
    status: str = "pending"
    priority: int = 5
    parent_task_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    agent_level: str | None = None
    thread_id: str | None = None
    created_by: str | None = None
    pipeline_run_id: str | None = None
    execution_record_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    input_data: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] | None = None


class TaskListResponse(BaseModel):
    """任务列表响应模型。"""

    items: list[TaskResponse]
    total: int


class TaskSubmitResponse(BaseModel):
    """任务提交响应模型。"""

    task_id: str
    status: str
    message: str


# ════════════════════════════════════════════════════════════
# 业务异常与请求校验（channel_api/deps.py 子集搬入）
# ════════════════════════════════════════════════════════════


class APIError(Exception):
    """API 业务异常，携带错误码和 HTTP 状态码。"""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def validate_pagination(limit: int, offset: int, max_limit: int = 100) -> None:
    """验证分页参数。"""
    if limit < 1 or limit > max_limit:
        raise APIError(
            status_code=400,
            error_code="VAL_RANGE_7003",
            message=f"limit 必须在 1-{max_limit} 之间",
        )
    if offset < 0:
        raise APIError(
            status_code=400,
            error_code="VAL_RANGE_7003",
            message="offset 不能为负数",
        )


# ════════════════════════════════════════════════════════════
# HTTP 响应协议（内核 dispatcher 契约，与 agent_manager/task_form 同款）
# ════════════════════════════════════════════════════════════


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


def _http_exc_response(exc: Exception) -> dict[str, Any]:
    """把 APIError / HTTPException 转成带 status 的 HTTP 响应。"""
    status = getattr(exc, "status_code", None) or 500
    detail = getattr(exc, "detail", None)
    if detail is None:
        detail = str(exc)
    return _ok(_json_response({"detail": detail}, int(status)))


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 或明文 JSON）为 dict。"""
    if not raw_body:
        return {}
    try:
        try:
            decoded = base64.b64decode(raw_body).decode("utf-8")
            if not decoded.lstrip().startswith(("{", "[")):
                decoded = raw_body
        except Exception:  # noqa: BLE001
            decoded = raw_body
        return json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON body: {e}") from e


def _pydantic_to_dict(obj: Any) -> Any:
    """把 pydantic 模型转成可 JSON 化的 dict。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, exclude_none=True)
    return obj


# ════════════════════════════════════════════════════════════
# caller 身份解析（内核 0.2 开发期 token）
# ════════════════════════════════════════════════════════════


def _decode_kernel_token(token: str) -> tuple[str, str, int] | None:
    """解码内核 0.2 开发期 token（base64_nopad("access:{user_id}:{username}:{exp}")）。

    与 kernel http/src/auth.rs decode_token 同构（agent_manager 同款实现）；
    无效/过期返回 None。
    """
    try:
        padded = token.strip() + "=" * (-len(token.strip()) % 4)
        payload = base64.b64decode(padded, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    parts = payload.split(":", 3)
    if len(parts) != 4:
        return None
    try:
        exp = int(parts[3])
    except ValueError:
        return None
    return parts[1], parts[2], exp


def _resolve_caller(headers: dict[str, str] | None) -> dict[str, Any]:
    """从请求头解析可信 caller 身份（sub/username）。

    ``http.handle`` 由内核 dispatcher 调度（鉴权在 dispatcher 层按
    ``http_endpoints.auth=user`` 完成），此处仅取真实 caller 身份供
    handler 做垂直隔离（get_task 的跨用户 404）。无/无效 token → {}
    （保持既有未鉴权兼容行为）。
    """
    authz = ""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and v:
            authz = str(v)
            break
    token = authz[7:] if authz.lower().startswith("bearer ") else ""
    if not token:
        return {}
    decoded = _decode_kernel_token(token)
    if decoded is None:
        return {}
    user_id, username, exp = decoded
    if int(time.time()) >= exp:
        return {}
    return {"sub": user_id, "username": username}


def _current_user(_user: Any) -> dict[str, Any]:
    """SDK 进程内分发不解析 FastAPI Depends——_user 为 dict 时透传，否则空用户。"""
    return _user if isinstance(_user, dict) else {}


# ════════════════════════════════════════════════════════════
# 映射辅助（routes_tasks.py 原样搬入）
# ════════════════════════════════════════════════════════════


def _map_status_for_api(status: str) -> str:
    """直接返回后端原始状态值，前后端统一字段。"""
    return status


def _task_model_to_dict(task_model: Any) -> dict[str, Any]:
    """将 TaskModel dataclass 转为字典。"""

    from dataclasses import asdict  # noqa: PLC0415

    d = asdict(task_model)
    raw_status = safe_enum_value(task_model.status)
    d["status"] = _map_status_for_api(raw_status)
    if hasattr(task_model, "priority") and hasattr(task_model.priority, "value"):
        d["priority"] = task_model.priority.value
    return d


def _task_to_response(t: dict[str, Any]) -> TaskResponse:
    """将存储层任务字典转为 TaskResponse。"""

    raw_status = t.get("status", "pending")

    # 从 metadata 中提取 agent_level
    meta = t.get("metadata", {}) or {}
    agent_level = t.get("agent_level")
    if agent_level is None and meta.get("agent_level"):
        agent_level = meta.get("agent_level")
    if agent_level is not None and hasattr(agent_level, "value"):
        agent_level = agent_level.value

    return TaskResponse(
        id=t["id"],
        title=t["title"],
        description=t.get("description"),
        status=_map_status_for_api(raw_status),
        priority=t.get("priority", 5),
        parent_task_id=t.get("parent_task_id"),
        agent_id=t.get("agent_id"),
        agent_name=t.get("agent_name"),
        agent_level=agent_level,
        # 归属会话：顶层 thread_id 缺失时从 metadata.session_id 回退（手动创建
        # 任务只写 metadata.session_id；前端任务条目据此定位归属，不依赖 runs 快照）
        thread_id=t.get("thread_id") or meta.get("session_id"),
        created_by=t.get("created_by"),
        pipeline_run_id=t.get("pipeline_run_id"),
        execution_record_id=t.get("execution_record_id"),
        tags=t.get("tags", []),
        input_data=t.get("input_data", {}),
        result=t.get("result"),
        error=t.get("error"),
        created_at=t.get("created_at", ""),
        updated_at=t.get("updated_at", ""),
        metadata=meta,
    )


# ════════════════════════════════════════════════════════════
# 服务与能力访问（插件内部引用；测试可 monkeypatch）
# ════════════════════════════════════════════════════════════

_get_task_service = get_task_service


def _now_iso() -> str:
    """当前时间 ISO 串（子任务登记时间戳）。"""
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).isoformat()


def _capability(name: str) -> Any:
    """取内核能力句柄（走 __main__ 的 AgentOSPlugin 实例；未注入时抛 KeyError）。

    本插件由 ``python server.py`` 启动，SDK 注入 capabilities 的实例是
    ``__main__.plugin``；``import server`` 会重新执行 server.py 顶层并新建
    第二个空 AgentOSPlugin（capabilities 永远为空），故直接取 ``__main__``。
    测试可 monkeypatch 本函数（test_tasks_plugin 既有做法）。
    """
    try:
        import __main__  # noqa: PLC0415

        return __main__.plugin.get_capability(name)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise KeyError(f"capability not injected: {name}") from exc


async def _suspend_task_pipeline(task_id: str) -> bool:
    """GAP-1 统一：暂停/取消任务 = 挂起任务管道（suspend_pipeline）。

    run 终态 suspended → 内核回写 task.status=suspended。返回是否派发成功。
    """
    try:
        handle = _capability("pipeline-executor")
        resp = await handle.call("suspend_pipeline", {"pipeline_id": task_id})
        return bool(resp and resp.get("run_id") is not None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tasks http] suspend_pipeline 失败 | task_id=%s | err=%s", task_id, exc)
        return False


async def _resume_task_pipeline(task_id: str) -> bool:
    """GAP-1 统一：恢复任务 = resume_pipeline（按管道恢复最新 suspended run）。"""
    try:
        handle = _capability("pipeline-executor")
        resp = await handle.call("resume_pipeline", {"pipeline_id": task_id})
        return bool(resp and resp.get("run_id") is not None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tasks http] resume_pipeline 失败 | task_id=%s | err=%s", task_id, exc)
        return False


async def _cascade_suspend_children(parent_task_id: str) -> int:
    """GAP-1 统一：级联挂起子任务管道（state 聚合中 lineage 子管道逐个挂起）。"""
    cascaded = 0
    try:
        handle = _capability("pipeline-state")
        rows = await handle.call("list", {})
        if not isinstance(rows, list):
            return 0
        for row in rows:
            if str(row.get("lineage.parent_pipeline_id") or "") == parent_task_id:
                child_id = str(row.get("pipeline_id") or "")
                if child_id and await _suspend_task_pipeline(child_id):
                    cascaded += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tasks http] 级联挂起失败 | task_id=%s | err=%s", parent_task_id, exc)
    return cascaded


async def _submit_task_event(
    *,
    title: str,
    description: str = "",
    acceptance_criteria: dict | None = None,
    dependencies: list | None = None,
    parent_pipeline_id: str = "",
    user_id: str = "task_system",
    scope: str = "non_container",
    execution_context: dict | None = None,
    task_id: str = "",
    agent_id: str = "",
    thread_id: str = "",
) -> str:
    """GAP-1 统一：经 chat.send_message 驱动任务执行管道，返回 pipeline_id（= task.id）。

    双模式（与内核 chat_send_handler 契约对齐）：
    - 创建模式（task_id 空）：create 分支，引擎生成 pipeline_id；state 出生
      即带 task.* 字段、lineage 有父/根二选一、background 派发不阻塞请求。
      YAML 无写路径——返回的 id 即任务身份。
    - 注入模式（task_id 非空）：重跑已有任务（UI submit/resume = task_manage
      retry 映射），以 task_id 作 pipeline_id 走注入分支，background 立即返回。
    """
    try:
        chat = _capability("chat")

        if task_id:
            # ── 注入模式：重跑已有任务管道（retry/resume）──
            kickoff = f"重新执行任务「{title}」（任务 ID: {task_id}）。"
            if description:
                kickoff += f"\n任务描述：{description}"
            params: dict[str, Any] = {
                "pipeline_id": task_id,
                "message": kickoff,
                "user_id": user_id or "task_system",
                "background": True,
            }
            resp = await chat.call("send_message", params)
            got = str(resp.get("pipeline_id") or "") if isinstance(resp, dict) else ""
            if not got:
                logger.warning(
                    "_submit_task_event: 注入派发缺少 pipeline_id | task_id=%s | resp=%s",
                    task_id,
                    resp,
                )
                return ""
            logger.info(
                "_submit_task_event: 任务管道已重跑 | task_id=%s",
                task_id,
            )
            return task_id

        # ── 创建模式 ──
        if parent_pipeline_id:
            lineage: dict[str, Any] = {
                "parent_pipeline_id": parent_pipeline_id,
                # 归属会话优先（任务创建时的真实会话），无会话调用维持父管道引用
                "origin_session_id": thread_id or parent_pipeline_id,
            }
        else:
            lineage = {
                "root": True,
                "origin": {"kind": "channel", "source": "task_service"},
            }

        kickoff = f"执行任务「{title}」。"
        if description:
            kickoff += f"\n任务描述：{description}"

        params = {
            "create": True,
            "background": True,
            "message": kickoff,
            "user_id": user_id or "task_system",
            # 归属会话：手动创建任务透传用户会话，内核创建分支
            # 以真实会话 link pipeline_sessions（runs 快照/前端导航据此归属）；
            # 缺省（LLM 派发无会话上下文）不传，管道保持独立。
            **({"thread_id": thread_id} if thread_id else {}),
            # 目标执行 agent 传导（默认为主 agent）
            **({"agent_id": agent_id} if agent_id else {}),
            "state": {
                "task.goal": title,
                "task.status": "pending",
                "task.description": description or "",
                "task.acceptance_criteria": acceptance_criteria or {},
                "task.dependencies": dependencies or [],
                "task.scope": scope,
                "task.submitted_by": user_id or "",
            },
            "lineage": lineage,
        }
        if execution_context:
            params["execution_context"] = execution_context

        resp = await chat.call("send_message", params)
        pipeline_id = str(resp.get("pipeline_id") or "") if isinstance(resp, dict) else ""
        if not pipeline_id:
            logger.warning(
                "_submit_task_event: 创建派发缺少 pipeline_id | title=%s | resp=%s",
                title,
                resp,
            )
            return ""
        logger.info(
            "_submit_task_event: 任务执行管道已创建 | task_id=%s | title=%s",
            pipeline_id,
            title,
        )
        # 触发方登记（任务树数据链）：父管道提交的子任务，把新管道 id 记回
        # 父管道 state（task.owned.<id>.*，与 task_submit 同款契约）——前端
        # 任务树据此把子任务挂到主管道下；根任务（无父）无需登记。
        if parent_pipeline_id:
            try:
                await chat.call(
                    "send_message",
                    {
                        "pipeline_id": parent_pipeline_id,
                        "message": f"登记任务「{title}」（{pipeline_id}）。",
                        "user_id": user_id or "task_system",
                        "no_dispatch": True,
                        "state": {
                            f"task.owned.{pipeline_id}.title": title,
                            f"task.owned.{pipeline_id}.status": "pending",
                            f"task.owned.{pipeline_id}.scope": scope,
                            f"task.owned.{pipeline_id}.created_at": _now_iso(),
                            f"task.owned.{pipeline_id}.submitted_by": user_id or "",
                        },
                    },
                )
            except Exception as exc:
                logger.warning(
                    "[tasks http] 任务登记到提交者管道失败（不影响执行）| task=%s | err=%s",
                    pipeline_id,
                    exc,
                )
        return pipeline_id

    except Exception as exc:  # noqa: BLE001
        logger.warning("_submit_task_event: 派发失败 | title=%s | error=%s", title, exc)
        return ""


# ════════════════════════════════════════════════════════════
# tasks 域 handler（routes_tasks.py 21 端点原样搬入，去掉 memory_store 残留）
# ════════════════════════════════════════════════════════════


async def list_tasks(
    status: str | None = None,
    priority: int | None = None,
    session_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
    skip: int | None = None,
    _user: dict[str, Any] | None = None,
) -> TaskListResponse:
    """获取任务列表。

    支持按状态、优先级和会话 ID 筛选，分页返回。合并 api_store 和
    TaskStorage（YAML 文件）两个数据源。session_id 筛选基于
    task.metadata["session_id"] 字段匹配。同时支持 skip 和 offset 参数
    （skip 优先）。

    GAP-1 统一：任务即管道（state 单一真值）：0.2 提交的任务
    （task_submit / create_root_task 经 chat.send_message 创建）不再写 YAML，
    只存在于管道 state 聚合（pipeline-state.list 的 task.* 行）。本端点合并
    state 聚合行（组装逻辑对齐 task_manage 工具层 _list_tasks_from_state），
    否则任务管理面板（前端 /ext/task_service/tasks 拉取）看不到 0.2 任务，
    任务↔管道关系无从体现。state 行与 YAML 行按 pipeline_id 去重（state 优先）。
    """
    if skip is not None:
        offset = skip

    validate_pagination(limit, offset)

    task_service = _get_task_service()

    tasks: list[dict[str, Any]] = []

    if task_service is not None:
        try:
            ts_tasks = await task_service.list_all(limit=1000, session_id=session_id)

            for tm in ts_tasks:
                tasks.append(_task_model_to_dict(tm))

        except Exception as exc:
            logger.warning("从 TaskStorage 加载任务失败: %s", exc)

    # GAP-1 统一：合并 state 聚合行（0.2 任务真值所在；YAML 只读镜像无 0.2 任务）
    state_tasks = await _list_tasks_from_state()
    if state_tasks:
        seen_ids = {str(t.get("id") or "") for t in tasks}
        for st in state_tasks:
            if str(st.get("id") or "") in seen_ids:
                continue
            tasks.append(st)
            seen_ids.add(str(st.get("id") or ""))

    if status:
        tasks = [t for t in tasks if t.get("status") == status]

    if priority is not None:
        tasks = [t for t in tasks if t.get("priority") == priority]

    total = len(tasks)

    end = offset + limit

    page = tasks[offset:end]

    items = [_task_to_response(t) for t in page]

    return TaskListResponse(items=items, total=total)


async def _list_tasks_from_state() -> list[dict[str, Any]] | None:
    """从管道 state 聚合组装任务字典（GAP-1 统一：task = pipeline）。

    两类任务（语义区分）：
    - **task.owned.\<id\>.\***：提交者管道自持的任务（容器任务等"只登记不执行"，
      无下级管道——project id 只登记在提交者管道 state，本管道插件也能读它处理它）；
    - **task.\* 行**：执行管道收到的任务（task.id 引擎注入 + task.assigned 上级
      派发），普通任务（执行）创建新管道后在此。

    字段映射对齐 _task_to_response 的消费键（id/title/status/thread_id/
    pipeline_run_id/parent_task_id/metadata）。None = 桥未就绪/无任务行
    （调用方保持既有行为）。
    """
    try:
        handle = _capability("pipeline-state")
        resp = await handle.call("list", {})
    except Exception as exc:  # noqa: BLE001 — 读面降级不崩
        logger.warning("[tasks http] state 聚合读取失败（任务列表降级为 YAML 面）| err=%s", exc)
        return None
    # 响应形状：内核 capability_router 的 pipeline-state.list 返回裸数组
    # （workspace/task 工具层同此解析）。
    rows = resp if isinstance(resp, list) else None
    if not isinstance(rows, list):
        return None
    out: list[dict[str, Any]] = []
    # 第一趟：普通任务行（task.* 键）——这些任务有自己的执行管道 state，
    # 归属（lineage 父）与状态以本管道行为准；收集 pid 供 owned 行去重。
    state_pids: set[str] = set()
    task_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not any(
            str(k).startswith("task.") and not str(k).startswith("task.owned.")
            for k in row.keys()
        ):
            continue
        pid = str(row.get("pipeline_id") or "")
        if not pid:
            continue
        state_pids.add(pid)
        task_rows.append(
            {
                "id": pid,
                "title": str(row.get("task.goal") or pid),
                "status": str(row.get("task.status") or "pending"),
                "thread_id": _session_anchor(row),
                "pipeline_run_id": pid,
                "parent_task_id": str(row.get("lineage.parent_pipeline_id") or "") or None,
                "metadata": {
                    "session_id": _session_anchor(row),
                    "task_scope": str(row.get("task.scope") or "non_container"),
                    "submitted_by": str(row.get("task.submitted_by") or ""),
                    # 父是容器任务（task.owned 声明）时：容器 project id（前端
                    # 任务树据此把子任务挂到容器节点下）
                    "parent_project_id": str(row.get("task.parent_project_id") or "") or None,
                    # 工作空间坐标（任务面板"打开工作空间"按钮数据源；
                    # workspace_lifecycle init 写入 + persistent_fields 落表）
                    "ws_meta": row.get("ws_meta") or None,
                    "workspace": str(row.get("workspace") or "") or None,
                },
                "created_at": str(row.get("task.created_at") or ""),
            }
        )
    out.extend(task_rows)
    # 第二趟：容器任务/登记声明（task.owned.<id>.* 键）——只在无独立 state 行时
    # 出口（有 state 行的任务以 state 行为准：归属/状态更真；双行同 id 会让
    # 前端 taskById 覆盖 + 面板重复节点）。
    for row in rows:
        if not isinstance(row, dict):
            continue
        owned = _collect_owned_tasks(row)
        for pid, fields in owned.items():
            if pid in state_pids:
                continue
            out.append(
                {
                    "id": pid,
                    "title": str(fields.get("title") or pid),
                    "status": str(fields.get("status") or "active"),
                    "thread_id": _session_anchor(row),
                    "pipeline_run_id": str(row.get("pipeline_id") or ""),
                    "parent_task_id": None,
                    "metadata": {
                        "session_id": _session_anchor(row),
                        # 登记键带真实 scope（task_submit 写入）；缺省视为容器
                        # （task.owned 通道的设计场景是容器任务登记）
                        "task_scope": str(fields.get("scope") or "container"),
                        "submitted_by": str(fields.get("submitted_by") or ""),
                        "workspace": str(fields.get("workspace") or ""),
                    },
                    "created_at": str(fields.get("created_at") or ""),
                }
            )
    return out


def _session_anchor(row: dict[str, Any]) -> str | None:
    """会话锚点：任务管道无 sessions 行，thread_id 恒等于自身 pipeline_id；
    出生侧 lineage.origin_session_id 修正后为真 thread id（对齐 task_manage
    工具层 _get_task_from_state 的取舍）。"""
    pid = str(row.get("pipeline_id") or "")
    origin_sess = str(row.get("lineage.origin_session_id") or "")
    row_thread = str(row.get("thread_id") or "")
    anchor = (
        origin_sess
        if origin_sess and origin_sess != pid
        else (row_thread if row_thread and row_thread != pid else origin_sess or row_thread)
    )
    return anchor or None


def _collect_owned_tasks(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从聚合行收集 task.owned.<id>.<field> 扁平键 → {id: {field: value}}。

    容器任务等"只登记不执行"的声明（提交者管道自持），键形态：
    task.owned.<project_id>.title / .status / .scope / .created_at / .workspace。
    """
    out: dict[str, dict[str, Any]] = {}
    prefix = "task.owned."
    for k, v in row.items():
        if not (isinstance(k, str) and k.startswith(prefix)):
            continue
        rest = k[len(prefix):]
        parts = rest.split(".", 1)
        if len(parts) != 2:
            continue
        tid, field = parts
        if not tid or not field:
            continue
        out.setdefault(tid, {})[field] = v
    return out


async def get_tasks_debug(
    skip: int = 0,
    limit: int = 100,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    status: str | None = None,
    session_id: str | None = None,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取任务调试数据（全字段）。"""

    task_service = _get_task_service()

    if task_service is None:
        return {"items": [], "total": 0}

    try:
        all_tasks = await task_service.list_all(limit=limit, reverse=(sort_order == "desc"))

        if status:
            all_tasks = [t for t in all_tasks if t.status.value == status]

        if session_id:
            all_tasks = [t for t in all_tasks if t.metadata.get("session_id") == session_id]

        items = [_task_model_to_dict(t) for t in all_tasks]

        return {"items": items, "total": len(items)}

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "获取任务调试数据失败，返回空 | limit=%s status=%s session_id=%s err=%s",
            limit,
            status,
            session_id,
            exc,
            exc_info=True,
        )
        return {"items": [], "total": 0}


async def create_task(
    body: TaskCreate | dict[str, Any],
    _user: dict[str, Any] | None = None,
) -> TaskResponse:
    """创建新任务。

    [改造] memory_store 无依赖；GAP-1 统一经 chat.send_message 创建执行管道
    （引擎生成 pipeline_id = task_id），YAML 无写路径。
    """
    # SDK HTTP 端点把 body 作 dict 透传（不实例化 Pydantic 模型）——兼容两种形状
    if isinstance(body, dict):
        body = TaskCreate(**body)

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法创建任务",
        )

    # P0-安全: 执行者必须显式指定，禁止创建没有 target_id 的任务。
    if not body.agent_id:
        raise APIError(
            status_code=400,
            error_code="MISSING_TARGET_AGENT",
            message="创建任务必须指定执行 Agent（agent_id），禁止静默降级到默认 Agent",
        )

    # GAP-1 统一（state 单一真值）：任务即管道——直接经 chat.send_message 创建
    # 执行管道（引擎生成 pipeline_id = task_id），YAML 无写路径。
    task_id = await _submit_task_event(
        title=body.title,
        description=body.description or "",
        user_id=_current_user(_user).get("sub", ""),
    )
    if not task_id:
        raise APIError(
            status_code=500,
            error_code="TASK_CREATE_FAILED",
            message="任务执行管道创建失败",
        )

    logger.info("用户 %s 创建任务: %s", _current_user(_user).get("username", "system"), task_id)

    return _task_to_response({"id": task_id, "title": body.title, "status": "pending"})


async def create_root_task(
    body: TaskRootCreate | dict[str, Any],
    _user: dict[str, Any] | None = None,
) -> TaskResponse:
    """用户手动创建根任务。

    等价于 L1 主 agent 调 task_submit 提交根任务，为 L2+ 子 agent 提供合法的
    任务上下文。container / non_container 都走现有下游逻辑。

    [改造] 已删除 ``store.get_session(thread_id)`` 旧 0.1 主管道读取块——
    active_pipeline_id 恒为空串（根任务出生即 root，lineage 由引擎落 root）。
    """
    # SDK HTTP 端点把 body 作 dict 透传——兼容两种形状
    if isinstance(body, dict):
        body = TaskRootCreate(**body)

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法创建任务",
        )

    # ── 校验 ──
    if body.task_scope not in ("container", "non_container"):
        raise APIError(
            status_code=400,
            error_code="INVALID_TASK_SCOPE",
            message=f"task_scope 必须为 container 或 non_container，收到: {body.task_scope}",
        )

    # 非容器必须有执行 agent；容器是工作空间集合，无执行 target
    if body.task_scope != "container" and not body.target_id:
        raise APIError(
            status_code=400,
            error_code="MISSING_TARGET_AGENT",
            message="非容器根任务必须指定执行 Agent（target_id），容器任务除外",
        )

    # workspace 路径安全校验（复用 task_submit 同款）
    if body.workspace:
        import importlib.util  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        _ws_spec = importlib.util.spec_from_file_location(
            "task_submit_tool_ws_check",
            str(
                Path(__file__).resolve().parent.parent.parent
                / "tools" / "task_submit" / "tool.py"
            ),
        )
        assert _ws_spec is not None and _ws_spec.loader is not None
        _ws_mod = importlib.util.module_from_spec(_ws_spec)
        sys.modules["task_submit_tool_ws_check"] = _ws_mod
        _ws_spec.loader.exec_module(_ws_mod)

        ws_error = _ws_mod._validate_workspace_path(body.workspace)

        if ws_error:
            raise APIError(
                status_code=400,
                error_code="UNSAFE_WORKSPACE",
                message=ws_error,
            )

    # ── 父容器校验（挂子任务时）：父任务必须存在且为 container ──
    parent_task_id = body.parent_task_id or None

    if parent_task_id:
        _parent = task_service.get_task(parent_task_id)

        if _parent is None:
            raise APIError(
                status_code=400,
                error_code="PARENT_TASK_NOT_FOUND",
                message=f"父任务不存在: {parent_task_id}",
            )

        _parent_scope = (_parent.metadata or {}).get("task_scope", "non_container")

        if _parent_scope != "container":
            raise APIError(
                status_code=400,
                error_code="PARENT_NOT_CONTAINER",
                message=f"父任务必须是容器（container）任务，当前 scope: {_parent_scope}",
            )

    # ── 复用当前会话 ──
    thread_id = body.thread_id
    active_pipeline_id = ""

    # ── 构造 metadata（字段集对齐 task_submit._build_metadata） ──
    metadata: dict[str, Any] = {
        "task_scope": body.task_scope,
        "target_id": body.target_id,
        "session_id": thread_id,
        "submitted_by_level": 1,  # 用户层 = L1
        "acceptance_criteria": {},  # 默认空，_build_full_task_input 会自动跳过评估段
        "workspace": body.workspace,
        "isolation_level": body.isolation_level,
        "user_id": _current_user(_user).get("sub", ""),
        "inherit": body.inherit or {},
        "source": "user_manual",  # 审计标记：区分用户直接发起
    }

    # GAP-1 统一（state 单一真值）：任务即管道——直接经 chat.send_message 创建
    # 执行管道（引擎生成 pipeline_id = task_id），YAML 无写路径。
    # execution_context：组装工作区拓扑/隔离声明（对齐 task_submit._build_execution_context）。
    _ws_mode = body.workspace or "worktree"
    if _ws_mode not in ("worktree", "plain"):
        _ws_mode = "worktree"
    execution_context = {
        "isolation": {"level": body.isolation_level or "non_isolated"},
        "workspace": {
            "mode": _ws_mode,
            "source_path": "",
            "explicit": bool(body.workspace),
        },
    }
    task_id = await _submit_task_event(
        title=body.title,
        description=body.description or "",
        acceptance_criteria=metadata.get("acceptance_criteria") or {},
        dependencies=list(metadata.get("dependencies") or []),
        parent_pipeline_id=active_pipeline_id or "",
        user_id=_current_user(_user).get("sub", ""),
        scope=body.task_scope,
        execution_context=execution_context,
        agent_id=body.target_id,
        thread_id=thread_id,
    )
    if not task_id:
        raise APIError(
            status_code=500,
            error_code="TASK_CREATE_FAILED",
            message="根任务执行管道创建失败",
        )

    logger.info(
        "[create_root_task] 用户 %s 手动创建根任务 | task_id=%s | scope=%s | thread=%s",
        _current_user(_user).get("username", "system"),
        task_id,
        body.task_scope,
        thread_id,
    )

    return _task_to_response({"id": task_id, "title": body.title, "status": "pending"})


async def list_container_tasks(
    session_id: str = "",
    _user: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """返回当前会话下所有 task_scope=container 的任务，供前端下拉选父容器。

    容器任务 = 提交者管道自持的声明（task.owned.*），
    从 state 聚合组装；YAML 私有存储已退役（0.2 容器任务不写 YAML）。
    """

    containers: list[dict[str, Any]] = []

    state_tasks = await _list_tasks_from_state()
    if state_tasks:
        for t in state_tasks:
            if t.get("metadata", {}).get("task_scope") != "container":
                continue
            if session_id and t.get("thread_id") != session_id:
                continue
            containers.append({"id": t["id"], "title": t["title"]})
        return containers

    # 降级：state 聚合不可用 → YAML 面（0.1 遗留容器任务）
    task_service = _get_task_service()

    if task_service is None:
        return []

    try:
        tasks = await task_service.list_all(limit=1000, session_id=session_id or None)

        for tm in tasks:
            _meta = tm.metadata or {}

            if _meta.get("task_scope") == "container":
                containers.append({"id": tm.id, "title": tm.title})

    except Exception as exc:
        logger.warning("[list_container_tasks] 加载失败 | session=%s | error=%s", session_id, exc)

    return containers


def get_task(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> TaskResponse:
    """获取指定任务的详情。"""

    task = None

    task_service = _get_task_service()

    if task_service is not None:
        tm = task_service.get_task(task_id)

        if tm is not None:
            # 跨用户资源隔离：仅允许任务创建者访问自己的任务
            task_user_id = (tm.metadata or {}).get("user_id")
            if task_user_id is not None and task_user_id != _current_user(_user).get("sub"):
                raise APIError(
                    status_code=404,
                    error_code="API_NOTF_2004",
                    message="任务不存在或已被删除",
                )

            task = _task_model_to_dict(tm)

    if task is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="任务不存在或已被删除",
        )

    return _task_to_response(task)


async def update_task(
    task_id: str,
    body: TaskUpdate | dict[str, Any],
    _user: dict[str, Any] | None = None,
) -> TaskResponse:
    """更新指定任务的字段。

    GAP-1 统一：任务字段真值在 state（出生即定，不可经 UI 改写）——
    update 语义退役，端点只读返回当前 state 行（status 的变更由 run 终态 /
    挂起恢复表达；描述/优先级在提交时已定型）。
    """
    if isinstance(body, dict):
        body = TaskUpdate(**body)

    # 读 state 聚合行（task = pipeline）
    try:
        handle = _capability("pipeline-state")
        rows = await handle.call("list", {})
    except Exception:
        rows = None

    if not isinstance(rows, list):
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="state 聚合不可用，无法读取任务",
        )

    row = next(
        (r for r in rows if str(r.get("pipeline_id") or "") == task_id),
        None,
    )

    if row is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="任务不存在或已被删除",
        )

    return _task_to_response({
        "id": task_id,
        "title": str(row.get("task.goal") or task_id),
        "status": str(row.get("task.status") or "pending"),
    })


async def delete_task(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, str]:
    """删除指定任务，根据任务类型执行不同策略：取消运行中的管道，并区分
    容器子任务与根任务。

    - 容器任务: 软删除（标记取消，保留数据）
    - 非容器任务(容器的子任务): 取消自己及下级管道 + 删除数据（不清理工作空间）
    - 非容器任务(根任务): 取消管道 + 清理工作空间 + 删除数据

    GAP-1 统一：0.2 任务 = 管道（state 单一真值，无 YAML
    记录）——YAML 存储查不到时回退 state 聚合判定存在性，删除 = 调内核
    pipeline-executor.delete_pipeline 清管道全部执行数据（runs/traces/
    messages/state/checkpoints + registry 条目）。
    """

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法删除任务",
        )

    # ① YAML 面（旧任务/容器任务）：task_service.delete_task 级联清理
    deleted = await task_service.delete_task(task_id)

    if not deleted:
        # ② state 面（0.2 任务）：state 聚合存在性判定 + 内核删管道数据
        try:
            handle = _capability("pipeline-state")
            rows = await handle.call("list", {})
        except Exception:
            rows = None
        row = None
        if isinstance(rows, list):
            row = next(
                (r for r in rows if str(r.get("pipeline_id") or "") == task_id),
                None,
            )
        if row is None:
            raise APIError(
                status_code=404,
                error_code="API_NOTF_2004",
                message="任务不存在或已被删除",
            )
        try:
            exec_handle = _capability("pipeline-executor")
            await exec_handle.call("delete_pipeline", {"pipeline_id": task_id})
        except Exception as exc:  # noqa: BLE001 — 删除失败透传
            logger.warning(
                "[tasks http] delete_pipeline 失败 | task_id=%s | err=%s",
                task_id,
                exc,
            )
            raise APIError(
                status_code=500,
                error_code="TASK_DELETE_FAILED",
                message=f"任务管道删除失败: {exc}",
            ) from exc
        logger.info(
            "用户 %s 删除 0.2 任务（state 面）: %s",
            _current_user(_user).get("username", "system"),
            task_id,
        )
        return {"message": "任务已删除"}

    task = task_service.get_task(task_id)

    if task is not None and task.metadata.get("soft_deleted"):
        logger.info(
            "用户 %s 软删除容器任务 %s",
            _current_user(_user).get("username", "system"),
            task_id,
        )

        return {"message": "容器任务已标记删除"}

    logger.info(
        "用户 %s 删除任务 %s",
        _current_user(_user).get("username", "system"),
        task_id,
    )

    return {"message": "任务已删除"}


async def submit_task(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> TaskSubmitResponse:
    """提交任务进入执行队列（pending/failed → 注入模式重跑任务管道）。"""

    task_service = _get_task_service()

    task = None

    if task_service is not None:
        tm = task_service.get_task(task_id)

        if tm is not None:
            task = _task_model_to_dict(tm)

    if task is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="任务不存在或已被删除",
        )

    current_status = task.get("status", "pending")

    backend_status = current_status

    allowed_statuses = {"pending", "failed"}

    if backend_status not in allowed_statuses:
        raise APIError(
            status_code=400,
            error_code="API_VAL_2003",
            message=f"当前状态 '{current_status}' 不允许提交，仅允许: {', '.join(allowed_statuses)}",
        )

    # GAP-1 统一：重跑已有任务 = 注入模式（task_id 即 pipeline_id，retry 映射）
    submitted = await _submit_task_event(
        title=str(task.get("title") or task_id),
        task_id=task_id,
        user_id=_current_user(_user).get("sub", ""),
    )

    logger.info("用户 %s 提交任务 %s 执行", _current_user(_user).get("username", "system"), task_id)

    return TaskSubmitResponse(
        task_id=task_id,
        status="queued" if not submitted else "pending",
        message="任务已提交到执行队列",
    )


async def pause_task(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """暂停指定任务（GAP-1：挂起任务管道，run 终态回写 task.status）。"""

    # GAP-1 统一：暂停 = 挂起任务管道（suspend_pipeline）
    if not await _suspend_task_pipeline(task_id):
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message=f"任务不存在或无运行中 run: {task_id}",
        )

    logger.info(
        "用户 %s 暂停任务 %s（suspend_pipeline）",
        _current_user(_user).get("username", "system"),
        task_id,
    )

    return {
        "success": True,
        "task_id": task_id,
        "paused_count": 1,
        "pipeline_cancelled": True,
        "message": "任务已暂停（管道已挂起）",
    }


async def resume_task(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """恢复指定暂停的任务（GAP-1：resume_pipeline 按管道恢复最新 suspended run）。"""

    # GAP-1 统一：恢复 = resume_pipeline；task_submitted 恒 False（旧字段仅保留响应形状）
    task_submitted = False
    if not await _resume_task_pipeline(task_id):
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message=f"任务不存在或无挂起 run: {task_id}",
        )

    logger.info(
        "用户 %s 恢复任务 %s (task_submitted=%s)",
        _current_user(_user).get("username", "system"),
        task_id,
        task_submitted,
    )

    return {
        "success": True,
        "task_id": task_id,
        "resumed_count": 1,
        "task_submitted": task_submitted,
        "message": "任务已恢复" + ("，已重新提交执行" if task_submitted else ""),
    }


async def cancel_task(
    task_id: str,
    body: dict[str, Any] | None = None,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """取消指定任务（挂起管道 + lineage 级联）。"""

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法取消任务",
        )

    # GAP-1 统一：取消 = 挂起任务管道 + lineage 级联
    pipeline_cancelled = await _suspend_task_pipeline(task_id)

    cascaded = await _cascade_suspend_children(task_id)

    logger.info(
        "用户 %s 取消任务 %s (pipeline_cancelled=%s, cascaded=%d)",
        _current_user(_user).get("username", "system"),
        task_id,
        pipeline_cancelled,
        cascaded,
    )

    updated_task = task_service.get_task(task_id)

    if updated_task is not None:
        from dataclasses import asdict as _asdict  # noqa: PLC0415

        task_dict = _asdict(updated_task)

        raw_status = safe_enum_value(updated_task.status)

        task_dict["status"] = _map_status_for_api(raw_status)

        if hasattr(updated_task, "priority") and hasattr(updated_task.priority, "value"):
            task_dict["priority"] = updated_task.priority.value

        if hasattr(updated_task, "agent_level") and hasattr(updated_task.agent_level, "value"):
            task_dict["agent_level"] = updated_task.agent_level.value

        return _pydantic_to_dict(_task_to_response(task_dict))

    return {
        "success": True,
        "task_id": task_id,
        "cancelled": True,
        "message": "任务已取消",
        "cascaded_subtasks": cascaded,
    }


# ════════════════════════════════════════════════════════════
# task_phase / ac handler（routes_missing.py task_phase_router 原样搬入）
# ════════════════════════════════════════════════════════════

_STATUS_TO_PHASE: dict[str, tuple[str, str]] = {
    "pending": ("prepare", "pending"),
    "scheduled": ("prepare", "pending"),
    "suspended": ("prepare", "pending"),
    "running": ("execute", "running"),
    "blocked": ("execute", "running"),
    "evaluating": ("evaluate", "running"),
    "completed": ("evaluate", "completed"),
    "failed": ("execute", "failed"),
    "cancelled": ("prepare", "failed"),
    "timeout": ("execute", "failed"),
}


async def get_task_phase(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取任务当前执行阶段（状态 → 前端阶段概念映射）。"""

    task_service = _get_task_service()
    if task_service:
        try:
            task = task_service.get_task(task_id)
            if task:
                status_str = safe_enum_value(task.status)
                phase, phase_status = _STATUS_TO_PHASE.get(status_str, ("prepare", "pending"))
                return {
                    "taskId": task_id,
                    "currentPhase": phase,
                    "phaseStatus": phase_status,
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "解析任务阶段失败，回退默认 prepare/pending | task_id=%s err=%s",
                task_id,
                exc,
                exc_info=True,
            )

    return {
        "taskId": task_id,
        "currentPhase": "prepare",
        "phaseStatus": "pending",
    }


async def complete_prepare_phase(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """标记准备阶段完成。"""

    return {"task_id": task_id, "current_phase": "execute"}


async def complete_execute_phase(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """标记执行阶段完成。"""

    return {"task_id": task_id, "current_phase": "review"}


async def get_phase_output(
    task_id: str,
    phase: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取指定阶段的输出结果（占位：0.2 无阶段产物存储面）。"""

    return {"output": None, "error": None}


async def get_task_ac(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取任务的验收标准列表（占位：AC 真值在 state task.acceptance_criteria）。"""

    return {"taskId": task_id, "acceptanceCriteria": []}


async def evaluate_ac(
    task_id: str,
    ac_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """评估单个验收标准（占位：未评估）。"""

    return {
        "acceptance_criterion": {
            "id": ac_id,
            "task_id": task_id,
            "status": "not_evaluated",
            "passed": None,
        },
    }


async def evaluate_all_ac(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """评估任务的所有验收标准（占位）。"""

    return {"taskId": task_id, "acceptanceCriteria": []}


async def get_ac_result(
    task_id: str,
    ac_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取验收标准的评估结果（占位：未评估）。"""

    return {
        "acceptance_criterion": {
            "id": ac_id,
            "task_id": task_id,
            "status": "not_evaluated",
            "passed": None,
        },
    }


# ════════════════════════════════════════════════════════════
# projects 域 handler（projects = 容器任务）
# ════════════════════════════════════════════════════════════
#
# 语义：项目容器就是任务系统的容器任务。
# - 创建 project = 创建容器任务（task_scope=container，含 workspace 关联元数据）
# - list/get/pause/resume/auto-execute/delete = 容器任务生命周期操作
# - project_id 即 container_task_id（task.id），workspace 插件按此索引工作空间

_PROJECT_STATUS_MAP: dict[str, str] = {
    "pending": "planning",
    "running": "running",
    "evaluating": "running",
    "stopped": "suspended",
    "completed": "completed",
    "failed": "failed",
    "timeout": "failed",
}


def _is_container_task(task: Any) -> bool:
    """是否为项目容器任务（task_scope=container 且未被软删除）。"""
    meta = task.metadata or {}
    return meta.get("task_scope") == "container" and not meta.get("soft_deleted")


def _task_to_project(task: Any, tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """容器任务 TaskModel → 前端 Project 形状（frontend/src/types/task.ts 对齐）。"""

    meta = task.metadata or {}
    return {
        "id": task.id,
        "userId": str(meta.get("user_id", "") or ""),
        "sessionId": meta.get("session_id") or None,
        "goal": task.title or "",
        "status": _PROJECT_STATUS_MAP.get(str(safe_enum_value(task.status)), "planning"),
        "autoExecute": bool(meta.get("auto_execute", False)),
        "currentTaskIndex": int(meta.get("current_task_index", 0) or 0),
        "tasks": tasks if tasks is not None else (meta.get("tasks", []) or []),
        "timestamps": {
            "createdAt": task.created_at or "",
            "updatedAt": task.updated_at or "",
        },
        "metadata": meta,
    }


def _get_project_or_404(task_service: Any, project_id: str) -> Any:
    """取项目容器任务，不存在/非容器/已软删除 → 404。"""
    task = task_service.get_task(project_id)
    if task is None or not _is_container_task(task):
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="项目不存在或已被删除",
        )
    return task


async def list_projects(
    limit: int = 20,
    offset: int = 0,
    page: int | None = None,
    status: str | None = None,
    session_id: str | None = None,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取项目列表（= 容器任务列表，task_scope=container）。"""

    task_service = _get_task_service()

    if task_service is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    # page 兼容（前端传 page+limit）；offset 显式给定时优先
    if offset == 0 and page is not None and page > 1:
        offset = (page - 1) * limit

    try:
        tasks = await task_service.list_all(limit=1000, session_id=session_id or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[projects] list_all 失败，返回空 | error=%s", exc)
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    projects = [_task_to_project(t) for t in tasks if _is_container_task(t)]

    if status:
        projects = [p for p in projects if p.get("status") == status]

    total = len(projects)
    page_items = projects[offset:offset + limit]

    return {"items": page_items, "total": total, "limit": limit, "offset": offset}


async def create_project(
    body: dict[str, Any] | None = None,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """创建项目 = 创建容器任务（task_scope=container）+ workspace 关联。

    副作用可观测：响应 project.metadata 携带 ws_meta（workspace 关联声明），
    日志落项目/容器任务 ID、goal、session、用户与 workspace 模式。
    workspace 实体由 workspace 插件按 container_task_id 惰性物化
    （GET /workspaces/{container_task_id} 不存在则自动创建）。
    """

    body = dict(body or {})
    goal = str(body.get("goal") or body.get("title") or "").strip()
    if not goal:
        raise APIError(
            status_code=400,
            error_code="MISSING_GOAL",
            message="创建项目必须指定 goal",
        )

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法创建项目",
        )

    session_id = str(body.get("session_id") or "")
    auto_execute = bool(body.get("auto_execute", False))
    user_id = str(_current_user(_user).get("sub", ""))

    # workspace 关联声明（workspace 插件按 container_task_id 索引惰性物化）
    ws_mode = str(body.get("workspace_mode") or "worktree")
    if ws_mode not in ("worktree", "plain"):
        ws_mode = "worktree"
    explicit_ws = str(body.get("workspace") or "")

    extra_meta = body.get("metadata")
    metadata: dict[str, Any] = {
        "task_scope": "container",
        "session_id": session_id,
        "user_id": user_id,
        "auto_execute": auto_execute,
        "source": "project",
        "acceptance_criteria": {},
        "ws_meta": {
            "mode": ws_mode,
            "path": explicit_ws,
            "explicit": bool(explicit_ws),
        },
    }
    if isinstance(extra_meta, dict):
        for k, v in extra_meta.items():
            if k not in metadata and isinstance(k, str):
                metadata[k] = v

    task = await task_service.create_task(
        title=goal,
        description=str(body.get("description") or ""),
        metadata=metadata,
    )

    logger.info(
        "[projects] 创建项目 = 容器任务 | project_id(container_task_id)=%s | goal=%s | "
        "session=%s | user=%s | ws_mode=%s | auto_execute=%s",
        task.id,
        goal,
        session_id or "-",
        user_id or "-",
        ws_mode,
        auto_execute,
    )

    return {"project": _task_to_project(task)}


async def get_project(
    project_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取项目详情（含其下子任务摘要列表）。"""

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法读取项目",
        )

    task = _get_project_or_404(task_service, project_id)

    subtasks = task_service.list_subtasks(project_id)
    tasks_summary = [
        {
            "id": st.id,
            "title": st.title,
            "status": safe_enum_value(st.status),
        }
        for st in subtasks
    ]

    return {"project": _task_to_project(task, tasks=tasks_summary)}


async def toggle_auto_execute(
    project_id: str,
    body: dict[str, Any] | None = None,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """切换项目自动执行开关（持久化 metadata.auto_execute）。

    enabled 缺省时翻转现值；前端显式传 enabled。
    """

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法切换自动执行",
        )

    task = _get_project_or_404(task_service, project_id)

    current = bool((task.metadata or {}).get("auto_execute", False))
    if body and "enabled" in body:
        enabled = bool(body.get("enabled"))
    else:
        enabled = not current

    meta = dict(task.metadata or {})
    meta["auto_execute"] = enabled
    updated = task_service.update_task_fields_sync(project_id, metadata=meta)

    logger.info(
        "[projects] 切换自动执行 | project_id=%s | %s → %s",
        project_id,
        current,
        enabled,
    )

    return {"project": _task_to_project(updated if updated is not None else task)}


async def pause_project(
    project_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """暂停项目 = 容器任务生命周期暂停（尽力挂起任务管道 + 落服务状态机 STOPPED）。"""

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法暂停项目",
        )

    task = _get_project_or_404(task_service, project_id)

    # GAP-1：尽力挂起任务管道（容器可能无 run，失败不阻断状态机落盘）
    pipeline_suspended = await _suspend_task_pipeline(project_id)

    current = str(safe_enum_value(task.status))
    if current != "stopped":
        try:
            await task_service.pause_task(project_id)
        except Exception as exc:  # noqa: BLE001 — 状态机拒绝（如已终态）不阻断响应
            logger.warning(
                "[projects] pause_task 状态机拒绝 | project_id=%s | current=%s | err=%s",
                project_id,
                current,
                exc,
            )

    refreshed = task_service.get_task(project_id) or task

    logger.info(
        "[projects] 项目已暂停 | project_id=%s | pipeline_suspended=%s | status=%s",
        project_id,
        pipeline_suspended,
        safe_enum_value(refreshed.status),
    )

    return {"project": _task_to_project(refreshed)}


async def resume_project(
    project_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """恢复项目 = 容器任务生命周期恢复（服务状态机 running + 尽力恢复任务管道）。"""

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法恢复项目",
        )

    task = _get_project_or_404(task_service, project_id)

    current = str(safe_enum_value(task.status))
    if current == "stopped":
        try:
            await task_service.resume_task(project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[projects] resume_task 状态机拒绝 | project_id=%s | err=%s",
                project_id,
                exc,
            )

    pipeline_resumed = await _resume_task_pipeline(project_id)

    refreshed = task_service.get_task(project_id) or task

    logger.info(
        "[projects] 项目已恢复 | project_id=%s | pipeline_resumed=%s | status=%s",
        project_id,
        pipeline_resumed,
        safe_enum_value(refreshed.status),
    )

    return {"project": _task_to_project(refreshed)}


async def delete_project(
    project_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """删除项目 = 容器任务软删除（保留数据，服务级级联清理子任务）。"""

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法删除项目",
        )

    deleted = await task_service.delete_task(project_id)

    if not deleted:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="项目不存在或已被删除",
        )

    logger.info(
        "[projects] 删除项目（容器任务）| project_id=%s | user=%s",
        project_id,
        _current_user(_user).get("username", "system"),
    )

    return {"message": "项目已删除", "id": project_id}


# ════════════════════════════════════════════════════════════
# http.handle 分发（server.py http.handle 工具委托入口）
# ════════════════════════════════════════════════════════════

_PREFIX = "/ext/task_service"


def _qint(query: dict[str, str], key: str, default: int | None) -> int | None:
    if key not in query:
        return default
    try:
        return int(query[key])
    except (TypeError, ValueError):
        return default


def _qopt(query: dict[str, str], key: str) -> str | None:
    return query.get(key)


async def _dispatch_task_phase(task_id: str, parts: list[str], method: str) -> dict[str, Any]:
    """分发 /tasks/{id}/phase/** 子路由。"""
    if not parts and method == "GET":
        return await get_task_phase(task_id)
    if len(parts) == 2 and parts[0] == "prepare" and parts[1] == "complete" and method == "POST":
        return await complete_prepare_phase(task_id)
    if len(parts) == 2 and parts[0] == "execute" and parts[1] == "complete" and method == "POST":
        return await complete_execute_phase(task_id)
    if len(parts) == 2 and parts[1] == "output" and method == "GET":
        return await get_phase_output(task_id, parts[0])
    raise APIError(404, "API_NOTF_2004", f"task phase route not found: /{'/'.join(parts)}")


async def _dispatch_task_ac(task_id: str, parts: list[str], method: str) -> dict[str, Any]:
    """分发 /tasks/{id}/ac/** 子路由。"""
    if not parts and method == "GET":
        return await get_task_ac(task_id)
    if len(parts) == 1 and parts[0] == "evaluate-all" and method == "POST":
        return await evaluate_all_ac(task_id)
    if len(parts) == 2 and parts[1] == "evaluate" and method == "POST":
        return await evaluate_ac(task_id, parts[0])
    if len(parts) == 2 and parts[1] == "result" and method == "GET":
        return await get_ac_result(task_id, parts[0])
    raise APIError(404, "API_NOTF_2004", f"task ac route not found: /{'/'.join(parts)}")


async def handle_http(
    path: str,
    method: str,
    raw_body: str,
    query: dict[str, str] | None,
    headers: dict[str, str] | None,
) -> dict[str, Any]:
    """http.handle 按 path 分发（tasks 域 21 端点 + projects 域 7 端点）。

    返回 ToolExecutionResult{success, data}；业务异常转带 status 的响应。
    """
    q = query or {}
    caller = _resolve_caller(headers)

    try:
        # ── projects 域（/ext/task_service/projects...）──
        if path.startswith(f"{_PREFIX}/projects"):
            sub = path[len(f"{_PREFIX}/projects"):]
            if sub in ("", "/") and method == "GET":
                limit = _qint(q, "limit", 20)
                return _ok(_json_response(await list_projects(
                    limit=limit if limit is not None else 20,
                    offset=_qint(q, "offset", 0) or 0,
                    page=_qint(q, "page", None),
                    status=_qopt(q, "status"),
                    session_id=_qopt(q, "session_id"),
                    _user=caller,
                )))
            if sub in ("", "/") and method == "POST":
                body = _decode_body(raw_body)
                return _ok(_json_response(await create_project(body, caller)))
            if sub.startswith("/") and "/" not in sub[1:]:
                pid = sub[1:]
                if method == "GET":
                    return _ok(_json_response(await get_project(pid, caller)))
                if method == "DELETE":
                    return _ok(_json_response(await delete_project(pid, caller)))
            elif sub.startswith("/") and "/" in sub[1:]:
                pid, action = sub[1:].split("/", 1)
                if action == "auto-execute" and method == "POST":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await toggle_auto_execute(pid, body, caller)))
                if action == "pause" and method == "POST":
                    return _ok(_json_response(await pause_project(pid, caller)))
                if action == "resume" and method == "POST":
                    return _ok(_json_response(await resume_project(pid, caller)))

        # ── tasks 域（/ext/task_service/tasks...）──
        if path.startswith(f"{_PREFIX}/tasks"):
            sub = path[len(f"{_PREFIX}/tasks"):]
            # 顶层路由
            if sub in ("", "/") and method == "GET":
                limit = _qint(q, "limit", 20)
                skip = _qint(q, "skip", None) if "skip" in q else None
                return _ok(_json_response(_pydantic_to_dict(await list_tasks(
                    status=_qopt(q, "status"),
                    priority=_qint(q, "priority", None),
                    session_id=_qopt(q, "session_id"),
                    limit=limit if limit is not None else 20,
                    offset=_qint(q, "offset", 0) or 0,
                    skip=skip,
                    _user=caller,
                ))))
            if sub in ("", "/") and method == "POST":
                return _ok(_json_response(_pydantic_to_dict(await create_task(
                    TaskCreate(**_decode_body(raw_body)), caller,
                ))))
            if sub == "/debug/all" and method == "GET":
                return _ok(_json_response(await get_tasks_debug(
                    skip=_qint(q, "skip", 0) or 0,
                    limit=_qint(q, "limit", 100) or 100,
                    sort_by=_qopt(q, "sort_by") or "created_at",
                    sort_order=_qopt(q, "sort_order") or "desc",
                    status=_qopt(q, "status"),
                    session_id=_qopt(q, "session_id"),
                )))
            if sub == "/root" and method == "POST":
                return _ok(_json_response(_pydantic_to_dict(await create_root_task(
                    TaskRootCreate(**_decode_body(raw_body)), caller,
                ))))
            if sub == "/containers" and method == "GET":
                return _ok(_json_response(await list_container_tasks(
                    session_id=_qopt(q, "session_id") or "",
                    _user=caller,
                )))
            # /{task_id} 系列
            if sub.startswith("/") and len(sub) > 1:
                rest = sub[1:]
                parts = rest.split("/")
                tid = parts[0]
                # 单级 /{task_id}
                if len(parts) == 1:
                    if method == "GET":
                        return _ok(_json_response(_pydantic_to_dict(get_task(tid, caller))))
                    if method == "PATCH":  # 前端 UPDATE 用 PATCH
                        return _ok(_json_response(_pydantic_to_dict(await update_task(
                            tid, TaskUpdate(**_decode_body(raw_body)), caller,
                        ))))
                    if method == "DELETE":
                        return _ok(_json_response(await delete_task(tid, caller)))
                # /{task_id}/submit|evaluate|pause|resume|cancel
                if len(parts) == 2:
                    action = parts[1]
                    if action == "submit" and method == "POST":
                        return _ok(_json_response(_pydantic_to_dict(await submit_task(tid, caller))))
                    if action == "evaluate" and method == "POST":
                        # 0.2 评估闸门已插件化：评估由 task_evaluate 工具承载
                        # （plugins/shared/tools/task_evaluate），本端点退役恒 410，
                        # 明确报错替代旧"评估引擎不可用"降级假成功。
                        raise APIError(
                            status_code=410,
                            error_code="API_GONE_2006",
                            message=(
                                f"任务 {tid} 的 HTTP 评估端点已下线"
                                "（0.2 评估已插件化）：请改用 task_evaluate 工具执行评估"
                            ),
                        )
                    if action == "pause" and method == "POST":
                        return _ok(_json_response(await pause_task(tid, caller)))
                    if action == "resume" and method == "POST":
                        return _ok(_json_response(await resume_task(tid, caller)))
                    if action == "cancel" and method == "POST":
                        body = _decode_body(raw_body)
                        return _ok(_json_response(await cancel_task(tid, body, caller)))
                # task_phase：/{task_id}/phase... 与 /{task_id}/ac...
                if len(parts) >= 2 and parts[1] == "phase":
                    return _ok(_json_response(await _dispatch_task_phase(tid, parts[2:], method)))
                if len(parts) >= 2 and parts[1] == "ac":
                    return _ok(_json_response(await _dispatch_task_ac(tid, parts[2:], method)))

        logger.warning("tasks http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except APIError as exc:
        return _http_exc_response(exc)
    except ValueError as exc:
        # _decode_body 的 JSON 解析失败 / pydantic 校验失败
        logger.warning("tasks http.handle: 请求体解析失败 | path=%s | err=%s", path, exc)
        return _ok(_json_response({"error": "invalid request body", "detail": str(exc)}, 400))
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("tasks http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))

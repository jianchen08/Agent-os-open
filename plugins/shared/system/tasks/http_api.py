"""tasks 插件自持 HTTP 面——tasks/projects 域。

来源：plugins/shared/system/channel_api/（只读参考，本文件为搬迁改造产物）：
- routes_tasks.py（21 端点：/tasks 域）
- routes_missing.py task_phase_router（task_phase/ac 9 端点）
- routes_missing.py projects_router（7 端点）

端点职责：
1. 任务域读面/写面单一真值 = 管道 state 聚合（pipeline-state.list 的
   task.* 行）；创建经 tool-executor 走 task_submit 工具（单一业务入口）。
2. task_phase/ac 端点：phase 由 state 行任务状态映射，AC 保持未评估占位语义。
3. projects 域：projects = 真实文件夹 + project_registry 登记行（非任务），
   list/get/pause/resume/auto-execute/delete = 项目生命周期操作；
   名下子任务摘要读 state 聚合（task.parent_project_id）。
4. 能力访问（chat / pipeline-state / pipeline-executor / tool-executor 内核
   能力）经 ``_capability()`` 统一取句柄（懒 import server.plugin；测试可
   monkeypatch）。

协议：http.handle 工具按 path 分发（plugin.json http_endpoints 声明、内核
dispatcher 调度的标准插件 HTTP 面模式，与 agent_manager/task_form 同款）。
返回 ToolExecutionResult{success, data}，data 为 HttpHandleResponse
{status, headers, body(base64)}。
"""
from __future__ import annotations

import base64
import logging
import os
import sys
import time
from typing import Any

import state_fields  # noqa: PLC0415 — plugins/shared 平铺模块（裸名导入先例 tenant_data）

from pydantic import BaseModel, Field

# 共享层样板（plugins/shared/http_json.py）入 sys.path 后裸名导入。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from http_json import (  # noqa: E402
    decode_body as _decode_body,
    json_response as _json_response,
    ok as _ok,
)
from task_birth import TaskBirthError  # noqa: E402

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
    project_id: str = ""  # 挂靠项目 id（登记行；空 = 独立任务）
    target_id: str = ""  # 执行 agent（必填）
    workspace: str = ""
    workspace_mode: str = ""  # worktree/plain（空 = 工具侧按 workspace 显式性缺省）
    isolation_level: str = ""  # isolated/non_isolated（空 = 工具侧默认 isolated）
    inherit: dict[str, Any] | None = None
    thread_id: str  # 复用当前会话 → 作 session_id


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
# HTTP 响应协议（内核 dispatcher 契约）：公共实现 plugins/shared/http_json.py
# （文件头已导入）；_http_exc_response 为本插件特有转换。
# ════════════════════════════════════════════════════════════


def _http_exc_response(exc: Exception) -> dict[str, Any]:
    """把 APIError / HTTPException 转成带 status 的 HTTP 响应。"""
    status = getattr(exc, "status_code", None) or 500
    detail = getattr(exc, "detail", None)
    if detail is None:
        detail = str(exc)
    return _ok(_json_response({"detail": detail}, int(status)))


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
# 能力访问（测试可 monkeypatch）
# ════════════════════════════════════════════════════════════


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
    task_id: str,
    user_id: str = "task_system",
    description: str = "",
) -> str:
    """重跑已有任务执行管道（UI submit/resume = task_manage retry 映射）。

    以 task_id 作 pipeline_id 走 chat.send_message 注入分支，background
    立即返回。创建不走本函数——面板 HTTP 创建是 task_submit 工具的表单
    传输层（_invoke_task_submit_tool），出生写面单一真值归工具侧。

    Returns:
        task_id（注入分支原值）；派发响应缺 pipeline_id 返回空串（调用方
        据此感知提交未生效）。

    Raises:
        TaskBirthError: chat capability 未注入。
    """
    try:
        chat = _capability("chat")
    except KeyError as exc:
        raise TaskBirthError(f"chat capability 未注入: {exc}") from exc

    # ── 注入模式：重跑已有任务管道（retry/resume）──
    kickoff = f"重新执行任务「{title}」（任务 ID: {task_id}）。"
    if description:
        kickoff += f"\n任务描述：{description}"
    params: dict[str, Any] = {
        "pipeline_id": task_id,
        "message": kickoff,
        "user_id": user_id or "task_system",
        "background": True,
        # 重跑复位任务状态：failed → running（DSL 任务终态收束条件据此放行
        # 重跑轮次；任务重新进入执行态语义）
        "state": {"task.status": "running"},
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
    """获取任务列表（state 单一真值：任务 = 管道，行源 = pipeline-state 聚合）。

    支持按状态、优先级和会话 ID 筛选，分页返回（skip 优先）。session_id
    筛选基于 task.metadata["session_id"] 字段匹配；组装逻辑对齐 task_manage
    工具层（前端任务管理面板即本端点拉取）。
    """
    if skip is not None:
        offset = skip

    validate_pagination(limit, offset)

    tasks: list[dict[str, Any]] = []
    state_tasks = await _list_tasks_from_state()
    if state_tasks:
        tasks = list(state_tasks)

    if status:
        tasks = [t for t in tasks if t.get("status") == status]

    if session_id:
        tasks = [
            t for t in tasks if (t.get("metadata") or {}).get("session_id") == session_id
        ]

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
    - **task.\* 行**：执行管道收到的任务（task.id = 管道 id，统一出生协议写入
      + task.assigned 上级派发），普通任务（执行）创建新管道后在此。

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
                    "submitted_by": str(row.get("task.submitted_by") or ""),
                    # 父是容器任务（task.owned 声明）时：容器 project id（前端
                    # 任务树据此把子任务挂到容器节点下）
                    "parent_project_id": str(row.get("task.parent_project_id") or "") or None,
                    # 工作空间坐标（任务面板"打开工作空间"按钮数据源；
                    # workspace_lifecycle init 写入 + persistent_fields 落表）。
                    # as_dict 兼容跨边界 JSON 字符串形态（state_fields 契约）。
                    "ws_meta": state_fields.as_dict(row.get("ws_meta"), field="ws_meta"),
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
                        "submitted_by": str(fields.get("submitted_by") or ""),
                        "workspace": str(fields.get("workspace") or ""),
                    },
                    "created_at": str(fields.get("created_at") or ""),
                }
            )
    return out


async def _get_task_row_from_state(task_id: str) -> dict[str, Any] | None:
    """按 id 从 state 聚合取单个任务行（get/submit/phase 等单任务读面同源）。

    None = state 未出口/桥未就绪，调用方按任务不存在处理——state 是任务域
    唯一数据源，无 YAML 兜底。
    """
    tasks = await _list_tasks_from_state()
    if not tasks:
        return None
    return next(
        (t for t in tasks if str(t.get("id") or "") == task_id),
        None,
    )


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
    """获取任务调试数据（state 聚合行全字段）。"""

    tasks = await _list_tasks_from_state() or []

    if status:
        tasks = [t for t in tasks if t.get("status") == status]

    if session_id:
        tasks = [
            t for t in tasks if (t.get("metadata") or {}).get("session_id") == session_id
        ]

    tasks.sort(key=lambda t: str(t.get("created_at") or ""), reverse=(sort_order == "desc"))

    return {"items": tasks, "total": len(tasks)}


async def _invoke_task_submit_tool(inputs: dict[str, Any], user_sub: str) -> dict[str, Any]:
    """面板创建 = task_submit 工具的表单提交（单一业务入口）。

    HTTP 创建端点只是人类的传输层：字段映射为工具入参，经 tool-executor.invoke
    调 task_submit 工具 sidecar——闸门（agent 存在性/指标 ID/工作空间安全/
    层级归属）与出生协议全在工具侧单一路径执行。parent_agent_level=1
    （人类 = L1 之上，可显式 workspace/project）+ 认证 user_id 作注入参数。

    Returns:
        工具成功信封的 data（含 task_id/pipeline_id）。

    Raises:
        APIError: 能力未注入（503）/ 调用失败（500）/ 工具拒绝信封（400，
            DISPATCH_FAILED 为 500；error_code 原样透传）。
    """
    try:
        handle = _capability("tool-executor")
    except KeyError as exc:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="tool-executor 能力未注入，无法创建任务",
        ) from exc
    try:
        result = await handle.call(
            "invoke",
            {
                # plugin_id 缺省由内核工具注册表按 tool_name 反查（单一真值，
                # 不硬编码插件 id；工具未启用/未注册时 fail-closed 报未注册）
                "tool_name": "task_submit",
                "args": {"parent_agent_level": 1, "user_id": user_sub, **inputs},
            },
            60.0,
        )
    except RuntimeError as exc:
        raise APIError(
            status_code=500,
            error_code="TOOL_INVOKE_FAILED",
            message=f"task_submit 工具调用失败: {exc}",
        ) from exc
    if isinstance(result, dict) and result.get("success"):
        # 信封形状自适应（对齐 approval 消费先例）：output 为 SDK 标准键，
        # data/result 兼容平铺变体。
        data = result.get("output") or result.get("data") or result.get("result") or {}
        return data if isinstance(data, dict) else {}
    if isinstance(result, dict):
        error = str(result.get("error") or "任务提交被拒绝")
        error_code = str(result.get("error_code") or "TASK_SUBMIT_REJECTED")
    else:
        error = f"工具调用返回异常形态: {result!r}"
        error_code = "TASK_SUBMIT_REJECTED"
    raise APIError(
        status_code=500 if error_code == "DISPATCH_FAILED" else 400,
        error_code=error_code,
        message=error,
    )


async def create_task(
    body: TaskCreate | dict[str, Any],
    _user: dict[str, Any] | None = None,
) -> TaskResponse:
    """创建新任务（面板表单 = task_submit 工具提交，单一业务入口）。

    执行者/描述必填、目标 agent 存在性等闸门全由工具侧承载，本端点只做
    字段映射与结果转写；任务即管道（引擎生成 pipeline_id = task_id）。
    """
    # SDK HTTP 端点把 body 作 dict 透传（不实例化 Pydantic 模型）——兼容两种形状
    if isinstance(body, dict):
        body = TaskCreate(**body)

    data = await _invoke_task_submit_tool(
        {
            "goal_title": body.title,
            "goal_description": body.description or "",
            "target_type": "agent",
            "target_id": body.agent_id,
        },
        _current_user(_user).get("sub", ""),
    )
    task_id = str(data.get("task_id") or data.get("pipeline_id") or "")

    logger.info(
        "用户 %s 创建任务: %s",
        _current_user(_user).get("username", "system"),
        task_id,
    )

    return _task_to_response({"id": task_id, "title": body.title, "status": "pending"})


async def create_root_task(
    body: TaskRootCreate | dict[str, Any],
    _user: dict[str, Any] | None = None,
) -> TaskResponse:
    """用户手动创建根任务（project_id 可选挂靠项目 = 文件夹+登记）。

    等价于 L1 主 agent 调 task_submit 提交根任务：本端点把表单字段映射为
    工具入参经 _invoke_task_submit_tool 提交——项目挂靠校验、workspace
    安全校验、工作区拓扑/隔离缺省全由工具侧承载。
    """
    # SDK HTTP 端点把 body 作 dict 透传——兼容两种形状
    if isinstance(body, dict):
        body = TaskRootCreate(**body)

    inputs: dict[str, Any] = {
        "goal_title": body.title,
        "goal_description": body.description or "",
        "target_type": "agent",
        "target_id": body.target_id,
        "workspace": body.workspace,
        "workspace_mode": body.workspace_mode,
        "isolation_level": body.isolation_level,
        "thread_id": body.thread_id,
    }
    if body.project_id:
        inputs["project_id"] = body.project_id
    if body.inherit:
        inputs["inherit"] = body.inherit

    data = await _invoke_task_submit_tool(inputs, _current_user(_user).get("sub", ""))
    task_id = str(data.get("task_id") or data.get("pipeline_id") or "")

    logger.info(
        "[create_root_task] 用户 %s 手动创建根任务 | task_id=%s | project_id=%s | thread=%s",
        _current_user(_user).get("username", "system"),
        task_id,
        body.project_id or "-",
        body.thread_id,
    )

    return _task_to_response({"id": task_id, "title": body.title, "status": "pending"})


async def get_task(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> TaskResponse:
    """获取指定任务的详情（state 单一真值，无 YAML 兜底）。

    读法与 list/update 同源：state 聚合行命中即出口，未命中 404。
    """
    row = await _get_task_row_from_state(task_id)
    if row is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="任务不存在或已被删除",
        )

    return _task_to_response(row)


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
    """删除指定任务（state 单一真值：任务 = 管道）。

    存在性以 state 聚合为准；删除 = 调内核 pipeline-executor.delete_pipeline
    级联清理管道全部执行数据（runs/traces/messages/state/checkpoints +
    registry 条目）。
    """
    # state 聚合存在性判定
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
        "用户 %s 删除任务（state 面）: %s",
        _current_user(_user).get("username", "system"),
        task_id,
    )
    return {"message": "任务已删除"}


async def submit_task(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> TaskSubmitResponse:
    """提交任务进入执行队列（pending/failed → 注入模式重跑任务管道）。

    状态门读 state 聚合行（state 单一真值，无 YAML 兜底）。
    """
    row = await _get_task_row_from_state(task_id)

    if row is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="任务不存在或已被删除",
        )

    current_status = str(row.get("status") or "pending")

    allowed_statuses = {"pending", "failed"}

    if current_status not in allowed_statuses:
        raise APIError(
            status_code=400,
            error_code="API_VAL_2003",
            message=f"当前状态 '{current_status}' 不允许提交，仅允许: {', '.join(allowed_statuses)}",
        )

    # GAP-1 统一：重跑已有任务 = 注入模式（task_id 即 pipeline_id，retry 映射）
    submitted = await _submit_task_event(
        title=str(row.get("title") or task_id),
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
    """获取任务当前执行阶段（state 行状态 → 前端阶段概念映射）。

    state 无此任务行时维持既有缺省形状（prepare/pending）——阶段面板对
    未知任务的既有契约。
    """
    row = await _get_task_row_from_state(task_id)
    if row is not None:
        status_str = str(row.get("status") or "pending")
        phase, phase_status = _STATUS_TO_PHASE.get(status_str, ("prepare", "pending"))
        return {
            "taskId": task_id,
            "currentPhase": phase,
            "phaseStatus": phase_status,
        }

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
# projects 域 handler（project = 文件夹 + 登记行）
# ════════════════════════════════════════════════════════════
#
# 语义（ADR 2026-08-27-project-folder-registration）：项目是真实文件夹 +
# tasks 插件登记行（id ↔ path），不是任务实体——无 task_id/状态机/管道。
# 子任务挂靠键 = state 行 ``task.parent_project_id`` / 镜像行
# ``metadata.project_id``（两处同值，task_submit 双写）。

from service_access import get_project_registry  # noqa: E402


def _get_project_registry() -> Any:
    """取项目登记簿；不可用时抛 503 APIError（读写面 fail-honest）。"""
    registry = get_project_registry()
    if registry is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="ProjectRegistry 不可用，无法访问项目数据",
        )
    return registry


def _project_status_out(status: str) -> str:
    """登记行 status → 前端 Project.status（running/suspended）。"""
    return "suspended" if status == "paused" else "running"


def _project_to_dict(project: Any, tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """登记行 → 前端 Project 形状（frontend/src/types 对齐）。"""
    return {
        "id": project.id,
        "userId": project.submitted_by or "",
        "sessionId": project.session_id or None,
        "goal": project.title or "",
        "status": _project_status_out(str(project.status)),
        "autoExecute": bool(project.auto_execute),
        "currentTaskIndex": 0,
        "tasks": tasks if tasks is not None else [],
        "timestamps": {
            "createdAt": project.created_at or "",
            "updatedAt": project.updated_at or "",
        },
        "metadata": {
            "path": project.path,
            "source": "project",
        },
    }


def _get_project_or_404(registry: Any, project_id: str) -> Any:
    project = registry.get(project_id)
    if project is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="项目不存在或已被删除",
        )
    return project


async def _project_child_pids(project_id: str) -> list[str]:
    """项目名下子任务管道 id（state 行 task.parent_project_id 匹配）。"""
    try:
        handle = _capability("pipeline-state")
        rows = await handle.call("list", {})
    except Exception as exc:  # noqa: BLE001 — 读面降级返回空，不阻断登记操作
        logger.warning("[projects] 名下子任务读取失败 | project_id=%s | err=%s", project_id, exc)
        return []
    if not isinstance(rows, list):
        return []
    return [
        str(row.get("pipeline_id") or "")
        for row in rows
        if isinstance(row, dict)
        and str(row.get("task.parent_project_id") or "") == project_id
        and str(row.get("pipeline_id") or "")
    ]


async def list_projects(
    limit: int = 20,
    offset: int = 0,
    page: int | None = None,
    status: str | None = None,
    session_id: str | None = None,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取项目列表（登记行；session_id 过滤创建会话）。"""
    registry = _get_project_registry()

    # page 兼容（前端传 page+limit）；offset 显式给定时优先
    if offset == 0 and page is not None and page > 1:
        offset = (page - 1) * limit

    projects = registry.list()
    if session_id:
        projects = [p for p in projects if p.session_id == session_id]

    out = [_project_to_dict(p) for p in projects]
    if status:
        out = [p for p in out if p.get("status") == status]

    total = len(out)
    return {"items": out[offset:offset + limit], "total": total, "limit": limit, "offset": offset}


async def create_project(
    body: dict[str, Any] | None = None,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """创建项目 = 建文件夹（显式路径优先，缺省 {ws_base}/projects/<slug>）+ 登记。

    非 git 文件夹会 git init（子任务 worktree 前提）；git init 失败创建整体失败。
    """
    from project_registry import ProjectModel, ensure_project_folder  # noqa: PLC0415

    body = dict(body or {})
    goal = str(body.get("goal") or body.get("title") or "").strip()
    if not goal:
        raise APIError(
            status_code=400,
            error_code="MISSING_GOAL",
            message="创建项目必须指定 goal",
        )
    registry = _get_project_registry()

    explicit_path = str(body.get("path") or body.get("workspace") or "").strip()
    try:
        folder = ensure_project_folder(goal, explicit_path)
    except (ValueError, RuntimeError) as exc:
        raise APIError(
            status_code=400,
            error_code="PROJECT_FOLDER_FAILED",
            message=str(exc),
        ) from exc

    project = ProjectModel(
        path=folder,
        title=goal,
        auto_execute=bool(body.get("auto_execute", False)),
        submitted_by=str(_current_user(_user).get("sub", "") or ""),
        session_id=str(body.get("session_id") or ""),
    )
    registry.save(project)

    logger.info(
        "[projects] 创建项目 | project_id=%s | goal=%s | path=%s | user=%s",
        project.id,
        goal,
        folder,
        project.submitted_by or "-",
    )
    return {"project": _project_to_dict(project)}


async def get_project(
    project_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取项目详情（含名下子任务摘要列表）。"""
    registry = _get_project_registry()
    project = _get_project_or_404(registry, project_id)

    tasks_summary: list[dict[str, Any]] = []
    try:
        handle = _capability("pipeline-state")
        rows = await handle.call("list", {})
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("task.parent_project_id") or "") != project_id:
                    continue
                tasks_summary.append(
                    {
                        "id": str(row.get("pipeline_id") or ""),
                        "title": str(row.get("task.goal") or row.get("pipeline_id") or ""),
                        "status": str(row.get("task.status") or "pending"),
                    }
                )
    except Exception as exc:  # noqa: BLE001 — 子任务摘要读取降级为空
        logger.warning("[projects] 子任务摘要读取失败 | project_id=%s | err=%s", project_id, exc)

    return {"project": _project_to_dict(project, tasks=tasks_summary)}


async def toggle_auto_execute(
    project_id: str,
    body: dict[str, Any] | None = None,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """切换项目自动执行开关（登记行持久化）。enabled 缺省翻转现值。"""
    registry = _get_project_registry()
    project = _get_project_or_404(registry, project_id)

    if body and "enabled" in body:
        enabled = bool(body.get("enabled"))
    else:
        enabled = not project.auto_execute
    project.auto_execute = enabled
    registry.save(project)

    logger.info("[projects] 切换自动执行 | project_id=%s | %s", project_id, enabled)
    return {"project": _project_to_dict(project)}


async def pause_project(
    project_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """暂停项目 = 登记行 status=paused + 尽力挂起名下子任务管道。"""
    registry = _get_project_registry()
    project = _get_project_or_404(registry, project_id)

    suspended = 0
    for pid in await _project_child_pids(project_id):
        if await _suspend_task_pipeline(pid):
            suspended += 1

    project.status = "paused"
    registry.save(project)

    logger.info(
        "[projects] 项目已暂停 | project_id=%s | suspended_children=%s",
        project_id,
        suspended,
    )
    return {"project": _project_to_dict(project)}


async def resume_project(
    project_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """恢复项目 = 登记行 status=active + 尽力恢复名下子任务管道。"""
    registry = _get_project_registry()
    project = _get_project_or_404(registry, project_id)

    resumed = 0
    for pid in await _project_child_pids(project_id):
        if await _resume_task_pipeline(pid):
            resumed += 1

    project.status = "active"
    registry.save(project)

    logger.info(
        "[projects] 项目已恢复 | project_id=%s | resumed_children=%s",
        project_id,
        resumed,
    )
    return {"project": _project_to_dict(project)}


async def delete_project(
    project_id: str,
    query: dict[str, str] | None = None,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """删除项目 = 级联挂起名下子任务 + 删登记行；delete_files=true 时删文件夹。

    文件夹删除带路径安全校验（盘符根/仓库根/工作空间基目录拒删）。
    """
    from project_registry import remove_project_folder  # noqa: PLC0415

    registry = _get_project_registry()
    project = _get_project_or_404(registry, project_id)

    suspended = 0
    for pid in await _project_child_pids(project_id):
        if await _suspend_task_pipeline(pid):
            suspended += 1

    registry.delete(project_id)

    folder_removed = False
    if query and str(query.get("delete_files") or "").lower() in ("1", "true", "yes"):
        folder_removed = remove_project_folder(project.path)

    logger.info(
        "[projects] 删除项目 | project_id=%s | suspended_children=%s | folder_removed=%s | user=%s",
        project_id,
        suspended,
        folder_removed,
        _current_user(_user).get("username", "system"),
    )
    return {"message": "项目已删除", "id": project_id, "folder_removed": folder_removed}


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


async def _route_projects_domain(
    sub: str, method: str, q: dict[str, str], raw_body: str, caller: dict[str, Any]
) -> dict[str, Any] | None:
    """projects 域路由（/ext/task_service/projects 后的 sub），未命中返回 None。

    GET/POST 列表与创建（"" 或 "/"）；/{pid} GET/DELETE；/{pid}/{action} POST
    （auto-execute/pause/resume）。
    """
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
            return _ok(_json_response(await delete_project(pid, q, caller)))
    elif sub.startswith("/") and "/" in sub[1:]:
        pid, action = sub[1:].split("/", 1)
        project_actions: dict[tuple[str, str], Any] = {
            ("auto-execute", "POST"): lambda: toggle_auto_execute(pid, _decode_body(raw_body), caller),
            ("pause", "POST"): lambda: pause_project(pid, caller),
            ("resume", "POST"): lambda: resume_project(pid, caller),
        }
        handler = project_actions.get((action, method))
        if handler is not None:
            return _ok(_json_response(await handler()))
    return None


async def _route_tasks_collection(
    sub: str, method: str, q: dict[str, str], raw_body: str, caller: dict[str, Any]
) -> dict[str, Any] | None:
    """tasks 域字面量集合路由（列表/创建/debug/root），未命中返回 None。"""
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
    return None


# /{task_id}/{action} 路由说明：submit/pause/resume/cancel 为 (method,action)
# 分发表成员；evaluate 退役恒 410 在表前单列；cancel 需请求体故取 raw_body。


async def _route_task_actions(
    tid: str, action: str, method: str, raw_body: str, caller: dict[str, Any]
) -> dict[str, Any] | None:
    """派发 /{task_id}/{action} 二段路径，未命中返回 None。"""
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

    async def _submit() -> dict[str, Any]:
        return _ok(_json_response(_pydantic_to_dict(await submit_task(tid, caller))))

    async def _pause() -> dict[str, Any]:
        return _ok(_json_response(await pause_task(tid, caller)))

    async def _resume() -> dict[str, Any]:
        return _ok(_json_response(await resume_task(tid, caller)))

    async def _cancel() -> dict[str, Any]:
        return _ok(_json_response(await cancel_task(tid, _decode_body(raw_body), caller)))

    handlers: dict[tuple[str, str], Any] = {
        ("submit", "POST"): _submit,
        ("pause", "POST"): _pause,
        ("resume", "POST"): _resume,
        ("cancel", "POST"): _cancel,
    }
    handler = handlers.get((action, method))
    if handler is None:
        return None
    return await handler()


async def _route_tasks_domain(
    sub: str, method: str, q: dict[str, str], raw_body: str, caller: dict[str, Any]
) -> dict[str, Any] | None:
    """tasks 域路由（/ext/task_service/tasks 后的 sub），未命中返回 None。"""
    collection = await _route_tasks_collection(sub, method, q, raw_body, caller)
    if collection is not None:
        return collection

    # /{task_id} 系列
    if not (sub.startswith("/") and len(sub) > 1):
        return None
    parts = sub[1:].split("/")
    tid = parts[0]
    # 单级 /{task_id}（前端 UPDATE 用 PATCH）
    if len(parts) == 1:
        if method == "GET":
            return _ok(_json_response(_pydantic_to_dict(await get_task(tid, caller))))
        if method == "PATCH":
            return _ok(_json_response(_pydantic_to_dict(await update_task(
                tid, TaskUpdate(**_decode_body(raw_body)), caller,
            ))))
        if method == "DELETE":
            return _ok(_json_response(await delete_task(tid, caller)))
        return None
    # 二段以上：action 路由优先；phase/ac 为子资源树分发
    routed = await _route_task_actions(tid, parts[1], method, raw_body, caller)
    if routed is not None:
        return routed
    if parts[1] == "phase":
        return _ok(_json_response(await _dispatch_task_phase(tid, parts[2:], method)))
    if parts[1] == "ac":
        return _ok(_json_response(await _dispatch_task_ac(tid, parts[2:], method)))
    return None


async def handle_http(
    path: str,
    method: str,
    raw_body: str,
    query: dict[str, str] | None,
    headers: dict[str, str] | None,
) -> dict[str, Any]:
    """http.handle 按 path 分发（tasks 域 21 端点 + projects 域 7 端点）。

    返回 ToolExecutionResult{success, data}；业务异常转带 status 的响应。
    域内细粒度匹配委托 _route_projects_domain/_route_tasks_domain。
    """
    q = query or {}
    caller = _resolve_caller(headers)

    try:
        if path.startswith(f"{_PREFIX}/projects"):
            resp = await _route_projects_domain(
                path[len(f"{_PREFIX}/projects"):], method, q, raw_body, caller
            )
            if resp is not None:
                return resp
        if path.startswith(f"{_PREFIX}/tasks"):
            resp = await _route_tasks_domain(
                path[len(f"{_PREFIX}/tasks"):], method, q, raw_body, caller
            )
            if resp is not None:
                return resp

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

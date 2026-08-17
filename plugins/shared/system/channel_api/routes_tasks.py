"""任务管理 API 路由。



提供任务的 CRUD 操作、提交和评估接口。

"""

from __future__ import annotations

import asyncio  # noqa: F401
import logging
import sys
from datetime import datetime  # noqa: F401
from typing import Any

from deps import APIError, require_auth, validate_pagination
from fastapi import Depends, Query

def _current_user(_user):
    """SDK 进程内分发（_handle_tasks_domain）不解析 FastAPI Depends——
    _user 为 Depends 对象时降级空用户；FastAPI 原生路由下发 dict。"""
    return _user if isinstance(_user, dict) else {}


from memory_store import store
from models import (
    TaskCreate,
    TaskEvaluateRequest,
    TaskEvaluateResponse,
    TaskListResponse,
    TaskResponse,
    TaskRootCreate,
    TaskSubmitResponse,
    TaskUpdate,
)
from tasks.enum_utils import safe_enum_value
from tasks.service_access import get_task_service

logger = logging.getLogger(__name__)


# FastAPI 在模块级别使用 -> 注解时需要 APIRouter 实例

from fastapi import APIRouter  # noqa: E402

router = APIRouter(prefix="/api/v1/tasks", tags=["任务"])


# A-3/A-6: 委托到公共接口，保持模块内调用兼容

_get_task_service = get_task_service


def _map_status_for_api(status: str) -> str:
    """直接返回后端原始状态值，前后端统一字段。



    Args:

        status: 后端状态字符串



    Returns:

        原始状态字符串

    """

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
        thread_id=t.get("thread_id"),
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


@router.get(
    "",
    response_model=TaskListResponse,
    summary="获取任务列表",
)
async def list_tasks(
    status: str | None = Query(default=None, description="按状态筛选"),
    priority: int | None = Query(
        default=None,
        ge=1,
        le=9,
        description="按优先级筛选",
    ),
    session_id: str | None = Query(default=None, description="按会话 ID 筛选"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    skip: int | None = Query(default=None, ge=0, description="skip 参数（等价于 offset）"),
    _user: dict = Depends(require_auth),
) -> TaskListResponse:
    """获取当前用户的任务列表。



    支持按状态、优先级和会话 ID 筛选，分页返回。

    合并 api_store 和 TaskStorage（YAML 文件）两个数据源。

    session_id 筛选基于 task.metadata["session_id"] 字段匹配。

    同时支持 skip 和 offset 参数（skip 优先）。



    Returns:

        TaskListResponse 包含 items 和 total

    """

    # R7: skip 参数兼容，等价于 offset

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

    if session_id:
        pipeline_ids = set()

        thread_data = store.threads.get(session_id)

        if thread_data:
            pipeline_ids.update(thread_data.get("pipeline_ids", []))

        # 从任务自身的 pipeline_run_id / parent_pipeline_id 递归扩展管道树

        if pipeline_ids:
            changed = True

            while changed:
                changed = False

                for td in tasks:
                    ppid = td.get("parent_pipeline_id", "")

                    prid = td.get("pipeline_run_id", "")

                    if ppid and ppid in pipeline_ids and prid and prid not in pipeline_ids:
                        pipeline_ids.add(prid)

                        changed = True

        def _task_matches_pipeline(task_dict: dict) -> bool:
            meta = task_dict.get("metadata", {})

            task_session = meta.get("session_id")

            if task_session == session_id:
                return True

            if pipeline_ids:
                ppid = task_dict.get("parent_pipeline_id", "")

                prid = task_dict.get("pipeline_run_id", "")

                if ppid in pipeline_ids or prid in pipeline_ids:
                    return True

                for pid in pipeline_ids:
                    if meta.get("parent_pipeline_id") == pid or meta.get("pipeline_run_id") == pid:
                        return True

            return False

        tasks = [t for t in tasks if _task_matches_pipeline(t)]

    if status:
        tasks = [t for t in tasks if t.get("status") == status]

    if priority is not None:
        tasks = [t for t in tasks if t.get("priority") == priority]

    total = len(tasks)

    end = offset + limit

    page = tasks[offset:end]

    items = [_task_to_response(t) for t in page]

    return TaskListResponse(items=items, total=total)


@router.get(
    "/debug/all",
    summary="获取任务调试数据（全字段）",
)
async def get_tasks_debug(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
    status: str | None = Query(default=None),
    session_id: str | None = Query(default=None, description="按会话 ID 筛选"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取任务调试数据（全字段）。



    支持按状态和会话 ID 筛选，返回全字段数据用于调试。



    Returns:

        包含 items 和 total 的字典

    """

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


@router.post(
    "",
    response_model=TaskResponse,
    status_code=201,
    summary="创建任务",
)
async def create_task(
    body: TaskCreate,
    _user: dict = Depends(require_auth),
) -> TaskResponse:
    """创建新任务。



    Args:

        body: 任务创建请求



    Returns:

        TaskResponse 新创建的任务

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

    # 把必填校验从执行期（task_executor）前移到创建期，避免错误信号被队列/重试延迟，

    # 也防止下游任何路径静默降级到默认 Agent。

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


@router.post(
    "/root",
    response_model=TaskResponse,
    status_code=201,
    summary="手动创建根任务",
)
async def create_root_task(
    body: TaskRootCreate,
    _user: dict = Depends(require_auth),
) -> TaskResponse:
    """用户手动创建根任务。

    等价于 L1 主 agent 调 task_submit 提交根任务，为 L2+ 子 agent 提供合法的
    任务上下文（根任务 ID 注入管道 state 后，L2 调 task_submit 即可正确挂子任务）。
    container / non_container 都走现有下游逻辑，容器是工作空间集合（编排语义），
    非容器由 target agent 直接执行。L2 拦截规则、层级配置、注入链均不变。
    """

    # SDK HTTP 端点把 body 作 dict 透传（不实例化 Pydantic 模型）——兼容两种形状
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
        # 0.2 迁移：task_submit 独立插件目录（tools.builtin 包已废弃），
        # channel_api 的 sys.path 不含该目录——importlib 磁盘加载。
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

    is_child = False

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

        is_child = True

    # ── 复用当前会话和主管道 ──

    thread_id = body.thread_id

    active_pipeline_id = ""

    try:
        _session = store.get_session(thread_id)

        if _session:
            active_pipeline_id = _session.active_pipeline_id or ""

    except Exception as exc:
        logger.warning("[create_root_task] 获取会话主管道失败 | thread=%s | error=%s", thread_id, exc)

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
    # 执行管道（引擎生成 pipeline_id = task_id），父链由 lineage 表达
    # （parent_pipeline_id = active_pipeline_id），YAML 无写路径。
    # execution_context：组装工作区拓扑/隔离声明（对齐 task_submit._build_execution_context），
    # workspace_lifecycle 插件据此在 init 阶段创建 worktree/plain 空间。
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


@router.get(
    "/containers",
    summary="列出会话的容器任务（供新建子任务选父容器）",
)
async def list_container_tasks(
    session_id: str = Query(..., description="会话 ID（=thread_id）"),
    _user: dict = Depends(require_auth),
) -> list[dict[str, Any]]:
    """返回当前会话下所有 task_scope=container 的任务，供前端下拉选父容器。"""

    task_service = _get_task_service()

    if task_service is None:
        return []

    containers: list[dict[str, Any]] = []

    try:
        tasks = await task_service.list_all(limit=1000, session_id=session_id)

        for tm in tasks:
            _meta = tm.metadata or {}

            if _meta.get("task_scope") == "container":
                containers.append({"id": tm.id, "title": tm.title})

    except Exception as exc:
        logger.warning("[list_container_tasks] 加载失败 | session=%s | error=%s", session_id, exc)

    return containers


@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    summary="获取任务详情",
)
def get_task(
    task_id: str,
    _user: dict = Depends(require_auth),
) -> TaskResponse:
    """获取指定任务的详情。



    Args:

        task_id: 任务 ID



    Returns:

        TaskResponse 任务详情



    Raises:

        APIError: 任务不存在 (404)

    """

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


@router.patch(
    "/{task_id}",
    response_model=TaskResponse,
    summary="更新任务",
)
async def update_task(
    task_id: str,
    body: TaskUpdate,
    _user: dict = Depends(require_auth),
) -> TaskResponse:
    """更新指定任务的字段。



    Args:

        task_id: 任务 ID

        body: 任务更新请求（仅传入需要更新的字段）



    Returns:

        TaskResponse 更新后的任务



    Raises:

        APIError: 任务不存在 (404)

    """

    # GAP-1 统一：任务字段真值在 state（出生即定，不可经 UI 改写）——
    # update 语义退役，端点只读返回当前 state 行（status 的变更由 run 终态 /
    # 挂起恢复表达；描述/优先级在提交时已定型）。

    # 读 state 聚合行（task = pipeline）
    from routes_missing import _channel_api_plugin  # noqa: PLC0415

    try:
        handle = _channel_api_plugin().get_capability("pipeline-state")
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


@router.delete(
    "/{task_id}",
    summary="删除任务",
)
async def delete_task(
    task_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, str]:
    """删除指定任务，根据任务类型执行不同策略：取消运行中的管道，并区分容器子任务与根任务。

      - 容器任务: 软删除（标记取消，保留数据）

      - 非容器任务(容器的子任务): 取消自己及下级管道 + 删除数据（不清理工作空间）

      - 非容器任务(根任务): 取消管道 + 清理工作空间 + 删除数据



    Args:

        task_id: 任务 ID



    Returns:

        删除成功消息



    Raises:

        APIError: 任务不存在 (404)

    """

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法删除任务",
        )

    deleted = await task_service.delete_task(task_id)

    if not deleted:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="任务不存在或已被删除",
        )

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


@router.post(
    "/{task_id}/submit",
    response_model=TaskSubmitResponse,
    summary="提交任务执行",
)
async def submit_task(
    task_id: str,
    _user: dict = Depends(require_auth),
) -> TaskSubmitResponse:
    """提交任务进入执行队列。



    将任务状态从 pending 变为 queued，等待调度器分配执行。



    Args:

        task_id: 任务 ID



    Returns:

        TaskSubmitResponse 包含 task_id 和状态



    Raises:

        APIError: 任务不存在 (404) 或状态不允许 (400)

    """

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


@router.post(
    "/{task_id}/evaluate",
    response_model=TaskEvaluateResponse,
    summary="评估任务",
)
def evaluate_task(
    task_id: str,
    body: TaskEvaluateRequest | None = None,
    _user: dict = Depends(require_auth),
) -> TaskEvaluateResponse:
    """对指定任务执行评估。



    根据指定的评估指标对任务结果进行自动化评估。

    如果未指定 metric_ids，则执行任务关联 Agent 的推荐指标。



    Args:

        task_id: 任务 ID

        body: 评估请求（可选，默认执行所有推荐指标）



    Returns:

        TaskEvaluateResponse 评估结果



    Raises:

        APIError: 任务不存在 (404)

    """

    task = None

    task_service = _get_task_service()

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

    metric_ids = []

    if body:
        metric_ids = body.metric_ids

    # 尝试使用评估引擎

    try:
        from evaluation.loader import MetricLoader  # noqa: PLC0415

        loader = MetricLoader()

        loader.load_all()

        # 如果未指定指标，尝试从关联 Agent 获取推荐指标

        if not metric_ids:
            agent_id = task.get("agent_id")

            if agent_id:
                reg = _get_agent_registry()

                if reg:
                    agent_cfg = reg.get(agent_id)

                    if agent_cfg:
                        metric_ids = [m.metric_id for m in agent_cfg.recommended_metrics]

        # 如果仍无指标，加载所有

        if not metric_ids:
            metric_ids = loader.list_metrics()

        results: list[dict[str, Any]] = []

        for mid in metric_ids:
            metric_def = loader.get(mid)

            if metric_def is None:
                continue

            results.append(
                {
                    "metric_id": mid,
                    "name": metric_def.name,
                    "status": "skipped",
                    "message": "评估引擎未连接（API 模式下暂不支持自动执行）",
                    "passed": None,
                }
            )

        return TaskEvaluateResponse(
            task_id=task_id,
            overall_passed=False,
            summary=f"共 {len(results)} 个指标待评估（需连接评估引擎）",
            results=results,
        )

    except Exception as exc:
        logger.warning("评估引擎加载失败: %s", exc)

        return TaskEvaluateResponse(
            task_id=task_id,
            overall_passed=False,
            summary="评估引擎不可用",
            results=[],
        )

@router.post(
    "/{task_id}/pause",
    summary="暂停任务",
)
async def pause_task(
    task_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """暂停指定任务，同时取消正在运行的 PipelineEngine。



    执行两步操作：

    1. 将任务状态从 running/pending 变为 paused（持久化到 YAML）

    2. 取消该任务关联的 PipelineEngine 协程（真正停止 LLM 调用）



    重启后 paused 状态的任务不会被 TaskWorker 自动恢复执行。



    Args:

        task_id: 任务 ID



    Returns:

        暂停成功消息



    Raises:

        APIError: TaskService 不可用 (503)、任务不存在 (404) 或状态不允许 (400)

    """

    # GAP-1 统一：暂停 = 挂起任务管道（suspend_pipeline，run 终态回写 task.status）
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


@router.post(
    "/{task_id}/resume",
    summary="恢复任务",
)
async def resume_task(
    task_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """恢复指定暂停的任务，同时重新触发 PipelineEngine 执行。



    执行两步操作：

    1. 将任务状态从 paused 变为 pending

    2. 发布 task.submitted 事件，触发 TaskWorker 重新执行任务



    Args:

        task_id: 任务 ID



    Returns:

        恢复成功消息



    Raises:

        APIError: TaskService 不可用 (503)、任务不存在 (404) 或状态不允许 (400)

    """

    # GAP-1 统一：恢复 = resume_pipeline（按管道恢复最新 suspended run）
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


@router.post(
    "/{task_id}/cancel",
    summary="取消任务",
)
async def cancel_task(
    task_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """取消指定任务，同时取消运行中的管道并级联取消所有子任务。

    实现模式参照 pause_task / resume_task 端点。

    执行三步操作：

    1. 将任务状态设为 failed（持久化到 YAML），记录取消原因

    2. 取消该任务关联的 PipelineEngine 协程（真正停止 LLM 调用）

    3. 级联取消所有子任务，避免子任务管道继续执行

    Args:

        task_id: 任务 ID

        body: 可选请求体，包含 reason 字段（取消原因）

        _user: 当前认证用户（由 Depends 注入）



    Returns:

        取消成功消息，包含级联取消的子任务数量



    Raises:

        APIError: TaskService 不可用 (503)、任务不存在 (404) 或状态不允许 (400)

    """

    task_service = _get_task_service()

    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message="TaskService 不可用，无法取消任务",
        )

    # 获取任务并校验状态

    reason = (body or {}).get("reason", "用户请求取消")

    # GAP-1 统一：取消 = 挂起任务管道 + lineage 级联（不再写 YAML 状态机；
    # 取消语义由 run 终态 suspended 表达，状态真值在 state）
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

        return _task_to_response(task_dict)

    return {
        "success": True,
        "task_id": task_id,
        "cancelled": True,
        "message": "任务已取消",
        "cascaded_subtasks": cascaded,
    }


async def _suspend_task_pipeline(task_id: str) -> bool:
    """GAP-1 统一：暂停/取消任务 = 挂起任务管道（suspend_pipeline）。

    run 终态 suspended → 内核回写 task.status=suspended。返回是否派发成功。
    """
    try:
        from routes_missing import _channel_api_plugin  # noqa: PLC0415

        handle = _channel_api_plugin().get_capability("pipeline-executor")
        resp = await handle.call("suspend_pipeline", {"pipeline_id": task_id})
        return bool(resp and resp.get("run_id") is not None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[routes_tasks] suspend_pipeline 失败 | task_id=%s | err=%s", task_id, exc)
        return False


async def _resume_task_pipeline(task_id: str) -> bool:
    """GAP-1 统一：恢复任务 = resume_pipeline（按管道恢复最新 suspended run）。"""
    try:
        from routes_missing import _channel_api_plugin  # noqa: PLC0415

        handle = _channel_api_plugin().get_capability("pipeline-executor")
        resp = await handle.call("resume_pipeline", {"pipeline_id": task_id})
        return bool(resp and resp.get("run_id") is not None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[routes_tasks] resume_pipeline 失败 | task_id=%s | err=%s", task_id, exc)
        return False


async def _cascade_suspend_children(parent_task_id: str) -> int:
    """GAP-1 统一：级联挂起子任务管道（state 聚合中 lineage 子管道逐个挂起）。"""
    cascaded = 0
    try:
        from routes_missing import _channel_api_plugin  # noqa: PLC0415

        handle = _channel_api_plugin().get_capability("pipeline-state")
        rows = await handle.call("list", {})
        if not isinstance(rows, list):
            return 0
        for row in rows:
            if str(row.get("lineage.parent_pipeline_id") or "") == parent_task_id:
                child_id = str(row.get("pipeline_id") or "")
                if child_id and await _suspend_task_pipeline(child_id):
                    cascaded += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("[routes_tasks] 级联挂起失败 | task_id=%s | err=%s", parent_task_id, exc)
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
) -> str:
    """GAP-1 统一：经 chat.send_message 驱动任务执行管道，返回 pipeline_id（= task.id）。

    双模式（与内核 chat_send_handler 契约对齐）：
    - **创建模式**（task_id 空）：create 分支，引擎生成 pipeline_id；state 出生
      即带 task.* 字段、lineage 有父/根二选一、background 派发不阻塞请求。
      YAML 无写路径（task_service.create_task 退役）——返回的 id 即任务身份。
    - **注入模式**（task_id 非空）：重跑已有任务（UI submit/resume = task_manage
      retry 映射），以 task_id 作 pipeline_id 走注入分支，background 立即返回。
    """
    try:
        def _plugin_obj() -> Any:
            from routes_missing import _channel_api_plugin  # noqa: PLC0415

            return _channel_api_plugin()

        chat = _plugin_obj().get_capability("chat")

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
                "origin_session_id": parent_pipeline_id,
            }
        else:
            lineage = {
                "root": True,
                "origin": {"kind": "channel", "source": "channel_api"},
            }

        kickoff = f"执行任务「{title}」。"
        if description:
            kickoff += f"\n任务描述：{description}"

        params = {
            "create": True,
            "background": True,
            "message": kickoff,
            "user_id": user_id or "task_system",
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
        return pipeline_id

    except Exception as exc:
        logger.warning("_submit_task_event: 派发失败 | title=%s | error=%s", title, exc)
        return ""



def _get_agent_registry() -> Any:
    """惰性获取 Agent 注册表。"""

    try:
        from agents.registry import AgentRegistry  # noqa: PLC0415

        if AgentRegistry.has_instance():
            return AgentRegistry.get_instance()

    except ImportError:
        pass

    return None

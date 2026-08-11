"""线程与消息相关 API 路由。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, status

from deps import APIError, require_auth, validate_pagination

_recovered_user_ids: set[str] = set()


def _notify_session_update(thread_id: str, action: str) -> None:
    """推送会话变更事件。

    0.2 推送改走 frontend.emit capability（ADR §3.5），SDK 暂未实现该 capability；
    当前推送静默跳过，0.2 栈不再依赖 0.1 的 src/channels/websocket/
    ws_interaction_notifier（task_11 P2-7）。待 SDK 实现后改用
    ctx.frontend.emit(event="session_update", scope=...) 恢复。
    """
    return


def _destroy_session_container(workspace: str | None) -> None:
    """异步销毁会话隔离容器（同步路由上下文投递到事件循环，失败仅告警）。

    仅销毁该 workspace 对应的容器；容器销毁是幂等的（不存在视为成功）。
    """

    if not workspace:
        return

    try:
        from infrastructure.session.session_workspace import (  # noqa: PLC0415
            SessionWorkspaceService,
        )

        async def _destroy() -> None:
            await SessionWorkspaceService.destroy_session_container(workspace)

        loop = asyncio.get_event_loop()

        if loop.is_running():
            asyncio.ensure_future(_destroy())

        else:
            loop.run_until_complete(_destroy())

    except Exception:
        logger.warning("[session] 投递会话容器销毁任务失败 | ws=%s", workspace, exc_info=True)


import contextlib  # noqa: E402

from memory_store import _parse_iso_time, store  # noqa: E402
from models import (  # noqa: E402
    ThreadCreate,
    ThreadResponse,
    ThreadUpdate,
)
logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1/threads", tags=["线程"])


def _get_task_service() -> Any:
    """通过 ServiceProvider 获取全局 TaskService 实例。"""

    try:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()

        return provider.get_or_create(
            "task_service",
            lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService(),
        )

    except Exception:
        return None


def _safe_get_service(service_name: str) -> Any:
    """通过 ServiceProvider 安全获取服务实例，失败时返回 None。"""
    try:
        return get_service_provider().get(service_name)
    except Exception:
        return None


def _expand_pipeline_ids_with_task_data(pipeline_ids: list[str]) -> list[str]:
    """利用任务数据的 parent_pipeline_id 链，将 pipeline_ids 扩展为完整集合。"""
    if not pipeline_ids:
        return []

    task_service = _get_task_service()
    if task_service is None:
        return list(pipeline_ids)

    try:
        all_tasks = task_service.get_all_tasks()
    except Exception:
        return list(pipeline_ids)

    return _expand_pipeline_ids_with_tasks(pipeline_ids, all_tasks or [])


def _expand_pipeline_ids_with_tasks(
    pipeline_ids: list[str],
    all_tasks: list[Any],
) -> list[str]:
    """使用已获取的任务列表扩展 pipeline_ids（避免重复调用 get_all_tasks）。"""
    if not pipeline_ids or not all_tasks:
        return list(pipeline_ids)

    # 保持顺序的去重：seen 用于 O(1) 判重，ordered 保留插入顺序
    seen: set[str] = set()
    ordered: list[str] = []
    for pid in pipeline_ids:
        if pid and pid not in seen:
            seen.add(pid)
            ordered.append(pid)

    # 迭代扩展直到不动点，新增项追加到末尾（不改变原始顺序）
    changed = True
    while changed:
        changed = False
        for task in all_tasks:
            ppid = getattr(task, "parent_pipeline_id", "") or ""
            prid = getattr(task, "pipeline_run_id", "") or ""
            if ppid and ppid in seen and prid and prid not in seen:
                seen.add(prid)
                ordered.append(prid)
                changed = True

    return ordered


def _build_thread_response(t: dict) -> ThreadResponse:
    """将存储层的线程字典转换为前端期望的 ThreadResponse 格式。"""

    _meta = t.get("metadata") or {}

    return ThreadResponse(
        thread_id=t["id"],
        title=t.get("title") or None,
        intent=t.get("intent") or t.get("title") or None,
        current_state=t.get("current_state", "active"),
        created_at=t["created_at"],
        updated_at=t["updated_at"],
        agent_id=t.get("agent_id"),
        message_count=t.get("message_count", 0),
        pipeline_ids=t.get("pipeline_ids", []),
        active_pipeline_id=t.get("active_pipeline_id") or None,
        metadata=t.get("metadata"),
        workspace=_meta.get("workspace") or None,
        isolation_mode=_meta.get("isolation_mode") or None,
    )


@router.get(
    "",
    summary="获取线程列表（支持分页）",
)
def list_threads(
    session_type: str | None = Query(default=None, description="按会话类型过滤，如 main_pipeline"),
    skip: int = Query(default=0, ge=0, description="偏移量"),
    limit: int = Query(default=100, ge=1, le=9999, description="每页数量"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取当前用户的所有线程列表，支持分页。"""

    from models import ThreadListResponse  # noqa: PLC0415

    validate_pagination(limit, skip)

    threads = store.get_user_threads(_user["sub"])

    if session_type is not None:
        threads = [t for t in threads if t.get("metadata", {}).get("session_type") == session_type]

    total = len(threads)

    page_items = threads[skip : skip + limit]

    # 对分页内的每个线程，通过任务数据的 parent_pipeline_id 链扩展 pipeline_ids，
    # 确保子管道 ID 全部包含在内，前端 findPipelineLocation 可直接命中。
    # 先批量获取所有任务，避免每线程重复调用 get_all_tasks()。
    all_tasks_cache: list[Any] | None = None
    expanded_page_items: list[dict[str, Any]] = []
    for t in page_items:
        raw_ids = t.get("pipeline_ids", []) or []
        if raw_ids:
            if all_tasks_cache is None:
                task_service = _get_task_service()
                all_tasks_cache = task_service.get_all_tasks() if task_service else []
            expanded_ids = _expand_pipeline_ids_with_tasks(raw_ids, all_tasks_cache)
        else:
            expanded_ids = raw_ids
        expanded_page_items.append({**t, "pipeline_ids": expanded_ids})

    thread_responses = [_build_thread_response(t) for t in expanded_page_items]

    return ThreadListResponse(
        threads=thread_responses,
        total=total,
        skip=skip,
        limit=limit,
    ).model_dump()


@router.post(
    "",
    response_model=ThreadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建线程",
)
def create_thread(
    body: ThreadCreate,
    _user: dict = Depends(require_auth),
) -> ThreadResponse:
    """创建新线程。"""

    # 前端默认创建会话是 agentos（业务约定），未指定时用 agentos

    _effective_agent_id = body.agent_id or "agentos"

    # 自动标记为主管道会话（前端通过主界面创建的都是主管道）

    merged_metadata = body.metadata or {}

    if "session_type" not in merged_metadata:
        merged_metadata["session_type"] = "main_pipeline"

    # ── 会话工作空间与隔离模式（会话系统自洽管理，与任务级隔离解耦）──
    # workspace：用户选择的项目目录（绝对路径），主对话所有工具在此目录工作
    # isolation_mode：non_isolated（默认，读取放行+危险阻拦）/ isolated（容器）
    _workspace = (body.workspace or "").strip() or None
    _isolation_mode: str | None = None

    if _workspace:
        from infrastructure.session.session_workspace import (  # noqa: PLC0415
            SessionWorkspaceService,
            normalize_isolation_mode,
        )

        _ws_error = SessionWorkspaceService.validate_workspace(_workspace)
        if _ws_error:
            raise APIError(
                status_code=400,
                error_code="API_VAL_4001",
                message=f"工作空间校验失败: {_ws_error}",
            )
        if not os.path.isdir(_workspace):
            raise APIError(
                status_code=400,
                error_code="API_VAL_4001",
                message=f"工作空间目录不存在: {_workspace}。请指定一个已存在的项目目录。",
            )
        _isolation_mode = normalize_isolation_mode(body.isolation_mode)
        merged_metadata["workspace"] = _workspace
        merged_metadata["isolation_mode"] = _isolation_mode

    thread = store.create_thread(
        user_id=_user["sub"],
        title=body.title,
        agent_id=_effective_agent_id,
        metadata=merged_metadata,
        intent=body.intent,
    )

    # 创建新线程后，清除该用户的恢复缓存，以便下次列表请求时重新检查

    _recovered_user_ids.discard(_user["sub"])

    # 桥接基础设施层：以 thread_id 作为 session_id 创建 SessionModel

    session = _session_svc.create(
        channel_type="web",
        channel_ref=thread["id"],
        session_id=thread["id"],
    )

    # 创建会话时立即分配 pipeline_id，前端拿到后可直接激活管道

    # 后续消息处理时 Engine 会沿用这个 pipeline_id

    import uuid as _uuid  # noqa: PLC0415

    pipeline_id = _uuid.uuid4().hex[:12]

    session.register_pipeline(pipeline_id)

    store.set_session(thread["id"], session)

    # 会话系统作为创建者，注册管道到引擎注册表（tags 含 agent_id）。

    # 这是创建者的职责——谁创建谁注册。引擎层只管转发，不在此解析 agent。

    _register_session_pipeline(
        pipeline_id,
        thread["id"],
        _effective_agent_id,
        user_id=_user["sub"],
        workspace=_workspace,
        isolation_level=_isolation_mode,
    )

    thread["pipeline_ids"] = list(session.pipeline_ids)

    thread["active_pipeline_id"] = session.active_pipeline_id

    return _build_thread_response(thread)


@router.get(
    "/{thread_id}",
    response_model=ThreadResponse,
    summary="获取线程详情",
)
def get_thread(
    thread_id: str,
    _user: dict = Depends(require_auth),
) -> ThreadResponse:
    """获取指定线程的详情。"""

    thread = store.get_thread(thread_id)

    if thread is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )

    # 扩展 pipeline_ids，包含所有子管道（通过任务 parent_pipeline_id 链追溯）
    raw_ids = thread.get("pipeline_ids", []) or []
    expanded_ids = _expand_pipeline_ids_with_task_data(raw_ids)
    return _build_thread_response({**thread, "pipeline_ids": expanded_ids})


@router.patch(
    "/{thread_id}",
    response_model=ThreadResponse,
    summary="更新线程",
)
def update_thread(
    thread_id: str,
    body: ThreadUpdate,
    _user: dict = Depends(require_auth),
) -> ThreadResponse:
    """更新指定线程的标题。"""

    # ── 会话工作空间/隔离模式变更（经 metadata 传递，浅合并保留其他键）──
    # workspace/isolation_mode 变化时同步更新注册表 tags，
    # 使后续 idle 引擎重启使用新工作空间/隔离模式。
    _meta = dict(body.metadata or {})
    _ws_changed = "workspace" in _meta or "isolation_mode" in _meta
    _ws: str | None = None
    _iso: str | None = None

    if _ws_changed:
        from infrastructure.session.session_workspace import (  # noqa: PLC0415
            SessionWorkspaceService,
            normalize_isolation_mode,
        )

        _existing_meta = (store.get_thread(thread_id) or {}).get("metadata") or {}
        _ws = (_meta.get("workspace") or "").strip() or None
        if _ws:
            _ws_error = SessionWorkspaceService.validate_workspace(_ws)
            if _ws_error:
                raise APIError(
                    status_code=400,
                    error_code="API_VAL_4001",
                    message=f"工作空间校验失败: {_ws_error}",
                )
            if not os.path.isdir(_ws):
                raise APIError(
                    status_code=400,
                    error_code="API_VAL_4001",
                    message=f"工作空间目录不存在: {_ws}。请指定一个已存在的项目目录。",
                )
            _iso = normalize_isolation_mode(_meta.get("isolation_mode") or _existing_meta.get("isolation_mode"))
            _meta["workspace"] = _ws
            _meta["isolation_mode"] = _iso
        else:
            # workspace 置空 → 显式写 None 覆盖旧值（浅合并无法靠删键清除）
            _meta["workspace"] = None
            _meta["isolation_mode"] = None

    thread = store.update_thread(
        thread_id,
        title=body.title or body.intent,
        agent_id=body.agent_id,
        metadata=_meta,
    )

    if thread is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )

    # 工作空间/隔离模式变更 → 重注册管道 tags（register_pipeline tags 合并，
    # 覆盖时保留 bridge/engine_task，仅更新 workspace/isolation_level）
    if _ws_changed:
        _agent_id = thread.get("agent_id") or ""
        _pids = list(thread.get("pipeline_ids") or [])
        for _pid in _pids:
            _register_session_pipeline(
                _pid,
                thread_id,
                _agent_id,
                user_id=thread.get("user_id") or "",
                workspace=_ws or "",
                isolation_level=_iso,
            )

    return _build_thread_response(thread)


@router.delete(
    "/{thread_id}",
    summary="删除线程",
)
def delete_thread(  # noqa: PLR0912
    thread_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, str]:
    """删除指定线程及其所有消息和关联的管道执行记录。"""

    session = store.get_session(thread_id)

    pipeline_ids = list(session.pipeline_ids) if session else []

    # 删除前读取会话工作空间配置（isolated 会话需销毁对应容器）
    _thread_data = store.get_thread(thread_id) or {}

    _thread_meta = _thread_data.get("metadata") or {}

    _session_ws = (_thread_meta.get("workspace") or "").strip() or None

    _session_iso = (_thread_meta.get("isolation_mode") or "").strip() or None

    deleted = store.delete_thread(thread_id)

    if not deleted:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )

    _recovered_user_ids.discard(_user["sub"])

    # 迭代式收集关联管道（以 all_pipeline_ids 中每个 ID 匹配直到不动点）。
    # 历史：曾通过 ExecutionRecordStorage._pipeline_root_map 补全子管道，
    # 0.2 架构下 YAML 存储已废弃（内核 SQLite 接管），此处仅靠 task_service 收集。

    all_pipeline_ids = set(pipeline_ids)

    prev_size = 0

    while len(all_pipeline_ids) > prev_size:
        prev_size = len(all_pipeline_ids)

        task_service = _safe_get_service("task_service")

        if task_service:
            for task in task_service.get_all_tasks():
                if task.parent_pipeline_id in all_pipeline_ids or task.parent_pipeline_id == thread_id:
                    all_pipeline_ids.add(task.id)

                    if task.pipeline_run_id:
                        all_pipeline_ids.add(task.pipeline_run_id)

                    for sub in task_service.list_subtasks(task.id):
                        if sub.pipeline_run_id:
                            all_pipeline_ids.add(sub.pipeline_run_id)

    try:
        checkpoint_dir = Path("data/pipeline_checkpoints")

        if checkpoint_dir.exists():
            for pid in all_pipeline_ids:
                for cp_file in checkpoint_dir.glob(f"{pid}_*.json"):
                    with contextlib.suppress(OSError):
                        cp_file.unlink()

    except Exception:
        logger.warning("清理检查点文件失败", exc_info=True)

    task_service = _safe_get_service("task_service")

    if task_service:
        for task in task_service.get_all_tasks():
            if task.parent_pipeline_id in all_pipeline_ids or task.parent_pipeline_id == thread_id:
                try:
                    task_service.hard_delete_sync(task.id)

                except Exception:
                    logger.warning("删除关联任务 %s 失败", task.id, exc_info=True)

    task_worker = _safe_get_service("task_worker")

    if task_worker:
        for pid in all_pipeline_ids:
            with contextlib.suppress(Exception):
                task_worker.cancel_pipeline(pid)

    _notify_session_update(thread_id, "deleted")

    # 销毁会话级隔离容器（isolated 会话；non_isolated 无容器）
    if _session_ws and _session_iso == "isolated":
        _destroy_session_container(_session_ws)

    return {"message": "线程已删除"}


def _ensure_session(thread_id: str) -> SessionModel | None:
    """确保 thread_id 对应的 session 存在，若不存在则从 thread 数据自动补建。"""

    session = store.get_session(thread_id)

    if session is not None:
        return session

    thread = store.get_thread(thread_id)

    if thread is None:
        return None

    pipeline_ids = thread.get("pipeline_ids", [])

    active_pipeline_id = thread.get("active_pipeline_id", "")

    created_at = thread.get("created_at", "")

    updated_at = thread.get("updated_at", "")

    session = SessionModel(
        session_id=thread_id,
        channel_type="web",
        channel_ref=thread_id,
        pipeline_ids=pipeline_ids,
        active_pipeline_id=active_pipeline_id,
        created_at=_parse_iso_time(created_at) if created_at else None,
        last_active_at=_parse_iso_time(updated_at) if updated_at else None,
        metadata=thread.get("metadata"),
    )

    # 改用 store.set_session() 自动同步 pipeline_ids 并触发持久化

    store.set_session(thread_id, session)

    return session


@router.get(
    "/{thread_id}/state",
    summary="获取线程状态",
)
def get_thread_state(
    thread_id: str,
    _user: dict = Depends(require_auth),
) -> dict:
    """获取线程当前状态。"""

    thread = store.get_thread(thread_id)

    if thread is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )

    return {
        "thread_id": thread_id,
        "state": "active",
        "updated_at": thread["updated_at"],
    }


@router.patch(
    "/{thread_id}/agent",
    response_model=ThreadResponse,
    summary="更新会话绑定的Agent",
)
def update_thread_agent(
    thread_id: str,
    body: dict,
    _user: dict = Depends(require_auth),
) -> ThreadResponse:
    """更新会话绑定的Agent，直接返回完整线程信息。"""

    thread = store.get_thread(thread_id)

    if thread is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )

    agent_id = body.get("agent_id", "")

    # P0-安全: 校验 agent_id 在 registry 中存在，禁止把无效 agent_id 写入线程存储，
    # 否则后续 WS 入口会因解析失败而静默降级到默认 Agent。
    if agent_id:
        # 校验 agent_id 在 registry 中存在，禁止把无效 agent_id 写入线程存储，
        # 否则后续 WS 入口会因解析失败而静默降级到默认 Agent。
        #
        # 注意：GET /api/v1/agents（下拉框数据源）用的是 global_registry 单例，
        # 它懒加载自愈 + 支持热重载；而 service_provider 里的 "agent_registry"
        # 仅在 pipeline 上下文初始化成功时才注册，且不感知热重载。
        # 若此处只查 service_provider，会出现"下拉框能看到、保存却 400"的不一致。
        # 因此以 global_registry 为主，service_provider 兜底，保证校验口径与下拉框一致。
        from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415

        agent_registry = get_global_agent_registry_sync()
        if agent_registry.get(agent_id) is None:
            provider = get_service_provider()
            fallback = provider.get("agent_registry") if provider else None
            agent_registry = fallback or agent_registry

        if agent_registry.get(agent_id) is None:
            raise APIError(
                status_code=400,
                error_code="AGENT_NOT_FOUND",
                message=f"Agent '{agent_id}' 未在系统中注册，禁止绑定（禁止静默降级到默认 Agent）",
            )

    updated_thread = store.update_thread(thread_id, agent_id=agent_id)

    # 切 Agent 只精确更新【会话主管道】那一个 entry 的 agent_id。
    # 注册表以 pipeline_id 为准：只有该 pipeline_id 明确匹配上才改；
    # 同会话下的子任务管道（各自有独立的 target agent，见 task_executor 注册）
    # pipeline_id ≠ 主管道 id，匹配不上，绝不波及——否则子任务被 stop_generation
    # 停止后 idle 重启会解析到主 agent 配置（表现为“停止后再发消息 agent 变了”）。
    _main_pid = (thread.get("active_pipeline_id") or "")
    if agent_id and _main_pid:
        from pipeline.registry import get_engine_registry  # noqa: PLC0415

        _entry = get_engine_registry().get(_main_pid)
        if _entry and _entry.tags.get("agent_id") != agent_id:
            _entry.tags["agent_id"] = agent_id

    return _build_thread_response(updated_thread)


def _register_session_pipeline(
    pipeline_id: str,
    thread_id: str,
    agent_id: str,
    user_id: str = "",
    workspace: str | None = None,
    isolation_level: str | None = None,
) -> None:
    """创建者（会话系统）注册管道到引擎注册表，tags 含 agent_id 和 user_id。

    user_id 写入 tags 后，会随 message_bus → engine state → param_inject 一路下传，

    最终落入 task_submit 写入的任务 metadata，使任务状态变更事件能按用户定向推送。

    workspace / isolation_level 写入 tags 后，随 message_bus → engine.run 播种到
    管道 state（workspace 已有链路；isolation_level 由 session_isolation 插件消费），
    使主对话工具在会话工作空间内工作、会话级隔离模式生效（与任务级隔离解耦）。
    """

    import logging  # noqa: PLC0415

    _logger = logging.getLogger(__name__)

    # agent_id / user_id 为空时从 api_store 补全（与 agent_id 对称，避免历史调用方漏传）

    if not agent_id or not user_id:
        try:
            _t = store.get_thread(thread_id) or {}

            agent_id = agent_id or _t.get("agent_id") or ""

            user_id = user_id or _t.get("user_id") or ""

        except Exception:
            pass

    if not agent_id:
        _logger.error("[session] 注册失败：会话 %s 无 agent_id（api_store 数据错误）", thread_id[:12])

        return

    try:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        _sp = get_service_provider()

        _irt = _sp.get("input_route_table") if _sp else None

        _ort = _sp.get("output_route_table") if _sp else None

        _pr = _sp.get("plugin_registry") if _sp else None

        _logger.info(
            "[session] ServiceProvider: irt=%s ort=%s pr=%s", _irt is not None, _ort is not None, _pr is not None
        )

        # ServiceProvider 未就绪时直接加载配置（兜底）

        if not _irt or not _ort or not _pr:
            from pipeline.config import build_plugin_registry, load_pipeline_config  # noqa: PLC0415

            _cfg = load_pipeline_config("config/pipelines/default.yaml")

            if not _irt:
                _irt = _cfg.input_route_table  # noqa: E701

            if not _ort:
                _ort = _cfg.output_route_table  # noqa: E701

            if not _pr:
                try:
                    _pr = build_plugin_registry(_cfg)  # noqa: E701

                except Exception as _be:
                    _logger.error("[session] build_plugin_registry 失败: %s", _be)  # noqa: E701

            _logger.info(
                "[session] 兜底加载后: irt=%s ort=%s pr=%s", _irt is not None, _ort is not None, _pr is not None
            )

        _reg_tags = {
            "mode": "interactive",
            "channel": "ws",
            "session_id": thread_id,
            "agent_id": agent_id,
            "user_id": user_id,
        }

        if workspace is not None:
            if workspace:
                _reg_tags["workspace"] = workspace
                _reg_tags["isolation_level"] = isolation_level or "non_isolated"
            else:
                # 显式清空：写空值覆盖旧 tags（message_bus 读 "" → 无工作空间）
                _reg_tags["workspace"] = ""
                _reg_tags["isolation_level"] = ""

        from pipeline.registry import get_engine_registry  # noqa: PLC0415

        _result = get_engine_registry().register_pipeline(
            pipeline_id=pipeline_id,
            thread_id=thread_id,
            tags=_reg_tags,
            input_route_table=_irt,
            output_route_table=_ort,
            plugin_registry=_pr,
            services=_sp.get_all_services() if _sp else {},
        )

        if _result is None:
            _logger.error(
                "[session] register_pipeline 返回 None: irt=%s ort=%s pr=%s pid=%s",
                _irt is not None,
                _ort is not None,
                _pr is not None,
                pipeline_id[:12],
            )

        else:
            _logger.info("[session] 注册成功: pid=%s tags=%s", pipeline_id[:12], _result.tags)

    except Exception as exc:
        _logger.error("[session] 管道预注册异常: pipeline=%s error=%s", pipeline_id[:12], exc, exc_info=True)


def restore_session_pipelines() -> int:
    """启动时从 api_store 恢复所有会话的管道注册（会话系统职责）。"""

    import logging  # noqa: PLC0415

    _logger = logging.getLogger(__name__)

    _count = 0

    _skipped = 0

    try:
        for tid, thread in list(store.threads.items()):
            _pid = thread.get("active_pipeline_id") or ""

            if not _pid:
                continue

            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            if get_engine_registry().get(_pid):
                continue  # 已注册

            _agent_id = thread.get("agent_id") or ""

            if not _agent_id:
                _skipped += 1

                continue

            # 恢复会话工作空间与隔离模式到注册 tags（服务重启后不丢失）
            _meta = thread.get("metadata") or {}

            _ws = (_meta.get("workspace") or "").strip() or None

            _iso = (_meta.get("isolation_mode") or "").strip() or None

            _register_session_pipeline(
                _pid,
                tid,
                _agent_id,
                user_id=thread.get("user_id") or "",
                workspace=_ws,
                isolation_level=_iso,
            )

            _count += 1

        if _count:
            _logger.info("[session] 启动恢复 %d 个会话管道", _count)

        if _skipped:
            _logger.warning("[session] %d 个会话因无 agent_id 跳过（数据错误，需前端补选 agent）", _skipped)

    except Exception as exc:
        _logger.warning("[session] 启动恢复会话管道失败: %s", exc)

    return _count

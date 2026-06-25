"""线程与消息相关 API 路由。



提供线程的 CRUD 操作和消息查询接口，所有接口需要 Bearer token 认证。

使用共享的 require_auth 依赖注入统一认证逻辑。

"""



from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from channels.api.deps import APIError, require_auth, validate_pagination

_recovered_user_ids: set[str] = set()





def _notify_session_update(thread_id: str, action: str) -> None:

    """通过 WebSocket 推送会话变更事件。



    在会话 CRUD 操作后调用，通知前端刷新会话列表。

    推送失败不影响主流程。



    Args:

        thread_id: 线程 ID

        action: 操作类型（created / updated / deleted）

    """

    try:

        import asyncio  # noqa: PLC0415

        from channels.websocket.ws_handler import ws_interaction_notifier  # noqa: PLC0415
        from pipeline.stream_bridge import create_targeted_sink  # noqa: PLC0415



        if ws_interaction_notifier and thread_id:

            _sink = create_targeted_sink(ws_interaction_notifier, thread_id)

            if _sink:

                loop = asyncio.get_event_loop()

                if loop.is_running():

                    asyncio.ensure_future(_sink.send_event({

                        "type": "session_update",

                        "data": {"action": action, "thread_id": thread_id},

                    }))

                else:

                    loop.run_until_complete(_sink.send_event({

                        "type": "session_update",

                        "data": {"action": action, "thread_id": thread_id},

                    }))

    except Exception:

        pass

import contextlib  # noqa: E402

from channels.api.memory_store import _parse_iso_time, store  # noqa: E402
from channels.api.models import (  # noqa: E402
    MessageResponse,
    ThreadCreate,
    ThreadResponse,
    ThreadUpdate,
)
from infrastructure.execution_record_storage import ExecutionRecordStorage  # noqa: E402
from infrastructure.service_provider import get_service_provider  # noqa: E402
from infrastructure.session.models import SessionModel  # noqa: E402
from infrastructure.session.session_service import SessionService  # noqa: E402

logger = logging.getLogger(__name__)



# Web API 层不持久化会话状态，使用无 session_dir 的 SessionService

_session_svc = SessionService()



router = APIRouter(prefix="/api/v1/threads", tags=["线程"])





def _get_execution_record_storage() -> ExecutionRecordStorage | None:

    """从 ServiceProvider 获取全局 ExecutionRecordStorage 实例。



    当 ServiceProvider 中未注册时，使用 get_or_create 懒加载。



    Returns:

        ExecutionRecordStorage 实例，服务不可用返回 None

    """

    provider = get_service_provider()



    # 1. 尝试从已注册服务获取

    storage = provider.get("execution_record_storage")

    if storage is not None:

        return storage



    # 2. 懒加载 fallback：ServiceProvider 未注册时直接创建

    return provider.get_or_create(

        "execution_record_storage",

        lambda: ExecutionRecordStorage(

            data_dir=str(Path(__file__).resolve().parent.parent.parent.parent / "data" / "pipelines"),

        ),

    )





def _get_task_service() -> Any:

    """通过 ServiceProvider 获取全局 TaskService 实例。



    Returns:

        TaskService 实例，服务不可用或创建失败时返回 None

    """

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
    """通过 ServiceProvider 安全获取服务实例，失败时返回 None。

    封装 delete_thread 中反复出现的 try/get_service_provider/except 样板。

    Args:
        service_name: 服务在 ServiceProvider 中的注册名

    Returns:
        服务实例，服务不可用或创建失败时返回 None
    """
    try:
        return get_service_provider().get(service_name)
    except Exception:
        return None


def _expand_pipeline_ids_with_task_data(pipeline_ids: list[str]) -> list[str]:
    """利用任务数据的 parent_pipeline_id 链，将 pipeline_ids 扩展为完整集合。

    后端任务系统中，子任务的 parent_pipeline_id 指向父管道，
    通过迭代扩展：已知管道 → 找到 parent_pipeline_id 匹配的任务 → 加入其 pipeline_run_id，
    直到不动点。这样 session.pipeline_ids 就能覆盖所有子管道，
    前端 findPipelineLocation 的 Level 2 查找即可直接命中。

    Args:
        pipeline_ids: 当前已知的管道 ID 列表

    Returns:
        扩展后的完整管道 ID 列表（包含所有子孙管道）
    """
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
    pipeline_ids: list[str], all_tasks: list[Any],
) -> list[str]:
    """使用已获取的任务列表扩展 pipeline_ids（避免重复调用 get_all_tasks）。

    Args:
        pipeline_ids: 当前已知的管道 ID 列表
        all_tasks: 所有任务对象列表

    Returns:
        扩展后的完整管道 ID 列表

    BUG-FIX-fix_20260622_pipeline_ids_order_lost:
    问题根因: 原实现用 set 收集后 list() 返回，set 无序导致主管道
              （原始 pipeline_ids[0]）可能被排到任意位置。前端依赖
              pipelineIds[0] 作为主管道（agentTabStore.getMainPipelineId），
              顺序被打乱后主 Tab 会加载到子管道消息。
    修复方案: 用 list + seen 集合去重，保持"原始顺序在前，扩展项追加在后"，
              确保 pipeline_ids[0] 永远是主管道。
    影响范围: 会话列表/详情的 pipeline_ids 顺序，前端主管道定位
    修复日期: 2026-06-22
    """
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


async def _build_execution_graph(  # noqa: PLR0912,PLR0915

    pipeline_ids: list[str],

    active_pipeline_id: str | None = None,

) -> dict[str, Any]:

    """从 ExecutionRecordStorage + TaskService 构建前端期望的执行图数据。



    遍历 pipeline_ids 中每个 pipeline_run_id，查询其执行记录和摘要，

    将每条 ExecutionRecordData 转换为 BackendNodeData。

    同时通过 TaskService 遍历任务树，将子任务的 pipeline_run_id 也纳入图节点，

    并根据任务的 parent_pipeline_id 构建 edges 表示父子关系。



    Args:

        pipeline_ids: 关联的管道执行 ID 列表

        active_pipeline_id: 当前活跃的管道执行 ID（用于标记主节点）



    Returns:

        包含 nodes 和 edges 的字典，格式为:

        {"nodes": [BackendNodeData, ...], "edges": [BackendEdgeData, ...]}

        如果 ExecutionRecordStorage 不可用或无数据，返回空图

    """

    storage = _get_execution_record_storage()

    if storage is None or not pipeline_ids:

        return {"nodes": [], "edges": []}



    # 通过 TaskService 遍历任务树，收集子任务的 pipeline_run_id

    task_service = _get_task_service()

    pipeline_to_parent: dict[str, str | None] = {}

    all_pipeline_ids = list(pipeline_ids)



    if task_service:

        try:

            all_tasks = await task_service.list_all(limit=500, reverse=False)

            pipeline_to_task: dict[str, Any] = {}

            for t in all_tasks:

                if t.pipeline_run_id:

                    pipeline_to_task[t.pipeline_run_id] = t

            for pid in pipeline_ids:

                pipeline_to_parent[pid] = None

                task = pipeline_to_task.get(pid)

                if task:

                    subtasks = task_service.list_subtasks(task.id)

                    for sub in subtasks:

                        if sub.pipeline_run_id and sub.pipeline_run_id not in pipeline_to_parent:

                            pipeline_to_parent[sub.pipeline_run_id] = sub.parent_pipeline_id

                            all_pipeline_ids.append(sub.pipeline_run_id)

        except Exception:

            logger.warning("通过任务服务查找子管道失败")



    nodes: list[dict[str, Any]] = []

    edges: list[dict[str, Any]] = []



    for pipeline_run_id in all_pipeline_ids:

        try:

            records, _ = storage.list_by_pipeline(pipeline_run_id)

        except Exception:

            logger.warning("查询管道执行记录失败: %s", pipeline_run_id)

            continue



        if not records:

            continue



        # 获取管道摘要信息（状态、耗时等）

        summary = storage.get_summary(pipeline_run_id)



        # 父管道 ID：直接从任务数据中获取

        parent_pipeline_id = pipeline_to_parent.get(pipeline_run_id)



        # 计算时间范围和持续时间

        start_time = records[0].created_at if records else None

        end_time = records[-1].created_at if records else None

        duration = None

        if start_time and end_time:

            try:

                from datetime import datetime  # noqa: PLC0415

                dt_start = datetime.fromisoformat(start_time)

                dt_end = datetime.fromisoformat(end_time)

                duration = (dt_end - dt_start).total_seconds()

            except (ValueError, TypeError):

                pass



        # 从记录中提取 agent 名称

        agent_name = None

        for rec in records:

            if rec.type == "ai" and rec.name:

                agent_name = rec.name

                break



        # 收集日志信息

        logs: list[str] = []

        for rec in records:

            if rec.error:

                logs.append(f"[ERROR] {rec.error}")



        # 构建节点

        node_status = "pending"

        if summary:

            node_status = summary.status or "completed"

        if summary and summary.error:

            node_status = "failed"



        # 获取最后一条 ai 记录的内容作为描述

        description = None

        for rec in reversed(records):

            if rec.type == "ai" and rec.content:

                description = rec.content[:200] + ("..." if len(rec.content) > 200 else "")

                break



        # 提取输入（第一条 user 记录）

        input_data = None

        for rec in records:

            if rec.type == "user" and rec.content:

                input_data = {"content": rec.content[:500]}

                break



        # 提取输出（summary 的 final_output 或最后一条 ai 记录）

        output_data = None

        if summary and summary.final_output:

            output_data = {"result": summary.final_output[:500]}



        node: dict[str, Any] = {

            "id": pipeline_run_id,

            "label": agent_name or pipeline_run_id[:8],

            "status": node_status,

            "type": "task",

            "description": description,

            "input": input_data,

            "output": output_data,

            "logs": logs if logs else None,

            "isMainAgent": pipeline_run_id == active_pipeline_id,

            "agentName": agent_name,

            "parentId": parent_pipeline_id,

            "startTime": start_time,

            "endTime": end_time,

            "duration": duration,

        }

        node = {k: v for k, v in node.items() if v is not None}

        nodes.append(node)



        # 构建边：如果有父管道，建立父子关系

        if parent_pipeline_id and parent_pipeline_id in all_pipeline_ids:

            edge_id = f"edge-{parent_pipeline_id}-{pipeline_run_id}"

            edges.append({

                "id": edge_id,

                "source": parent_pipeline_id,

                "target": pipeline_run_id,

                "label": "subtask",

            })



    return {"nodes": nodes, "edges": edges}





def _build_thread_response(t: dict) -> ThreadResponse:

    """将存储层的线程字典转换为前端期望的 ThreadResponse 格式。



    字段映射：id -> thread_id, title -> intent,

    并添加 current_state、agent_id、pipeline_ids 等字段。



    Args:

        t: 存储层返回的线程字典



    Returns:

        ThreadResponse 与前端 mapThreadToSession 格式对齐

    """

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

    """获取当前用户的所有线程列表，支持分页。



    支持按 session_type 过滤：

    - 不传参数：返回所有线程

    - session_type=main_pipeline：只返回主管道线程



    分页参数：

    - skip：偏移量，默认 0

    - limit：每页数量，默认 20，最大 100



    Returns:

        包含 threads、total、skip、limit 的分页结果字典

    """

    from channels.api.models import ThreadListResponse  # noqa: PLC0415



    validate_pagination(limit, skip)



    threads = store.get_user_threads(_user["sub"])

    if session_type is not None:

        threads = [

            t for t in threads

            if t.get("metadata", {}).get("session_type") == session_type

        ]



    total = len(threads)

    page_items = threads[skip:skip + limit]

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

    """创建新线程。



    Args:

        body: 线程创建请求，包含可选标题



    Returns:

        ThreadResponse 新创建的线程

    """

    # 前端默认创建会话是 lingxi（业务约定），未指定时用 lingxi

    _effective_agent_id = body.agent_id or "lingxi"



    # 自动标记为主管道会话（前端通过主界面创建的都是主管道）

    merged_metadata = body.metadata or {}

    if "session_type" not in merged_metadata:

        merged_metadata["session_type"] = "main_pipeline"



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

    _register_session_pipeline(pipeline_id, thread["id"], _effective_agent_id)



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

    """获取指定线程的详情。



    Args:

        thread_id: 线程 ID



    Returns:

        ThreadResponse 线程详情



    Raises:

        APIError: 线程不存在 (404)

    """

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

    """更新指定线程的标题。



    Args:

        thread_id: 线程 ID

        body: 线程更新请求



    Returns:

        ThreadResponse 更新后的线程



    Raises:

        APIError: 线程不存在 (404)

    """

    thread = store.update_thread(

        thread_id,

        title=body.title or body.intent,

        agent_id=body.agent_id,

        metadata=body.metadata,

    )

    if thread is None:

        raise APIError(

            status_code=404,

            error_code="API_NOTF_2004",

            message="线程不存在",

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

    """删除指定线程及其所有消息和关联的管道执行记录。



    清理范围包括:

    - 线程数据、消息、关联会话

    - 关联管道（含子管道）的执行记录（内存 + YAML 文件）

    - 管道映射（含子管道映射）

    - 管道检查点文件

    - 关联任务（取消运行中任务 + 删除任务数据）

    - 关联工作空间



    Args:

        thread_id: 线程 ID



    Returns:

        删除成功消息



    Raises:

        APIError: 线程不存在 (404)

    """

    session = store.get_session(thread_id)

    pipeline_ids = list(session.pipeline_ids) if session else []



    deleted = store.delete_thread(thread_id)

    if not deleted:

        raise APIError(

            status_code=404,

            error_code="API_NOTF_2004",

            message="线程不存在",

        )



    _recovered_user_ids.discard(_user["sub"])



    # 迭代式收集关联管道（以 all_pipeline_ids 中每个 ID 匹配直到不动点）

    exec_storage = _get_execution_record_storage()

    all_pipeline_ids = set(pipeline_ids)

    prev_size = 0



    while len(all_pipeline_ids) > prev_size:

        prev_size = len(all_pipeline_ids)



        if exec_storage:

            for child_id, root_id in list(exec_storage._pipeline_root_map.items()):

                if root_id in all_pipeline_ids or root_id == thread_id:

                    all_pipeline_ids.add(child_id)



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



    if exec_storage:

        for pid in all_pipeline_ids:

            try:

                exec_storage.delete_by_session(pid)

            except Exception:

                logger.warning("清理管道 %s 执行记录失败", pid, exc_info=True)



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

    return {"message": "线程已删除"}





def _record_to_message_response(  # noqa: PLR0912,PLR0915

    record: Any,

    thread_id: str,

) -> MessageResponse:

    """将 ExecutionRecordData 转换为前端期望的 MessageResponse 格式。



    映射管道执行记录的丰富字段到前端消息模型：

    - type=user → role=user

    - type=ai → role=assistant，含 thinking/toolCalls

    - type=tool → role=tool，含 toolName/toolArgs/toolResult



    Args:

        record: ExecutionRecordData 实例

        thread_id: 线程 ID



    Returns:

        MessageResponse 包含完整字段的响应

    """

    import json as _json  # noqa: PLC0415



    role_map = {"user": "user", "ai": "assistant", "tool": "tool", "system": "system"}

    role = role_map.get(record.type, record.role or "user")



    metadata: dict[str, Any] | None = None

    tool_calls: list[dict[str, Any]] | None = None

    tool_call_id: str | None = None

    tool_name: str | None = None

    tool_args: dict[str, Any] | None = None

    tool_result: Any = None

    tool_error: str | None = None

    agent_name: str | None = None



    _content_stripped = (record.content or "").lstrip()

    _is_system_user_msg = (

        record.type == "user"

        and _content_stripped

        and (

            _content_stripped.startswith("[系统提示]")

            or _content_stripped.startswith("[系统通知]")

            or _content_stripped.startswith("[系统提醒]")

            or _content_stripped.startswith("[触发器通知]")

        )

    )

    if _is_system_user_msg:

        role = "system"

        metadata = {

            "record_type": "system",

            "type": "system",

            "sender_type": "system",

            "notification_level": "info",

            "notification_type": "system_notification",

        }



    if record.type == "ai":

        if record.thinking_content:

            metadata = {"thinkingContent": record.thinking_content}

        if record.name:

            agent_name = record.name

            if metadata is None:

                metadata = {}

            metadata["agentName"] = agent_name

        if record.tool_calls_json:

            try:

                raw_calls = _json.loads(record.tool_calls_json)

                if raw_calls:

                    tool_calls = []

                    for tc in raw_calls:

                        # 兼容两种 tool_call 序列化格式：
                        # 1. 扁平格式（LLMCore 的 RAW_TOOL_CALLS 落盘）：
                        #    {"id", "name", "arguments"/"args"}
                        # 2. OpenAI 嵌套格式（pipe 继承历史经 _reconstruct_tool_calls
                        #    重建后落盘到 tool_calls_json，再随继承记录落盘）：
                        #    {"id", "type": "function", "function": {"name", "arguments"}}
                        #    顶层没有 name/arguments，必须下钻到 function.*。
                        # BUG-FIX-fix_20260625_inherit_tool_card_empty:
                        #   问题根因: 原代码只读扁平顶层 name/arguments，继承记录是嵌套
                        #   结构时 tool_name=""/tool_args={}，前端工具卡片渲染为空。
                        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}

                        args = tc.get("arguments", fn.get("arguments", tc.get("args", {})))

                        if isinstance(args, str):

                            try:

                                args = _json.loads(args)

                            except (_json.JSONDecodeError, TypeError):

                                args = {"raw": args}

                        tool_calls.append({

                            "call_id": tc.get("id", tc.get("call_id", "")),

                            "tool_name": tc.get("name", fn.get("name", tc.get("tool_name", ""))),

                            "tool_args": args if isinstance(args, dict) else {"raw": args},

                            "status": "completed",

                            "container_task_id": record.container_task_id,

                        })

            except (_json.JSONDecodeError, TypeError):

                pass



    elif record.type == "system":

        metadata = {

            "record_type": "system",

            "type": "system",

            "sender_type": "system",

            "notification_level": record.name or "info",

            "notification_type": (record.tool_input or {}).get("notificationType", "task_notification") if isinstance(record.tool_input, dict) else "task_notification",

        }



    elif record.type == "tool":

        tool_call_id = record.tool_call_id

        if record.tool_input and isinstance(record.tool_input, dict):

            tool_name = record.tool_input.get("name", record.name)

            raw_args = record.tool_input.get("args", {})

            tool_args = raw_args if isinstance(raw_args, dict) else None

        else:

            tool_name = record.name

        content_str = record.content or ""

        if content_str:

            try:

                parsed = _json.loads(content_str)

                if isinstance(parsed, dict):

                    output = parsed.get("output", parsed.get("data", parsed))

                    err = parsed.get("error")

                    if err:

                        tool_error = str(err) if err else None

                    tool_result = output if output is not parsed or not err else parsed

                else:

                    tool_result = content_str

            except (_json.JSONDecodeError, TypeError):

                tool_result = content_str



    # 透传前端乐观消息 ID，供前端 initFromAPI 对账（消除重复/丢失）

    if getattr(record, "client_message_id", None):

        if metadata is None:

            metadata = {}

        metadata["client_message_id"] = record.client_message_id



    # 恢复附件信息

    attachments: list[dict[str, Any]] | None = None

    if getattr(record, "attachments_json", None):

        try:

            attachments = _json.loads(record.attachments_json)

        except (_json.JSONDecodeError, TypeError):

            pass



    return MessageResponse(

        id=record.record_id,

        thread_id=thread_id,

        role=role,

        content=record.content or "",

        timestamp=record.created_at,

        sequence=record.sequence,

        parentId=None,

        metadata=metadata,

        toolCalls=tool_calls,

        toolCallId=tool_call_id,

        toolName=tool_name,

        toolArgs=tool_args,

        toolResult=tool_result,

        toolError=tool_error,

        status="completed",

        agentId=None,

        agentName=agent_name,

        durationMs=None,

        attachments=attachments,

    )





def _ensure_session(thread_id: str) -> SessionModel | None:

    """确保 thread_id 对应的 session 存在，若不存在则从 thread 数据自动补建。



    Args:

        thread_id: 线程 ID



    Returns:

        SessionModel 实例，thread 不存在时返回 None

    """

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





def _try_recover_pipeline_ids(  # noqa: PLR0912

    thread_id: str,

    session: SessionModel,

    exec_storage: ExecutionRecordStorage,

) -> list[str]:

    """尝试从 ExecutionRecordStorage 恢复旧会话的 pipeline_ids。



    恢复步骤:

    1. 用 thread_id 作为 pipeline_run_id 直接查询

    2. 扫描管道映射表查找以 thread_id 为根的子管道

    3. 终极 fallback: 全量扫描 summary.thread_id 字段匹配

    4. 恢复成功时自动修复 session 并持久化



    Args:

        thread_id: 线程 ID（旧系统中可能直接作为 pipeline_run_id 使用）

        session: 当前会话模型（pipeline_ids 为空）

        exec_storage: 执行记录存储实例



    Returns:

        恢复到的 pipeline_run_id 列表，恢复失败返回空列表

    """

    recovered: list[str] = []



    # 1. 尝试 thread_id 作为 pipeline_run_id 直接查询

    try:

        records, _ = exec_storage.list_by_pipeline(thread_id)

        if records:

            recovered.append(thread_id)

    except Exception:

        logger.warning("恢复旧会话管道记录失败: thread_id=%s", thread_id)



    # 2. 扫描管道映射表，查找以 thread_id 为根的子管道

    for child_id, root_id in exec_storage._pipeline_root_map.items():

        if root_id == thread_id and child_id != thread_id:

            try:

                child_records, _ = exec_storage.list_by_pipeline(child_id)

                if child_records:

                    recovered.append(child_id)

            except Exception:

                pass



    # 3. 终极 fallback: 扫描所有管道 YAML 文件的 summary.thread_id 字段

    if not recovered:

        try:

            all_summaries = exec_storage.list_all_summaries()

            for s in all_summaries:

                if getattr(s, "thread_id", None) == thread_id and s.run_id:  # noqa: SIM102

                    if s.run_id not in recovered:

                        recovered.append(s.run_id)

        except Exception:

            logger.warning("扫描管道 summary 关联 thread_id 失败: thread_id=%s", thread_id)



    # 4. 恢复成功时自动修复 session 并持久化（合并而非覆盖 pipeline_ids）

    if recovered:

        existing = set(session.pipeline_ids) if session.pipeline_ids else set()

        merged = existing | set(recovered)

        session.pipeline_ids = list(merged)

        if not session.active_pipeline_id:

            session.active_pipeline_id = recovered[-1]

        store.set_session(thread_id, session)

        logger.info(

            "自动恢复旧会话 pipeline_ids (merged): thread=%s, existing=%s, recovered=%s, merged=%s",

            thread_id,

            list(existing),

            recovered,

            session.pipeline_ids,

        )



    return recovered





@router.get(

    "/{thread_id}/messages",

    summary="获取消息列表（支持倒序分页）",

)

def list_messages(

    thread_id: str,

    pipeline_run_id: str | None = Query(default=None, description="按管道运行 ID 过滤消息"),

    limit: int = Query(default=20, ge=1, le=100, description="每页数量"),

    before_sequence: int | None = Query(default=None, description="加载此 sequence 之前的消息（游标分页）"),

    after_sequence: int | None = Query(default=None, description="加载此 sequence 之后的消息（断线补漏）"),

    _user: dict = Depends(require_auth),

) -> dict[str, Any]:

    """获取指定线程的消息列表，支持倒序分页。



    优先从 ExecutionRecordStorage 读取完整的管道执行记录，

    包含 thinking、toolCalls、toolResult 等丰富字段。

    当管道记录不可用时，回退到 MemoryStore 的基础消息。



    当传入 pipeline_run_id 时，仅返回该管道运行实例的消息记录，

    用于子任务标签页加载子管道的对话历史。



    分页逻辑：

    - 不传 before_sequence：返回最后 limit 条消息（倒序初始加载）

    - 传 before_sequence：返回 sequence < before_sequence 的最后 limit 条消息

    - 传 after_sequence：返回 sequence > after_sequence 的所有新消息（断线补漏）



    before_sequence 和 after_sequence 互斥，不能同时使用。



    Args:

        thread_id: 线程 ID

        pipeline_run_id: 可选，管道运行实例 ID

        limit: 每页数量，默认 20，最大 100

        before_sequence: 可选，游标分页的 sequence 边界（向前翻页）

        after_sequence: 可选，断线补漏的 sequence 边界（返回此值之后的新消息）



    Returns:

        包含 messages、total、has_more 的字典



    Raises:

        HTTPException: before_sequence 和 after_sequence 同时使用时返回 400

        APIError: 线程不存在 (404)

    """

    from channels.api.models import MessageListResponse  # noqa: PLC0415



    # 打开会话时同步 agent_id 到注册表 tags（覆盖存量会话的缺失）

    _thread = store.get_thread(thread_id)

    if _thread:

        _aid = _thread.get("agent_id", "")

        if _aid:

            _sync_agent_to_registry_tags(thread_id, _aid)



    # before_sequence 和 after_sequence 不能同时使用

    if before_sequence is not None and after_sequence is not None:

        raise HTTPException(status_code=400, detail="before_sequence 和 after_sequence 不能同时使用")



    exec_storage = _get_execution_record_storage()



    # FEATURE-pipeline_unify: 所有管道（主/子）统一走 pipelineRunId 路径加载消息。

    # - 优先用前端传来的 pipeline_run_id（子管道用 pipelineId，主管道前端也传 pipelineId）

    # - 未传时 fallback 用 thread_id 作为 pipeline_run_id（兼容 thread_id == pipeline_run_id 的旧数据）

    target_pid = pipeline_run_id or thread_id



    if exec_storage and target_pid:

        try:

            records, has_more = exec_storage.list_by_pipeline(

                target_pid,

                limit=limit,

                before_sequence=before_sequence,

                after_sequence=after_sequence,

            )

        except Exception:

            logger.warning("按 pipeline_run_id 查询执行记录失败: %s", target_pid)

            records, has_more = [], False



        msgs = [_record_to_message_response(r, thread_id) for r in records]

        return MessageListResponse(

            messages=msgs,

            total=len(msgs),

            has_more=has_more,

        ).model_dump()



    # exec_storage 不可用：尝试从 MemoryStore 的 _messages 读取（保持向后兼容）

    thread = store.get_thread(thread_id)

    if thread is not None:

        raw_msgs = store.get_messages(thread_id, limit=100000)

        if raw_msgs["messages"]:

            fallback_msgs = [

                MessageResponse(

                    id=m.get("id", ""),

                    thread_id=thread_id,

                    role=m.get("role", "user"),

                    content=m.get("content", ""),

                    timestamp=m.get("timestamp", ""),

                    sequence=m.get("sequence", 0),

                )

                for m in raw_msgs["messages"]

            ]

            # 简单内存分页（保留旧行为）

            filtered = fallback_msgs

            if before_sequence is not None:

                filtered = [m for m in filtered if (m.sequence or 0) < before_sequence]

            if after_sequence is not None:

                filtered = [m for m in filtered if (m.sequence or 0) > after_sequence]

            has_more = len(filtered) > limit

            page = filtered[-limit:] if has_more else filtered

            return MessageListResponse(

                messages=page, total=len(fallback_msgs), has_more=has_more,

            ).model_dump()



    return MessageListResponse(messages=[], total=0, has_more=False).model_dump()





@router.get(

    "/{thread_id}/detail",

    summary="获取线程详情（含执行图数据）",

)

async def get_thread_detail(

    thread_id: str,

    _user: dict = Depends(require_auth),

) -> dict:

    """获取线程详情，包含执行图数据。



    从 SessionModel 获取关联的 pipeline_ids，再从 ExecutionRecordStorage

    查询真实的执行记录，构建为前端期望的 execution_graph 格式返回。



    Args:

        thread_id: 线程 ID



    Returns:

        包含线程基本信息、消息列表和执行图的字典



    Raises:

        APIError: 线程不存在 (404)

    """

    thread = store.get_thread(thread_id)

    if thread is None:

        raise APIError(

            status_code=404,

            error_code="API_NOTF_2004",

            message="线程不存在",

        )



    session = _ensure_session(thread_id)

    pipeline_ids = list(session.pipeline_ids) if session else []

    active_pipeline_id = session.active_pipeline_id if session else None



    # 会话被打开时同步 agent_id 到注册表 tags（覆盖存量会话的缺失）。

    # 旧会话创建时代码还没有 _register_session_pipeline，tags 里没 agent_id。

    _agent_id = thread.get("agent_id", "")

    if _agent_id:

        _sync_agent_to_registry_tags(thread_id, _agent_id)



    if not pipeline_ids and session:

        exec_storage = _get_execution_record_storage()

        if exec_storage:

            pipeline_ids = _try_recover_pipeline_ids(thread_id, session, exec_storage)

            active_pipeline_id = session.active_pipeline_id



    execution_graph = await _build_execution_graph(pipeline_ids, active_pipeline_id)



    exec_storage = _get_execution_record_storage()

    rich_messages: list[dict[str, Any]] = []

    if exec_storage and session and pipeline_ids:

        # 性能优化：遍历调用 list_by_pipeline 收集所有管道记录（保留全量行为，适配新签名）

        try:

            all_records: list[Any] = []

            for pid in pipeline_ids:

                records, _ = exec_storage.list_by_pipeline(pid)

                all_records.extend(records)

        except Exception:

            all_records = []

        if all_records:

            rich_messages = [_record_to_message_response(r, thread_id).model_dump() for r in all_records]



    return {

        "thread_id": thread["id"],

        "intent": thread.get("title") or None,

        "current_state": "active",

        "created_at": thread["created_at"],

        "updated_at": thread["updated_at"],

        "messages": rich_messages,

        "execution_graph": execution_graph,

    }





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





@router.get(

    "/{thread_id}/history",

    summary="获取线程历史（支持分页）",

)

def get_thread_history(  # noqa: PLR0912

    thread_id: str,

    limit: int = Query(default=0, ge=0, le=100, description="每页数量，0 表示返回全部"),

    before_sequence: int | None = Query(default=None, description="游标分页的 sequence 边界（向前翻页）"),

    _user: dict = Depends(require_auth),

) -> dict:

    """获取线程的完整历史记录，支持游标分页。



    分页逻辑：

    - 不传 limit（或 limit=0）：返回全部消息（向后兼容）

    - 传 limit 不传 before_sequence：返回最新 limit 条消息

    - 传 limit + before_sequence：返回 sequence < before_sequence 的最新 limit 条消息



    Args:

        thread_id: 线程 ID

        limit: 每页数量，默认 0（返回全部）

        before_sequence: 游标分页的 sequence 边界



    Returns:

        包含 messages、total、has_more 的分页结果字典



    Raises:

        APIError: 线程不存在 (404)

    """

    thread = store.get_thread(thread_id)

    if thread is None:

        raise APIError(

            status_code=404,

            error_code="API_NOTF_2004",

            message="线程不存在",

        )



    exec_storage = _get_execution_record_storage()

    session = _ensure_session(thread_id)

    rich_messages: list[dict[str, Any]] = []



    pipeline_ids: list[str] = []

    if session:

        if session.pipeline_ids:

            pipeline_ids = list(session.pipeline_ids)

        elif exec_storage:

            pipeline_ids = _try_recover_pipeline_ids(thread_id, session, exec_storage)



    if exec_storage and pipeline_ids:

        # 性能优化：遍历调用 list_by_pipeline 收集所有管道记录（保留全量行为，适配新签名）

        try:

            all_records: list[Any] = []

            for pid in pipeline_ids:

                records, _ = exec_storage.list_by_pipeline(pid)

                all_records.extend(records)

        except Exception:

            all_records = []

        if all_records:

            rich_messages = [_record_to_message_response(r, thread_id).model_dump() for r in all_records]



    # Fallback: exec_storage 无记录时从 MemoryStore 读取

    if not rich_messages:

        raw_msgs = store.get_messages(thread_id, limit=100000)

        if raw_msgs["messages"]:

            for m in raw_msgs["messages"]:

                rich_messages.append({

                    "id": m.get("id", ""),

                    "thread_id": thread_id,

                    "role": m.get("role", "user"),

                    "content": m.get("content", ""),

                    "timestamp": m.get("timestamp", ""),

                    "sequence": m.get("sequence", 0),

                })



    total = len(rich_messages)



    # 无分页时返回全部（向后兼容）

    if limit == 0:

        return {

            "thread_id": thread_id,

            "messages": rich_messages,

            "total": total,

            "has_more": False,

        }



    # 有分页：应用游标分页逻辑（复用 _paginate_messages 的思路）

    # 按 before_sequence 过滤

    if before_sequence is not None:

        filtered = [m for m in rich_messages if m.get("sequence", 0) < before_sequence]

    else:

        filtered = list(rich_messages)



    filtered_total = len(filtered)

    has_more = filtered_total > limit



    # 取最后 limit 条（即最新的 limit 条）

    page = filtered[-limit:] if filtered_total > limit else filtered



    return {

        "thread_id": thread_id,

        "messages": page,

        "total": total,

        "has_more": has_more,

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

    """更新会话绑定的Agent，直接返回完整线程信息。



    性能优化: PATCH 接口返回与 GET 线程详情相同的 ThreadResponse 格式，

    前端无需在 PATCH 之后再发一次 GET 请求获取最新状态。

    """

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

        provider = get_service_provider()

        agent_registry = provider.get("agent_registry") if provider else None

        if agent_registry is None or agent_registry.get(agent_id) is None:

            raise APIError(

                status_code=400,

                error_code="AGENT_NOT_FOUND",

                message=f"Agent '{agent_id}' 未在系统中注册，禁止绑定（禁止静默降级到默认 Agent）",

            )

    updated_thread = store.update_thread(thread_id, agent_id=agent_id)

    # 同步更新注册表 tags：前端切 Agent 时，会话关联的所有管道 tags 也更新 agent_id。

    # 这样引擎层 idle 重启时直接从 tags 拿，覆盖存量会话的缺失。

    if agent_id:

        _sync_agent_to_registry_tags(thread_id, agent_id)

    return _build_thread_response(updated_thread)





@router.get(

    "/messages/search",

    response_model=list[MessageResponse],

    summary="搜索消息",

)

def search_messages(

    query: str = Query(..., description="搜索关键词"),

    limit: int = Query(default=20, ge=1, le=100, description="返回数量"),

    offset: int = Query(default=0, ge=0, description="偏移量"),

    _user: dict = Depends(require_auth),

) -> list[MessageResponse]:

    """在所有线程中搜索包含关键词的消息。



    Args:

        query: 搜索关键词

        limit: 返回数量

        offset: 偏移量



    Returns:

        MessageResponse 匹配的消息列表

    """

    raise APIError(

        status_code=501,

        error_code="NOT_IMPLEMENTED",

        message="消息搜索功能暂未实现，请通过管道执行记录查询",

    )





def _register_session_pipeline(pipeline_id: str, thread_id: str, agent_id: str) -> None:

    """创建者（会话系统）注册管道到引擎注册表，tags 含 agent_id。



    会话系统从 api_store（持久化真源）读取 agent_id，None 时默认 lingxi。

    这是「谁创建谁注册」的职责。引擎层只管转发，从 tags 读 agent_id。

    """

    import logging  # noqa: PLC0415

    _logger = logging.getLogger(__name__)

    # agent_id 为空时从 api_store 读，仍为空则报错（数据错误）

    if not agent_id:

        try:

            _t = store.get_thread(thread_id)

            agent_id = _t.get("agent_id") if _t else ""

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

        _logger.info("[session] ServiceProvider: irt=%s ort=%s pr=%s",

                     _irt is not None, _ort is not None, _pr is not None)

        # ServiceProvider 未就绪时直接加载配置（兜底）

        if not _irt or not _ort or not _pr:

            from pipeline.config import build_plugin_registry, load_pipeline_config  # noqa: PLC0415

            _cfg = load_pipeline_config("config/pipelines/default.yaml")

            if not _irt: _irt = _cfg.input_route_table  # noqa: E701

            if not _ort: _ort = _cfg.output_route_table  # noqa: E701

            if not _pr:

                try: _pr = build_plugin_registry(_cfg)  # noqa: E701

                except Exception as _be: _logger.error("[session] build_plugin_registry 失败: %s", _be)  # noqa: E701

            _logger.info("[session] 兜底加载后: irt=%s ort=%s pr=%s",

                         _irt is not None, _ort is not None, _pr is not None)

        _reg_tags = {

            "mode": "interactive", "channel": "ws",

            "session_id": thread_id,

            "agent_id": agent_id,

        }

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

            _logger.error("[session] register_pipeline 返回 None: irt=%s ort=%s pr=%s pid=%s",

                          _irt is not None, _ort is not None, _pr is not None, pipeline_id[:12])

        else:

            _logger.info("[session] 注册成功: pid=%s tags=%s", pipeline_id[:12], _result.tags)

    except Exception as exc:

        _logger.error("[session] 管道预注册异常: pipeline=%s error=%s", pipeline_id[:12], exc, exc_info=True)





def restore_session_pipelines() -> int:

    """启动时从 api_store 恢复所有会话的管道注册（会话系统职责）。



    遍历 api_store 所有会话，给每个有 active_pipeline_id 的会话注册管道到

    EngineRegistry，agent_id 从 api_store 读取。agent_id 为空的数据错误会话

    跳过并记录。

    """

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

            _register_session_pipeline(_pid, tid, _agent_id)

            _count += 1

        if _count:

            _logger.info("[session] 启动恢复 %d 个会话管道", _count)

        if _skipped:

            _logger.warning("[session] %d 个会话因无 agent_id 跳过（数据错误，需前端补选 agent）", _skipped)

    except Exception as exc:

        _logger.warning("[session] 启动恢复会话管道失败: %s", exc)

    return _count





def _sync_agent_to_registry_tags(thread_id: str, agent_id: str) -> None:

    """会话系统同步 agent_id 到注册表 tags——覆盖存量会话的 agent_id 缺失。



    按 thread_id 和 session_id 两种方式匹配 entry（旧 entry 可能没 thread_id

    但有 session_id tag）。这是会话系统的职责，不是引擎层反查。

    """

    import logging  # noqa: PLC0415

    _logger = logging.getLogger(__name__)

    try:

        from pipeline.registry import get_engine_registry  # noqa: PLC0415

        _registry = get_engine_registry()

        _synced = 0

        for entry in _registry.all_entries().values():

            # 按 thread_id 或 session_id tag 匹配

            _matched = (

                entry.thread_id == thread_id

                or entry.tags.get("session_id") == thread_id

            )

            if not _matched:

                continue

            if entry.tags.get("agent_id") == agent_id:

                continue

            entry.tags["agent_id"] = agent_id

            _synced += 1

            _logger.info("[session] 同步 agent_id: pipeline=%s agent=%s",

                         entry.engine.pipeline_id[:12] if entry.engine else "?", agent_id)

        if _synced == 0:

            _logger.info("[session] 同步 agent_id: 无匹配 entry（thread=%s 可能还没注册管道）", thread_id[:12])

    except Exception as exc:

        _logger.warning("[session] 同步 agent_id 失败: thread=%s error=%s",

                        thread_id[:12], exc)


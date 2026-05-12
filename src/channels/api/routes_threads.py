"""线程与消息相关 API 路由。

提供线程的 CRUD 操作和消息查询接口，所有接口需要 Bearer token 认证。
使用共享的 require_auth 依赖注入统一认证逻辑。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from channels.api.deps import APIError, require_auth


def _notify_session_update(thread_id: str, action: str) -> None:
    """通过 WebSocket 推送会话变更事件。

    在会话 CRUD 操作后调用，通知前端刷新会话列表。
    推送失败不影响主流程。

    Args:
        thread_id: 线程 ID
        action: 操作类型（created / updated / deleted）
    """
    try:
        import asyncio

        from infrastructure.service_provider import get_service_provider
        from pipeline.stream_bridge import WebSocketSink

        _notifier = get_service_provider().get("ws_interaction_notifier")
        if _notifier and thread_id:
            _sink = WebSocketSink(_notifier, thread_id)
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
from channels.api.models import (
    MessageResponse,
    ThreadCreate,
    ThreadResponse,
    ThreadUpdate,
    store,
)
from infrastructure.execution_record_storage import ExecutionRecordStorage
from infrastructure.service_provider import get_service_provider
from infrastructure.session.session_service import SessionService

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
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        return provider.get_or_create(
            "task_service",
            lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService(),
        )
    except Exception:
        return None


# BUG-FIX-fix_20260512_async_list_all: 改为 async def，内部 await task_service.list_all()
async def _build_execution_graph(
    pipeline_ids: list[str],
    active_pipeline_id: str | None = None,
) -> dict[str, Any]:
    """从 ExecutionRecordStorage + TaskService 构建前端期望的执行图数据。

    遍历 pipeline_ids 中每个 pipeline_run_id，查询其执行记录和摘要，
    将每条 ExecutionRecordData 转换为 BackendNodeData。
    同时通过 TaskService 遍历任务树，将子任务的 pipeline_run_id 也纳入图节点，
    并根据任务的 parent_pipeline_id 构建 edges 表示父子关系。

    BUG-FIX-fix_20260507_subtask_not_shown:
    问题根因: session.pipeline_ids 只包含主管道 ID，不包含子任务的管道 ID。
              _build_execution_graph 只遍历传入的 pipeline_ids，
              导致子管道节点和边被遗漏，前端执行图中看不到子任务。
    修复方案: 通过 TaskService 遍历任务树，找到每个管道关联任务的子任务，
              将子任务的 pipeline_run_id 追加到遍历列表中。
              父子管道关系直接从任务的 parent_pipeline_id 字段获取。
    影响范围: 执行图 API（get_thread_detail）
    修复日期: 2026-05-07

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

    # BUG-FIX-fix_20260507_subtask_not_shown:
    # 通过 TaskService 遍历任务树，收集子任务的 pipeline_run_id。
    # 策略：pipeline_run_id → 找到关联任务 → 查找子任务 → 收集子任务的 pipeline_run_id。
    task_service = _get_task_service()
    pipeline_to_parent: dict[str, str | None] = {}
    all_pipeline_ids = list(pipeline_ids)

    if task_service:
        try:
            # BUG-FIX-fix_20260512_async_list_all: 添加 await
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
            records = storage.list_by_pipeline(pipeline_run_id)
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
                from datetime import datetime
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
    response_model=list[ThreadResponse],
    summary="获取线程列表",
)
def list_threads(
    session_type: str | None = Query(default=None, description="按会话类型过滤，如 main_pipeline"),
    _user: dict = Depends(require_auth),
) -> list[ThreadResponse]:
    """获取当前用户的所有线程列表。

    支持按 session_type 过滤：
    - 不传参数：返回所有线程
    - session_type=main_pipeline：只返回主管道线程

    Returns:
        ThreadResponse 列表
    """
    threads = store.get_user_threads(_user["sub"])
    # 按 session_type 过滤：只显示匹配的线程
    if session_type is not None:
        threads = [
            t for t in threads
            if t.get("metadata", {}).get("session_type") == session_type
        ]
    return [_build_thread_response(t) for t in threads]


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
    # 自动标记为主管道会话（前端通过主界面创建的都是主管道）
    merged_metadata = body.metadata or {}
    if "session_type" not in merged_metadata:
        merged_metadata["session_type"] = "main_pipeline"

    thread = store.create_thread(
        user_id=_user["sub"],
        title=body.title,
        agent_id=body.agent_id,
        metadata=merged_metadata,
        intent=body.intent,
    )

    # 桥接基础设施层：以 thread_id 作为 session_id 创建 SessionModel
    session = _session_svc.create(
        channel_type="web",
        channel_ref=thread["id"],
        session_id=thread["id"],
    )
    store.set_session(thread["id"], session)

    # 将 SessionModel 的管道信息同步到线程字典
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
    return _build_thread_response(thread)


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
def delete_thread(
    thread_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, str]:
    """删除指定线程及其所有消息和关联的管道执行记录。

    清理范围包括:
    - 线程数据、消息、关联会话
    - 关联管道的执行记录（内存 + YAML 文件）
    - 管道检查点文件

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

    if pipeline_ids:
        exec_storage = _get_execution_record_storage()
        if exec_storage:
            for pid in pipeline_ids:
                try:
                    exec_storage.delete_by_session(pid)
                except Exception:
                    logger.warning("清理管道 %s 执行记录失败", pid, exc_info=True)

    _notify_session_update(thread_id, "deleted")
    return {"message": "线程已删除"}


def _record_to_message_response(
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
    import json as _json

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
                        args = tc.get("arguments", tc.get("args", {}))
                        if isinstance(args, str):
                            try:
                                args = _json.loads(args)
                            except (_json.JSONDecodeError, TypeError):
                                args = {"raw": args}
                        tool_calls.append({
                            "call_id": tc.get("id", tc.get("call_id", "")),
                            "tool_name": tc.get("name", tc.get("tool_name", "")),
                            "tool_args": args if isinstance(args, dict) else {"raw": args},
                            "status": "completed",
                        })
            except (_json.JSONDecodeError, TypeError):
                pass

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
                    if output is not parsed or not err:
                        tool_result = output
                    else:
                        tool_result = parsed
                else:
                    tool_result = content_str
            except (_json.JSONDecodeError, TypeError):
                tool_result = content_str

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
    )


def _try_recover_pipeline_ids(
    thread_id: str,
    session: SessionModel,
    exec_storage: ExecutionRecordStorage,
) -> list[str]:
    """尝试从 ExecutionRecordStorage 恢复旧会话的 pipeline_ids。

    BUG-FIX-fix_empty_pipeline_ids:
    问题根因: Bug6 修复前创建的旧会话，pipeline_ids 为空，
              但 YAML 文件（如 data/pipelines/{thread_id}.yaml）中存在完整执行记录。
    修复方案: 用 thread_id 作为 pipeline_run_id 查询 ExecutionRecordStorage，
              同时扫描管道映射表查找以 thread_id 为根的子管道，
              最后通过全量扫描 summary.thread_id 作为终极 fallback。
    影响范围: list_messages 等消息查询接口的 fallback 路径。
    修复日期: 2026-05-05

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
        records = exec_storage.list_by_pipeline(thread_id)
        if records:
            recovered.append(thread_id)
    except Exception:
        logger.warning("恢复旧会话管道记录失败: thread_id=%s", thread_id)

    # 2. 扫描管道映射表，查找以 thread_id 为根的子管道
    for child_id, root_id in exec_storage._pipeline_root_map.items():
        if root_id == thread_id and child_id != thread_id:
            try:
                child_records = exec_storage.list_by_pipeline(child_id)
                if child_records:
                    recovered.append(child_id)
            except Exception:
                pass

    # 3. 终极 fallback: 扫描所有管道 YAML 文件的 summary.thread_id 字段
    # BUG-FIX-fix_pipeline_thread_association:
    # 当步骤 1 和 2 都找不到时，通过全量扫描 summary 中的 thread_id 字段关联管道。
    # 这能覆盖管道运行时已正确写入 thread_id 但 session.pipeline_ids 未记录的情况。
    if not recovered:
        try:
            all_summaries = exec_storage.list_all_summaries()
            for s in all_summaries:
                if getattr(s, "thread_id", None) == thread_id and s.run_id:
                    if s.run_id not in recovered:
                        recovered.append(s.run_id)
        except Exception:
            logger.warning("扫描管道 summary 关联 thread_id 失败: thread_id=%s", thread_id)

    # 4. 恢复成功时自动修复 session 并持久化
    if recovered:
        session.pipeline_ids = recovered
        if not session.active_pipeline_id:
            session.active_pipeline_id = recovered[-1]
        store.set_session(thread_id, session)
        logger.info(
            "自动恢复旧会话 pipeline_ids: thread=%s, pipelines=%s",
            thread_id,
            recovered,
        )

    return recovered


def _paginate_messages(
    all_msgs: list[MessageResponse],
    limit: int,
    before_sequence: int | None,
    after_sequence: int | None = None,
) -> dict[str, Any]:
    """对已排序的消息列表进行倒序分页。

    all_msgs 必须按 sequence 正序排列（最旧在前）。

    分页逻辑：
    - before_sequence=None：返回最后 limit 条消息
    - before_sequence=N：返回 sequence < N 的最后 limit 条消息
    - after_sequence=N：返回 sequence > N 的消息（断线补漏，不支持分页）

    before_sequence 和 after_sequence 互斥，不能同时使用。

    Args:
        all_msgs: 按 sequence 正序排列的完整消息列表
        limit: 每页数量
        before_sequence: 游标分页的 sequence 边界（向前翻页）
        after_sequence: 断线补漏的 sequence 边界（返回此值之后的新消息）

    Returns:
        包含 messages、total、has_more 的分页结果字典
    """
    from channels.api.models import MessageListResponse

    total = len(all_msgs)

    # 按 before_sequence 过滤
    if before_sequence is not None:
        filtered = [m for m in all_msgs if m.sequence < before_sequence]
    else:
        filtered = all_msgs

    # 按 after_sequence 过滤（只返回 sequence > after_sequence 的消息）
    if after_sequence is not None:
        filtered = [m for m in filtered if m.sequence > after_sequence]

    filtered_total = len(filtered)

    # 判断是否还有更多消息
    has_more = filtered_total > limit

    # 取最后 limit 条（即最新的 limit 条）
    page = filtered[-limit:] if filtered_total > limit else filtered

    return MessageListResponse(
        messages=page,
        total=total,
        has_more=has_more,
    ).model_dump()


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
    from channels.api.models import MessageListResponse

    # before_sequence 和 after_sequence 不能同时使用
    if before_sequence is not None and after_sequence is not None:
        raise HTTPException(status_code=400, detail="before_sequence 和 after_sequence 不能同时使用")

    thread = store.get_thread(thread_id)
    if thread is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )

    exec_storage = _get_execution_record_storage()

    # DEBUG: 诊断历史消息不显示问题
    logger.info(
        "[list_messages] thread=%s pipeline_run_id=%s exec_storage=%s",
        thread_id[:12] if thread_id else "?",
        (pipeline_run_id or "")[:12] if pipeline_run_id else "None",
        "yes" if exec_storage else "None",
    )

    session = store.get_session(thread_id)
    logger.info(
        "[list_messages] session=%s pipeline_ids=%s",
        "yes" if session else "None",
        session.pipeline_ids if session else "N/A",
    )

    # 按指定 pipeline_run_id 加载子管道消息
    if pipeline_run_id and exec_storage:
        try:
            records = exec_storage.list_by_pipeline(pipeline_run_id)
            if records:
                records.sort(key=lambda r: (r.sequence, r.created_at or ""))
                msgs = [_record_to_message_response(r, thread_id) for r in records]
                return MessageListResponse(
                    messages=msgs,
                    total=len(msgs),
                    has_more=False,
                ).model_dump()
        except Exception:
            logger.warning("按 pipeline_run_id 查询执行记录失败: %s", pipeline_run_id)
        return MessageListResponse(
            messages=[],
            total=0,
            has_more=False,
        ).model_dump()

    session = store.get_session(thread_id)

    # 确定要查询的 pipeline_ids：优先使用已有值，否则尝试从 YAML 文件恢复
    pipeline_ids: list[str] = []
    if session and session.pipeline_ids:
        pipeline_ids = list(session.pipeline_ids)
    elif exec_storage and session:
        # BUG-FIX: 旧会话 pipeline_ids 为空时，尝试从 YAML 文件恢复
        pipeline_ids = _try_recover_pipeline_ids(thread_id, session, exec_storage)

    if exec_storage and pipeline_ids:
        all_records: list[Any] = []
        pipeline_order = {pid: idx for idx, pid in enumerate(pipeline_ids)}
        for pipeline_id in pipeline_ids:
            try:
                records = exec_storage.list_by_pipeline(pipeline_id)
                all_records.extend(records)
            except Exception:
                logger.warning("查询管道执行记录失败: %s", pipeline_id)

        if all_records:
            all_records.sort(key=lambda r: (
                pipeline_order.get(r.pipeline_run_id, 999),
                r.sequence,
                r.created_at or "",
            ))
            all_msgs = [_record_to_message_response(r, thread_id) for r in all_records]
            return _paginate_messages(all_msgs, limit, before_sequence, after_sequence)

    # Fallback: pipeline_ids 为空时，直接用 thread_id 作为 pipeline_run_id 尝试加载
    # 解决重启后 session.pipeline_ids 未持久化或恢复失败的场景
    if exec_storage and not pipeline_ids:
        try:
            records = exec_storage.list_by_pipeline(thread_id)
            if records:
                records.sort(key=lambda r: (r.sequence, r.created_at or ""))
                all_msgs = [_record_to_message_response(r, thread_id) for r in records]
                if session:
                    session.pipeline_ids = [thread_id]
                    store.set_session(thread_id, session)
                return _paginate_messages(all_msgs, limit, before_sequence, after_sequence)
        except Exception:
            logger.warning("Fallback: 用 thread_id 查询执行记录失败: %s", thread_id)

    # 回退到 MemoryStore 基础消息
    messages = store.get_messages(thread_id)
    all_msgs = [
        MessageResponse(
            id=m.get("id", ""),
            thread_id=thread_id,
            role=m.get("role", "user"),
            content=m.get("content", ""),
            timestamp=m.get("created_at", ""),
            sequence=m.get("sequence", 0),
            parentId=m.get("parentId"),
            metadata=m.get("metadata"),
            toolCalls=m.get("toolCalls"),
            toolCallId=m.get("toolCallId"),
            toolName=m.get("toolName"),
            toolArgs=m.get("toolArgs"),
            toolResult=m.get("toolResult"),
            toolError=m.get("toolError"),
            status=m.get("status"),
            agentId=m.get("agentId"),
            agentName=m.get("agentName"),
            durationMs=m.get("durationMs"),
        )
        for m in messages
    ]
    return _paginate_messages(all_msgs, limit, before_sequence, after_sequence)


@router.get(
    "/{thread_id}/detail",
    summary="获取线程详情（含执行图数据）",
)
# BUG-FIX-fix_20260512_async_list_all: 改为 async def 以支持 await _build_execution_graph
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

    session = store.get_session(thread_id)
    pipeline_ids = list(session.pipeline_ids) if session else []
    active_pipeline_id = session.active_pipeline_id if session else None

    # BUG-FIX-fix_pipeline_thread_association: 尝试恢复空 pipeline_ids
    if not pipeline_ids and session:
        exec_storage = _get_execution_record_storage()
        if exec_storage:
            pipeline_ids = _try_recover_pipeline_ids(thread_id, session, exec_storage)
            active_pipeline_id = session.active_pipeline_id

    # BUG-FIX-fix_20260512_async_list_all: 添加 await
    execution_graph = await _build_execution_graph(pipeline_ids, active_pipeline_id)

    exec_storage = _get_execution_record_storage()
    rich_messages: list[dict[str, Any]] = []
    if exec_storage and session and pipeline_ids:
        pipeline_order = {pid: idx for idx, pid in enumerate(pipeline_ids)}
        all_records: list[Any] = []
        for pid in pipeline_ids:
            try:
                records = exec_storage.list_by_pipeline(pid)
                all_records.extend(records)
            except Exception:
                pass
        if all_records:
            all_records.sort(key=lambda r: (
                pipeline_order.get(r.pipeline_run_id, 999),
                r.sequence,
                r.created_at or "",
            ))
            rich_messages = [_record_to_message_response(r, thread_id).model_dump() for r in all_records]

    if not rich_messages:
        messages = store.get_messages(thread_id)
        rich_messages = [
            {
                "id": m.get("id", ""),
                "thread_id": thread_id,
                "role": m.get("role", "user"),
                "content": m.get("content", ""),
                "timestamp": m.get("created_at", ""),
            }
            for m in messages
        ]

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
    summary="获取线程历史",
)
def get_thread_history(
    thread_id: str,
    _user: dict = Depends(require_auth),
) -> dict:
    """获取线程的完整历史记录。"""
    thread = store.get_thread(thread_id)
    if thread is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )

    exec_storage = _get_execution_record_storage()
    session = store.get_session(thread_id)
    rich_messages: list[dict[str, Any]] = []

    # BUG-FIX-fix_pipeline_thread_association: 尝试恢复空 pipeline_ids
    pipeline_ids: list[str] = []
    if session:
        if session.pipeline_ids:
            pipeline_ids = list(session.pipeline_ids)
        elif exec_storage:
            pipeline_ids = _try_recover_pipeline_ids(thread_id, session, exec_storage)

    if exec_storage and pipeline_ids:
        pipeline_order = {pid: idx for idx, pid in enumerate(pipeline_ids)}
        all_records: list[Any] = []
        for pid in pipeline_ids:
            try:
                records = exec_storage.list_by_pipeline(pid)
                all_records.extend(records)
            except Exception:
                pass
        if all_records:
            all_records.sort(key=lambda r: (
                pipeline_order.get(r.pipeline_run_id, 999),
                r.sequence,
                r.created_at or "",
            ))
            rich_messages = [_record_to_message_response(r, thread_id).model_dump() for r in all_records]

    if not rich_messages:
        messages = store.get_messages(thread_id)
        rich_messages = [
            {
                "id": m.get("id", ""),
                "thread_id": thread_id,
                "role": m.get("role", "user"),
                "content": m.get("content", ""),
                "timestamp": m.get("created_at", ""),
            }
            for m in messages
        ]

    return {
        "thread_id": thread_id,
        "messages": rich_messages,
        "total": len(rich_messages),
    }


@router.patch(
    "/{thread_id}/agent",
    summary="更新会话绑定的Agent",
)
def update_thread_agent(
    thread_id: str,
    body: dict,
    _user: dict = Depends(require_auth),
) -> dict:
    """更新会话绑定的Agent。"""
    thread = store.get_thread(thread_id)
    if thread is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )
    agent_id = body.get("agent_id", "")
    store.update_thread(thread_id, agent_id=agent_id)
    return {
        "thread_id": thread_id,
        "agent_id": agent_id,
        "message": "Agent 已更新",
    }


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
    results = store.search_messages(query=query, limit=limit, offset=offset)
    return [
        MessageResponse(
            id=m["id"],
            thread_id=m["thread_id"],
            role=m["role"],
            content=m["content"],
            timestamp=m["created_at"],
        )
        for m in results
    ]

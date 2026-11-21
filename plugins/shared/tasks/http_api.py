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

import asyncio
import logging
import os
import sys
import time
from typing import Any

from _root_api import state_fields  # noqa: PLC0415 — plugins/shared 平铺模块（裸名导入先例 tenant_data）
from pydantic import BaseModel, Field

# 共享层样板（plugins/shared/http_json.py）入 sys.path 后裸名导入。
# 本文件在 plugins/shared/tasks/ 下，共享根 = 上一层（写 ..\.. 落到 plugins，
# _root_api/http_json 永不可达——垫错层即死自举，2026-10-27 与 reconcile
# 自举同刀修正）。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
# 归一判重：他写入方可能以变体拼写（大小写/分隔符）注入同一根，裸字符串 in 会被
# 击穿产生重复条目——与消费方（测试 abspath 计数）同一归一口径。
if not any(os.path.abspath(p or ".") == _SHARED_ROOT for p in sys.path):
    sys.path.insert(0, _SHARED_ROOT)
# 共享时间戳单点（task.ended_at 落 state，task_evaluate 同源）
import events as _task_events  # noqa: E402 — 同插件平铺模块（reconcile/server 同款裸名导入）
from _root_api import (  # noqa: E402
    TaskBirthError,  # noqa: E402
    decode_body as _decode_body,
    decode_kernel_token as _decode_kernel_token,  # noqa: E402
    json_response as _json_response,
    now_iso_utc as _now_iso,  # noqa: E402
    ok as _ok,
)

logger = logging.getLogger(__name__)

# 核心流程失败错误码（HTTP 500 面，非入参非法）：两类都可能已有副作用落库
# ——DISPATCH_FAILED 管道未创建；WORKSPACE_PREPARE_FAILED 工作空间建不出来，
# 任务记录已保留并标 failed（ADR 2026-10-17）。
_CORE_FAILURE_ERROR_CODES = frozenset({"DISPATCH_FAILED", "WORKSPACE_PREPARE_FAILED"})

# 子任务挂号键前缀（task_submit 写入提交者管道，值非空 = 终态回执未写）。
_PENDING_SUBTASK_PREFIX = "task.subtasks_pending."
# 子任务 run 已到终态（task 投影可能因修复前残留未随写）。
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed"})
# 子任务仍在执行/等待：父挂起是在等它，不能对其重复派发父续跑轮。
_ACTIVE_RUN_STATUSES = frozenset({"running", "pending", "suspended"})


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
    acceptance_criteria: dict[str, Any] | None = None  # 验收标准（task_submit 原生参数透传；评测批派发用）


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
# caller 身份解析（内核 0.2 开发期 token；解码公共实现在 kernel_token）
# ════════════════════════════════════════════════════════════


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

# 正门注入的本插件 AgentOSPlugin 实例（server.py 顶层接线）。合宿模式下
# __main__ 是 host.py、无 plugin 全局，独占路径失效——注入路径为主通道。
_plugin_instance: Any = None


def set_plugin_instance(instance: Any | None) -> None:
    """注入本插件 AgentOSPlugin 实例；传 None 清除（回到独占探测路径）。"""
    global _plugin_instance  # noqa: PLW0603
    _plugin_instance = instance


def _capability(name: str) -> Any:
    """取内核能力句柄（正门注入实例优先，独占 ``__main__.plugin`` 兜底）。

    独占模式下本插件由 ``python server.py`` 启动，SDK 注入 capabilities 的
    实例是 ``__main__.plugin``；``import server`` 会重新执行 server.py 顶层
    并新建第二个空 AgentOSPlugin（capabilities 永远为空），故独占路径直接
    取 ``__main__``。合宿模式下 ``__main__`` 是 host.py（无 plugin 全局），
    由 server.py 顶层经 :func:`set_plugin_instance` 正门注入成员实例。
    测试可 monkeypatch 本函数（test_tasks_plugin 既有做法）。
    """
    instance: Any = _plugin_instance
    if instance is None:
        try:
            import __main__  # noqa: PLC0415

            instance = __main__.plugin  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — 路径失效统一报未注入
            raise KeyError(f"capability not injected: {name}") from exc
    try:
        return instance.get_capability(name)
    except Exception as exc:  # noqa: BLE001 — 保持既有 KeyError 契约
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


async def _stop_task_pipeline(task_id: str) -> str:
    """取消面引擎停止：挂起任务管道，返回被停 run 的 run_id（空 = 无活跃运行）。

    与 _suspend_task_pipeline（pause/级联面：bool 吞错）契约刻意不同——
    停止是终态写面入口（谁终态谁随写），引擎调用失败必须如实上抛：
    活任务停止失败不得谎报已停止。空串返回 = 引擎侧无活跃运行
    （孤儿投影形态），停止照常接受，state 行终态随写照走。
    """
    handle = _capability("pipeline-executor")
    resp = await handle.call("suspend_pipeline", {"pipeline_id": task_id})
    return str((resp or {}).get("run_id") or "") if isinstance(resp, dict) else ""


def _frontend_emitter() -> Any:
    """frontend.emit 发射器（取消终态 WS 腿）。

    经 :func:`_capability` 取 frontend 能力句柄包成 SDK FrontendEmitter；
    能力未注入（旧内核）或句柄失效 → None（调用方跳过推送，不报错）。
    """
    from agentos_plugin_sdk.capability import FrontendEmitter  # noqa: PLC0415

    try:
        return FrontendEmitter(_capability("frontend"))
    except Exception:  # noqa: BLE001 — 能力缺席降级不推送（观测出口契约）
        return None


def _task_cancelled_ws_frame(
    task_id: str, row: dict[str, Any] | None
) -> tuple[str, dict[str, Any]] | None:
    """停止终态 → 前端 WS 帧（task_cancelled 发射源，P-19/U-1 接线）。

    帧名 ``task_cancelled`` 是前端事件词汇（ws_events.yaml 契约）原样保留，
    与任务域 stopped 写值互不干扰（同内核 run 域署名词 task_cancelled 的
    保留逻辑，ADR 2026-10-30）。路由与负载契约与 events.task_completed_ws_frame
    同构（路由坐标 = 任务自身管道，task_deleted 先例；thread→user 反查即
    目标 user 过滤）；非任务行（聊天会话/墓碑）不出帧——用户停止普通会话
    不得误报任务停止。负载 status = stopped（任务域词汇收敛后值）。
    """
    if not _task_events.is_task_state(row or {}):
        return None
    payload: dict[str, Any] = {
        "task_id": task_id,
        "title": str((row or {}).get("task.goal") or ""),
        "thread_id": task_id,
        "pipeline_id": task_id,
        "user_id": str((row or {}).get("task.submitted_by") or ""),
        "status": "stopped",
    }
    return ("task_cancelled", payload)


async def _emit_task_cancelled_ws(task_id: str, row: dict[str, Any] | None) -> None:
    """推送 task_cancelled 前端帧（fire-and-forget；缺席/失败静默降级）。"""
    frame = _task_cancelled_ws_frame(task_id, row)
    if frame is None:
        return
    emitter = _frontend_emitter()
    if emitter is None or not emitter.available:
        return
    try:
        await emitter.emit(*frame)
    except Exception as exc:  # noqa: BLE001 — 观测推送失败不影响取消主流程
        logger.warning(
            "[tasks http] task_cancelled 前端帧推送失败 | task_id=%s | err=%s",
            task_id,
            exc,
        )


async def _write_cancel_terminal(task_id: str) -> None:
    """停止终态随写：task.status=stopped + task.ended_at 落 state 行。

    身份门（ADR 2026-11-06 D5 N1b）：行缺失或非任务管道
    （:func:`events.is_task_pipeline_row` 判据）→ INFO 留痕拒绝——主管道
    （会话）不该有 task.* 自身字段，停止动作本身不因此失败；写面对未知
    pipeline_id 是 upsert，行缺失照写会凭空造幽灵任务行。

    pipeline-state.update 单通道（与评估闸门 task_evaluate 终态写同面，
    state 单一真值）。词汇 = ``stopped``（暂停放那里，可恢复——ADR
    2026-10-30-task-status-authority-vocabulary-convergence：cancelled 任务域
    退役，用户停止 = stopped，与等子任务/重启排空三态同词；stopped 可重入，
    被放那的任务允许被新 run/子任务结算自然唤醒，防自动复活由恢复守卫
    （拦真终态 completed/failed）接管）。失败向上抛（调用方如实报错——
    终态没落账就谎报停止成功 = 本缺陷本体）。

    owned 镜像已退役（ADR 2026-11-02-state-key-governance）：只写自身投影，
    父会话面板 join 子行取真值。

    task_cancelled 前端帧（P-19/U-1，帧名为前端事件词汇、原样保留）在终态
    落账成功后随发（同事务面：写面异常上抛即不发射）；引擎 run.cancelled
    事件回流的重复停止由 :func:`follow_write_cancel_terminal` 的幂等省写
    去重。
    """
    row = await _read_task_row(task_id)
    if row is None or not _task_events.is_task_pipeline_row(row):
        logger.info(
            "[tasks http] 停止终态随写跳过（行缺失/非任务管道——不造幽灵行）"
            "| task_id=%s",
            task_id,
        )
        return
    handle = _capability("pipeline-state")
    await handle.call(
        "update",
        {
            "pipeline_id": task_id,
            "fields": {"task.status": "stopped", "task.ended_at": _now_iso()},
        },
    )
    await _emit_task_cancelled_ws(task_id, row)


async def _read_task_row(task_id: str) -> dict[str, Any] | None:
    """读任务管道的 state 聚合行（父锚点来源）。

    行内 ``lineage.parent_pipeline_id`` = 父管道锚点（提交者管道）——
    task_submit 登记面用它写 ``task.owned.<子id>.*`` 登记键。
    """
    try:
        handle = _capability("pipeline-state")
        rows = await handle.call("list", {})
        if not isinstance(rows, list):
            return None
        row = next(
            (
                r
                for r in rows
                if isinstance(r, dict) and str(r.get("pipeline_id") or "") == task_id
            ),
            None,
        )
        return row if isinstance(row, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tasks http] 任务行读面故障 | task_id=%s | err=%s", task_id, exc)
        return None


def _pending_child_ids(row: dict[str, Any] | None) -> list[str]:
    """父行上仍持有非空值的子任务挂号 id（空值 = 已回执墓碑）。"""
    if not isinstance(row, dict):
        return []
    child_ids: list[str] = []
    for key, value in row.items():
        k = str(key)
        if not k.startswith(_PENDING_SUBTASK_PREFIX) or value in (None, ""):
            continue
        child_id = k[len(_PENDING_SUBTASK_PREFIX) :]
        if child_id:
            child_ids.append(child_id)
    return child_ids


async def _stale_pending_clear_fields(row: dict[str, Any] | None) -> dict[str, Any]:
    """已终态子任务的挂号键 → 恢复 overlay 清除字段（null = 已回执）。

    父任务挂起等子任务时，子 run 已 completed/failed 但 task.status 因
    「评估结论未投影终态」等修复前残留停在 running，父的
    ``task.subtasks_pending.<child>`` 就永远不会被终态回执清除（装机
    2026-11-11 e475/885b 实证：子 run 08:36 已 completed，父 09:47 恢复后
    再挂起，双停滞 >90min）。恢复前把这些已终态子任务的挂号键加入 overlay
    ——overlay 在 state 恢复合并之后应用，父续跑轮不再被陈旧挂号键二次挂起。

    判据取子行 ``task.status`` 终态 **或** ``run_status`` 终态：后者覆盖
    子任务投影未随写的存量残留；子行缺失/读面故障时不动（保守，不猜）。
    """
    child_ids = _pending_child_ids(row)
    if not child_ids:
        return {}
    try:
        handle = _capability("pipeline-state")
        rows = await handle.call("list", {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tasks http] 陈旧挂号键读面故障（本轮不清理） | err=%s", exc)
        return {}
    if not isinstance(rows, list):
        return {}
    by_pipeline = {
        str(r.get("pipeline_id") or ""): r for r in rows if isinstance(r, dict)
    }
    fields: dict[str, Any] = {}
    for child_id in child_ids:
        child = by_pipeline.get(child_id)
        if not isinstance(child, dict):
            continue
        task_status = str(child.get("task.status") or "")
        run_status = str(child.get("run_status") or "")
        if task_status in _TERMINAL_RUN_STATUSES or run_status in _TERMINAL_RUN_STATUSES:
            fields[f"{_PENDING_SUBTASK_PREFIX}{child_id}"] = None
    return fields


def _has_active_pending_child(
    row: dict[str, Any], by_pipeline: dict[str, dict[str, Any]]
) -> bool:
    """父行是否仍在等一个未终态子任务（活跃子任务不重复拉起父续跑轮）。"""
    for child_id in _pending_child_ids(row):
        child = by_pipeline.get(child_id)
        if isinstance(child, dict) and str(child.get("run_status") or "") in _ACTIVE_RUN_STATUSES:
            return True
    return False


async def follow_write_cancel_terminal(task_id: str) -> None:
    """停止终态随写（事件路径入口）：自身投影。

    事件路径 = 内核 ``run.cancelled`` 域事件（用户停止 / 任务取消 / 删除
    署名，事件名属内核 run 域词汇原样）。与 API 停止路径
    （:func:`_write_cancel_terminal`）同语义：自身投影未达该终态才写
    （幂等省写）。写面故障降级告警——不吞事件派生。

    身份门（ADR 2026-11-06 D5 N1，2026-11-06 09:02:01 事故根因）：行缺失或
    非任务管道（:func:`events.is_task_pipeline_row` 判据）→ INFO 留痕跳过
    ——停止链路不得把幽灵 ``task.status=stopped`` 写在会话主管道上（行缺失
    照写 = upsert 造幽灵任务行）。
    owned 镜像已退役（ADR 2026-11-02-state-key-governance）。

    task_cancelled 前端帧（帧名为前端事件词汇原样）仅在真转移（未 stopped →
    stopped 写成功）后随发：REST 停止面已发过一次，回流重复（已 stopped →
    省写）不双发；写面故障与非任务行不发射。
    """
    if not task_id:
        return
    row = await _read_task_row(task_id)
    if row is None or not _task_events.is_task_pipeline_row(row):
        logger.info(
            "[tasks http] run.cancelled 停止随写跳过（行缺失/非任务管道——"
            "会话主管道不落幽灵任务标记）| task_id=%s",
            task_id,
        )
        return
    current = str(row.get("task.status") or "")
    if current != "stopped":
        try:
            handle = _capability("pipeline-state")
            await handle.call(
                "update",
                {
                    "pipeline_id": task_id,
                    "fields": {"task.status": "stopped", "task.ended_at": _now_iso()},
                },
            )
            await _emit_task_cancelled_ws(task_id, row)
        except Exception as exc:  # noqa: BLE001 — 写面故障降级告警（不吞事件派生）
            logger.warning(
                "[tasks http] 停止终态自身投影随写失败 | task_id=%s | err=%s", task_id, exc
            )


# run 簿记在飞时允许纠偏的投影词面：这些值都表示"上一轮已结束"，与
# ``run_status=running`` 直接矛盾（2026-11-10 装机实证：resume 后 run 在飞，
# task.status 停在 stopped，面板显示已停止且 submit 口被 400 拒）。
_STALE_PROJECTION_STATUSES = frozenset({"stopped", "failed", "timeout", "pending", ""})


def derive_task_status(row: dict[str, Any]) -> str:
    """任务状态投影纠偏：run 簿记在飞时以 ``run_status`` 为真值。

    任务状态是域投影，但投影会与实际脱节——典型路径：resume 续跑轮从快照
    恢复出的 ``task.status`` 仍是上一轮的终态（stopped），复位写被后续状态
    持久化顶掉；结果是 run 在飞而面板/守卫读到 stopped（submit 直接 400）。

    判据只碰"明确矛盾"的组合：``run_status == "running"`` 且投影是上一轮
    终态词面 → 返回 ``running``。其余（无 run 簿记 / suspended / 投影已是
    running / 用户侧 completed）原样返回，不猜不改语义。
    """
    projected = str(row.get("task.status") or "")
    if str(row.get("run_status") or "") == "running" and (
        projected in _STALE_PROJECTION_STATUSES
    ):
        return "running"
    return projected or "pending"


async def _read_task_status(task_id: str) -> str | None:
    """读任务管道的 task.status 投影（恢复链仲裁依据）。

    读面故障（能力缺席/调用失败）→ None：仲裁降级放行（恢复链是主路径，
    投影读失败不得拦死恢复；分歧场景由启动调和兜底）。
    """
    try:
        handle = _capability("pipeline-state")
        rows = await handle.call("list", {})
        if not isinstance(rows, list):
            return None
        row = next(
            (
                r
                for r in rows
                if isinstance(r, dict) and str(r.get("pipeline_id") or "") == task_id
            ),
            None,
        )
        if row is None:
            return None
        # 与列表/详情同源纠偏：run 在飞而投影停在上一轮终态时以 running 裁决
        # ——恢复守卫据实读数（stopped 被当"可恢复"放行、submit 被当"不可提交"
        # 拒绝，两边都错在读了僵尸投影）。
        return derive_task_status(row) or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tasks http] 投影读面故障，恢复仲裁降级放行 | task_id=%s | err=%s", task_id, exc)
        return None


async def _resume_task_pipeline(
    task_id: str, user_id: str = "", allow_crash_swept: bool = False
) -> tuple[bool, bool, str]:
    """恢复任务 = resume_pipeline（内核按 run 状态簿记或拉起续跑轮）。

    携带终态键复位 overlay（B13①，defd5b555"恢复重置"同族）：恢复投影
    （registry 快照/checkpoint/pipeline_state 表）可能残留上一 run 的
    task.status 终态，stop_check 缓存路径会在续跑轮第一轮即收束。内核对
    overlay 只做不透明透传并在恢复合并之后应用（显式派发指令 > 恢复基线）。

    终态防线（幽灵续跑改判事故 2026-10-19 修复④B；U13 前置仲裁下沉至此，
    覆盖 resume_task/resume_project 全部调用方；ADR 2026-10-30 扩为真终态
    全集）：派发前读权威投影行 task.status（_read_task_status 同源读面）——
    真终态（completed/failed）拒绝派发：completed 按钮语义是"恢复执行"，
    裸恢复正是幽灵轮形态（返工重开是 task_manage continue 带指令的职责，
    即修复④A）；failed 不可恢复执行，重试走重新提交（submit 允许 failed）
    或 task_manage continue。timeout 非真终态（可重入），照常恢复且 overlay
    追加清失败注记（pipeline-state null 即清除语义，重试轮不顶上一轮失败
    原因展示）；其余状态与投影读面故障（_read_task_status 降级 None）一律
    现行为放行。内核 resume_pipeline 能力不加任何任务语义守卫（内核零任务
    知识，架构公理）。

    failed 窄放行（ADR 2026-11-12-crash-swept-running-tasks-auto-relaunch）：
    ``allow_crash_swept=True`` 且投影行 ``run_stop_reason`` 恰为内核崩溃清扫
    署名 ``crash_swept``（重启前 running 被打断）时放行 failed——仅启动自动
    恢复通道使用；缺任一条件照旧拒绝，幽灵续跑防线对其余全部路径原样。

    Returns:
        (resumed, submitted, error)：resumed = 内核确认恢复语义生效（run 在飞/
        审批挂起翻面/续跑轮已派发，run_id 非空或 dispatched）；submitted =
        内核实际拉起了续跑轮（dispatched=true）；error = 显式失败消息
        （非空 = 拒绝派发，HTTP 面如实转 409 语义错误）。空 run_id 且未
        派发且无 error = 无可恢复对象（簿记 failed 等终态且派发通道缺席）
        → (False, False, "")——此前空 run_id 被当作成功（假恢复，2026-10-10
        装机 B76 实证）。
    """
    # 恢复前置读面单点（行级）：行缺席/读面故障 → task_status None = 降级放行
    # （恢复链是主路径，与既有 _read_task_status 降级口径一致）。
    row = await _read_task_row(task_id)
    task_status = str((row or {}).get("task.status") or "") or None
    # 恢复 overlay 身份门（ADR 2026-11-06 D5 N7）：会话主管道行（伪任务行，
    # 可带幽灵残留）不是任务 → 拒绝派发——overlay 的 task.status=running
    # 正是会话残留的疑似写入面之一。
    if row is not None and not _task_events.is_task_pipeline_row(row):
        message = (
            f"目标不是任务管道（会话主管道无任务字段），拒绝恢复: {task_id}"
        )
        logger.warning(
            "[tasks http] 拒绝恢复会话主管道行（恢复 overlay 身份门） | task_id=%s",
            task_id,
        )
        return False, False, message
    if task_status == "completed":
        message = f"任务已完成，无可恢复对象；如需返工请由上级带指令继续: {task_id}"
        logger.warning("[tasks http] 拒绝裸恢复已完成任务（幽灵续跑防线） | task_id=%s", task_id)
        return False, False, message
    # 崩溃清扫窄放行：flag + 署名双条件（行内再验一次，防调用方误用）。
    crash_swept_bypass = (
        allow_crash_swept
        and task_status == "failed"
        and str((row or {}).get("run_stop_reason") or "") == _CRASH_SWEEP_REASON
    )
    if task_status == "failed" and not crash_swept_bypass:
        message = (
            f"任务已失败，不可恢复执行；请通过重新提交（submit）或"
            f" task_manage continue 重试: {task_id}"
        )
        logger.warning("[tasks http] 拒绝恢复真终态失败任务（resume 入口守卫） | task_id=%s", task_id)
        return False, False, message
    # suspended_reason 恢复即清（P-23 片2，ADR 2026-11-04-run-suspended-reason-key）：
    # null 即清除语义（下方 timeout 三键同款）——G8 排空任务恢复后，陈旧 drain
    # 归因残留会被面板误显为后续挂起的原因（片1 复核移交要件①）
    overlay: dict[str, Any] = {
        "task.status": "running",
        "task_status": "running",
        "suspended_reason": None,
    }
    # 旧停泊归因随新 run 清除（与派发通道/失败窄放行分支无关）：crash_swept
    # （内核崩溃清扫署名）与排空/水位自愈类归因是「run 被打断暂泊」的署名，
    # 新 run 一起即陈旧；真实失败归因（llm_fail_loop / duplicate_loop /
    # user_requested 等）不在停泊词表，原样保留（恢复链不得抹掉失败史）。
    # 2026-11-12 第 4 次实证：task.status=running 的 crash_swept 行不走 failed
    # 窄放行分支，旧 overlay 只在窄放行内清键 → 恢复后署名残留，面板误读为
    # 当前 run 归因。
    if _is_parking_stop_reason(str((row or {}).get("run_stop_reason") or "")):
        overlay["run_stop_reason"] = None
    if crash_swept_bypass:
        # 崩溃清扫派的终态残键同批清：派发到收尾窗口内面板会把陈旧署名误读
        # 为当前 run 的失败归因；ended_at/failure_* 清理让重试轮不顶上一轮失败
        # 原因展示（timeout 路径同款）。run_stop_reason 由上方停泊归因判别清键。
        overlay.update(
            {
                "task.ended_at": None,
                "task.failure_class": None,
                "task.failure_reason": None,
            }
        )
    if task_status == "timeout":
        overlay.update(
            {
                "task.ended_at": None,
                "task.failure_class": None,
                "task.failure_reason": None,
            }
        )
    # 陈旧子任务挂号键：子 run 已终态而 task 投影未随写的存量残留 → 恢复
    # overlay 同批清键，否则父续跑轮被 task_reminder 按挂号真值再次挂起。
    overlay.update(await _stale_pending_clear_fields(row))
    try:
        handle = _capability("pipeline-executor")
        # params.user_id 已退役（内核刀3）：反调面身份由内核执行上下文注入
        # （`_identity` 回带 → 桥 scope 重建），插件手传不可指派身份——内核
        # 对该键已不读，携带徒增伪造面。user_id 形参仅留日志审计。
        resp = await handle.call(
            "resume_pipeline",
            {
                "pipeline_id": task_id,
                "state_overlay": overlay,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[tasks http] resume_pipeline 失败 | task_id=%s | user=%s | err=%s",
            task_id,
            user_id,
            exc,
        )
        return False, False, ""
    if not isinstance(resp, dict):
        return False, False, ""
    run_id = str(resp.get("run_id") or "")
    dispatched = resp.get("dispatched") is True
    return (run_id != "" or dispatched), dispatched, ""


# ──────────────────────────────────────────────
# 排空自动恢复（2026-11-11 用户裁定）
# ──────────────────────────────────────────────

# 内核 drain_and_exit75 的归因字串前缀（三个触发源：水位自愈 / 手动 G8 /
# watcher cdylib 集变更）。suspended_reason 在位 ⇔ 本次挂起因排空——用户停止/
# 审批挂起/llm_fail_loop 等非排空停靠不写该键，故不会被自动拉起。
_DRAIN_SUSPEND_MARKERS = (
    "memory watermark self-heal",
    "system/restart",
    "plugin set changed",
)
# 内核崩溃清扫署名（reap_orphan_runs 对受害行的 run_stop_reason 署名值，
# ADR 2026-11-12-crash-swept-running-tasks-auto-relaunch）：在位 ⇔ 该 run
# 是内核重启时在飞被打断的——重启前就真失败的任务带真实归因、永无此署名。
_CRASH_SWEEP_REASON = "crash_swept"
# 单次启动的恢复上限与逐个间隔：避免几十个任务同时抢 LLM 配额/工作空间锁；
# 超出上限的留待下一轮人工或下次重启（不静默丢弃——逐个 WARNING 留痕）。
_AUTO_RESUME_LIMIT = 20
_AUTO_RESUME_STAGGER_SECS = 1.5


def _is_drain_suspend_reason(reason: str) -> bool:
    """归因字串是否来自内核排空（大小写不敏感前缀包含，纯函数可单测）。"""
    lowered = reason.strip().lower()
    return any(marker in lowered for marker in _DRAIN_SUSPEND_MARKERS)


def _is_parking_stop_reason(reason: str) -> bool:
    """``run_stop_reason`` 是否停泊归因（新 run 起始即陈旧的暂泊署名）。

    停泊面 = 内核崩溃清扫署名 ``crash_swept`` + 排空/水位自愈挂起归因
    （三源同 :data:`_DRAIN_SUSPEND_MARKERS`）。真实失败归因（``llm_fail_loop``
    / ``duplicate_loop`` / ``user_requested`` 等）不在词表——恢复链只清停泊
    署名，不覆盖真实失败史（2026-11-12 用户要求）。
    """
    value = reason.strip().lower()
    if not value:
        return False
    return value == _CRASH_SWEEP_REASON or _is_drain_suspend_reason(value)


async def _clear_crash_sweep_signature(state_cap: Any, pipeline_id: str) -> None:
    """拉起成功后显式清 ``run_stop_reason=crash_swept``（旧 run 署名）。

    内核 resume overlay 的 null 清键在装机实证未落库（2026-11-13 08:20
    `1af174` running 仍带 crash_swept），插件侧补一刀确定性清除；写失败仅
    告警，下一次 run 收尾的 terminal_run_fields 会覆写。
    """
    try:
        await state_cap.call(
            "update",
            {"pipeline_id": pipeline_id, "fields": {"run_stop_reason": None}},
        )
    except Exception as exc:  # noqa: BLE001 — 恢复成功优先，清署名失败只留痕
        logger.warning(
            "[tasks http] 崩溃清扫署名清除失败 | task_id=%s | err=%s",
            pipeline_id,
            exc,
        )


async def _mark_relaunch_failed(
    state_cap: Any, pipeline_id: str, error: str
) -> None:
    """崩溃清扫自动拉起失败 → 才把任务投影为 failed（ADR 2026-11-13）。

    成功拉起时任务保持 running（不写 failed/suspended）；只有这里失败才写
    ``task.status=failed`` + ``failure_class=relaunch_failed``。``run_stop_reason``
    保留 crash_swept，下次启动仍可凭署名重试。
    """
    fields = {
        "task.status": "failed",
        "task.ended_at": _now_iso(),
        "task.failure_class": "relaunch_failed",
        "task.failure_reason": f"重启自动拉起失败：{error or '无可恢复对象'}",
    }
    try:
        await state_cap.call(
            "update", {"pipeline_id": pipeline_id, "fields": fields}
        )
    except Exception as exc:  # noqa: BLE001 — 失败投影写不进只留痕，不阻断其余任务
        logger.warning(
            "[tasks http] 自动拉起失败投影写入失败 | task_id=%s | err=%s",
            pipeline_id,
            exc,
        )


async def auto_resume_drained_tasks() -> list[dict[str, Any]]:
    """启动对账后自动恢复被打断的任务（排空 + 内核重启两类）。

    背景①排空：``drain_and_exit75`` 排空在途 run（``run_status=suspended`` +
    ``suspended_reason``），重启后 reconcile 把任务投影复位 ``stopped``——
    此前只能人工 ``task_manage continue`` 续跑，每次自愈都留一批停摆任务。

    背景②崩溃清扫（ADR 2026-11-12-crash-swept-running-tasks-auto-relaunch，
    用户裁定「重启前 running 的，重启后自动重新拉起」）：内核 ``reap_orphan_runs``
    把重启时在飞的 run 清扫为 ``run_status=failed`` 并署名
    ``run_stop_reason=crash_swept``——本函数凭署名识别此类任务并自动派发
    续跑轮（经 ``allow_crash_swept`` 窄放行 failed 守卫）。重启前就真失败的
    任务带真实归因、无署名，不会被拉起。

    本函数按 state 行筛选后逐个走 :func:`_resume_task_pipeline`
    （= 内核 ``pipeline-executor.resume_pipeline``：run 簿记在飞则接管、
    否则派发续跑轮），恢复即经 overlay 清 ``suspended_reason``；``run_stop_reason``
    仅在旧值是停泊归因（``crash_swept`` / 排空/水位自愈）时清除，真实失败
    归因（``llm_fail_loop`` 等）保留。

    守卫（两类共用）：
    - 排空类 ``run_status == "suspended"``：已被恢复/重跑的管道不重复拉起；
    - 排空类 ``suspended_reason`` 命中排空标记：非排空停靠不自动恢复；
    - 崩溃类 ``run_status == "failed"`` ∧ 署名恰为 ``crash_swept``；
    - 任务管道身份门（:func:`events.is_task_pipeline_row`）：会话主管道不是
      任务，恢复它等于凭空发一轮消息；
    - ``task.status`` 真终态 completed 跳过（返工属上级带指令职责）；排空类
      另拒 failed（未署名的 failed 不是本通道的恢复对象）；
    - 有活跃子任务（run running/pending/suspended）跳过：父在等子，重复派发
      只会制造并行轮。

    Returns:
        成功恢复的记录列表（``pipeline_id``/``dispatched``）；读面或单个恢复
        失败只告警，不影响其余任务（自愈兜底不反向阻断装载）。
    """
    try:
        handle = _capability("pipeline-state")
        rows = await handle.call("list", {})
    except Exception as exc:  # noqa: BLE001 — 读面故障降级：本轮不恢复，留痕迹
        logger.warning("[tasks http] 排空自动恢复读面故障（本轮跳过） | err=%s", exc)
        return []
    if not isinstance(rows, list):
        return []
    by_pipeline = {
        str(r.get("pipeline_id") or ""): r for r in rows if isinstance(r, dict)
    }
    candidates: list[tuple[str, bool]] = []  # (pipeline_id, crash_swept)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _task_events.is_task_pipeline_row(row):
            continue
        run_status = str(row.get("run_status") or "")
        if run_status == "suspended":
            if not _is_drain_suspend_reason(str(row.get("suspended_reason") or "")):
                continue
            # 排空类：failed 同为真终态（未署名，非本类对象）
            if str(row.get("task.status") or "") in ("completed", "failed"):
                continue
            crash_swept = False
        elif run_status == "failed" and str(
            row.get("run_stop_reason") or ""
        ) == _CRASH_SWEEP_REASON:
            # 崩溃清扫类：completed 是真完成（即使带陈旧署名也不拉）
            if str(row.get("task.status") or "") == "completed":
                continue
            crash_swept = True
        else:
            continue
        pipeline_id = str(row.get("pipeline_id") or "")
        if not pipeline_id:
            continue
        # 活跃子任务门（两类同受）：父停靠是在等它，子任务的终态通知会唤醒父
        # ——重新派发父续跑轮只会制造并行重复轮（2026-11-11 e475/885b 同批）。
        if _has_active_pending_child(row, by_pipeline):
            continue
        candidates.append((pipeline_id, crash_swept))
    resumed: list[dict[str, Any]] = []
    for idx, (pipeline_id, crash_swept) in enumerate(candidates[:_AUTO_RESUME_LIMIT]):
        if idx:
            await asyncio.sleep(_AUTO_RESUME_STAGGER_SECS)
        ok, dispatched, error = await _resume_task_pipeline(
            pipeline_id,
            user_id="task_service:auto_resume",
            allow_crash_swept=crash_swept,
        )
        if ok:
            resumed.append({"pipeline_id": pipeline_id, "dispatched": dispatched})
            logger.info(
                "[tasks http] %s自动恢复 | task_id=%s | dispatched=%s",
                "崩溃清扫" if crash_swept else "排空",
                pipeline_id,
                dispatched,
            )
            if crash_swept:
                # 任务保持 running；旧 run 署名确定性清除（不依赖 kernel overlay）
                await _clear_crash_sweep_signature(handle, pipeline_id)
        elif crash_swept:
            # 拉起失败才把任务置 failed（ADR 2026-11-13）；署名保留供下次启动重试
            await _mark_relaunch_failed(handle, pipeline_id, error)
            logger.warning(
                "[tasks http] 崩溃清扫任务自动恢复失败，已投影 failed | task_id=%s | err=%s",
                pipeline_id,
                error or "无可恢复对象",
            )
        elif error:
            logger.warning(
                "[tasks http] %s任务自动恢复被拒 | task_id=%s | err=%s",
                "崩溃清扫" if crash_swept else "排空",
                pipeline_id,
                error,
            )
    overflow = len(candidates) - _AUTO_RESUME_LIMIT
    if overflow > 0:
        logger.warning(
            "[tasks http] 待恢复任务超单次上限，剩余 %d 个未拉起"
            "（ids=%s）",
            overflow,
            ",".join(pid for pid, _ in candidates[_AUTO_RESUME_LIMIT:]),
        )
    return resumed


async def _cascade_suspend_children(parent_task_id: str) -> int:
    """GAP-1 统一：级联取消子任务管道（state 聚合中 lineage 子管道逐个挂起）。

    每个被级联取消的子任务随写 task.status 终态（与父任务同一条「接受取消
    即随写」纪律——级联只挂起不随写 = 同一刀留下批量僵尸投影）。级联面保持
    既有吞错风格：单子任务挂起/随写失败仅告警，不阻断其余子任务。
    """
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
                    try:
                        await _write_cancel_terminal(child_id)
                    except Exception as exc:  # noqa: BLE001 — 级联面吞错，告警留痕
                        logger.warning(
                            "[tasks http] 级联子任务终态随写失败 | child=%s | err=%s",
                            child_id,
                            exc,
                        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tasks http] 级联挂起失败 | task_id=%s | err=%s", parent_task_id, exc)
    return cascaded


async def _submit_task_event(
    *,
    title: str,
    task_id: str,
    user_id: str = "",
    description: str = "",
) -> str:
    """重跑已有任务执行管道（UI submit/resume = task_manage retry 映射）。

    以 task_id 作 pipeline_id 走 chat.send_message 注入分支，background
    立即返回。创建不走本函数——面板 HTTP 创建是 task_submit 工具的表单
    传输层（_invoke_task_submit_tool），出生写面单一真值归工具侧。
    user_id 为认证用户（HTTP 面 external 调用者身份，形参如实转发；
    禁止合成用户——ADR 2026-10-17-reverse-call-user-identity-injection）。

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
        "background": True,
        # 重跑复位运行态：task.status → running（DSL 终态收束条件据此放行
        # 重跑轮次）+ ended → false（上一次运行的终态收束标志是 per-run 键，
        # 不复位会让新 run 一轮不跑直接退出）
        "state": {"task.status": "running", "ended": False},
        **({"user_id": user_id} if user_id else {}),
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
    r"""从管道 state 聚合组装任务字典（GAP-1 统一：task = pipeline）。

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
        logger.warning(
            "[tasks http] state 聚合读取失败（任务列表返回空：state 为任务域唯一数据源，无 YAML 兜底）| err=%s",
            exc,
        )
        return None
    # 响应形状：内核 capability_router 的 pipeline-state.list 返回裸数组
    # （workspace/task 工具层同此解析）。
    rows = resp if isinstance(resp, list) else None
    if not isinstance(rows, list):
        return None
    out: list[dict[str, Any]] = []
    # 第一趟：普通任务行（task.* 自身键）——这些任务有自己的执行管道 state，
    # 归属（lineage 父）与状态以本管道行为准；收集 pid 供 owned 行去重。
    # 判据单源 = events.is_task_state：提交者侧记账键（task.owned.* 登记 /
    # task.subtasks_pending.* 挂号回执）与空值墓碑键（清扫/回执写 null 后
    # 键行保留）都不算任务自身声明——否则提交过任务/被清扫过的聊天主管道
    # 会被误聚合成任务行（无 task.goal → title 兜底成主管道裸 id，
    # 2026-10-17 / 2026-10-27 两轮装机实证）。
    state_pids: set[str] = set()
    task_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _task_events.is_task_state(row):
            continue
        pid = str(row.get("pipeline_id") or "")
        if not pid:
            continue
        state_pids.add(pid)
        task_rows.append(
            {
                "id": pid,
                "title": str(row.get("task.goal") or pid),
                "status": derive_task_status(row),
                "thread_id": _session_anchor(row),
                "pipeline_run_id": pid,
                "parent_task_id": str(row.get("lineage.parent_pipeline_id") or "") or None,
                "metadata": {
                    "session_id": _session_anchor(row),
                    "submitted_by": str(row.get("task.submitted_by") or ""),
                    # 父是容器任务（task.owned 声明）时：容器 project id（前端
                    # 任务树据此把子任务挂到容器节点下）
                    "parent_project_id": str(row.get("task.parent_project_id") or "") or None,
                    # 工作空间坐标（任务面板"打开工作空间"按钮数据源）。
                    # 单键 task.ws_meta（出生/创建时一次写全，state 单一真相源）；
                    # as_dict 兼容跨边界 JSON 字符串形态（state_fields 契约）。
                    "ws_meta": state_fields.as_dict(
                        row.get("task.ws_meta"), field="task.ws_meta"
                    ),
                    "workspace": str(row.get("workspace") or "") or None,
                },
                "created_at": str(row.get("task.created_at") or ""),
                # 执行 agent 归属（出生协议 agent.id 快照，task_birth 日志同
                # 口径）：顶层 agent_name 供前端任务面板 Agent 列映射；历史行
                # 无此键 → None（前端显示 '--'，不回填）
                "agent_name": str(row.get("agent.id") or "") or None,
            }
        )
    out.extend(task_rows)
    # 第二趟：容器任务/登记声明（task.owned.<id>.* 键）——只在无独立 state 行时
    # 出口（有 state 行的任务以 state 行为准：归属/状态更真；双行同 id 会让
    # 前端 taskById 覆盖 + 面板重复节点）。
    # owned 镜像已退役（ADR 2026-11-02-state-key-governance）：登记键只承载
    # 声明元数据（title/created_at/submitted_by），无 status 字段——登记型
    # 任务出口恒 active（登记不是运行态；有行任务的状态 join 第一趟行真值）。
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
                    # 登记声明无 status 键（镜像退役）：恒 active（登记存活语义，
                    # 非运行态——有行任务的状态由第一趟行真值承载）
                    "status": "active",
                    "thread_id": _session_anchor(row),
                    # 登记不是管道运行：不指回提交者管道（否则前端按
                    # pipeline_run_id 建映射会把主管道误判成任务行）。登记行的
                    # 导航消费方按「无 pipeline_run_id 不出对话入口」守卫
                    # （FileTreeWidget hasPipeline 同契约）。
                    "pipeline_run_id": "",
                    "parent_task_id": None,
                    "metadata": {
                        "session_id": _session_anchor(row),
                        # 用户归属继承宿主行 task.submitted_by（get_task 归属闸
                        # 按用户比对；owned.submitted_by 是系统登记标记如
                        # review_system，非用户身份）
                        "submitted_by": (
                            str(row.get("task.submitted_by") or "")
                            or str(fields.get("submitted_by") or "")
                        ),
                        "workspace": str(fields.get("workspace") or ""),
                    },
                    "created_at": str(fields.get("created_at") or ""),
                    # 登记声明（只登记不执行）无执行 agent 可言：恒 None，
                    # 不借宿主管道的 agent.id 冒充归属
                    "agent_name": None,
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
    task.owned.<project_id>.title / .created_at / .submitted_by（status 已
    退役，ADR 2026-11-02-state-key-governance——登记型出口恒 active）。
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
        工具成功信封的 data（含 task_id/pipeline_id）。成功含义 = 已受理**且
        工作空间已就绪**（ADR 2026-10-17 出生协议四段式），前端可据此外显
        「已创建/已派发」而非「开始执行」。

    Raises:
        APIError: 能力未注入（503）/ 调用失败（500）/ 工具拒绝信封（入参类
            400，核心流程失败类 500——见 _CORE_FAILURE_ERROR_CODES；
            error_code 原样透传）。
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
    # 500 面 = 核心流程失败而非入参非法：DISPATCH_FAILED（管道未创建）与
    # WORKSPACE_PREPARE_FAILED（工作空间建不出来，任务记录已保留并标 failed）
    # 同属此类——两者都可能有副作用落库，不能当"改下入参再试"的 400 回。
    raise APIError(
        status_code=500 if error_code in _CORE_FAILURE_ERROR_CODES else 400,
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
    if body.acceptance_criteria:
        inputs["acceptance_criteria"] = body.acceptance_criteria

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


def _task_owner_id(row: dict[str, Any]) -> str:
    """任务归属权威字段值（用户 id）：task.submitted_by。

    task_submit 出生协议把提交者 user_id 一次写全进出生 state
    （plugins/shared/task_birth.py，无降级路径）；登记型任务（task.owned.*）
    的用户归属继承宿主行 task.submitted_by（owned.submitted_by 是系统登记
    标记如 review_system，非用户身份），见 _list_tasks_from_state。
    """
    return str((row.get("metadata") or {}).get("submitted_by") or "")


def _assert_task_ownership(row: dict[str, Any], _user: dict[str, Any] | None) -> None:
    """任务归属闸：请求方与任务归属不一致 → 404（防存在性探测）。

    caller 未认证 / 与归属不一致时与"任务不存在"同响应形状，不可区分；
    任务侧缺归属元数据（存量数据）fail-closed 拒绝并在消息中说明原因，
    不做放行式降级（回填属运行时操作）。
    """
    sub = str(_current_user(_user).get("sub") or "")
    owner = _task_owner_id(row)
    if not owner:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="任务归属元数据缺失（task.submitted_by），拒绝访问；存量任务需回填归属后可达",
        )
    if not sub or sub != owner:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="任务不存在或已被删除",
        )


async def get_task(
    task_id: str,
    _user: dict[str, Any] | None = None,
) -> TaskResponse:
    """获取指定任务的详情（state 单一真值，无 YAML 兜底）。

    读法与 list/update 同源：state 聚合行命中即出口，未命中 404；
    命中后过归属闸（_assert_task_ownership，_user 真消费）。
    """
    row = await _get_task_row_from_state(task_id)
    if row is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="任务不存在或已被删除",
        )

    _assert_task_ownership(row, _user)

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
        "status": derive_task_status(row),
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

    # 提交注入身份门（ADR 2026-11-06 D5 N7）：会话主管道行（伪任务行，可带
    # 幽灵残留）不是任务 → 404——注入分支的 state overlay
    # （task.status=running）不得落在会话主管道上。
    raw_row = await _read_task_row(task_id)
    if raw_row is None or not _task_events.is_task_pipeline_row(raw_row):
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="任务不存在或已被删除",
        )

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
    """恢复指定暂停的任务（resume_pipeline 按管道簿记或拉起续跑轮）。

    终态防线在 _resume_task_pipeline 内部（U13 前置仲裁下沉，同时覆盖
    resume_project 级联路径；ADR 2026-10-30 扩为真终态全集）：投影真终态
    （completed/failed）→ 拒绝派发，error 如实转 409 语义（completed 返工
    重开走上级 task_manage continue 带指令，即事故修复④A 职责面；failed
    重试走 submit/continue）。幽灵 running（收尾写失败后 runs 残留 running）
    会让 resume_pipeline 幂等空转却返回 run_id——投影终态证据识破同在此防线。
    """

    # 恢复 = resume_pipeline（内核续跑派发，state 由快照恢复，无需透传）；
    # task_submitted 如实映射内核 dispatched——恒 False 是假恢复形状
    # （2026-10-10 装机 B76 实证：恢复成功而任务原地不动，响应无法区分）。
    resumed, task_submitted, resume_error = await _resume_task_pipeline(
        task_id, user_id=_current_user(_user).get("sub", "")
    )
    if resume_error:
        raise APIError(
            status_code=409,
            error_code="API_STATE_2009",
            message=resume_error,
        )
    if not resumed:
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
    """取消指定任务（挂起管道 + lineage 级联 + state 行终态随写）。

    谁终态谁随写（runs 退役收尾原则）：cancel 是合法停止写面，接受取消即
    随写 task.status=stopped（暂停放那里，可恢复——ADR 2026-10-30 词汇收敛：
    cancelled 退役，用户停止 = stopped，孤儿投影与活任务两态同路）。修复
    僵尸投影 cancel 无效（2026-10-08 二度实锤）：此前孤儿任务 cancel 200 而
    投影恒 running。引擎停止失败或终态写失败均如实报错，不落终态、不谎报
    取消成功。
    """

    # 随写面前置仲裁（存在性以 state 行为准，delete_task 同款语义）：
    # 行缺席（含读面故障同形 404）不得写——写面对未知 pipeline_id 是
    # upsert，bogus id 会凭空造出幽灵任务行；非任务管道行（会话主管道，
    # 可带幽灵残留）不是任务 → 同形 404（ADR 2026-11-06 D5 N7，停止链路
    # 不得在会话上放大幽灵任务标记）；completed 是评估闸门/用户侧裁决
    # 终态，不可被取消推翻（resume 链同款 409）。
    row = await _read_task_row(task_id)
    task_status = str((row or {}).get("task.status") or "") or None
    if row is None or task_status is None or not _task_events.is_task_pipeline_row(row):
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message=f"任务不存在或已被删除: {task_id}",
        )
    if task_status == "completed":
        raise APIError(
            status_code=409,
            error_code="API_STATE_2009",
            message=f"任务已完成，不可取消: {task_id}",
        )

    # 引擎停止（活任务）→ 终态随写（孤儿照走）：任一失败如实报错
    try:
        pipeline_cancelled = bool(await _stop_task_pipeline(task_id))
    except Exception as exc:  # noqa: BLE001 — 边界翻译：能力面故障 → 503
        logger.warning("[tasks http] 取消停止引擎失败 | task_id=%s | err=%s", task_id, exc)
        raise APIError(
            status_code=503,
            error_code="API_EXT_2010",
            message=f"引擎停止失败，任务未被取消: {exc}",
        ) from exc
    try:
        await _write_cancel_terminal(task_id)
    except Exception as exc:  # noqa: BLE001 — 终态没落账不得谎报取消成功
        logger.warning("[tasks http] 取消终态随写失败 | task_id=%s | err=%s", task_id, exc)
        raise APIError(
            status_code=503,
            error_code="API_EXT_2010",
            message=f"取消终态写 state 失败，任务未落账终态: {exc}",
        ) from exc

    cascaded = await _cascade_suspend_children(task_id)

    logger.info(
        "用户 %s 取消任务 %s (pipeline_cancelled=%s, cascaded=%d, terminal=stopped)",
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
    # 词汇收敛（ADR 2026-10-30）：stopped = 暂停放那里（含旧 suspended/
    # cancelled 语义），prepare/pending 同旧 suspended 桶。
    "stopped": ("prepare", "pending"),
    "running": ("execute", "running"),
    "blocked": ("execute", "running"),
    "evaluating": ("evaluate", "running"),
    "completed": ("evaluate", "completed"),
    "failed": ("execute", "failed"),
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
# 子任务挂靠键 = state 行 ``task.parent_project_id``（task_submit 出生写全，
# 项目挂靠唯一真值）。

async def _project_call(method: str, args: dict[str, Any]) -> dict[str, Any]:
    """project.<method> 服务调用（T6a：登记簿读写收口项目域主 project_manager）。

    服务不可用/返回非 dict = 503 APIError（读写面 fail-honest，同原登记簿
    None 形态）；provider 失败经 ``error`` 键表达 → 400 APIError。
    """
    try:
        handle = _capability("project")
        result = await handle.call(method, args)
    except APIError:
        raise
    except Exception as exc:  # noqa: BLE001 — 能力缺席/调用失败统一 503
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message=f"project 服务不可用（project.{method}）: {exc}",
        ) from exc
    if not isinstance(result, dict):
        raise APIError(
            status_code=503,
            error_code="API_TIME_2005",
            message=f"project.{method} 返回载荷非法",
        )
    if "error" in result:
        raise APIError(status_code=400, error_code="API_REQ_4002", message=str(result["error"]))
    return result


async def _get_project_row(project_id: str) -> dict[str, Any]:
    """按 id 取登记行 dict；未登记 = 404。"""
    row = await _project_call("get", {"project_id": project_id})
    project = row.get("project") if row.get("found") else None
    if not isinstance(project, dict):
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="项目不存在或已被删除",
        )
    return project


def _project_status_out(status: str) -> str:
    """登记行 status → 前端 Project.status（running/suspended）。"""
    return "suspended" if status == "paused" else "running"


def _project_to_dict(project: dict[str, Any], tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """登记行（project.* 服务 dict 形态）→ 前端 Project 形状（frontend/src/types 对齐）。"""
    return {
        "id": project.get("id") or "",
        "userId": project.get("submitted_by") or "",
        "sessionId": project.get("session_id") or None,
        "goal": project.get("title") or "",
        "status": _project_status_out(str(project.get("status") or "active")),
        "autoExecute": bool(project.get("auto_execute")),
        "currentTaskIndex": 0,
        "tasks": tasks if tasks is not None else [],
        "timestamps": {
            "createdAt": project.get("created_at") or "",
            "updatedAt": project.get("updated_at") or "",
        },
        "metadata": {
            "path": project.get("path") or "",
            "source": "project",
        },
    }


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
    """获取项目列表（project.list 服务登记行；session_id 过滤创建会话）。"""
    # page 兼容（前端传 page+limit）；offset 显式给定时优先
    if offset == 0 and page is not None and page > 1:
        offset = (page - 1) * limit

    result = await _project_call("list", {})
    projects = [p for p in (result.get("items") or []) if isinstance(p, dict)]
    if session_id:
        projects = [p for p in projects if p.get("session_id") == session_id]

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

    同路径幂等：登记中已有该文件夹 → 返回既有登记（created=false），
    不重复建文件夹/登记行。非 git 文件夹会 git init（worktree 前提）；
    git init 失败创建整体失败。
    """
    body = dict(body or {})
    goal = str(body.get("goal") or body.get("title") or "").strip()
    if not goal:
        raise APIError(
            status_code=400,
            error_code="MISSING_GOAL",
            message="创建项目必须指定 goal",
        )

    explicit_path = str(body.get("path") or body.get("workspace") or "").strip()
    result = await _project_call(
        "create",
        {
            "title": goal,
            "path": explicit_path,
            "session_id": str(body.get("session_id") or ""),
            "submitted_by": str(_current_user(_user).get("sub", "") or ""),
            "auto_execute": bool(body.get("auto_execute", False)),
        },
    )
    project = result.get("project")
    created = bool(result.get("created"))
    if not isinstance(project, dict):
        raise APIError(
            status_code=500,
            error_code="PROJECT_FOLDER_FAILED",
            message="project.create 返回载荷缺 project 行",
        )

    logger.info(
        "[projects] %s项目 | project_id=%s | goal=%s | path=%s | user=%s",
        "复用已登记" if not created else "创建",
        project.get("id") or "",
        goal,
        project.get("path") or "",
        project.get("submitted_by") or "-",
    )
    return {"project": _project_to_dict(project), "created": created}


async def get_project(
    project_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取项目详情（含名下子任务摘要列表）。"""
    project = await _get_project_row(project_id)

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
                        "status": derive_task_status(row),
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
    project = await _get_project_row(project_id)

    if body and "enabled" in body:
        enabled = bool(body.get("enabled"))
    else:
        enabled = not project.get("auto_execute")
    project["auto_execute"] = enabled
    project = (await _project_call("save", {"project": project})).get("project") or project

    logger.info("[projects] 切换自动执行 | project_id=%s | %s", project_id, enabled)
    return {"project": _project_to_dict(project)}


async def pause_project(
    project_id: str,
    _user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """暂停项目 = 登记行 status=paused + 尽力挂起名下子任务管道。"""
    project = await _get_project_row(project_id)

    suspended = 0
    for pid in await _project_child_pids(project_id):
        if await _suspend_task_pipeline(pid):
            suspended += 1

    project["status"] = "paused"
    project = (await _project_call("save", {"project": project})).get("project") or project

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
    project = await _get_project_row(project_id)

    resumed = 0
    uid = _current_user(_user).get("sub", "")
    for pid in await _project_child_pids(project_id):
        child_resumed, _submitted, _guard_error = await _resume_task_pipeline(pid, user_id=uid)
        if child_resumed:
            resumed += 1

    project["status"] = "active"
    project = (await _project_call("save", {"project": project})).get("project") or project

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
    """删除项目 = 处置名下子任务 + 删登记行；delete_files=true 时删文件夹。

    子任务处置口径由 delete_children 决定：缺省级联挂起（子任务保留），
    true 时逐个级联删除任务管道——任一失败即中止且不删登记行（已删除的
    子任务不回滚）。文件夹删除带路径安全校验（盘符根/仓库根/工作空间
    基目录拒删）。
    """
    project = await _get_project_row(project_id)

    suspended = 0
    deleted_children = 0
    if _qflag(query, "delete_children"):
        failed: list[str] = []
        for pid in await _project_child_pids(project_id):
            try:
                await delete_task(pid, _user)
                deleted_children += 1
            except APIError as exc:
                if exc.status_code != 404:  # 列举后已被删的子任务视为已处置
                    failed.append(pid)
        if failed:
            raise APIError(
                status_code=500,
                error_code="PROJECT_CHILDREN_DELETE_FAILED",
                message=f"子任务删除失败，项目未删除: {', '.join(failed)}",
            )
    else:
        for pid in await _project_child_pids(project_id):
            if await _suspend_task_pipeline(pid):
                suspended += 1

    await _project_call("delete", {"project_id": project_id})

    folder_removed = False
    if _qflag(query, "delete_files"):
        folder_removed = bool(
            (await _project_call("remove_folder", {"path": project.get("path") or ""})).get("removed")
        )

    logger.info(
        "[projects] 删除项目 | project_id=%s | suspended_children=%s | deleted_children=%s | folder_removed=%s | user=%s",
        project_id,
        suspended,
        deleted_children,
        folder_removed,
        _current_user(_user).get("username", "system"),
    )
    return {
        "message": "项目已删除",
        "id": project_id,
        "suspended_children": suspended,
        "deleted_children": deleted_children,
        "folder_removed": folder_removed,
    }


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


def _qflag(query: dict[str, str] | None, key: str) -> bool:
    """查询参数布尔解析（1/true/yes 为真，缺省假）。"""
    return bool(query and str(query.get(key) or "").lower() in ("1", "true", "yes"))


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
# 分发表成员；evaluate 端点恒 410（评估只走 task_evaluate 工具）；cancel 需请求体故取 raw_body。


async def _route_task_actions(
    tid: str, action: str, method: str, raw_body: str, caller: dict[str, Any]
) -> dict[str, Any] | None:
    """派发 /{task_id}/{action} 二段路径，未命中返回 None。"""
    if action == "evaluate" and method == "POST":
        # 评估由 task_evaluate 工具承载（plugins/shared/task_evaluate），
        # 本 HTTP 端点恒 410 指路，不提供评估能力。
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

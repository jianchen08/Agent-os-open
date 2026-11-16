"""任务域事件派生（ADR 2026-08-28 事件下沉）。

内核 run 终态只广播运行域 run.* 事件；本模块订阅 run.completed / run.failed，
取该管道的终态 state，按任务域语义派生 task_completed / task_failed 并经
event-bus.emit_domain 发回域事件总线——裁决词汇（task.status 值、task.* 键面）
归任务域所有者（本插件），内核零知识。

终态随写（谁终态谁随写）：任务域终态事件落终态时，**任务自身 task.status 投影
随写**为对应终态。两个消费方（前端任务列表、父会话状态视角）读的都是投影，
投影不随写 = 僵尸 running（事故 2026-10-20：管道 3f968d0d946f 以
run_status=failed 终态化，投影停在 running，父会话据脏投影答"运行中"）。
随写先于通知——父会话被唤醒读投影时投影已是终态。owned 镜像已退役
（ADR 2026-11-02-state-key-governance）：父管道 ``task.owned.<id>.status``
是双写漂移源（装机 2026-11-02 幽灵 running 实证），面板 join 子行取真值。

state 来源（ADR 2026-09-11）：优先取事件自带的 ``state`` 载荷（内核终态把该
管道 state 随事件交出，剥离 messages/raw_* 等引擎大字段）——零回查，且不受
``export_fields`` 白名单约束；载荷缺席时回退 pipeline-state.list 回查。

判定语义（与内核原 derive_run_terminal_events 行为逐条对齐）：
- 任务管道判据 = state 含 ``task.`` 前缀键且不含 ``task.owned.``（后者是
  父管道登记子任务的键，不是任务自身声明）；
- run.failed + 任务管道 → task_failed；
- run.completed：suspended / router.stop_reason=user_requested → 不派生
  （挂起与用户停止都不是任务终态）；task.status=completed → task_completed；
  task.status=failed → task_failed；其余（pending 等未决态）→
  不派生（杜绝"跑完就假完成通知上级"）。

事件标签：pipeline_id / thread_id / task_id / parent_pipeline_id / user_id /
title / error / failure_class / failure_reason / retry_count / eval_summary /
merge_notes / context_usage——triggers_ext 的父任务通知注入器
（_auto_notify_parent）依赖这组标签拼装 0.1 同款富通知（标题/失败原因/重试
计数/评估结论/合并备注/上下文使用率）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TASK_PREFIX = "task."
_OWNED_PREFIX = "task.owned."
# 提交者侧挂号键（task_submit 写入提交者管道的「已提交待回执」标记，
# task_reminder 信号③消费；子任务终态写 null 回执，键常驻不删）——
# 与 owned 同属提交者侧记账键面，挂在聊天主管道上不构成任务自身声明。
_PENDING_PREFIX = "task.subtasks_pending."
# 触发器注册表旧前缀（2026-11-04 前）：triggers_ext 曾因写面白名单借用 task.*
# 命名空间；现前缀已迁出（trigger.registry.*），旧键在迁移期可能残留——与
# owned/挂号同属「挂在任意管道上的记账键」，不构成任务自身声明。
_TRIGGER_LEGACY_PREFIX = "task.trigger."

# 用户侧终态（新 run 启动复位豁免）：completed = 用户手动完成（评估通过完成
# 同值，复活走新建任务）；deleted = 用户侧删除署名。可重入状态（failed/
# timeout/stopped 等）不在豁免列——与 state_machine._TASK_TRANSITIONS 一致
# （completed 无 → running 边）。词汇收敛（ADR
# 2026-10-30-task-status-authority-vocabulary-convergence）：cancelled/
# canceled 退役（用户停止 = stopped，可重入不在豁免列；历史行由 reconcile
# 开机迁移 stopped），豁免列收窄为 {completed, deleted}。
_USER_TERMINAL_STATUSES = frozenset({"completed", "deleted"})

# 任务域终态事件 → 任务投影终态词汇（谁终态谁随写的目标值）
_TERMINAL_EVENT_STATUS: dict[str, str] = {
    "task_failed": "failed",
    "task_completed": "completed",
}

# 结算唤醒守卫拦截集（真终态，ADR 2026-10-30 §4.3）：父任务 completed/failed
# 不得被子任务结算唤醒（实证事故链：子任务 B 完成自动唤醒已终态父任务再跑
# 一轮）。stopped 不在拦截列——放那的任务允许被自然唤醒（设计意图）。
_PARENT_WAKE_BLOCKED_STATUSES = frozenset({"completed", "failed"})


def is_task_state(row: dict[str, Any]) -> bool:
    """任务管道判据：含非空 ``task.*`` 自身键且不含提交者侧记账键。

    记账键三类：``task.owned.*``（父管道登记子任务/容器/项目）、
    ``task.subtasks_pending.*``（提交挂号回执）与 ``task.trigger.*``
    （触发器注册表旧前缀，迁移期残留）。它们都写在提交者（聊天主管道）
    state 上，不改变其会话身份——只凭它们命中不算任务行。

    值语义（2026-10-27 主管道裸 ID 复发修复）：声明 = 非空值。null 与空串
    只是清除署名——pipeline-state.update 无键删除语义，清扫/回执写 null 后
    墓碑行保留且读面键仍在，判据若只看键存在，被清扫过的会话永久命中
    （面板伪任务行标题兜底成裸管道 ID）。真任务行的自身声明键
    （task.id/task.goal 等）出生即写非空值，不受墓碑兄弟键影响。

    **不再单独作为任务管道身份判据**（ADR 2026-11-06 D1）：会话主管道被
    误写 task.status 残留（装机 2026-11-06 事故，35ee45bf1da2 幽灵
    stopped/failed）后本判据会把会话误判为任务——派生/守卫/补发一律改用
    :func:`is_task_pipeline_row`（与本判据组合）。本判据保留给
    still-needs-it 的消费方（面板聚合过滤等）。
    """
    for key, value in row.items():
        k = str(key)
        if (
            k.startswith(_OWNED_PREFIX)
            or k.startswith(_PENDING_PREFIX)
            or k.startswith(_TRIGGER_LEGACY_PREFIX)
        ):
            continue
        if k.startswith(_TASK_PREFIX) and value is not None and value != "":
            return True
    return False


_LINEAGE_PREFIX = "lineage."


def is_task_pipeline_row(row: dict[str, Any]) -> bool:
    """任务管道身份判据（统一基座，ADR 2026-11-06 D1）：

    ``str(row["task.id"]) 非空 ∨ 任一 lineage.* 键非空``。

    任务管道出生协议保证 task.id 非空（= pipeline_id）且带血缘键（子任务
    lineage.parent_pipeline_id/origin_session_id，根任务 lineage.root/
    origin.kind）；会话主管道两者皆无——任何 task.* 残留（被误写的幽灵
    task.status 等）都不改变其会话身份。装机 534ef18c1ba8 反例（有 task.id
    无血缘键）由 task.id 臂覆盖。

    消费方：事件派生门（本模块）、结算唤醒守卫与血缘冒泡（本模块）、
    reconcile 补写/清扫/补发判据、tasks http_api 停止写面身份门。
    """
    if str(row.get("task.id") or "") != "":
        return True
    return any(
        str(key).startswith(_LINEAGE_PREFIX) and value is not None and value != ""
        for key, value in row.items()
    )


def resolve_wakeup_target(
    rows: list[dict[str, Any]], direct_parent: str
) -> tuple[str | None, bool, list[str]]:
    """血缘冒泡解析（ADR 2026-11-06 D3，纯函数可单测）：跳过 ≠ 丢弃。

    沿 ``lineage.parent_pipeline_id`` 上行（by_pid 内存走链，visited 防环；
    一次 list 建 map 的模式先例 = isolation/manager._resolve_workspace_key）：

    - 命中会话管道（非 :func:`is_task_pipeline_row`）→ 投递目标 = 它
      （终点 = 主管道，用户裁定：主管道失败/停止仍必须被唤醒）；
    - 命中活着的任务管道（task.status 不在终态拦截集）→ 投递目标 = 它；
    - 命中终态任务管道 → 继续上行；
    - 链顶终态任务（无父键）/ 环 / 中途祖先行缺失 → 结构性死路
      （处置非漏发，重试同一条死路是噪声）；
    - 直接父行整体缺席 → fail-open 直送（D2 读面异常三分：行缺席 = 管道
      已亡，投递失败由注入侧留痕）——仅祖先链中途缺失才判死路。

    Returns:
        ``(target, structural_dead_end, chain)``：target = 实际投递目标
        （死路时 None；空串 = 根任务无父可投）；chain = 上行链路（含起点
        直接父，按序）。
    """
    by_pid: dict[str, dict[str, Any]] = {
        str(r.get("pipeline_id") or ""): r for r in rows if isinstance(r, dict)
    }
    chain: list[str] = []
    visited: set[str] = set()
    bubbled = False  # 已至少跳过一个终态任务祖先（进入冒泡段）
    current = str(direct_parent or "")
    while current:
        if current in visited:
            return None, True, chain  # 环：结构性死路
        visited.add(current)
        chain.append(current)
        row = by_pid.get(current)
        if row is None:
            if not bubbled:
                return current, False, chain  # 直接父缺席：fail-open 直送
            return None, True, chain  # 祖先链中途缺失：结构性死路
        if not _parent_wakeup_blocked(row):
            return current, False, chain  # 会话祖先或活任务 = 终点（stopped 可唤醒语义保留）
        bubbled = True  # 终态任务祖先：继续上行
        current = str(row.get("lineage.parent_pipeline_id") or "")
    if chain:
        return None, True, chain  # 链顶终态任务（无父键）：结构性死路
    return "", False, []  # 空父（根任务）：无目标、非死路


def _tag(row: dict[str, Any], key: str) -> Any:
    val = row.get(key)
    return val if val is not None else ""


def _context_usage(row: dict[str, Any]) -> dict[str, Any]:
    """从 state 摘要行提取上下文使用率（0.1 通知同款遥测）。

    数据源：track.llm_usage（track 插件已出口）+ context_window（llm_core
    每轮写入，已出口）。占用取 last_input_tokens（最近一次 LLM 轮的输入 =
    当前上下文窗口占用，与前端输入框用量/浮窗同语义；total_* 是跨轮累计，
    不是当前占用）。缺任一键返回空 dict（通知侧按无遥测处理）。
    """
    usage = row.get("track.llm_usage")
    window = row.get("context_window")
    if not isinstance(usage, dict) or not window:
        return {}
    try:
        window = int(window)
        input_tokens = int(usage.get("last_input_tokens", 0) or 0)
    except (TypeError, ValueError):
        # 遥测值脏数据（非数值形态）：按无遥测处理，但留痕原始值便于追数据源
        logger.warning(
            "[events] 上下文用量解析失败（按无遥测处理）: context_window=%r "
            "last_input_tokens=%r",
            window,
            usage.get("last_input_tokens"),
        )
        return {}
    if window <= 0:
        return {}
    pct = round((input_tokens / window) * 100, 1)
    return {
        "pct": pct,
        "input_tokens": input_tokens,
        "context_window": window,
    }


def derive_task_terminal_events(
    event_name: str, row: dict[str, Any] | None
) -> list[tuple[str, dict[str, Any]]]:
    """由 run 终态事件 + state 摘要行派生任务域事件（纯函数，可单测）。

    Returns:
        [(event_name, tags)]——空列表 = 该 run 不派生任务域事件。
    """
    if not isinstance(row, dict):
        return []
    # 派生门 = 任务声明 ∧ 任务管道身份（ADR 2026-11-06 D1）：会话主管道的
    # task.* 残留（幽灵 stopped/failed）不再触发派生——终态随写随之豁免，
    # 残留清理归 reconcile 清扫刀。
    if not is_task_state(row) or not is_task_pipeline_row(row):
        return []
    tags = {
        "pipeline_id": _tag(row, "pipeline_id"),
        "thread_id": _tag(row, "thread_id"),
        "task_id": _tag(row, "task.id"),
        "parent_pipeline_id": _tag(row, "lineage.parent_pipeline_id"),
        "user_id": _tag(row, "task.submitted_by"),
        # 富通知字段（0.1 task_notifier 同款）：标题/失败原因/重试计数/
        # 评估结论/上下文使用率——state 已有键直接带出，缺键空串由通知侧兜底
        "title": _tag(row, "task.goal"),
        "error": _tag(row, "task.error"),
        "failure_class": _tag(row, "task.failure_class"),
        "failure_reason": _tag(row, "task.failure_reason"),
        "retry_count": _tag(row, "task.eval_total_calls"),
        "eval_summary": _tag(row, "task.eval_summary"),
        # 合并备注（worktree 未跟踪碰撞自愈处置，2026-10-23 用户裁定：备份落
        # 主空间后必须随完成通知告知上级对账）
        "merge_notes": _tag(row, "task.merge_notes"),
        "context_usage": _context_usage(row),
    }
    status = str(row.get("task.status") or "")
    if event_name == "run.failed":
        # 终态证据闸（U13 域界定 §二；词汇收敛 ADR 2026-10-30 豁免收窄）：
        # 投影已持 completed → 终态证据在场，不派生 task_failed——失败通知
        # 不得推翻评估终态。未决投影照常派生：task_failed 是运行失败的镜像
        # 派生（通知面）；投影随写由派生侧同批完成（谁终态谁随写，见
        # handle_run_terminal_event）。
        if status == "completed":
            return []
        return [("task_failed", tags)]
    if event_name != "run.completed":
        return []
    if status == "completed":
        return [("task_completed", tags)]
    if status == "failed":
        return [("task_failed", tags)]
    # 评估未通过（pending/running 等未决态）不派生——完成唯一判据 =
    # task_evaluate 评估通过落 task.status=completed
    return []


def pending_registration_clear_fields(tags: dict[str, Any]) -> dict[str, Any] | None:
    """子任务终态 → 父管道挂号键清除字段（纯函数，可单测）。

    挂号键 = task_submit 写入提交者管道 state 的 ``task.subtasks_pending.<task_id>``
    （值 = 提交时间戳，消费方 task_reminder 信号③按真值判定）。终态清除写
    null——pipeline-state.update 无键删除语义，null 即已回执；该键在本插件
    manifest ``state.writes`` 的 task.* 声明面内。无父锚点（根任务）或无
    任务 id → None（不写）。
    """
    task_id = str(tags.get("task_id") or "")
    parent = str(tags.get("parent_pipeline_id") or "")
    if not task_id or not parent:
        return None
    return {f"task.subtasks_pending.{task_id}": None}


def terminal_follow_write_plan(
    event_name: str, tags: dict[str, Any], row: dict[str, Any]
) -> list[dict[str, Any]]:
    """终态随写计划（纯函数，可单测）：仅任务自身投影写面。

    谁终态谁随写：任务域终态事件（task_failed / task_completed）落终态时，
    任务自身 ``task.status`` 投影必须随写为对应终态。两个消费方（前端任务
    列表、父会话状态视角）读的都是投影，投影不随写 = 僵尸 running（事故
    2026-10-20：管道 3f968d0d946f 终态 failed，投影停在 running）。

    写面纪律：
    - 自身投影已是该终态 → 不重复写（幂等省写）。

    owned 镜像已退役（ADR 2026-11-02-state-key-governance）：父管道
    ``task.owned.<id>.status`` 是双写漂移源（无清理机制，幽灵 running 装机
    2026-11-02 实证），随写面移除——面板 join 子管道行取真值。

    Args:
        event_name: 任务域终态事件名（task_failed / task_completed）。
        tags: 派生标签（pipeline_id / task_id / parent_pipeline_id）。
        row: 终态 state 行（取自身投影现值）。

    Returns:
        写面计划列表 [{"pipeline_id": ..., "fields": {...}}]；空 = 无需写。
    """
    status = _TERMINAL_EVENT_STATUS.get(event_name)
    if not status:
        return []
    plan: list[dict[str, Any]] = []
    # 自身投影写面 = 任务自身管道
    child_pipeline_id = str(tags.get("pipeline_id") or "")
    if child_pipeline_id and str(row.get("task.status") or "") != status:
        plan.append(
            {"pipeline_id": child_pipeline_id, "fields": {"task.status": status}}
        )
    return plan


async def _follow_write_terminal_projection(
    event_name: str,
    tags: dict[str, Any],
    row: dict[str, Any],
    pipeline_id: str,
    state_capability: Any,
) -> None:
    """终态随写执行器：自身投影（写失败降级留痕）。

    写失败只告警——终态事件派生与父管道挂号清除是主流程，不因投影写面故障
    而丢失（投影留脏由启动期对账治愈）。
    """
    plan = terminal_follow_write_plan(event_name, tags, row)
    if not plan:
        return
    target = _TERMINAL_EVENT_STATUS[event_name]
    current = str(row.get("task.status") or "")
    if event_name == "task_failed" and current != target:
        logger.warning(
            "[task_service] task_failed 派生时投影未随写终态（kill 方未随写）"
            "→ 任务域补随写 | pipeline=%s | 投影=%s",
            pipeline_id,
            current,
        )
    written: list[dict[str, Any]] = []
    for item in plan:
        try:
            await state_capability.call("update", item)
            written.append(item)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[task_service] 终态随写失败（投影留脏，事件派生不受影响）"
                "| event=%s | pipeline=%s | fields=%s",
                event_name,
                item["pipeline_id"],
                item["fields"],
            )
    if written:
        logger.info(
            "[task_service] 终态随写完成 | event=%s | pipeline=%s | 目标=%s | 写面=%s",
            event_name,
            pipeline_id,
            target,
            [item["fields"] for item in written],
        )


def _payload_state(params: dict[str, Any], pipeline_id: str) -> dict[str, Any] | None:
    """取事件自带的终态 state 载荷（ADR 2026-09-11）。

    内核在 run 终态把该管道 state 随事件交出（剥离 messages/raw_* 等大字段），
    订阅方据此零回查派生任务域语义。载荷里的 ``pipeline_id`` 须与事件坐标一致
    ——不一致说明载荷错配（防御性拒绝，退回回查而非按错行派生）。

    Returns:
        载荷行（含 pipeline_id 补全）；无载荷/形状非法/坐标错配 → None（回退）。
    """
    payload = params.get("state")
    if not isinstance(payload, dict):
        return None
    payload_pid = str(payload.get("pipeline_id") or "")
    if payload_pid and payload_pid != pipeline_id:
        logger.warning(
            "[task_service] 终态载荷 pipeline_id 与事件坐标不一致，退回回查 | event=%s payload=%s",
            pipeline_id,
            payload_pid,
        )
        return None
    return {**payload, "pipeline_id": pipeline_id}


async def _list_state_rows(state_capability: Any) -> list[dict[str, Any]] | None:
    """pipeline-state.list 全行读取（派生回退 + 唤醒路由共用的读面单点）。

    None = 读面故障（异常或非 list 形状）——调用方按各自契约处置
    （派生回退：不派生；唤醒路由：暂态扣发）。
    """
    try:
        rows = await state_capability.call("list", {})
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict)]


async def _lookup_state_row(
    pipeline_id: str, state_capability: Any
) -> dict[str, Any] | None:
    """回退路径：经 pipeline-state.list 按 pipeline_id 取摘要行。

    仅用于载荷缺席（旧内核 / 合成事件 / 防御网路径）——该读面过 ``export_fields``
    白名单，未声明的键（如 ``lineage.parent_pipeline_id``）会被静默过滤，
    故不能作为主路径。读面故障返回 None（调用方按未派生处理）。
    """
    rows = await _list_state_rows(state_capability)
    if rows is None:
        logger.warning(
            "[task_service] state 摘要回查失败（终态载荷缺席时无派生）| pipeline=%s",
            pipeline_id,
        )
        return None
    return next(
        (r for r in rows if str(r.get("pipeline_id") or "") == pipeline_id),
        None,
    )


async def handle_run_started_event(params: dict[str, Any], state_capability: Any) -> bool:
    """新 run 启动 → 任务激活：task.status 复位 running（复活语义单点）。

    复活语义（用户裁定 2026-09-30）：continue/重新执行一个任务后，任务状态
    必须是 running。task.status 的聚合投影承载上一次运行的终态（failed 等），
    state 聚合读面（stop_check 判死依据）不随新 run 启动自动刷新——历史
    终态会把刚启动的新 run 立即判死，故任务激活时在此单点复位。

    写面 = pipeline-state.update（DB pipeline_state 表 + 内存 registry，聚合
    读面同源）。running 不重复写（幂等省写）；用户侧终态豁免（completed =
    用户手动完成，deleted = 用户侧删除署名——新 run 不推翻用户显式裁决；
    词汇收敛 ADR 2026-10-30：cancelled 退役，stopped 可重入不在豁免列）；
    chat 会话管道（is_task_state 判据）与读/写面故障均不写不抛——复位失败
    仅留痕，不阻断 run。

    owned 镜像已退役（ADR 2026-11-02-state-key-governance）：复活只写自身
    投影，父管道侧无镜像可刷。

    Returns:
        是否发生了复位写。
    """
    pipeline_id = str(params.get("pipeline_id") or "")
    if not pipeline_id:
        return False
    row = await _lookup_state_row(pipeline_id, state_capability)
    # 复位门 = 任务声明 ∧ 任务管道身份（ADR 2026-11-06 D1，与派生门同款
    # 组合；running/completed/deleted 豁免列表保留）：会话主管道的残留
    # （幽灵 stopped 等）不触发复位写——overlay 式复活只作用于任务管道。
    if row is None or not is_task_state(row) or not is_task_pipeline_row(row):
        return False
    status = str(row.get("task.status") or "")
    if status == "running" or status in _USER_TERMINAL_STATUSES:
        return False
    try:
        await state_capability.call(
            "update",
            {"pipeline_id": pipeline_id, "fields": {"task.status": "running"}},
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[task_service] 新 run 启动复位写失败（不阻断 run）| pipeline=%s | prev=%s",
            pipeline_id,
            status,
        )
        return False
    logger.info(
        "[task_service] 新 run 启动复位任务状态（历史终态残留清除）| pipeline=%s | %s → running",
        pipeline_id,
        status,
    )
    return True


def task_terminal_ws_frame(event_name: str, tags: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """任务域终态事件 → 前端 WS 帧（frontend.emit 载荷）。

    投递腿（P-19/U-1 后端接线）：域事件按设计不达前端 WS（event-bus 仅供
    生命周期订阅者），插件到前端的一次性出口 = frontend.emit → 内核
    session.emit_event（task_deleted 通知先例）。路由坐标 = 任务自身管道
    （thread_id=pipeline_id=task_id：run 启动时内核 on_round_start 已注册
    route_id → user 映射，thread→user 反查即目标 user 过滤）。出帧面 =
    task_completed + task_failed（TASK_FAILED 前端词汇在
    ws_events.yaml server_events 名单，P-129 失败通知）；帧负载形态两者
    同款（任务 id/标题/路由坐标/user_id/status），失败帧另含失败归因
    error + failure_class/failure_reason（tags 空串不携带，completed 帧
    载荷形态保持原样只有 error）。task_cancelled
    发射源在取消写面（http_api），不在派生面双发。无路由坐标
    （pipeline_id 空）不出帧。
    """
    if event_name not in ("task_completed", "task_failed"):
        return None
    pipeline_id = str(tags.get("pipeline_id") or "")
    if not pipeline_id:
        return None
    payload: dict[str, Any] = {
        "task_id": pipeline_id,
        "title": str(tags.get("title") or ""),
        "thread_id": pipeline_id,
        "pipeline_id": pipeline_id,
        "user_id": str(tags.get("user_id") or ""),
        "status": "completed" if event_name == "task_completed" else "failed",
    }
    error = str(tags.get("error") or "")
    if error:
        payload["error"] = error
    if event_name == "task_failed":
        for key in ("failure_class", "failure_reason"):
            value = str(tags.get(key) or "")
            if value:
                payload[key] = value
    return (event_name, payload)


def _parent_wakeup_blocked(parent_row: dict[str, Any] | None) -> bool:
    """结算唤醒守卫（ADR 2026-11-06 D2 三分，行级纯判定）。

    对 2026-10-30 §4.3 的收窄（非放宽）：拦截只作用于**任务管道父**——

    - 父行缺席 → False（fail-open 放行：行缺席 = 管道已亡，投递失败由
      注入侧留痕）；
    - 父行非 :func:`is_task_pipeline_row`（会话/主管道）→ False（一律放行，
      无论其 task.status 残留为何值——装机 2026-11-06 事故：会话幽灵 failed
      残留拦掉了子任务结算通知）；
    - 任务管道父且 ``task.status ∈ {completed, failed}`` → True（跳过唤醒，
      转血缘冒泡）；stopped/running 等其它 → False（ADR 2026-10-30 stopped
      放行语义保留）。

    读面异常（list 抛错等暂态故障）由调用方在路由面处置（扣发 + 保留
    挂号，见 handle_run_terminal_event）——降级窗直送可能唤醒终态任务父
    （旧缺陷），扣发交补发面按恢复后的正确路由重投。
    """
    if parent_row is None or not is_task_pipeline_row(parent_row):
        return False
    return str(parent_row.get("task.status") or "") in _PARENT_WAKE_BLOCKED_STATUSES


async def _clear_parent_registration(
    tags: dict[str, Any], state_capability: Any
) -> None:
    """父管道子任务挂号键清除（信号③回执）：清除失败仅告警，不破坏事件派生。"""
    clear_fields = pending_registration_clear_fields(tags)
    if clear_fields is None:
        return
    try:
        await state_capability.call(
            "update",
            {
                "pipeline_id": str(tags.get("parent_pipeline_id") or ""),
                "fields": clear_fields,
            },
        )
        logger.info(
            "[task_service] 父管道子任务挂号已清除 | parent=%s | fields=%s",
            tags.get("parent_pipeline_id"),
            clear_fields,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[task_service] 父管道挂号键清除失败（不影响事件派生）| parent=%s | fields=%s",
            tags.get("parent_pipeline_id"),
            clear_fields,
        )


async def handle_run_terminal_event(
    event_name: str,
    params: dict[str, Any],
    state_capability: Any,
    bus_capability: Any,
    frontend_emitter: Any = None,
) -> int:
    """入口：取终态 state → 派生 → 经 event-bus.emit_domain 发回域总线。

    数据来源（ADR 2026-09-11）：优先取事件自带的 ``state`` 载荷（内核终态
    转发该管道 state，剥离 messages/raw_* 等引擎大字段）——零回查，且不受
    state 摘要出口白名单约束（``lineage.parent_pipeline_id`` 曾被摘要过滤，
    致父锚点恒空、通知链静默断）。载荷缺席（旧内核 / 合成事件）时回退
    ``pipeline-state.list`` 回查，兼容不破。

    派生出的任务终态事件同时清除父管道的子任务挂号键（信号③闭环：父管道
    收束等待 → 子任务终态唤醒 + 挂号解除）；清除失败仅告警，不破坏事件派生。

    终态随写（谁终态谁随写）：终态事件落终态时先随写自身投影，再派发事件
    ——写面先于通知（父会话被唤醒读投影时投影已是终态）。

    Args:
        event_name: run.completed / run.failed。
        params: 域事件标签（取 pipeline_id 定位管道；state 为终态载荷）。
        state_capability: pipeline-state 能力句柄（回退回查 / update 随写与清挂号）。
        bus_capability: event-bus 能力句柄（call emit_domain）。
        frontend_emitter: SDK FrontendEmitter（frontend.emit WS 腿）；None =
            frontend 能力缺席（旧内核），只走域总线不推前端。

    Returns:
        派生发出的事件数（0 = 未派生）。
    """
    pipeline_id = str(params.get("pipeline_id") or "")
    if not pipeline_id:
        return 0
    row = _payload_state(params, pipeline_id)
    rows_cache: list[dict[str, Any]] | None = None
    if row is None:
        rows_cache = await _list_state_rows(state_capability)
        if rows_cache is None:
            logger.warning(
                "[task_service] state 摘要回查失败（终态载荷缺席时无派生）| pipeline=%s",
                pipeline_id,
            )
            return 0
        row = next(
            (r for r in rows_cache if str(r.get("pipeline_id") or "") == pipeline_id),
            None,
        )
    if row is None:
        return 0
    emitted = 0
    for name, tags in derive_task_terminal_events(event_name, row):
        # 谁终态谁随写：终态事件落终态时，自身投影随写
        # （写面先于通知——父会话被唤醒读投影时投影已是终态）。
        await _follow_write_terminal_projection(
            name, tags, row, pipeline_id, state_capability
        )
        # WS 腿先于路由决策（ADR 2026-11-06 D6：凡派生必发，守卫/冒泡不得
        # 吞帧——终态遥测与唤醒路由解耦，修复守卫 continue 吞帧导致的面板
        # 终态滞后）。发射失败静默降级（观测出口不阻断派生主流程）。
        frame = task_terminal_ws_frame(name, tags)
        if frame is not None and frontend_emitter is not None and frontend_emitter.available:
            try:
                await frontend_emitter.emit(*frame)
            except Exception:  # noqa: BLE001 — 观测推送失败不影响派生
                logger.warning(
                    "[task_service] 前端 WS 帧推送失败 | event=%s | pipeline_id=%s",
                    frame[0], tags.get("pipeline_id"),
                )
        # 结算唤醒路由（ADR 2026-11-06 D2/D3/D4）：守卫身份前提 + 血缘冒泡。
        # list 一次建 by_pid 映射（载荷缺席路径复用派生回退读到的行集）。
        parent_pipeline_id = str(tags.get("parent_pipeline_id") or "")
        if rows_cache is None:
            rows_cache = await _list_state_rows(state_capability)
            if rows_cache is None:
                logger.warning(
                    "[task_service] 结算唤醒路由读面故障（暂态扣发，挂号保留，"
                    "补发面按恢复后路由重投）| event=%s | parent=%s | child=%s | task=%s",
                    name,
                    parent_pipeline_id,
                    tags.get("pipeline_id"),
                    tags.get("task_id"),
                )
                continue
        target, dead_end, chain = resolve_wakeup_target(rows_cache, parent_pipeline_id)
        if dead_end:
            # 结构性死路（链顶终态任务/环/祖先行缺失）：处置非漏发——清挂号
            # 防补发面每轮装载重复触发；重试同一条死路是噪声。
            logger.warning(
                "[task_service] 子任务结算唤醒被拒绝（父任务已真终态且血缘冒泡"
                "结构性死路：链顶终态任务/环/祖先行缺失，守卫 ADR 2026-10-30/"
                "2026-11-06）| event=%s | parent=%s | child=%s | task=%s | chain=%s",
                name,
                parent_pipeline_id,
                tags.get("pipeline_id"),
                tags.get("task_id"),
                chain,
            )
            await _clear_parent_registration(tags, state_capability)
            continue
        # tag 契约（字段只增不删）：parent_pipeline_id 恒=直接父（reconcile
        # 回执定位零改动）；wakeup_target_pipeline_id = 实际投递目标（=直接父
        # 时也显式写，便于观测）。
        tags["wakeup_target_pipeline_id"] = target
        if target and target != parent_pipeline_id:
            logger.info(
                "[task_service] 结算唤醒血缘冒泡上送 | event=%s | 直接父=%s | "
                "投递目标=%s | chain=%s",
                name, parent_pipeline_id, target, chain,
            )
        try:
            await bus_capability.call("emit_domain", {"event": name, "tags": tags})
            emitted += 1
            logger.info(
                "[task_service] 任务域事件派生 | event=%s | pipeline_id=%s | task_id=%s",
                name, tags.get("pipeline_id"), tags.get("task_id"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[task_service] emit_domain 失败 | event=%s", name)
        # 回执照写照走（送达闭环在直接父，注入目标在冒泡终点——两件事分离）。
        await _clear_parent_registration(tags, state_capability)
    return emitted

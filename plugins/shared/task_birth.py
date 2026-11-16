"""任务管道统一出生协议 —— task_submit 工具与 tasks http_api 共用的唯一出生写面。

设计裁定（2026-08-28 用户裁定）：「任务提交的时候就把任务相关的（比如父任务是
什么）放到 state 里面」「不要有降级路径，失败就报错」。

两路径（LLM 工具提交 / HTTP 手动创建）此前各自拼装 chat.send_message 参数，
出生记录不齐：
- task.id 迟到——引擎在创建分支才生成 pipeline_id，调用方派发时还不知道；单次
  create+background 派发把 state 原样带进引擎后立即开跑，任务身份键只能等 main
  体输入插件（context_build）推导，而 init 体插件（workspace_lifecycle 的工作
  区共享决策）先于它运行——子任务被当主会话落独立目录；
- 出生写面参数漂移——两路径各自维护 state 键集与会话归属参数，契约无法机械对账，
  出生记录缺键（如会话归属）即静默变异形态。

四段式出生协议（本模块唯一实现；任何一段失败抛 TaskBirthError，无降级路径；
ADR-2026-10-17-task-birth-workspace-inclusion 由三段扩四段）：
1. 出生登记（``create + no_dispatch``）：引擎生成 pipeline_id；内核创建分支落
   pipeline_sessions 映射（子任务终态唤醒注入的反查锚点）并把出生键逐键落表
   （冷读有基线）；no_dispatch 只登记不派发——执行必须等身份完整（阶段二）。
2. 身份登记（no_dispatch 注入）：``task.id = pipeline_id`` 写入出生 state
   （registry 热路径 + pipeline_state 表冷兜底）——init 体插件的工作区/隔离
   决策、task_reminder 的任务闸门、终态事件的 task_id 标签均以该键为判据。
3. **工作空间创建（同步准备，ADR 2026-10-17 新增段）**：工作空间目录名需
   ``task_id``，故必须夹在身份登记之后；又须在派发之前完成，才能兑现「派发时
   工作空间一定就绪」。复用 workspace_lifecycle 的四形态创建逻辑（不经提交层
   重造），创建结果（``workspace`` + ``task.ws_meta``）先经 no_dispatch 落
   state 再派发。失败即失败：保留任务记录（``task.status=failed`` +
   ``task.error=原因``）后抛错——不留记录则磁盘留下无任务归属的孤儿 worktree，
   比留一条 failed 记录更难清理。
    **继承坐标例外**（2026-10-20 用户裁定《继承机制简化方案》）：出生 state 带
   ``task.inherit_from``（接续源任务 id）+ 整份复制的 ``task.ws_meta`` 时，本段
   **无创建动作**——准备器走幂等跳过分支返回空坐标更新属预期，以继承坐标
   为准；准备器交出不一致的落点则判失败（坐标漂移不静默放行）。
4. 执行派发（注入分支 + background）：kickoff 消息 + execution_context
   （工作区/隔离声明，dispatch 合并点并入 initial_state）启动管道。

出生 state 内容（task.*/lineage.* 扁平键）由调用方构造——任务域语义（验收标准/
血缘有父与根二选一/scope）归各自持有；本模块固化「出生协议」本身：三段顺序、
键完整性与失败语义。血缘键可含 ``lineage.parent_ws_meta``（父管道工作空间坐标
快照，task_submit 经 param_inject 权威注入后随出生写全）：子任务 workspace_
lifecycle 的共享决策优先消费它——父管道运行中 registry 行尚未建立，仅靠聚合
解析存在发起瞬间的可见性时序窗口（2026-08-29 诊断：同会话子任务工作空间漂移）；
父链聚合查找仍为继承缺失时的次级来源，ws_meta 的读写权威始终在
workspace_lifecycle。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class TaskBirthError(RuntimeError):
    """任务管道出生失败（四段式任一阶段）。

    携带阶段标签与内核原始原因（__cause__ 保留），调用方必须显式报错，
    禁止吞掉后按部分成功继续。

    ``phase``：失败所处阶段名（「出生登记」/「身份登记」/「工作空间创建」/
    「执行派发」），供调用方区分失败面并选对错误码（提交接口按
    WORKSPACE_PREPARE_FAILED 回传「任务已记录为失败」而非「任务不存在」）。
    """

    def __init__(self, message: str, phase: str = "") -> None:
        super().__init__(message)
        self.phase = phase


#: 工作空间准备失败时的阶段标签（调用方据此选错误码）
WORKSPACE_PREPARE_PHASE = "工作空间创建"


async def birth_task_pipeline(
    send: Any,
    *,
    title: str,
    birth_state: dict[str, Any],
    kickoff: str,
    user_id: str,
    prepare_workspace: Any,
    agent_id: str = "",
    execution_context: dict[str, Any] | None = None,
    thread_id: str = "",
) -> str:
    """按四段式协议出生一条任务执行管道，返回任务 id（= 引擎管道 id）。

    Args:
        send: ``async send(params: dict) -> dict``——内核 chat.send_message
            调用通道（task_submit 传注入的派发器，tasks http_api 传
            capability call 适配器）。
        title: 任务标题（出生登记消息文案用）。
        birth_state: 出生 state（task.*/lineage.* 扁平键，非空——空即协议违约）。
        kickoff: 执行派发的 kickoff 消息（阶段四正文）。
        user_id: 提交者（四段必须同值——内核按 user_id 反查 tenant，异值会
            把出生 state 与执行派发落进不同租户）。
        prepare_workspace: ``async prepare_workspace(*, pipeline_id, state,
            execution_context) -> dict``——工作空间同步准备器（阶段三）；返回
            工作空间坐标 state 更新（须含非空 ``workspace``），失败抛异常。
            缺失即协议违约（出生协议无「跳过准备」路径）。
        agent_id: 目标执行 agent（缺省内核落主 agent）。
        execution_context: 工作区/隔离声明（阶段三准备入参 + 阶段四 dispatch
            合并点消费）。
        thread_id: 归属会话（根任务透传用户会话；缺省管道保持独立，
            pipeline_sessions 落自环映射仍可反查）。

    Returns:
        pipeline_id（引擎生成，12 位短 id，即 task.id）。

    Raises:
        TaskBirthError: 任一阶段调用失败 / 响应缺 pipeline_id / 阶段二四
            回带 id 与出生 id 不一致（误路由防护）/ 工作空间未就绪。
    """
    if not isinstance(birth_state, dict) or not birth_state:
        raise TaskBirthError(
            "任务管道出生 state 为空：task.*/lineage.* 出生键必须一次写全", "出生登记"
        )
    if not user_id:
        raise TaskBirthError(
            "任务管道出生缺 user_id：内核 chat.send_message 硬校验非空（tenant 反查）",
            "出生登记",
        )
    if prepare_workspace is None:
        raise TaskBirthError(
            "任务管道出生缺工作空间准备器：工作空间创建是出生协议第三段，无跳过路径",
            WORKSPACE_PREPARE_PHASE,
        )

    # 执行 agent 身份属管道快照（agent.id 自由字段，出生一次写全）：续跑
    # （task_manage 注入/重试等无 agent_id 的派发）与内核重启冷恢复据此解析，
    # 不随催促方线程的绑定漂移。内核对键语义零知识，只透传落库。
    if agent_id:
        birth_state = {**birth_state, "agent.id": agent_id}

    # 统一管道标题（ADR 2026-10-27-pipeline-title-field）：任务标题即管道
    # 标题，出生写一次（任务标题不再变更，无传播面）。title 与 task.goal
    # 同值不同义——title 是引擎域命名键（面板/推送单字段消费），task.goal
    # 是任务域验收目标键，互不替代。
    birth_state = {**birth_state, "title": title}

    # ── 阶段一：出生登记（create + no_dispatch，只登记不派发）──
    birth_params: dict[str, Any] = {
        "create": True,
        "no_dispatch": True,
        "message": f"登记任务「{title}」。",
        "user_id": user_id,
        "state": birth_state,
    }
    if agent_id:
        birth_params["agent_id"] = agent_id
    if thread_id:
        birth_params["thread_id"] = thread_id
    pipeline_id = await _send_phase(send, birth_params, "出生登记")

    # ── 阶段二：身份登记（task.id = 管道 id，先于任何管道步骤执行）──
    await _send_phase(
        send,
        {
            "pipeline_id": pipeline_id,
            "no_dispatch": True,
            "message": f"任务身份登记（{pipeline_id}）。",
            "user_id": user_id,
            "state": {"task.id": pipeline_id},
        },
        "身份登记",
        expect_pipeline_id=pipeline_id,
    )

    # ── 阶段三：工作空间创建（同步准备，失败即失败并回传）──
    # 准备器看到的 state = 阶段二之后的管道 state（task.id 已在位——工作空间
    # 目录名与继承决策均以该键为判据，与 init 体同形）。
    ws_state = await _prepare_task_workspace(
        send,
        prepare_workspace,
        pipeline_id,
        user_id,
        {**birth_state, "task.id": pipeline_id},
        execution_context or {},
    )
    await _send_phase(
        send,
        {
            "pipeline_id": pipeline_id,
            "no_dispatch": True,
            "message": f"任务工作空间登记（{pipeline_id}）。",
            "user_id": user_id,
            "state": {"task.id": pipeline_id, **ws_state},
        },
        WORKSPACE_PREPARE_PHASE,
        expect_pipeline_id=pipeline_id,
    )

    # ── 阶段四：执行派发（注入分支 + background，启动即带完整出生 state）──
    dispatch_params: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "message": kickoff,
        "user_id": user_id,
        "background": True,
    }
    if agent_id:
        dispatch_params["agent_id"] = agent_id
    if execution_context:
        dispatch_params["execution_context"] = execution_context
    try:
        await _send_phase(
            send, dispatch_params, "执行派发", expect_pipeline_id=pipeline_id
        )
    except TaskBirthError as exc:
        # 派发调用本身抛错（__cause__ 非空 = send 抛的）→ 内核未 spawn，任务
        # 永远不会跑起来，写失败终态免得面板上悬一条永不推进的 pending；
        # 工作空间保留不删——任务记录就是它的清理锚点。
        # 响应形态异常（__cause__ 为空：缺 id / id 不匹配）→ 内核 spawn 已发生，
        # 任务真在跑，改写状态会谎报，零写直接上报。
        if exc.__cause__ is None:
            raise
        await _mark_birth_failed(
            send, pipeline_id, user_id, f"执行派发失败: {exc}"
        )
        raise

    logger.info(
        "[task_birth] 任务管道已出生 | task=%s | title=%s | thread=%s | agent=%s",
        pipeline_id,
        title,
        thread_id or "-",
        agent_id or "-",
    )
    return pipeline_id


async def _prepare_task_workspace(
    send: Any,
    prepare_workspace: Any,
    pipeline_id: str,
    user_id: str,
    birth_state: dict[str, Any],
    execution_context: dict[str, Any],
) -> dict[str, Any]:
    """阶段三本体：调用准备器并校验就绪，失败则保留任务记录后抛错。

    准备器契约 = 返回工作空间坐标 state 更新（``workspace`` 为真实存在路径
    字符串）。未交出真实坐标（空 dict / 空串 / 非字符串）同判未就绪——禁止
    幻影路径裸奔（落库即谎报坐标，续跑轮会踩空壳）。

    **继承坐标例外**：出生 state 带 ``task.ws_meta``（源任务坐标整份复制，
    提交期已校验目录存在 + git 身份有效）时，工作空间**无创建动作**——准备器
    走幂等跳过分支返回空坐标更新属预期，此时以继承坐标为准；准备器交出与
    继承坐标**不一致**的落点则判失败（坐标漂移不得静默放行）。
    """
    try:
        ws_state = await prepare_workspace(
            pipeline_id=pipeline_id,
            state=birth_state,
            execution_context=execution_context,
        )
    except Exception as exc:
        await _mark_birth_failed(send, pipeline_id, user_id, f"工作空间创建失败: {exc}")
        raise TaskBirthError(
            f"任务管道{WORKSPACE_PREPARE_PHASE}失败: {exc}", WORKSPACE_PREPARE_PHASE
        ) from exc

    ws_state = ws_state if isinstance(ws_state, dict) else {}
    if birth_state.get("task.inherit_from"):
        # 继承坐标（出生 state 带 task.inherit_from + 整份复制的 task.ws_meta）：
        # 工作空间**无创建动作**——准备器走幂等跳过分支返回空坐标更新属预期，
        # 以继承坐标为准；准备器交出与继承坐标不一致的落点则判失败（坐标漂移
        # 不得静默放行）。
        inherited = birth_state.get("task.ws_meta")
        inherited_meta = dict(inherited) if isinstance(inherited, dict) else {}
        inherited_path = str(inherited_meta.get("path") or birth_state.get("workspace") or "")
        prepared_path = str(ws_state.get("workspace") or "")
        if not inherited_path:
            reason = "继承坐标缺 path（出生 state 无 task.ws_meta.path）"
            await _mark_birth_failed(send, pipeline_id, user_id, reason)
            raise TaskBirthError(
                f"任务管道{WORKSPACE_PREPARE_PHASE}失败: {reason}", WORKSPACE_PREPARE_PHASE
            )
        if not prepared_path:
            ws_state = {"workspace": inherited_path, "task.ws_meta": inherited_meta}
        elif not _same_path(prepared_path, inherited_path):
            reason = (
                f"继承坐标与工作空间准备结果不一致: 继承={inherited_path} "
                f"准备={prepared_path}（坐标漂移，拒绝以漂移落点派发执行）"
            )
            await _mark_birth_failed(send, pipeline_id, user_id, reason)
            raise TaskBirthError(
                f"任务管道{WORKSPACE_PREPARE_PHASE}失败: {reason}", WORKSPACE_PREPARE_PHASE
            )
        else:
            ws_state = {**ws_state, "task.ws_meta": inherited_meta}

    workspace = ws_state.get("workspace")
    if not (isinstance(workspace, str) and workspace.strip()):
        reason = (
            f"工作空间创建未返回有效路径: {ws_state!r}"
            "（未就绪，拒绝以幻影工作空间派发执行）"
        )
        await _mark_birth_failed(send, pipeline_id, user_id, reason)
        raise TaskBirthError(
            f"任务管道{WORKSPACE_PREPARE_PHASE}失败: {reason}", WORKSPACE_PREPARE_PHASE
        )
    return ws_state


def _same_path(left: str, right: str) -> bool:
    """路径同一性判定（大小写/分隔符归一后比较）。

    Windows 路径大小写不敏感、POSIX 敏感——按平台归一：Windows 折叠大小写
    并统一分隔符，POSIX 原样比较。
    """
    def _norm(path: str) -> str:
        text = str(path).replace("\\", "/").rstrip("/")
        return text.lower() if os.name == "nt" else text

    return _norm(left) == _norm(right)


async def _mark_birth_failed(send: Any, pipeline_id: str, user_id: str, reason: str) -> None:
    """出生未完成：把失败终态写进已出生任务的 state（保留任务记录）。

    任务记录已落库（阶段一），不写失败终态等于面板上悬一条永不推进的
    ``pending``——保留并标 failed 才是清理锚点。随写自身失败只 ERROR 留痕
    不改判（原始原因始终在抛出的异常消息里）。
    """
    try:
        await _send_phase(
            send,
            {
                "pipeline_id": pipeline_id,
                "no_dispatch": True,
                "message": f"任务出生未完成（{pipeline_id}）。",
                "user_id": user_id,
                "state": {
                    "task.id": pipeline_id,
                    "task.status": "failed",
                    "task.error": reason,
                },
            },
            "失败随写",
            expect_pipeline_id=pipeline_id,
        )
    except Exception as exc:  # noqa: BLE001 — 随写失败不改判（原始原因在异常里）
        logger.error(
            "[task_birth] 出生失败随写失败（任务记录将无失败原因）| task=%s | err=%s",
            pipeline_id,
            exc,
        )



async def _send_phase(
    send: Any,
    params: dict[str, Any],
    phase: str,
    expect_pipeline_id: str = "",
) -> str:
    """执行单阶段 send 并校验响应（失败/缺 id/id 不匹配一律 TaskBirthError）。"""
    try:
        resp = await send(params)
    except Exception as exc:
        raise TaskBirthError(f"任务管道{phase}失败: {exc}") from exc
    pipeline_id = str(resp.get("pipeline_id") or "") if isinstance(resp, dict) else ""
    if not pipeline_id:
        raise TaskBirthError(f"任务管道{phase}响应缺少 pipeline_id: {resp!r}")
    if expect_pipeline_id and pipeline_id != expect_pipeline_id:
        raise TaskBirthError(
            f"任务管道{phase}响应 pipeline_id 不匹配: {pipeline_id!r} != "
            f"{expect_pipeline_id!r}（出生 id 与后续阶段坐标不一致，拒绝继续）"
        )
    return pipeline_id

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

三段式出生协议（本模块唯一实现；任何一段失败抛 TaskBirthError，无降级路径）：
1. 出生登记（``create + no_dispatch``）：引擎生成 pipeline_id；内核创建分支落
   pipeline_sessions 映射（子任务终态唤醒注入的反查锚点）并把出生键逐键落表
   （冷读有基线）；no_dispatch 只登记不派发——执行必须等身份完整（阶段三）。
2. 身份登记（no_dispatch 注入）：``task.id = pipeline_id`` 写入出生 state
   （registry 热路径 + pipeline_state 表冷兜底）——init 体插件的工作区/隔离
   决策、task_reminder 的任务闸门、终态事件的 task_id 标签均以该键为判据。
3. 执行派发（注入分支 + background）：kickoff 消息 + execution_context
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
from typing import Any

logger = logging.getLogger(__name__)


class TaskBirthError(RuntimeError):
    """任务管道出生失败（三段式任一阶段）。

    携带阶段标签与内核原始原因（__cause__ 保留），调用方必须显式报错，
    禁止吞掉后按部分成功继续。
    """


async def birth_task_pipeline(
    send: Any,
    *,
    title: str,
    birth_state: dict[str, Any],
    kickoff: str,
    user_id: str,
    agent_id: str = "",
    execution_context: dict[str, Any] | None = None,
    thread_id: str = "",
) -> str:
    """按三段式协议出生一条任务执行管道，返回任务 id（= 引擎管道 id）。

    Args:
        send: ``async send(params: dict) -> dict``——内核 chat.send_message
            调用通道（task_submit 传注入的派发器，tasks http_api 传
            capability call 适配器）。
        title: 任务标题（出生登记消息文案用）。
        birth_state: 出生 state（task.*/lineage.* 扁平键，非空——空即协议违约）。
        kickoff: 执行派发的 kickoff 消息（阶段三正文）。
        user_id: 提交者（三段必须同值——内核按 user_id 反查 tenant，异值会
            把出生 state 与执行派发落进不同租户）。
        agent_id: 目标执行 agent（缺省内核落主 agent）。
        execution_context: 工作区/隔离声明（阶段三 dispatch 合并点消费）。
        thread_id: 归属会话（根任务透传用户会话；缺省管道保持独立，
            pipeline_sessions 落自环映射仍可反查）。

    Returns:
        pipeline_id（引擎生成，12 位短 id，即 task.id）。

    Raises:
        TaskBirthError: 任一阶段调用失败 / 响应缺 pipeline_id / 阶段二三
            回带 id 与出生 id 不一致（误路由防护）。
    """
    if not isinstance(birth_state, dict) or not birth_state:
        raise TaskBirthError(
            "任务管道出生 state 为空：task.*/lineage.* 出生键必须一次写全（用户裁定："
            "任务提交时就把任务相关的放到 state 里面）"
        )
    if not user_id:
        raise TaskBirthError(
            "任务管道出生缺 user_id：内核 chat.send_message 硬校验非空（tenant 反查）"
        )

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

    # ── 阶段三：执行派发（注入分支 + background，启动即带完整出生 state）──
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
    await _send_phase(
        send, dispatch_params, "执行派发", expect_pipeline_id=pipeline_id
    )

    logger.info(
        "[task_birth] 任务管道已出生 | task=%s | title=%s | thread=%s | agent=%s",
        pipeline_id,
        title,
        thread_id or "-",
        agent_id or "-",
    )
    return pipeline_id


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

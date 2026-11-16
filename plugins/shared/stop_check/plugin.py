"""停止检查 Output 插件 — 迭代/超时超线检测 + 评估结果/任务终态收束。

负责在管道循环的输出阶段统一管理"停止判断"逻辑（控制状态键契约
ADR 2026-08-30：终止经状态键 should_stop/ended 表达，引擎轮边界消费；
终止方随写 router.stop_reason 署名，run 收尾按署名映射终态）：
1. 轮次上限（按 LLM 调用次数计）/超时检测 → should_stop=true + task.status=failed + 会话 system 消息
2. task_evaluate 工具结果检测 → ended=true
3. 任务真终态（完成/失败，外部写入）检测 → ended=true + router.stop_reason
   署名（ADR 2026-10-30 收权：不再写 task.status，裁决权归任务域；
   stopped 暂停任务不触发驱停）

State 命名空间：
    - router.stop_reason : 终止署名键（引擎收尾映射终态的唯一依据）
    - run_started_at : run 起点墙钟（内核 run_init 写入，执行超时闸门的计时锚）
"""

from __future__ import annotations

import inspect
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from _libs.pipeline_base.plugin import IOutputPlugin, OutputResult, PluginContext
from _libs.pipeline_base.types import StateKeys

# state 聚合读取器（server.py on_load 经 pipeline-state capability 注入）：
# 任务实时状态的唯一可靠来源——外部终态写入（task_evaluate 经
# pipeline-state.update 写 task.status）对运行中循环的内存态不可见，
# 停止检测必须走聚合读面（用户裁定：任务终态当轮停止，不允许空转）。
_state_reader: Any = None


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（约定 () -> list[dict] | None，sync/async 均可）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def _get_state_reader() -> Any:
    return _state_reader

logger = logging.getLogger(__name__)

# 执行超时闸门的计时锚（run 真实起点墙钟）：内核 run_init 逐 run 覆盖写
# state.run_started_at（RFC3339，单写者契约保证 run 内恒定）——真实 run
# 时长与其他管道的插队无关。
_RUN_STARTED_AT_KEY = "run_started_at"
# 回落计时锚（run_started_at 缺席时用）的记账容量上限：超出逐出最旧起点。
_MAX_TRACKED_RUNS = 64


def _utc_now() -> datetime:
    """当前 UTC 时刻（抽成函数便于时序打桩，不改全局 time）。"""
    return datetime.now(UTC)


def _parse_run_started_at(raw: Any) -> datetime | None:
    """解析 run 起点墙钟（RFC3339）为 UTC 时刻；无法解析返回 None。

    内核 chrono 输出纳秒精度（Python datetime 只认微秒）——小数位截断到
    6 位后解析；"Z" 后缀与无时区输入分别按 UTC 处理。解析失败不静默当 0，
    由调用方回落记账锚。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class StopCheckPlugin(IOutputPlugin):
    """停止检查 Output 插件——迭代上限 / 超时 / 评估终态 / 任务终态。

    检查维度（按优先级）：
    1. 轮次上限检测 → LLM 调用次数（track.llm_usage.total_calls，缺席回退引擎
       iteration 锚）> max_iterations——轮次按大模型调用次数计（用户裁定
       2026-10-17；形态拆分管道一 LLM↔工具周期占 2 迭代，iteration 锚会把
       预算隐式减半）
    2. 执行超时检测 → elapsed ≥ max_duration（elapsed = 本 run 的真实运行时长，
       计时锚 state.run_started_at；时间预算已退役：配置缺省 -1，agent 面不再
       下发 timeout_seconds——任务可合法运行数小时，防挂死由 plugin_call_timeout
       单次调用上界承接）
    3. task_evaluate 工具结果检测 → completed/failed
    4. 任务真终态检测 → completed/failed 触发 ended + 驱停署名（stopped
       暂停任务不触发；ADR 2026-10-30 收权）

    should_stop 的引擎侧消费（含用户停止）不经过本插件——插件只写状态键。

    Attributes:
        _config: 插件配置字典
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化停止检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - max_iterations: 最大 LLM 调用次数（默认 20）
                - max_duration_seconds: 最大执行时间秒数（默认 -1 无限制；
                  时间预算退役，用户裁定 2026-10-17）
                - check_task_status: 是否检查任务状态（默认 True）
        """
        self._config = config or {}
        self._max_iterations = self._config.get("max_iterations", 20)
        self._max_duration = self._config.get("max_duration_seconds", -1)
        self._check_task_status = self._config.get("check_task_status", True)
        # 回落计时锚：按 (pipeline_id, run_id) 记账的 run 起点（单调钟）——
        # run_started_at 缺席时用，各 run 各自记账、互不污染。
        self._run_starts: dict[tuple[str, str], float] = {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "stop_check"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 1)

    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号类型。"""
        return ["end"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行停止检查。

        依次检查所有停止条件，任一条件满足即返回 end 路由信号。
        优先使用 Agent 配置通过 state 注入的参数覆盖构造时默认值。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含路由信号的输出结果（有停止条件时）
        """
        self._apply_runtime_config(ctx)
        result = await self._do_work(ctx)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行停止检查逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            停止检查结果字典
        """
        iteration = ctx.state.get(StateKeys.ITERATION, 0)
        pipeline_id = ctx.state.get("pipeline_id", "?")
        elapsed = self._elapsed_seconds(ctx)
        raw_tc_count = len(ctx.state.get(StateKeys.RAW_TOOL_CALLS, []))
        # 轮次预算锚（用户裁定 2026-10-17：轮次按大模型调用次数计）：主锚 =
        # track.llm_usage.total_calls（llm_core 累计，跨迭代形态稳定）；缺席
        # （旧 state / 无用量管道）回退引擎 iteration 锚（历史语义，形态拆分
        # 管道下该锚把预算隐式减半，仅作兼容兜底）。
        usage = ctx.state.get("track.llm_usage")
        if isinstance(usage, dict) and usage.get("total_calls") is not None:
            rounds = int(usage.get("total_calls") or 0)
            rounds_anchor = "LLM 调用"
        else:
            rounds = int(iteration or 0)
            rounds_anchor = "迭代"
        logger.debug(
            "[%s] pipeline=%s iter=%d rounds=%d(%s) max_iter=%d elapsed=%s/%d "
            "raw_tool_calls=%d",
            self.name,
            pipeline_id,
            iteration,
            rounds,
            rounds_anchor,
            self._max_iterations,
            "n/a" if elapsed is None else f"{elapsed:.1f}",
            self._max_duration,
            raw_tc_count,
        )

        # 1. 轮次上限检测（-1 表示无限制）
        if self._max_iterations != -1 and rounds > self._max_iterations:
            logger.warning(
                "[%s] Max rounds reached: %d > %d (anchor=%s)",
                self.name,
                rounds,
                self._max_iterations,
                rounds_anchor,
            )
            return self._task_failure_stop(
                ctx,
                stop_reason="max_iterations",
                reason=(
                    f"已连续执行 {rounds} 轮（上限 {self._max_iterations} 轮，"
                    f"按{rounds_anchor}计）"
                ),
            )

        # 2. 执行超时检测（阈值 -1 = 无限制；elapsed = 本 run 真实运行时长）
        if self._max_duration != -1:
            if elapsed is None:
                logger.warning(
                    "[%s] 无计时锚（run_started_at 与 run_id 均缺席），本轮跳过超时闸门",
                    self.name,
                )
            elif elapsed >= self._max_duration:
                logger.warning(
                    "[%s] Execution timeout: %.1f >= %d seconds",
                    self.name,
                    elapsed,
                    self._max_duration,
                )
                return self._task_failure_stop(
                    ctx,
                    stop_reason="timeout",
                    reason=f"已连续运行 {elapsed:.1f} 秒（上限 {self._max_duration} 秒）",
                )

        # 3. task_evaluate 工具结果检测
        eval_stop = self._check_task_evaluate_result(ctx)
        if eval_stop:
            return eval_stop

        # 4. 任务状态检测（state 缓存 + TaskService 实际查询）
        if self._check_task_status:
            task_status = await self._check_task_terminal_status(ctx)
            if task_status:
                # 判定源打点（B13① 诊断）：cached = ctx.state 缓存键命中，
                # aggregated = state 聚合实时读——残留终态定位依据。
                source = "cached" if self._cached_terminal_hit(ctx) else "aggregated"
                logger.info(
                    "[%s] Task terminal status detected: %s (source=%s)",
                    self.name,
                    task_status,
                    source,
                )
                # 终态收束两键一次写全（state_updates 带内平铺键，SDK 契约）：
                # - ended=true：引擎循环在本轮末立即 break（任务终态当轮停止，
                #   不允许空转——用户裁定）
                # - router.stop_reason：收束署名（task_failed/task_completed）
                # 不写 task.status（ADR 2026-10-30 收权）：任务终态的裁决权
                # 归任务域（评估闸门/失败链唯一写面），本插件只经状态键通道
                # 署名驱停，不反向裁决任务状态。
                return {
                    "ended": True,
                    "router.stop_reason": f"task_{task_status}",
                }

        # 无触发轮：复位陈旧署名（router.stop_reason 跨 run 非易失，上一 run
        # 的署名不得影响本 run 终态映射）。终止在途（should_stop/ended 已置位）
        # 不得复位——同轮更早写方（task_reminder）
        # 刚落的署名是 run 收尾映射终态的唯一依据，抹掉会把失败误标为完成。
        if ctx.state.get(StateKeys.SHOULD_STOP) or ctx.state.get("ended"):
            return {}
        return {"router.stop_reason": ""}

    def _task_failure_stop(self, ctx: PluginContext, stop_reason: str, reason: str) -> dict[str, Any]:
        """超线（迭代上限/超时）终止：任务管道同时落 task.status=failed。

        任务完成唯一判据是 task_evaluate 评估通过；跑到检测线仍未通过评估
        = 失败（用户裁定）。仅任务管道（state 带 task.id）写任务状态——
        聊天主管道无 task 上下文，写 task.* 会制造幽灵任务标记。

        同时往会话消息流追加一条 system 消息（无 seq 的 set = append，引擎
        merge_and_project 统一落库/投影）：超线强停的 run 用户可见终止原因，
        而非只有 agent 的半截回复。
        """
        updates: dict[str, Any] = {
            "router.stop_reason": stop_reason,
            StateKeys.SHOULD_STOP: True,
        }
        if ctx.state.get("task.id"):
            updates["task.status"] = "failed"
        label = self._STOP_LABELS.get(stop_reason, stop_reason)
        content = f"⚠️ {label}被强制停止：{reason}，进度已保存，可重新发送消息继续。"
        updates["messages"] = {
            "_ops": [{"op": "set", "msg": {"role": "system", "content": content}}]
        }
        return updates

    # stop_reason → 消息文案前缀（仅本插件两个超线分支会传入）
    _STOP_LABELS = {"max_iterations": "达到迭代上限", "timeout": "执行超时"}

    def _check_task_evaluate_result(self, ctx: PluginContext) -> dict[str, Any] | None:
        """检查 task_evaluate 工具执行结果是否表明任务已完成或失败。

        当 task_evaluate 返回 metadata.result 为 completed 或 failed 时，
        任务状态已被 TaskEvaluateTool 变更为终态，管道应立即停止。

        Args:
            ctx: 插件执行上下文

        Returns:
            停止结果字典，无匹配返回 None
        """
        tool_results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
        if not tool_results:
            return None

        for tr in tool_results:
            if not isinstance(tr, dict):
                continue
            tool_name = tr.get("tool_name", "")
            if tool_name != "task_evaluate":
                continue
            data = tr.get("data")
            if not isinstance(data, dict):
                continue
            metadata = data.get("metadata", {})
            if not isinstance(metadata, dict):
                continue
            result = metadata.get("result", "")
            if result in ("completed", "failed"):
                message = metadata.get("message", f"task_evaluate: {result}")
                logger.info(
                    "[%s] task_evaluate result: %s",
                    self.name,
                    result,
                )
                return {
                    "router.stop_reason": f"task_evaluate_{result}",
                    "ended": True,
                }
        return None

    # 驱停署名判定集（ADR 2026-10-30-task-status-authority-vocabulary-
    # convergence 收权）：仅任务域真终态 completed/failed 触发 ended + 驱停
    # 署名。stopped（暂停放那里，可恢复）不触发——放那的任务允许其在飞 run
    # 自然收束/被唤醒，暂停不等于驱停；deleted 走 delete_pipeline 整管清理，
    # 不需要本插件收束。run 域署名词 task_cancelled（内核 from_control_state
    # 硬编码 match 臂）原样保留不改名——本插件声明机制只喂 Failed 词表，
    # 改名会误折 Completed。
    _TERMINAL_STATUSES = frozenset({"completed", "failed"})

    def set_state_reader(self, reader: Any) -> None:
        """注入 state 聚合读取器（server.py on_load 经单例调用）。"""
        set_state_reader(reader)

    def _cached_terminal_hit(self, ctx: PluginContext) -> bool:
        """ctx.state 缓存键（task_status/task.status）是否命中终态（诊断打点用）。"""
        for key in ("task_status", "task.status"):
            if ctx.state.get(key, "") in self._TERMINAL_STATUSES:
                return True
        return False

    async def _check_task_terminal_status(self, ctx: PluginContext) -> str:
        """检查任务是否已到达真终态（完成/失败；stopped 暂停不驱动停）。

        两个检测路径：
        1. ctx.state 缓存键 task_status / task.status（进程内可见的写入）
        2. state 聚合行 task.status 实时读（0.2 单一真值——外部终态写入
           （task_evaluate 经 pipeline-state.update）对运行中循环的内存态
           不可见，聚合是唯一实时来源；用户裁定：任务到达终态当轮停止，
           不允许空转）

        Args:
            ctx: 插件执行上下文

        Returns:
            任务终态字符串，空字符串表示正常运行
        """
        for key in ("task_status", "task.status"):
            cached_status = ctx.state.get(key, "")
            if cached_status in self._TERMINAL_STATUSES:
                return cached_status

        actual_status = await self._check_task_actual_status(ctx)
        if actual_status:
            return actual_status

        return ""

    async def _check_task_actual_status(self, ctx: PluginContext) -> str:
        """从 state 聚合读任务实时状态（0.2 单一真值；每轮查，无节流）。

        外部终态写入（task_evaluate 经 pipeline-state.update 写 task.status）
        对运行中管道的循环内存态不可见，聚合读面是唯一可靠的实时来源——
        任务到达终态必须当轮停止（用户裁定：成功/失败都立即结束，不允许
        空转）。桥未就绪/读取失败按未终态继续（与既有降级语义一致）。

        Args:
            ctx: 插件执行上下文

        Returns:
            任务终态字符串，空字符串表示正常运行或查询失败
        """
        task_id = ctx.state.get(StateKeys.TASK_ID, "")
        if not task_id:
            return ""

        reader = _get_state_reader()
        if reader is None:
            return ""

        try:
            rows = reader()
            if inspect.isawaitable(rows):
                rows = await rows
        except Exception as exc:
            logger.warning(
                "[%s] state 聚合读取失败，本轮按未终态继续 | task=%s | %s: %s",
                self.name,
                task_id,
                type(exc).__name__,
                exc,
            )
            return ""

        if not isinstance(rows, list):
            return ""

        row = next(
            (
                r
                for r in rows
                if isinstance(r, dict)
                and (
                    str(r.get("task.id") or "") == task_id
                    or str(r.get("pipeline_id") or "") == task_id
                )
            ),
            None,
        )
        if row is None:
            return ""

        status = str(row.get("task.status") or "")

        if status in self._TERMINAL_STATUSES:
            logger.info(
                "[%s] Task actual status is terminal: %s (task=%s, detected via state aggregation)",
                self.name,
                status,
                task_id,
            )
            return status

        return ""

    def _elapsed_seconds(self, ctx: PluginContext) -> float | None:
        """本 run 的真实运行时长（秒）；无计时锚时返回 None。

        主锚 = state.run_started_at（内核 run_init 逐 run 覆盖写的墙钟，单写者
        契约保证 run 内恒定）——真实 run 时长，与其他管道的插队无关。

        回落锚 = 按 (pipeline_id, run_id) 记账的单调钟起点（run_started_at 缺席
        或不可解析时用）：各 run 各自记账，互不污染，也不因他管道插队清零。

        Args:
            ctx: 插件执行上下文

        Returns:
            本 run 已运行时长（秒）；无计时锚返回 None
        """
        started_at = _parse_run_started_at(ctx.state.get(_RUN_STARTED_AT_KEY))
        if started_at is not None:
            return max(0.0, (_utc_now() - started_at).total_seconds())

        run_id = str(ctx.state.get("run_id") or "")
        if not run_id:
            return None
        identity = (str(ctx.state.get("pipeline_id") or ""), run_id)
        start = self._run_starts.get(identity)
        if start is None:
            start = time.monotonic()
            self._run_starts[identity] = start
            self._prune_run_starts()
        return max(0.0, time.monotonic() - start)

    def _prune_run_starts(self) -> None:
        """回落计时记账表容量上限：超出逐出最旧起点（dict 保插入序）。

        妥协：并发活跃 run 超上限时，最旧 run 的回落计时被逐出（该 run 的
        elapsed 从零重算）；主锚 run_started_at 路径不受影响。
        """
        while len(self._run_starts) > _MAX_TRACKED_RUNS:
            self._run_starts.pop(next(iter(self._run_starts)))

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 Agent 配置覆盖运行时参数（每次 execute 复位后应用）。

        同 sidecar 实例被多个 agent 管道连续复用：先回构造默认值再应用本管道
        state 注入的 max_iterations / timeout_seconds 覆盖——前一 agent 注入的
        阈值不得残留到下一 agent。特殊值 -1 表示无限制。时间预算缺省已退役
        （构造缺省 -1，agent 面不再下发 timeout_seconds，state 覆盖通路保留
        供显式 opt-in）——运行时缺省与构造缺省同口径（-1），不得出现裸 600。

        Args:
            ctx: 插件执行上下文
        """
        self._max_iterations = self._config.get("max_iterations", 20)
        self._max_duration = self._config.get("max_duration_seconds", -1)

        agent_max_iter = ctx.state.get("max_iterations")
        if agent_max_iter is not None:
            self._max_iterations = agent_max_iter

        agent_timeout = ctx.state.get("timeout_seconds")
        if agent_timeout is not None:
            self._max_duration = agent_timeout

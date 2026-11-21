"""任务评估提醒 Output 插件。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from _libs.evaluation_result import extract_evaluation_result
from _libs.pipeline_base.active_children import find_active_children, read_state_rows
from _libs.pipeline_base.plugin import IOutputPlugin, OutputResult, PluginContext

logger = logging.getLogger(__name__)

# ── state 聚合读取器（server.py on_load 注入，pipeline-state capability）──
# 约定签名：``() -> list[dict]``（sync 或 async，管道 state 聚合行，行为扁平点号键
# 如 {"pipeline_id": ..., "task.status": ..., "lineage.parent_pipeline_id": ...}）。
# None = 未注入（活跃子任务判定经共享 helper 回退链降级）。
_state_reader: Any = None


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（server.py on_load 经 pipeline-state capability）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def _get_state_reader() -> Any:
    """获取 state 聚合读取器（None = 未注入，测试可 monkeypatch）。"""
    return _state_reader

# 子任务挂号键前缀（信号③）：task_submit 成功时经 chat.send_message no_dispatch
# 向提交者管道写 ``task.subtasks_pending.<task_id>``（值 = 提交时间戳，扁平键
# 标量值无跨边界 JSON 还原问题）；子任务终态事件经 task_service 写 null 清除
# （pipeline-state.update 无键删除语义，null 即已回执）。
_PENDING_SUBTASK_PREFIX = "task.subtasks_pending."

# 提醒额度记账键（持久键，声明见 plugin.json persistent_fields）：
# - reminder_seen_result_fp：上次判定轮 raw_result 的稳定指纹。本轮指纹一致
#   说明 llm_core 本轮未覆盖写（成功轮必写 raw_result）——残留即失败轮信号①。
# - evaluate_reminder_run：额度归属 run（值为引擎每 run 注入的 run_id，
#   run 内不变量）。不一致 = 恢复动作拉起了新 run，额度重置。
# - evaluate_reminder_total：任务级累计提醒总数（跨 run 不重置）——per-run
#   预算随恢复重置（B6），提醒循环跨 run 无界（R2 移动端实证 50 轮不收敛），
#   总预算触顶即「评估不收敛」失败裁决。
_RESULT_FP_KEY = "reminder_seen_result_fp"
_RUN_KEY = "evaluate_reminder_run"
_TOTAL_KEY = "evaluate_reminder_total"
# 任务级总预算缺省（触顶 = 评估不收敛）：远高于健康任务消耗（0-2 次），
# 低于 R2 失控形态（45 次注入）两个量级内的确定性上界。
_DEFAULT_MAX_REMINDERS_TOTAL = 10
# 失败轮短路标记：execute 包装层消费后摘除，不会进入 state 合并
# （下划线键被引擎 merge 过滤，双保险）。
_FAILURE_ROUND_FLAG = "_system_failure_round"


class TaskReminder(IOutputPlugin):
    """任务评估提醒 Output 插件。"""


    # 评估模式下连续仅工具调用的提醒阈值
    _EVAL_TOOL_ONLY_THRESHOLD = 6

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        # 构造期缺省预算：per-pipeline 覆盖一律在执行期从 state 读取。本类
        # 实例被同 sidecar 的多管道并发复用，实例字段写 per-pipeline 配置会
        # 被并发 execute 覆盖（2026-11-12 评估提醒误判实证：evaluation_mode
        # 被同宿任务管道的 execute 重置，评估者收到执行者提醒）。
        self._default_max_reminders: int = int(
            self._config.get("max_reminders", 10) or 10
        )
        self._default_max_reminders_total: int = int(
            self._config.get("max_reminders_total", _DEFAULT_MAX_REMINDERS_TOTAL)
            or _DEFAULT_MAX_REMINDERS_TOTAL
        )

    @property
    def name(self) -> str:
        return "task_reminder"

    @property
    def priority(self) -> int:
        return self._config.get("priority", 35)

    async def execute(self, ctx: PluginContext) -> OutputResult:  # noqa: PLR0911
        """执行任务评估提醒检测（包装层：失败轮识别 + 产出基线记账）。

        判定契约：本轮 raw_result 指纹与记账基线一致（llm_core 未覆盖写）
        且本 run 存在 llm_core 错误条目 → 系统失败轮，短路重试不计额度；
        其余轮交 `_execute_inner` 三信号判据（ADR
        2026-08-28-task-closure-three-signal-gate）。产出基线每轮推进
        （合并轮 LLM 恒产出，基线语义 = 最近一次 LLM 产出；失败轮冻结，
        连续失败按同一基线持续识别）。
        """
        state = ctx.state
        stale_output = self._is_stale_output(state)
        result = await self._execute_inner(ctx, stale_output=stale_output)
        updates = result.state_updates
        if updates.pop(_FAILURE_ROUND_FLAG, False):
            # 失败轮基线冻结：连续失败轮按同一基线持续识别（不推进）。
            return result
        new_fp = self._result_fingerprint(state.get("raw_result"))
        if state.get(_RESULT_FP_KEY) != new_fp:
            # 基线幂等推进：指纹未变的轮不重写（减少无谓 state diff）。
            updates[_RESULT_FP_KEY] = new_fp
        return result

    async def _execute_inner(
        self,
        ctx: PluginContext,
        *,
        stale_output: bool,
    ) -> OutputResult:  # noqa: PLR0911
        """三信号收束判据主体。

        收束判据 = 三信号按序短路（ADR 2026-08-28-task-closure-three-signal-gate），
        全部读 state / 对话结构，不解析渲染文本形态：
        0. 系统失败轮（stale_output）→ 不评判不计额，置续跑标志直接重试；
        1. 本轮有工具调用 → 路由工具执行，不评判；
        2. ``state.task.status == completed``（经 pipeline-state 写面写入的）→
           当轮收束 end——评估成功落库的当轮即收束，提醒根本不注入，
           "完成被覆盖为 failed" 在该路径不可达；
        3. 存在未回子任务挂号键 → 本轮收束等待唤醒通知，不催评估；
        4. 三信号皆否（纯文本 + 未完成 + 无挂号）→ 注入提醒；耗尽裁决改写
           终态前必须复查信号②，已完成态不可覆盖。

        前置门槛（不属三信号）：pending→running 推进（幂等）/ llm_call 轮 /
        L1 调度层永不触发 / ``task.id`` 存在性（state 单一真值，缺失即会话
        管道）/ 评估模式连续无文本计数。活跃子任务（实况判定，与信号③挂号键
        同语义的等待面）：纯文本是等待/协调行为 → 挂起等子任务终态唤醒，
        不催评估也不放行回 LLM（D2 路由会空转）。

        ``_has_successful_task_evaluate``（messages 通过证据检测，result=
        completed 才算，且只认**本次尝试之后**的证据——锚点 = 最后一条 user
        消息，ADR 2026-10-28 D4-3：上一轮的通过信封不得收束返工轮）降为次级
        证据保留：信号②缺席时的当轮收束回退——真实路径 LLM 面被 result_format
        渲染化，文本形态不作主证据。
        """
        state = ctx.state
        max_reminders = self._max_reminders_for(state)
        max_reminders_total = self._max_reminders_total_for(state)
        iteration = state.get("iteration", -1)

        advanced = self._advance_pending_status(state)
        if advanced is not None:
            return advanced

        # ── L1 调度层永不触发 ──
        # L1（灵汐）的纯文本输出是正常的调度/沟通汇报，不代表"忘了提交评估"。
        # 层级单一真值：顶层 agent_level（context_build 以实际 Agent 层级
        # 无条件覆盖，见 context_build/plugin.py）。reminder 只对叶子执行者有意义。
        # 豁免豁免：评估子管道（state.evaluation.mode="evaluator" /
        # evaluation.metric_id 派发标记）按构造不是调度层——身份降级（如缺
        # agent.id 静默回退 L1）时此管道字段比 agent_level 更可信，跳过 L1
        # 早退，否则 detected_result 永不落库、task_evaluate 轮询必超时
        # （B12 根因②）。
        if (
            state.get("agent_level", "") == "L1"
            and not self._is_evaluation_mode(state)
        ):
            logger.debug(
                "TaskReminder[iter=%s]: skip, L1 调度层不触发 reminder",
                iteration,
            )
            return OutputResult()

        # ── 系统失败轮：llm_core 本轮未产出（超时/失败轮引擎 warn+继续，
        #    state 残留上轮 raw_*）。系统瓶颈不是 agent 空转：不计提醒额度、
        #    不注入提醒（残留文本非本轮产出），置续跑标志驱动重试；基线由
        #    包装层冻结。连续失败持续豁免（同一基线 + llm_core 错误条目在
        #    本 run 内持续存在），系统恢复后下一轮有产出即回归正常计额。──
        if stale_output:
            logger.warning(
                "TaskReminder[iter=%s]: llm_core failed round (stale output + "
                "llm_core error), retrying without consuming reminder budget",
                iteration,
            )
            return OutputResult(
                state_updates={"_has_new_llm_input": True, _FAILURE_ROUND_FLAG: True},
            )

        # ── DB 权威终态（2026-10-19 幽灵续跑改判事故修复③，单轮至多一次）：
        # 写面分裂（task_evaluate 经 pipeline-state 写面只落 DB，续跑轮 state 无
        # 完成证据）+ 恢复 overlay 强改状态，state 视角的信号②/复查面全部失明，
        # 已完成任务被催促改判 failed 并空转。本管道聚合行（task_id==pipeline_id）
        # 的 task.status 与 state 证据同权：completed 参与信号②当轮收束与耗尽
        # 裁决复查面；reader 未注入/行缺失/行无 task.status → None，全部退化为
        # state-only 现状行为（保守）。DB failed/running 不构成完成证据（重试轮
        # 与真实失败裁决不受影响）；task.id 缺失（会话管道）不触达读面。──
        db_task_status = await self._db_task_status(str(state.get("task.id") or ""))
        db_completed = db_task_status == "completed"

        raw_tool_calls = state.get("raw_tool_calls", [])
        raw_result = state.get("raw_result", "")
        has_tool_calls = bool(raw_tool_calls)
        has_text = bool(raw_result and str(raw_result).strip())
        if not has_text and has_tool_calls:
            has_text = self._last_assistant_has_text(state)

        # ── 任务级总预算（F 缺陷 R2 修法，置于一切注入路径之前）：per-run
        # 预算随恢复重置（B6），提醒循环跨 run 无界——总预算跨 run 累计，
        # 触顶 = 评估不收敛，任务明确失败（完成证据在场不覆盖，与耗尽裁决
        # 同一复查面）。评估模式强制提醒同受此门（同一注入面单一上限）。──
        reminder_total = self._reminder_total(state)
        if reminder_total >= max_reminders_total:
            return self._reminder_exhausted_result(
                state,
                reminder_count=reminder_total,
                iteration=iteration,
                task_id=str(state.get("task.id") or ""),
                failure_class="evaluation_not_converged",
                failure_reason=(
                    f"评估不收敛（任务级累计提醒 {reminder_total} 次仍未提交评估结论）"
                ),
                db_completed=db_completed,
            )

        eval_counted = self._eval_no_text_tracking(
            state,
            iteration=iteration,
            has_text=has_text,
            max_reminders=max_reminders,
        )
        if eval_counted is not None:
            return eval_counted

        # ── 信号②：state 完成证据 ∪ DB 权威终态 completed → 当轮收束（置于
        # 工具轮路由之前）。评估成功的那一轮本身就是工具轮：若被信号①路由回
        # LLM，证据轮永不裁决 → LLM 反复重调 task_evaluate 的无限循环。DB 位
        # 覆盖写面分裂场景（证据只落 DB、恢复 state 无证据——幽灵续跑轮首轮
        # 即干净收束，不再进入催促循环）。task.id 缺失（会话管道）不可能有该
        # 证据，DB 读面也不触达，天然跳过。──
        if self._task_completed_in_state(state) or db_completed:
            return self._completed_round_result(
                state, iteration=iteration, task_id=str(state.get("task.id") or ""),
            )

        # ── 退化循环裁决（llm_core 流式守卫本轮判停）：不自动重试——同一
        # 上下文重跑只会再退化一次；任务直接失败并写明死因，完成证据在场不
        # 覆盖（与空回复/提醒耗尽同一复查面）。会话管道无 task.id 不落任务
        # 终态，只结束本轮。──
        if state.get("llm_repetition_detected"):
            return self._repetition_exhausted_result(
                state,
                iteration=iteration,
                task_id=str(state.get("task.id") or ""),
                db_completed=db_completed,
            )

        # ── 信号①：本轮有工具调用 → 路由工具执行，不评判（ADR 2026-08-28
        # 三信号判据原语义）。混合轮（文本+工具调用）同样按①放行工具执行；
        # 工具重复调用/死循环防护由 duplicate_check 按「同工具+相同参数」
        # 签名承接（ADR 2026-08-30-retire-tool-fail-streak-gate）。──
        if has_tool_calls:
            return OutputResult()

        if not has_text:
            # 空回复重试耗尽（llm_core 连续 N 轮空文本无工具调用后置
            # llm_empty_exhausted）：agent 未产出任何内容即退出，等同放弃
            # 执行义务 → 任务直接失败（用户裁定 2026-08-30）；完成证据在场
            # 不覆盖（已完成态不可覆盖，与提醒耗尽裁决同一复查面）。
            if self._llm_empty_exhausted(state):
                return self._empty_exhausted_result(
                    state, iteration=iteration, task_id=state.get("task.id"),
                    db_completed=db_completed,
                )
            logger.debug(
                "TaskReminder[iter=%s]: skip, raw_result is empty",
                iteration,
            )
            return OutputResult()

        # task.id 由引擎在管道出生时注入（== pipeline_id）；缺失即会话管道，
        # 跳过。state 有 task.id 键即视为任务存在——不查跨进程 task_service
        # （多 sidecar 下进程内单例可能读不到其他进程刚创建的任务）。
        task_id = state.get("task.id")
        if not task_id:
            logger.debug(
                "TaskReminder[iter=%s]: skip, no task_id in state",
                iteration,
            )
            return OutputResult()

        # ── 活跃子任务：纯文本是等待/协调行为 → 挂起（引擎轮边界消费，
        # run 落 Suspended 零 token，由子任务终态唤醒）。不挂起会被 D2 路由
        # （任务管道纯文本轮回 llm_call）把等待文本轮回 LLM 空转。──
        if await self._has_active_children(task_id, ctx):
            logger.info(
                "TaskReminder[iter=%s][task=%s]: active child tasks, "
                "suspending to wait for wake-up",
                iteration,
                task_id,
            )
            return OutputResult(
                state_updates={"_has_new_llm_input": False, "suspended": True},
            )

        # ── 信号③：存在未回子任务挂号 → 本轮收束等待唤醒通知，不催评估 ──
        if self._has_pending_subtask_registration(state):
            return self._pending_subtask_wait_result(
                iteration=iteration, task_id=task_id,
            )

        gated = self._resolve_text_output_gates(
            state, iteration=iteration, task_id=task_id,
        )
        if gated is not None:
            return gated

        reminder_count, _, run_renewed = self._reminder_budget(state)
        if run_renewed:
            logger.info(
                "TaskReminder[iter=%s][task=%s]: resumed into new run, reminder budget reset",
                iteration,
                task_id,
            )
        if reminder_count >= max_reminders:
            return self._reminder_exhausted_result(
                state, reminder_count=reminder_count, iteration=iteration, task_id=task_id,
                db_completed=db_completed,
            )
        return self._inject_reminder_result(
            state,
            reminder_count=reminder_count,
            iteration=iteration,
            task_id=task_id,
            max_reminders=max_reminders,
        )

    @staticmethod
    def _advance_pending_status(state: dict[str, Any]) -> OutputResult | None:
        """任务状态推进：pending → running。

        任务提交后出生值 pending 只应停留极短时间：管道走完第一轮插件即视为
        已开始执行。内核不再回写任务状态（run 终态只广播事件），此处由任务域
        插件推进——任何轮次都推进（幂等：非 pending 返回 None 不动）。

        身份门（ADR 2026-11-06 D5 N3）：无 ``task.id`` 的 state 是会话管道，
        不推进——会话残留（幽灵 pending）不被放大成 running（running 残留
        是面板伪任务行与 run-started 复位死锁形态）。
        """
        if not state.get("task.id"):
            return None
        if state.get("task.status") != "pending":
            return None
        logger.info(
            "TaskReminder[iter=%s]: task.status pending -> running（任务已开始执行）",
            state.get("iteration", -1),
        )
        return OutputResult(state_updates={"task.status": "running"})

    @staticmethod
    def _result_fingerprint(raw_result: Any) -> str:
        """raw_result 稳定指纹（sha1——内建 hash 有进程级随机化，跨 respawn
        后 state 恢复的基线不可对比）。"""
        return hashlib.sha1(str(raw_result).encode("utf-8", "replace")).hexdigest()

    @classmethod
    def _is_stale_output(cls, state: dict[str, Any]) -> bool:
        """失败轮判据：本轮 llm_core 未产出任何内容。

        双条件交叉（单条件皆误判）：
        ① raw_result 指纹与记账基线（reminder_seen_result_fp）一致——成功轮
           必覆盖写 raw_result；失败/超时轮引擎统一 warn+继续、state_updates
           为空，残留上轮值。
        ② 本 run 存在 llm_core 错误条目（``_plugin_error_stats``，引擎 run
           开头清空、run 内按 (plugin_id, message) 聚合）——排除 agent 连轮
           输出相同文本的正常重复轮（该轮必须正常计额，否则复读即可绕过
           提醒耗尽兜底）。
        """
        fp = cls._result_fingerprint(state.get("raw_result"))
        if fp != state.get(_RESULT_FP_KEY):
            return False
        errors = state.get("_plugin_error_stats")
        if not isinstance(errors, list):
            return False
        return any(
            isinstance(e, dict) and str(e.get("plugin_id") or "").endswith("llm_core")
            for e in errors
        )

    @staticmethod
    def _reminder_budget(state: dict[str, Any]) -> tuple[int, str, bool]:
        """提醒额度现状：``(计数, run_id, 是否恢复新 run)``。

        额度按 run 记账（``evaluate_reminder_run`` 记录计数归属的 run）：
        continue/retry 恢复经 resume_pipeline 拉起新 run，引擎每 run 注入新
        ``run_id``（run 内不变量，state 运行键）——不一致即恢复轮，额度重置
        为 0（计数随 state 继承会让恢复任务带着残缺额度，轻则提前耗尽重则
        被耗尽裁决误杀）。判定锚不用 ``run_started_at``：该键被内核双写
        （run_init 与 run 记录两次覆盖写，值差毫秒级），每轮误判「新 run」
        重置预算（2026-10-15 装机哨兵任务连续 9 轮实证）。``run_id`` 缺失
        （旧快照/异常态）按同 run 处理，保守不重置。
        """
        run_id = str(state.get("run_id") or "")
        counted_run = str(state.get(_RUN_KEY) or "")
        renewed = bool(run_id) and run_id != counted_run
        if renewed:
            return 0, run_id, True
        return int(state.get("evaluate_reminder_count", 0) or 0), run_id, False

    @staticmethod
    def _reminder_total(state: dict[str, Any]) -> int:
        """任务级累计提醒总数（跨 run 不重置，总预算裁决的记账锚）。"""
        return int(state.get(_TOTAL_KEY) or 0)

    def _eval_no_text_tracking(
        self,
        state: dict[str, Any],
        *,
        iteration: Any,
        has_text: bool,
        max_reminders: int,
    ) -> OutputResult | None:
        """评估模式连续无文本追踪（仅工具调用/空输出场景）。

        未达强制阈值：计数并返回计数结果；达阈值且有提醒余量：注入强制提醒；
        其余情形返回 None（交回主级联继续判定）。
        """
        if not (self._is_evaluation_mode(state) and not has_text):
            return None

        task_id = str(state.get("task.id") or "-")
        _, run_at, run_renewed = self._reminder_budget(state)
        tool_only_base = 0 if run_renewed else int(state.get("eval_tool_only_count", 0) or 0)
        tool_only_count = tool_only_base + 1
        if tool_only_count < self._EVAL_TOOL_ONLY_THRESHOLD:
            logger.debug(
                "TaskReminder[iter=%s][task=%s]: eval no-text count=%d",
                iteration,
                task_id,
                tool_only_count,
            )
            return OutputResult(
                state_updates={
                    "eval_tool_only_count": tool_only_count,
                    "evaluate_reminder_run": run_at,
                }
            )

        reminder_count, _, _ = self._reminder_budget(state)
        if reminder_count >= max_reminders:
            # 提醒已达上限不再注入，仅维持计数。
            logger.debug(
                "TaskReminder[iter=%s][task=%s]: eval no-text count=%d",
                iteration,
                task_id,
                tool_only_count,
            )
            return OutputResult(
                state_updates={
                    "eval_tool_only_count": tool_only_count,
                    "evaluate_reminder_run": run_at,
                }
            )
        reminder_message = (
            f"【评估强制提醒 #{reminder_count + 1}】"
            "你已经收集了足够的证据。"
            "请立即停止调用工具，直接输出评估结论 JSON：\n"
            "```json\n"
            '{"evaluation_result": {"passed": true/false, '
            '"score": 0-100, "feedback": "评估说明..."}}\n'
            "```"
        )
        logger.info(
            "TaskReminder[iter=%s][task=%s]: eval force reminder after %d no-text iters, reminder #%d",
            iteration,
            task_id,
            tool_only_count,
            reminder_count + 1,
        )
        # 回 LLM 由 _has_new_llm_input 状态键 + DSL 路由承载（控制状态键契约）
        return OutputResult(
            state_updates={
                "messages": self._reminder_append_ops(reminder_message),
                "evaluate_reminder_count": reminder_count + 1,
                "evaluate_reminder_run": run_at,
                _TOTAL_KEY: self._reminder_total(state) + 1,
                "eval_tool_only_count": 0,
                "_has_new_llm_input": True,
            },
        )

    def _resolve_text_output_gates(
        self,
        state: dict[str, Any],
        *,
        iteration: Any,
        task_id: str,
    ) -> OutputResult | None:
        """有文本输出时的次级收束面：evaluation JSON 检测 / 次级通过证据收束 / 会话模式。

        Returns:
            收束结果（end）；都不命中返回 None 继续（交提醒注入级联）。
        """
        raw_result = state.get("raw_result", "")
        raw_text = str(raw_result)

        if self._is_evaluation_mode(state):
            detected = self._detect_evaluation_result_json(raw_text)
            if detected is None:
                # 回退：raw_result 是纯在飞键，个别时序路径可能缺席——评估者
                # 合规输出带 JSON 尾巴，从最后一条 assistant 文本再扫一次，
                # 防 detected_result 缺席导致 task_evaluate 轮询超时（B12）
                detected = self._detect_last_assistant_json(state.get("messages", []))
            if detected is not None:
                logger.info(
                    "TaskReminder[iter=%s][task=%s]: evaluation_result JSON detected, sending end signal",
                    iteration,
                    task_id,
                )
                updates: dict[str, Any] = {
                    "evaluation.detected_result": detected,
                    "ended": True,
                }
                # 评估子管道自身终态投影（2026-11-11 修复）：检测到结论 JSON
                # 只落 evaluation.detected_result 时，子管道 task.status 停在
                # running → run.completed 派生不出 task_completed → 父任务
                # task.subtasks_pending 永不解除、父挂起等待唤醒（实证
                # 885b1a27752f 2026-11-11）。评估交付即完成，与
                # _completed_round_result 同一写面（终态不可被 failed 覆盖）。
                if task_id and str(state.get("task.status") or "") != "completed":
                    updates["task.status"] = "completed"
                    updates["task.ended_at"] = datetime.now(UTC).isoformat()
                return OutputResult(state_updates=updates)

        # ── 次级证据收束（降级保留，ADR 2026-08-28 证据契约）：信号②（state
        # 投影键 / DB 权威终态）缺席时的 messages 通过证据回退。评估通过
        # （metadata.result=completed）已证实任务完成 → 当轮直接收束补落终态；
        # 评估已通过再注入「请提交评估」提醒 = "评估结论确认"提醒循环，且 D2
        # 路由（任务管道纯文本轮回 llm_call）下旧「放行不收束」形态
        # （_has_new_llm_input=False）= 无监督空转循环，均已废弃。
        # retry/partial_pass/failed 不构成通过证据，照常进入提醒级联（预算
        # 受限）。证据作用域 = 本次尝试（最后一条 user 消息之后）——上一轮
        # 返工前的通过信封不得收束本轮（ADR 2026-10-28 D4-3）。──
        if self._has_successful_task_evaluate(state.get("messages", [])):
            logger.info(
                "TaskReminder[iter=%s][task=%s]: task_evaluate pass evidence in messages, closing round (end)",
                iteration,
                task_id,
            )
            return self._completed_round_result(
                state, iteration=iteration, task_id=task_id,
            )

        if state.get("conversation_mode"):
            logger.info(
                "TaskReminder[iter=%s][task=%s]: skip, conversation mode active",
                iteration,
                task_id,
            )
            return OutputResult()
        return None

    @staticmethod
    def _has_pending_subtask_registration(state: dict[str, Any]) -> bool:
        """信号③：存在未回子任务挂号（ADR 2026-08-28 三信号③）。

        任一 ``task.subtasks_pending.<task_id>`` 键真值即存在未回子任务；
        终态清除写 null（假值）不构成挂号。
        """
        return any(
            str(key).startswith(_PENDING_SUBTASK_PREFIX) and value
            for key, value in state.items()
        )

    def _pending_subtask_wait_result(
        self,
        *,
        iteration: Any,
        task_id: str,
    ) -> OutputResult:
        """信号③当轮挂起：等待子任务终态唤醒通知，不催评估。

        写 suspended=true（非 ended）——挂起不触发收尾体，父工作区/环境保持
        供唤醒后的 run 续用；run 终态落 Suspended，与 child_task_guard 同语义。
        """
        logger.info(
            "TaskReminder[iter=%s][task=%s]: pending subtask registration, "
            "suspending to wait for wake-up",
            iteration,
            task_id,
        )
        return OutputResult(
            state_updates={"_has_new_llm_input": False, "suspended": True},
        )

    @staticmethod
    def _task_completed_in_state(state: dict[str, Any]) -> bool:
        """信号②：评估成功经任务域写面落库的完成证据（ADR 2026-08-28 三信号②）。

        两个同通路证据源，任一在场即完成：
        - ``task.status == "completed"``：task_evaluate 评估通过经 pipeline-state
          写面落库的终态；
        - ``task_evaluation_completed``：tool_core 从 task_evaluate 结构化工具
          结果（success 且 metadata.result=completed）派生的 state 键——写面
          mid-run 落注册表/DB，在飞 state 当轮不可见，该键是同一裁决的当轮
          可观察投影。
        """
        if str(state.get("task.status") or "") == "completed":
            return True
        return bool(state.get("task_evaluation_completed"))

    @classmethod
    def _has_completion_evidence(cls, state: dict[str, Any]) -> bool:
        """完成证据全集：信号② state 证据 ∪ evaluation.detected_result（评估
        模式 JSON 检测产物）。耗尽裁决改写终态前的复查面——任一在场不得写
        failed（已完成态不可覆盖，ADR 2026-08-28）。"""
        if cls._task_completed_in_state(state):
            return True
        return bool(state.get("evaluation.detected_result"))

    def _completed_round_result(
        self,
        state: dict[str, Any],
        *,
        iteration: Any,
        task_id: str,
    ) -> OutputResult:
        """信号②当轮收束：评估成功已证实任务完成，本轮直接 end，提醒不注入。

        task.status 尚为出生/执行值时补落 completed（写面 mid-run 写注册表，
        在飞 state 当轮不可见；本放行检测点是补落的任务域写点，与耗尽裁决写
        failed 同一通路对称）。已落终态时补落幂等（不重复写）。

        身份门（ADR 2026-11-06 D5 N4）：无 ``task.id`` 的会话管道不落
        task.status/task.ended_at（收束语义保留——会话轮照常 end，只是不写
        任务终态；主管道不该有 task.* 自身字段）。
        """
        state_updates: dict[str, Any] = {"_has_new_llm_input": False}
        if (
            task_id
            and str(state.get("task.status") or "") != "completed"
        ):
            state_updates["task.status"] = "completed"
            state_updates["task.ended_at"] = datetime.now(UTC).isoformat()
        logger.info(
            "TaskReminder[iter=%s][task=%s]: state completion evidence, closing round (end)",
            iteration,
            task_id,
        )
        state_updates["ended"] = True
        return OutputResult(state_updates=state_updates)

    def _reminder_exhausted_result(
        self,
        state: dict[str, Any],
        *,
        reminder_count: int,
        iteration: Any,
        task_id: str,
        failure_class: str = "reminder_exhausted",
        failure_reason: str | None = None,
        db_completed: bool = False,
    ) -> OutputResult:
        """提醒耗尽 / 任务级总预算触顶（评估闸门的插件裁决，内核只落库不判定）。

        复查完成证据（信号② state 证据 ∪ evaluation.detected_result ∪ DB 权威
        终态 db_completed）：任一在场 → 保持现状交评估流程收尾，终态不可覆盖为
        failed；无任何证据 → task.status 标 failed（failure_class/failure_reason
        由调用方按裁决面给定：per-run 耗尽 = reminder_exhausted，跨 run 总预算
        触顶 = evaluation_not_converged「评估不收敛」；task_failed 事件携带
        parent_pipeline_id 经 triggers_ext 通知上级）。两者都结束本轮并发 end。

        身份门（ADR 2026-11-06 D5 N6，与空回复路径同款）：无 ``task.id`` 的
        会话管道不落任务终态（耗尽裁决是任务域写面，会话轮只收束 end）。
        """
        state_updates: dict[str, Any] = {}
        if task_id and not (self._has_completion_evidence(state) or db_completed):
            # 失败归因结构化（§10.2 裁定）：裁决方置 failed 时同时写失败类别
            # 与终止原因——复盘分诊只认终局归因声明，不从错误堆反推。
            state_updates["task.status"] = "failed"
            state_updates["task.ended_at"] = datetime.now(UTC).isoformat()
            state_updates["task.failure_class"] = failure_class
            state_updates["task.failure_reason"] = failure_reason or (
                f"评估提醒耗尽（{reminder_count} 次提醒后仍未提交评估结论）"
            )
            logger.warning(
                "TaskReminder[iter=%s][task=%s]: %s, task.status -> failed",
                iteration,
                task_id,
                failure_reason or f"max_reminders reached ({reminder_count}) without evaluation",
            )
        else:
            logger.warning(
                "TaskReminder[iter=%s][task=%s]: budget cap reached "
                "(class=%s, count=%d), sending end signal to prevent infinite loop",
                iteration,
                task_id,
                failure_class,
                reminder_count,
            )
        # 清除续跑标志：提醒已耗尽，本轮必须结束（防残留标志把
        # 后续纯文本轮误路由回 LLM 造成死循环）
        state_updates["_has_new_llm_input"] = False
        state_updates["ended"] = True
        return OutputResult(state_updates=state_updates)

    def _repetition_exhausted_result(
        self,
        state: dict[str, Any],
        *,
        iteration: Any,
        task_id: str,
        db_completed: bool = False,
    ) -> OutputResult:
        """LLM 输出退化循环 → 任务直接失败（流式守卫已截断，不自动重试）。

        与空回复耗尽同一裁决面：完成证据（state ∪ DB）在场不覆盖；无证据且
        有 task.id → task.status=failed + 结构化失败归因；会话管道只收束
        end，不落任务终态。失败原因写明"调整指令后重提"，不重跑同一上下文
        （退化形态可复现，重跑只会再烧一轮）。
        """
        state_updates: dict[str, Any] = {}
        if task_id and not (self._has_completion_evidence(state) or db_completed):
            state_updates["task.status"] = "failed"
            state_updates["task.ended_at"] = datetime.now(UTC).isoformat()
            state_updates["task.failure_class"] = "llm_degeneration"
            state_updates["task.failure_reason"] = (
                "LLM 输出退化循环（流式守卫已截断，本轮无有效进展；"
                "请调整任务指令后重新提交）"
            )
            logger.warning(
                "TaskReminder[iter=%s][task=%s]: llm repetition detected, "
                "task.status -> failed",
                iteration,
                task_id,
            )
        else:
            logger.info(
                "TaskReminder[iter=%s][task=%s]: llm repetition detected, "
                "completion evidence present or session pipeline, closing round",
                iteration,
                task_id,
            )
        state_updates["_has_new_llm_input"] = False
        state_updates["ended"] = True
        return OutputResult(state_updates=state_updates)

    @staticmethod
    def _llm_empty_exhausted(state: dict[str, Any]) -> bool:
        """空回复重试耗尽信号（llm_core 写入，本放行检测点的裁决触发面）。"""
        return bool(state.get("llm_empty_exhausted"))

    def _empty_exhausted_result(
        self,
        state: dict[str, Any],
        *,
        iteration: Any,
        task_id: Any,
        db_completed: bool = False,
    ) -> OutputResult:
        """空回复重试耗尽 → 任务直接失败（用户裁定 2026-08-30）。

        复查完成证据（信号② state 证据 ∪ evaluation.detected_result ∪ DB 权威
        终态 db_completed，与 _reminder_exhausted_result 同一复查面）：任一在场
        保持现状不覆盖（已完成态不可覆盖）；无证据且为任务管道 →
        task.status=failed（agent 连续空响应退出 = 未完成执行义务 = 任务失败，
        task_failed 事件经通知链上报上级）；会话管道（无 task.id）只收束不落
        任务终态。两者都结束本轮并发 end。
        """
        state_updates: dict[str, Any] = {}
        if task_id:
            if not (self._has_completion_evidence(state) or db_completed):
                # 失败归因结构化（§10.2 裁定）：裁决方置 failed 时同时写失败
                # 类别与终止原因（复盘分诊只认终局归因声明）。
                state_updates["task.status"] = "failed"
                state_updates["task.ended_at"] = datetime.now(UTC).isoformat()
                state_updates["task.failure_class"] = "empty_response"
                state_updates["task.failure_reason"] = (
                    "空回复重试耗尽（agent 连续空响应退出，未完成执行义务）"
                )
                logger.warning(
                    "TaskReminder[iter=%s][task=%s]: empty-response retries exhausted, "
                    "task.status -> failed",
                    iteration,
                    task_id,
                )
            else:
                logger.info(
                    "TaskReminder[iter=%s][task=%s]: empty-response retries exhausted, "
                    "completion evidence present, status preserved",
                    iteration,
                    task_id,
                )
        state_updates["_has_new_llm_input"] = False
        state_updates["ended"] = True
        return OutputResult(state_updates=state_updates)

    def _inject_reminder_result(
        self,
        state: dict[str, Any],
        *,
        reminder_count: int,
        iteration: Any,
        task_id: str,
        max_reminders: int,
    ) -> OutputResult:
        """注入下一轮系统提醒并置续跑标志触发 next_llm。"""
        reminder_message = self._build_reminder(state, reminder_count)

        logger.info(
            "TaskReminder[iter=%s][task=%s]: injecting reminder #%d/%d, triggering next_llm",
            iteration,
            task_id,
            reminder_count + 1,
            max_reminders,
        )
        # 回 LLM 经 _has_new_llm_input 状态键 + DSL 路由承载（控制状态键契约）
        return OutputResult(
            state_updates={
                "messages": self._reminder_append_ops(reminder_message),
                "evaluate_reminder_count": reminder_count + 1,
                "evaluate_reminder_run": str(state.get("run_id") or ""),
                _TOTAL_KEY: self._reminder_total(state) + 1,
                "_has_new_llm_input": True,
            },
        )

    @staticmethod
    def _reminder_append_ops(reminder_message: str) -> dict[str, Any]:
        """提醒消息的 slot ops 声明（messages 更新只认 {_ops:[set/insert]} 形态）。

        引擎 finalize 对全量数组零兼容（收到即 warn 丢弃）——数组回传时计数器
        与续跑标志照常生效而消息本体丢失，模型在无新输入的空转重调中复读至
        耗尽（2026-11-13 装机 1af174 reminder_exhausted 实证）。缺 seq 的
        set = append，seq 由引擎按现有 max seq 递增分配。
        """
        return {
            "_ops": [{"op": "set", "msg": {"role": "system", "content": reminder_message}}]
        }

    @staticmethod
    def _attempt_scope_start(messages: list) -> int:
        """本次尝试的起点下标 = 最后一条 user 消息之后（ADR 2026-10-28 D4-3）。

        返工重开 / 重试的指令以 user 消息形态入史（chat.send_message 注入 →
        引擎待输入队列 → round 边界落 role=user 消息），消息历史天然按尝试
        分段；上一轮的 task_evaluate 通过证据落在锚点之前，对新一轮无效
        （事故 2026-10-28：子任务 9746ad7e2386 返工轮被上一轮通过证据收束成
        「已完成」，实况是超时中断的假完成）。

        正常轮次行为不变：任务提示本身即最后一条 user 消息，其后的证据全在
        作用域内。消息面无 user 消息（会话管道 / 未见注入）→ 起点 0（全量即
        本次尝试）。动态变量消息不进 state.messages（llm_core 出站副本追加，
        prompt.dynamic_vars 是 state 键非消息），不参与分段。
        """
        start = 0
        for idx, msg in enumerate(messages or []):
            if isinstance(msg, dict) and msg.get("role") == "user":
                start = idx + 1
        return start

    @staticmethod
    def _has_successful_task_evaluate(messages: list) -> bool:
        """本次尝试的 messages 中是否存在「评估通过」证据（task_evaluate 评估通过判定）。

        作用域：只认**本次尝试之后**的通过证据（锚点 = 最后一条 user 消息，
        见 ``_attempt_scope_start``）——上一轮历史里的通过信封不得收束新一轮
        （ADR 2026-10-28 D4-3）。无 user 消息时锚点缺席 = 全量扫描（行为不变）。

        两种证据形态（对齐 tool_core ``tool_result_message`` 信封与 SDK
        ``ToolExecutionResult`` 序列化契约）：
        - 信封形态（现行 tool_core 正路）：role=tool 消息带 ``tool_result``
          对象字段（content 只序列化 data，metadata 在信封里）——
          ``tool_name == "task_evaluate"`` 且 ``success is True`` 且
          ``metadata.result == "completed"``；信封自名工具，无需 id 配对。
        - content JSON 形态（旧序列化/回放兼容）：assistant tool_calls 含
          name=task_evaluate 且对应 role=tool 的 content 为 JSON 载荷——
          success 语义成立（顶层 success=true 全量形态 / 无 success 键且无
          error 的 slim 形态）且 ``metadata.result == "completed"``。

        只有 ``metadata.result == "completed"``（评估通过、任务已落 completed）
        构成通过证据：retry（未通过继续改）/ partial_pass（部分指标通过）/
        failed（重试耗尽）均不收束——未通过误判为通过会把未完成任务提前放行；
        漏判的代价只是回退到正常提醒级联（预算受限），方向安全。真实路径
        LLM 面被 result_format 渲染化，本检测仅作主证据（信号②）缺席时的
        次级回退。
        """
        scoped = list(messages or [])[TaskReminder._attempt_scope_start(messages):]
        eval_call_ids = set()
        for msg in scoped:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for c in msg.get("tool_calls") or []:
                fn = c.get("function") or {}
                if fn.get("name") == "task_evaluate" and c.get("id"):
                    eval_call_ids.add(c["id"])
        for msg in scoped:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            envelope = msg.get("tool_result")
            if isinstance(envelope, dict):
                metadata = envelope.get("metadata")
                if (
                    envelope.get("tool_name") == "task_evaluate"
                    and envelope.get("success") is True
                    and isinstance(metadata, dict)
                    and metadata.get("result") == "completed"
                ):
                    return True
            if msg.get("tool_call_id") not in eval_call_ids:
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                # 多模态块等非字符串载荷无 JSON 语义 → TypeError 显式失败路径
                continue
            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                # 散文/损坏 JSON 不构成证据（子串命中是误判来源）
                continue
            if not isinstance(parsed, dict):
                continue
            metadata = parsed.get("metadata")
            if not (isinstance(metadata, dict) and metadata.get("result") == "completed"):
                # 非通过裁决（retry/partial_pass/failed/metadata 缺席）不构成证据
                continue
            # 成功形态（对齐 SDK ToolExecutionResult.to_dict 序列化契约）：
            # - 全量形态：顶层 success=true；
            # - slim 形态（LLM 面默认序列化）：成功时**省略 success 键**只留
            #   output/metadata，失败时才写 success=false——即「无 success 键
            #   且无 error」即成功。
            success_val = parsed.get("success")
            if success_val is False:
                continue
            if success_val is True:
                return True
            if "success" not in parsed and "error" not in parsed:
                return True
        return False

    @classmethod
    def _detect_last_assistant_json(cls, messages: list[Any]) -> dict[str, Any] | None:
        """回退检测：最后一条含文本的 assistant 消息里找 evaluation_result JSON。"""
        for msg in reversed(messages or []):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            text = str(msg.get("content") or "")
            if not text.strip():
                continue
            detected = cls._detect_evaluation_result_json(text)
            if detected is not None:
                return detected
            # 只看最后一条有文本的 assistant（更早的属旧轮次，不当本轮证据）
            return None
        return None

    @staticmethod
    def _detect_evaluation_result_json(text: str) -> dict[str, Any] | None:
        """检测文本中是否包含有效的 evaluation_result JSON。

        解析单源 = ``_libs.evaluation_result.extract_evaluation_result``
        （task_evaluate 回收兜底共用）：passed 仅认 bool / "true"/"false"
        字符串，score 仅认数值（else None），坏 payload 一律无证据。
        """
        return extract_evaluation_result(text)

    async def _has_active_children(
        self,
        task_id: str,
        ctx: PluginContext,
    ) -> bool:
        """检查当前任务是否有活跃的子任务（共享单通道）。

        判定收敛在 ``pipeline.active_children.find_active_children``：主判定 =
        state 聚合行 lineage.parent_pipeline_id + task.status；``submitted_task_ids``
        是加速标记而非独立真相（标记缺失不改变结论）；读面不可用时按共享回退链
        （加速标记 → task_service 枚举，两级服务获取同源）降级。
        """
        has_active, _ = await find_active_children(
            str(ctx.state.get("pipeline_id") or ""),
            task_id,
            state_reader=_get_state_reader(),
            ctx=ctx,
            submitted_marker=ctx.state.get("submitted_task_ids"),
        )
        return has_active

    async def _db_task_status(self, task_id: str) -> str | None:
        """本管道聚合行（task_id==pipeline_id）的 DB 权威 task.status。

        经 state 聚合读取器（server.py on_load 注入，与活跃子任务判定同一注入
        点）取行并返回该行 ``task.status``。``_get_state_reader()`` 结果按形态
        归一：可调用 = reader（sync/async，sync/async 读取与异常容错复用共享
        ``read_state_rows``）；list = 预取聚合行直用。诚实降级面：reader 未注入/
        行缺失/行无 ``task.status``（评估者模式管道自身行形态）/读面异常 →
        None，消费点全部退化为 state-only 现状行为。DB failed/running 等其他
        状态原样返回，完成语义判定由消费点自行承担（本方法不做语义过滤）。"""
        if not task_id:
            return None
        source = _get_state_reader()
        if source is None:
            return None
        rows = await read_state_rows(source if callable(source) else (lambda: source))
        if not rows:
            return None
        for row in rows:
            if str(row.get("pipeline_id") or "") == task_id:
                status = row.get("task.status")
                return str(status) if status else None
        return None

    @staticmethod
    def _last_assistant_has_text(state: dict[str, Any]) -> bool:
        """检查 messages 中最后一条 assistant 消息是否有文本内容。"""
        messages = state.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    return True
                break
        return False

    def _is_evaluation_mode(self, state: dict[str, Any]) -> bool:
        """按管道身份字段判定评估者模式。

        路由键 = 评估域登记字段 ``evaluation.metric_id``（task_evaluate 派发
        评估子管道时写入，随管道 state 持久化）；也接受显式
        ``evaluation.mode == "evaluator"``。不读插件实例配置：同 sidecar 多
        管道并发时实例字段会被其他管道的 execute 覆盖（2026-11-12 实证）。
        """
        return bool(state.get("evaluation.metric_id")) or (
            state.get("evaluation.mode") == "evaluator"
        )

    def _build_reminder(self, state: dict[str, Any], count: int) -> str:
        """根据任务模式构建提醒内容。"""
        # task.id 由内核创建管道时注入（值 = pipeline_id），无下划线 task_id 键
        task_id = state.get("task.id", "")

        if self._is_evaluation_mode(state):
            return self._build_evaluator_reminder(state, task_id, count)
        return self._build_executor_reminder(state, task_id, count)

    def _build_executor_reminder(
        self,
        state: dict[str, Any],
        task_id: str,
        count: int,
    ) -> str:
        """构建执行者提醒内容。"""
        parts = [
            f"【系统提醒 #{count + 1}】任务完成前必须提交评估：请先调用 task_evaluate 工具按验收标准评估本任务，评估通过后任务才能完成。"
            "请检查任务验收标准是否已满足："
        ]

        acceptance_criteria = state.get("acceptance_criteria", [])
        if acceptance_criteria:
            parts.append("验收标准：")
            for i, ac in enumerate(acceptance_criteria, 1):
                if isinstance(ac, dict):
                    desc = ac.get("description", ac.get("metric_id", str(ac)))
                    parts.append(f"  {i}. {desc}")
                else:
                    parts.append(f"  {i}. {ac}")

        parts.append(
            f'- 如果已完成所有验收标准：调用 task_evaluate(action="auto_complete", task_id="{task_id}") 提交评估',
        )
        parts.append("- 如果尚未完成：继续执行任务，完成后再提交评估")

        reject_count = state.get("reject_count", 0)
        if reject_count > 0:
            parts.append(f"\n⚠️ 此任务已被打回 {reject_count} 次，请仔细检查验收标准。")
            reject_reason = state.get("reject_reason", "")
            if reject_reason:
                parts.append(f"打回原因: {reject_reason}")

        return "\n".join(parts)

    @staticmethod
    def _positive_int(raw: Any) -> int:
        """容错正整数读取（state 标量可能是字符串/数字/None）。"""
        try:
            value = int(str(raw))
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    def _max_reminders_for(self, state: dict[str, Any]) -> int:
        """本管道 per-run 提醒预算（state 注入优先，缺省构造期默认）。"""
        value = self._positive_int(state.get("max_reminders"))
        return value if value > 0 else self._default_max_reminders

    def _max_reminders_total_for(self, state: dict[str, Any]) -> int:
        """本管道任务级总预算（plugin_configs 覆盖，缺省构造期默认）。"""
        config = state.get("plugin_configs") or {}
        if isinstance(config, dict):
            reminder_config = config.get("task_reminder") or {}
            if isinstance(reminder_config, dict):
                value = self._positive_int(reminder_config.get("max_reminders_total"))
                if value > 0:
                    return value
        return self._default_max_reminders_total

    def _build_evaluator_reminder(
        self,
        state: dict[str, Any],
        task_id: str,
        count: int,
    ) -> str:
        """构建评估者提醒内容。"""
        parts = [f"【评估提醒 #{count + 1}】请输出评估结论（JSON格式）："]

        parts.append('{"evaluation_result": {"passed": true/false, "score": 0-100, "feedback": "评估说明..."}}')

        acceptance_criteria = state.get("acceptance_criteria", [])
        if acceptance_criteria:
            parts.append("\n验收标准：")
            for i, ac in enumerate(acceptance_criteria, 1):
                if isinstance(ac, dict):
                    metric_id = ac.get("metric_id", f"metric_{i}")
                    threshold = ac.get("pass_threshold", 0.8)
                    parts.append(f"  {i}. [{metric_id}] 通过阈值: {threshold}")

        return "\n".join(parts)

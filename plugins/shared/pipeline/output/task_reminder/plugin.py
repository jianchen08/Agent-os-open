"""任务评估提醒 Output 插件。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from enum_utils import safe_enum_value
from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ACTIVE_TASK_STATUSES

logger = logging.getLogger(__name__)

# 子任务挂号键前缀（信号③）：task_submit 成功时经 chat.send_message no_dispatch
# 向提交者管道写 ``task.subtasks_pending.<task_id>``（值 = 提交时间戳，扁平键
# 标量值无跨边界 JSON 还原问题）；子任务终态事件经 task_service 写 null 清除
# （pipeline-state.update 无键删除语义，null 即已回执）。
_PENDING_SUBTASK_PREFIX = "task.subtasks_pending."


class TaskReminder(IOutputPlugin):
    """任务评估提醒 Output 插件。"""


    # 评估模式下连续仅工具调用的提醒阈值
    _EVAL_TOOL_ONLY_THRESHOLD = 6

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._max_reminders: int = self._config.get("max_reminders", 10)
        self._evaluation_mode: bool = self._config.get("evaluation_mode", False)

    @property
    def name(self) -> str:
        return "task_reminder"

    @property
    def priority(self) -> int:
        return self._config.get("priority", 35)

    async def execute(self, ctx: PluginContext) -> OutputResult:  # noqa: PLR0911
        """执行任务评估提醒检测。

        收束判据 = 三信号按序短路（ADR 2026-08-28-task-closure-three-signal-gate），
        全部读 state / 对话结构，不解析渲染文本形态：
        1. 本轮有工具调用 → 路由工具执行，不评判；
        2. ``state.task.status == completed``（经 pipeline-state 写面写入的）→
           当轮收束 end——评估成功落库的当轮即收束，提醒根本不注入，
           "完成被覆盖为 failed" 在该路径不可达；
        3. 存在未回子任务挂号键 → 本轮收束等待唤醒通知，不催评估；
        4. 三信号皆否（纯文本 + 未完成 + 无挂号）→ 注入提醒；耗尽裁决改写
           终态前必须复查信号②，已完成态不可覆盖。

        前置门槛（不属三信号）：pending→running 推进（幂等）/ llm_call 轮 /
        L1 调度层永不触发 / ``task.id`` 存在性（state 单一真值，缺失即会话
        管道）/ 活跃子任务（纯文本是等待/协调行为）/ 评估模式连续无文本计数。

        ``_has_successful_task_evaluate``（messages JSON 文本检测）降为次级
        证据保留——真实路径 LLM 面被 result_format 渲染化，文本形态不作主证据。
        """
        self._apply_runtime_config(ctx)
        state = ctx.state
        iteration = state.get("iteration", -1)

        advanced = self._advance_pending_status(state)
        if advanced is not None:
            return advanced

        core_type = state.get("core_type", "")
        if core_type != "llm_call":
            logger.debug(
                "TaskReminder[iter=%s]: skip, core_type=%s (need llm_call)",
                iteration,
                core_type,
            )
            return OutputResult()

        # ── L1 调度层永不触发 ──
        # L1（灵汐）的纯文本输出是正常的调度/沟通汇报，不代表"忘了提交评估"。
        # 层级单一真值：顶层 agent_level（context_build 以实际 Agent 层级
        # 无条件覆盖，见 context_build/plugin.py）。reminder 只对叶子执行者有意义。
        if state.get("agent_level", "") == "L1":
            logger.debug(
                "TaskReminder[iter=%s]: skip, L1 调度层不触发 reminder",
                iteration,
            )
            return OutputResult()

        raw_tool_calls = state.get("raw_tool_calls", [])
        raw_result = state.get("raw_result", "")
        has_tool_calls = bool(raw_tool_calls)
        has_text = bool(raw_result and str(raw_result).strip())
        if not has_text and has_tool_calls:
            has_text = self._last_assistant_has_text(state)

        eval_counted = self._eval_no_text_tracking(
            state, iteration=iteration, has_text=has_text,
        )
        if eval_counted is not None:
            return eval_counted

        # ── 信号②：state 完成证据 → 当轮收束（置于工具轮路由之前）。
        # 评估成功的那一轮本身就是工具轮：若被信号①路由回 LLM，证据轮永不
        # 裁决 → LLM 反复重调 task_evaluate 的无限循环。task.id 缺失（会话
        # 管道）不可能有该证据，天然跳过。──
        if self._task_completed_in_state(state):
            return self._completed_round_result(
                state, iteration=iteration, task_id=state.get("task.id"),
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

        # ── 活跃子任务：纯文本是等待/协调行为，不催提交评估 ──
        if await self._has_active_children(task_id, ctx):
            logger.info(
                "TaskReminder[iter=%s][task=%s]: skip, has active child tasks",
                iteration,
                task_id,
            )
            return OutputResult()

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

        reminder_count = state.get("evaluate_reminder_count", 0)
        if reminder_count >= self._max_reminders:
            return self._reminder_exhausted_result(
                state, reminder_count=reminder_count, iteration=iteration, task_id=task_id,
            )
        return self._inject_reminder_result(
            state, reminder_count=reminder_count, iteration=iteration, task_id=task_id,
        )

    @staticmethod
    def _advance_pending_status(state: dict[str, Any]) -> OutputResult | None:
        """任务状态推进：pending → running。

        任务提交后出生值 pending 只应停留极短时间：管道走完第一轮插件即视为
        已开始执行。内核不再回写任务状态（run 终态只广播事件），此处由任务域
        插件推进——任何轮次都推进（幂等：非 pending 返回 None 不动）。
        """
        if state.get("task.status") != "pending":
            return None
        logger.info(
            "TaskReminder[iter=%s]: task.status pending -> running（任务已开始执行）",
            state.get("iteration", -1),
        )
        return OutputResult(state_updates={"task.status": "running"})

    def _eval_no_text_tracking(
        self,
        state: dict[str, Any],
        *,
        iteration: Any,
        has_text: bool,
    ) -> OutputResult | None:
        """评估模式连续无文本追踪（仅工具调用/空输出场景）。

        未达强制阈值：计数并返回计数结果；达阈值且有提醒余量：注入强制提醒；
        其余情形返回 None（交回主级联继续判定）。
        """
        if not (self._is_evaluation_mode(state) and not has_text):
            return None

        task_id = str(state.get("task.id") or "-")
        tool_only_count = state.get("eval_tool_only_count", 0) + 1
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
                }
            )

        reminder_count = state.get("evaluate_reminder_count", 0)
        if reminder_count >= self._max_reminders:
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
        messages = list(state.get("messages", []))
        messages.append({"role": "system", "content": reminder_message})
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
                "messages": messages,
                "evaluate_reminder_count": reminder_count + 1,
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
        """有文本输出时的次级收束面：evaluation JSON 检测 / 次级证据放行 / 会话模式。

        Returns:
            收束结果（end 或清标志放行）；都不命中返回 None 继续。
        """
        raw_result = state.get("raw_result", "")
        raw_text = str(raw_result)

        if self._is_evaluation_mode(state):
            detected = self._detect_evaluation_result_json(raw_text)
            if detected is not None:
                logger.info(
                    "TaskReminder[iter=%s][task=%s]: evaluation_result JSON detected, sending end signal",
                    iteration,
                    task_id,
                )
                return OutputResult(
                    state_updates={
                        "evaluation.detected_result": detected,
                        "ended": True,
                    },
                )

        # 次级证据放行（降级保留，ADR 2026-08-28 证据契约）：messages role=tool
        # JSON 文本检测——真实路径 LLM 面被 result_format 渲染化，仅作主证据
        # （信号② state 证据）缺席时的补充放行；成功后清除续跑标志（防残留
        # 标志把后续纯文本轮误路由回 LLM 造成死循环）。
        if self._has_successful_task_evaluate(state.get("messages", [])):
            logger.info(
                "TaskReminder[iter=%s][task=%s]: task_evaluate success detected, allowing end",
                iteration,
                task_id,
            )
            return OutputResult(state_updates={"_has_new_llm_input": False})

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
        for key, value in state.items():
            if str(key).startswith(_PENDING_SUBTASK_PREFIX) and value:
                return True
        return False

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
        """
        state_updates: dict[str, Any] = {"_has_new_llm_input": False}
        if str(state.get("task.status") or "") != "completed":
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
    ) -> OutputResult:
        """提醒耗尽（评估闸门的插件裁决，内核只落库不判定）。

        复查完成证据（信号② state 证据 ∪ evaluation.detected_result）：任一
        在场 → 保持现状交评估流程收尾，终态不可覆盖为 failed；无任何证据 →
        task.status 标 failed（评估提醒耗尽 = agent 未完成评估义务 = 任务失败；
        task_failed 事件携带 parent_pipeline_id 经 triggers_ext 通知上级）。
        两者都结束本轮并发 end。
        """
        state_updates: dict[str, Any] = {}
        if not self._has_completion_evidence(state):
            state_updates["task.status"] = "failed"
            state_updates["task.ended_at"] = datetime.now(UTC).isoformat()
            logger.warning(
                "TaskReminder[iter=%s][task=%s]: max_reminders reached without evaluation, "
                "task.status -> failed",
                iteration,
                task_id,
            )
        else:
            logger.warning(
                "TaskReminder[iter=%s][task=%s]: max_reminders reached "
                "(%d >= %d), sending end signal to prevent infinite loop",
                iteration,
                task_id,
                reminder_count,
                self._max_reminders,
            )
        # 清除续跑标志：提醒已耗尽，本轮必须结束（防残留标志把
        # 后续纯文本轮误路由回 LLM 造成死循环）
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
    ) -> OutputResult:
        """空回复重试耗尽 → 任务直接失败（用户裁定 2026-08-30）。

        复查完成证据（信号② state 证据 ∪ evaluation.detected_result，与
        _reminder_exhausted_result 同一复查面）：任一在场保持现状不覆盖
        （已完成态不可覆盖）；无证据且为任务管道 → task.status=failed
        （agent 连续空响应退出 = 未完成执行义务 = 任务失败，task_failed
        事件经通知链上报上级）；会话管道（无 task.id）只收束不落任务终态。
        两者都结束本轮并发 end。
        """
        state_updates: dict[str, Any] = {}
        if task_id:
            if not self._has_completion_evidence(state):
                state_updates["task.status"] = "failed"
                state_updates["task.ended_at"] = datetime.now(UTC).isoformat()
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
    ) -> OutputResult:
        """注入下一轮系统提醒并置续跑标志触发 next_llm。"""
        reminder_message = self._build_reminder(state, reminder_count)

        messages = list(state.get("messages", []))
        messages.append({"role": "system", "content": reminder_message})

        logger.info(
            "TaskReminder[iter=%s][task=%s]: injecting reminder #%d/%d, triggering next_llm",
            iteration,
            task_id,
            reminder_count + 1,
            self._max_reminders,
        )
        # 回 LLM 经 _has_new_llm_input 状态键 + DSL 路由承载（控制状态键契约）
        return OutputResult(
            state_updates={
                "messages": messages,
                "evaluate_reminder_count": reminder_count + 1,
                "_has_new_llm_input": True,
            },
        )

    @staticmethod
    def _has_successful_task_evaluate(messages: list) -> bool:
        """messages 中是否存在成功的 task_evaluate 工具调用。

        assistant tool_calls 含 name=task_evaluate 且对应 role=tool 结果为
        JSON 载荷、顶层 success 字段布尔值为 true（task_evaluate 成功返回
        {"success": true,...}）。结构化精确判定：非 JSON 载荷（模型复述字段
        名的散文/打字稿）或非布尔 success 值均不构成评估证据。
        """
        eval_call_ids = set()
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for c in msg.get("tool_calls") or []:
                fn = c.get("function") or {}
                if fn.get("name") == "task_evaluate" and c.get("id"):
                    eval_call_ids.add(c["id"])
        if not eval_call_ids:
            return False
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
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
            # 两种成功形态（对齐 SDK ToolExecutionResult.to_dict 序列化契约）：
            # - 全量形态：顶层 success=true；
            # - slim 形态（LLM 面默认序列化）：成功时**省略 success 键**只留
            #   output/metadata，失败时才写 success=false——即「无 success 键
            #   且无 error」即成功证据。只认 success===true 会导致评估成功
            #   却永远识别不到（无限提醒循环，实测管道 b8b92a56ad72）。
            success_val = parsed.get("success")
            if success_val is False:
                continue
            if success_val is True:
                return True
            # slim 形态：无 success 键且无 error（SDK to_dict(slim=True) 成功时
            # 省略 success，失败时才写 success=false）
            if "success" not in parsed and "error" not in parsed:
                return True
        return False

    @staticmethod
    def _detect_evaluation_result_json(text: str) -> dict[str, Any] | None:
        """检测文本中是否包含有效的 evaluation_result JSON。"""
        candidates = []

        brace_depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and start >= 0:
                    candidates.append(text[start : i + 1])
                    start = -1

        for candidate in reversed(candidates):
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue

            inner = parsed.get("evaluation_result")
            if isinstance(inner, dict):
                target = inner
            elif isinstance(parsed, dict) and "passed" in parsed:
                target = parsed
            else:
                continue

            if "passed" in target:
                return {
                    "passed": bool(target["passed"]),
                    "score": float(target.get("score", 0)),
                    "feedback": str(target.get("feedback", "")),
                    "suggestions": target.get("suggestions", []),
                }

        return None

    async def _has_active_children(
        self,
        task_id: str,
        ctx: PluginContext,
    ) -> bool:
        """检查当前任务是否有活跃的子任务。

        数据源分层（任务身份 = 管道 state 单一真值）：
        ① **state 优先**：tool_core 在 task_submit 成功后自动把子任务 id 写入
           state 的 ``submitted_task_ids``（副作用收集，跨进程可靠）——
           有标记即视为有活跃子任务，不催提交评估；
        ② **task_service 回退**：state 无标记时经 0.2 service_access 查
           list_subtasks（兼容无副作用收集的旧路径/容器任务）。
        """
        submitted_ids = ctx.state.get("submitted_task_ids")
        if isinstance(submitted_ids, list) and submitted_ids:
            return True

        try:
            task_service = ctx.get_service("task_service")
        except KeyError:
            # 服务未注册（如本插件 sidecar 无 task_service）→ 回退进程内单例。
            # ImportError = 本进程不可达（已知 sidecar 布局），按无服务处理；
            # get_task_service 自身初始化失败返回 None（已记日志），不抛。
            try:
                from tasks.service_access import get_task_service  # noqa: PLC0415

                task_service = get_task_service()
            except ImportError:
                task_service = None

        if task_service is None:
            return False

        # 内部服务调用失败不吞——吞掉会把服务故障伪装成"无活跃子任务"，
        # 直接改变 reminder 触发判定。失败向上抛，由引擎统一 warn+继续
        # 本轮 reminder 并留下可见日志。
        subtasks = task_service.list_subtasks(task_id)

        for st in subtasks:
            status = safe_enum_value(st.status)
            if status in ACTIVE_TASK_STATUSES:
                return True
        return False

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
        """判断当前是否为评估者模式。"""
        return bool(self._evaluation_mode)

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

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 Agent 配置覆盖运行时参数（每次 execute 复位后应用）。

        同 sidecar 实例被多个 agent 管道连续复用：先回构造默认值再应用本管道
        state / plugin_configs 注入的 max_reminders / evaluation_mode——前一
        agent 的配置不得残留到下一 agent。
        """
        self._max_reminders = self._config.get("max_reminders", 10)
        self._evaluation_mode = self._config.get("evaluation_mode", False)

        agent_max_reminders = ctx.state.get("max_reminders")
        if agent_max_reminders is not None and agent_max_reminders > 0:
            self._max_reminders = agent_max_reminders

        plugin_configs = ctx.state.get("plugin_configs", {})
        task_reminder_config = plugin_configs.get("task_reminder", {})
        if "evaluation_mode" in task_reminder_config:
            self._evaluation_mode = task_reminder_config["evaluation_mode"]

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

"""任务评估提醒 Output 插件。"""

from __future__ import annotations

import json
import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, RouteSignal
from utils.enum_utils import safe_enum_value

logger = logging.getLogger(__name__)


class TaskReminder(IOutputPlugin):
    """任务评估提醒 Output 插件。"""

    error_policy = ErrorPolicy.SKIP

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

    async def execute(self, ctx: PluginContext) -> OutputResult:  # noqa: PLR0911,PLR0912,PLR0915
        """执行任务评估提醒检测。"""
        self._apply_runtime_config(ctx)

        state = ctx.state
        iteration = state.get("iteration", -1)

        core_type = state.get("core_type", "")
        if core_type != "llm_call":
            logger.debug(
                "TaskReminder[iter=%s]: skip, core_type=%s (need llm_call)",
                iteration,
                core_type,
            )
            return OutputResult()

        task_id = state.get("task_id")
        if not task_id:
            logger.debug(
                "TaskReminder[iter=%s]: skip, no task_id in state",
                iteration,
            )
            return OutputResult()

        # ── 规则 3：L1 调度层永不触发 ──
        # L1（灵汐）是调度层，它的纯文本输出是正常的调度/沟通汇报，
        # 不代表"忘了提交评估"。reminder 只对叶子执行者有意义。
        agent_level = state.get("agent_level", "")
        if agent_level == "L1":
            logger.debug(
                "TaskReminder[iter=%s][task=%s]: skip, L1 调度层不触发 reminder",
                iteration,
                task_id,
            )
            return OutputResult()

        task_service = state.get("task_service")
        if not task_service:
            try:
                from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

                task_service = get_service_provider().get("task_service")
            except Exception:
                pass
        if task_service:
            try:
                _task_obj = task_service.get_task(task_id)
                if _task_obj is None:
                    if task_id.startswith("__eval__"):
                        pass
                    else:
                        logger.info(
                            "TaskReminder[iter=%s][task=%s]: task not found, sending end signal",
                            iteration,
                            task_id,
                        )
                        return OutputResult(
                            route_signal=RouteSignal(
                                route_type="end",
                                reason=f"task_reminder: task {task_id} no longer exists",
                            ),
                        )
            except Exception:
                pass

        # ── 规则 4：有活跃下级任务时不触发（提前到最前面）──
        # 任务有活跃子任务说明在等子任务完成，当前任务的纯文本输出
        # 是正常的等待/协调行为，不该被催提交评估。
        if await self._has_active_children(task_id, ctx):
            logger.info(
                "TaskReminder[iter=%s][task=%s]: skip, has active child tasks",
                iteration,
                task_id,
            )
            return OutputResult()

        evaluation_mode = self._is_evaluation_mode(state)

        raw_tool_calls = state.get("raw_tool_calls", [])
        raw_result = state.get("raw_result", "")
        has_tool_calls = bool(raw_tool_calls)
        has_text = bool(raw_result and str(raw_result).strip())

        if not has_text and has_tool_calls:
            has_text = self._last_assistant_has_text(state)

        # 评估模式：追踪连续仅工具调用/空输出次数，达到阈值后强制注入提醒
        # 覆盖场景：LLM 只输出工具调用、LLM 调用失败（两者都为空）
        if evaluation_mode and not has_text:
            tool_only_count = state.get("eval_tool_only_count", 0) + 1
            if tool_only_count >= self._EVAL_TOOL_ONLY_THRESHOLD:
                reminder_count = state.get("evaluate_reminder_count", 0)
                if reminder_count < self._max_reminders:
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
                    return OutputResult(
                        state_updates={
                            "messages": messages,
                            "evaluate_reminder_count": reminder_count + 1,
                            "eval_tool_only_count": 0,
                            "_has_new_llm_input": True,
                        },
                        route_signal=RouteSignal(
                            route_type="next_llm",
                            reason=f"task_reminder: eval force reminder after {tool_only_count} no-text iters",
                        ),
                    )
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

        if has_tool_calls:
            logger.debug(
                "TaskReminder[iter=%s][task=%s]: skip, has tool calls (len=%d)",
                iteration,
                task_id,
                len(raw_tool_calls),
            )
            return OutputResult()

        if not has_text:
            logger.debug(
                "TaskReminder[iter=%s][task=%s]: skip, raw_result is empty",
                iteration,
                task_id,
            )
            return OutputResult()

        raw_text = str(raw_result)

        if evaluation_mode:
            detected = self._detect_evaluation_result_json(raw_text)
            if detected is not None:
                logger.info(
                    "TaskReminder[iter=%s][task=%s]: evaluation_result JSON detected, sending end signal",
                    iteration,
                    task_id,
                )
                return OutputResult(
                    state_updates={"evaluation.detected_result": detected},
                    route_signal=RouteSignal(
                        route_type="end",
                        reason="task_reminder: evaluation_result JSON detected in output",
                    ),
                )

        if state.get("task_evaluation_completed"):
            logger.debug(
                "TaskReminder[iter=%s][task=%s]: skip, task already evaluated and passed",
                iteration,
                task_id,
            )
            return OutputResult()

        if state.get("conversation_mode"):
            logger.info(
                "TaskReminder[iter=%s][task=%s]: skip, conversation mode active",
                iteration,
                task_id,
            )
            return OutputResult()

        # 注：_has_active_children 已在 execute 入口提前检查，此处不再重复。

        reminder_count = state.get("evaluate_reminder_count", 0)
        if reminder_count >= self._max_reminders:
            logger.warning(
                "TaskReminder[iter=%s][task=%s]: max_reminders reached "
                "(%d >= %d), sending end signal to prevent infinite loop",
                iteration,
                task_id,
                reminder_count,
                self._max_reminders,
            )
            return OutputResult(
                route_signal=RouteSignal(
                    route_type="end",
                    reason=f"task_reminder: max_reminders reached ({reminder_count}/{self._max_reminders}), task may be stuck",
                ),
            )

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

        return OutputResult(
            state_updates={
                "messages": messages,
                "evaluate_reminder_count": reminder_count + 1,
                "_has_new_llm_input": True,
            },
            route_signal=RouteSignal(
                route_type="next_llm",
                reason=f"task_reminder: text_only output, reminder #{reminder_count + 1}",
            ),
        )

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
        """检查当前任务是否有活跃的子任务。"""
        try:
            task_service = ctx.get_service("task_service")
        except KeyError:
            try:
                from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

                provider = get_service_provider()
                task_service = provider.get("task_service")
            except Exception:
                return False

        try:
            subtasks = task_service.list_subtasks(task_id)
        except Exception:
            return False

        active_statuses = {"pending", "running", "evaluating", "scheduled"}
        for st in subtasks:
            status = safe_enum_value(st.status)
            if status in active_statuses:
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
        task_id = state.get("task_id", "")

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
        parts = [f"【系统提醒 #{count + 1}】请检查任务验收标准是否已满足："]

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
        """从 Agent 配置覆盖运行时参数。"""
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

"""任务评估提醒 Output 插件。

在 LLM 只输出纯文本（没有工具调用）时，注入系统提醒消息，
触发新一轮 LLM 调用，提醒大模型提交评估或输出评估结果。

对应旧代码的 should_continue → TaskEvaluationStrategy → evaluate_reminder_node 流程。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, RouteSignal
from utils.enum_utils import safe_enum_value

logger = logging.getLogger(__name__)


class TaskReminder(IOutputPlugin):
    """任务评估提醒 Output 插件。

    检测 LLM 只输出纯文本（没有工具调用）时，根据任务模式注入不同的提醒消息，
    并通过 route_signal=next_llm 触发新一轮 LLM 调用。

    两种模式：
    - 执行者模式（默认）：提醒"完成任务后使用 task_evaluate 工具提交评估"
    - 评估者模式（evaluation_mode=True）：提醒"输出结构化的评估结果"

    Attributes:
        max_reminders: 最大提醒次数（默认 3）
        cooldown_seconds: 提醒冷却秒数（默认 300）
    """

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
        """执行任务评估提醒检测。

        触发条件（BUG-FIX-fix_20260624_task_reminder_loop）：
        1. core_type 为 llm_call（仅 LLM 输出阶段，非工具执行后）
        2. 有 task_id（在任务上下文中）
        3. **非 L1 调度层**：L1 的纯文本输出是正常调度汇报，不该被催
        4. **任务没有活跃下级**：有下级任务说明在等子任务，不该催当前任务
        5. **LLM 这一轮没调工具（只输出纯文本）**：正在调工具说明有进展，不催；
           只在 LLM"光说不练"时才提醒它该提交 task_evaluate 了
        6. 提醒次数未超限

        满足条件时注入系统提醒消息到 messages，触发 next_llm。
        """
        self._apply_runtime_config(ctx)

        state = ctx.state
        iteration = state.get("iteration", -1)

        core_type = state.get("core_type", "")
        if core_type != "llm_call":
            logger.debug(
                "TaskReminder[iter=%s]: skip, core_type=%s (need llm_call)",
                iteration, core_type,
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
                iteration, task_id,
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
                            iteration, task_id,
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
                iteration, task_id,
            )
            return OutputResult()

        evaluation_mode = self._is_evaluation_mode(state)

        raw_tool_calls = state.get("raw_tool_calls", [])
        raw_result = state.get("raw_result", "")
        has_tool_calls = bool(raw_tool_calls)
        has_text = bool(raw_result and str(raw_result).strip())

        # BUG-FIX-fix_20260625_reminder_on_empty_output:
        # 历史逻辑在 has_text 和 has_tool_calls 同时为 False（本轮 LLM 无输出，
        # 如流式截断/调用失败）时，回退到 _last_assistant_has_text 去历史消息
        # 里捞旧文本，把"本轮空输出"误判成"有文本输出"，从而触发 reminder。
        # 这会导致 LLM 一旦某轮输出为空就被 reminder 反复催促，形成死循环。
        #
        # 回退逻辑只保留它原本的用途：本轮 LLM 返回了 tool_calls 但文本被
        # output_repetition_guard 等前置插件清空（has_tool_calls=True 且 has_text=False）
        # 时，从 messages 里确认 LLM 确实输出过文本（用于评估模式判定）。
        # 绝不在本轮完全无输出时用历史文本伪装成有输出。
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
                        '```json\n'
                        '{"evaluation_result": {"passed": true/false, '
                        '"score": 0-100, "feedback": "评估说明..."}}\n'
                        '```'
                    )
                    messages = list(state.get("messages", []))
                    messages.append({"role": "system", "content": reminder_message})
                    logger.info(
                        "TaskReminder[iter=%s][task=%s]: eval force reminder "
                        "after %d no-text iters, reminder #%d",
                        iteration, task_id, tool_only_count,
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
                iteration, task_id, tool_only_count,
            )
            return OutputResult(state_updates={
                "eval_tool_only_count": tool_only_count,
            })

        if has_tool_calls:
            logger.debug(
                "TaskReminder[iter=%s][task=%s]: skip, has tool calls (len=%d)",
                iteration, task_id, len(raw_tool_calls),
            )
            return OutputResult()

        if not has_text:
            logger.debug(
                "TaskReminder[iter=%s][task=%s]: skip, raw_result is empty",
                iteration, task_id,
            )
            return OutputResult()

        raw_text = str(raw_result)

        if evaluation_mode:
            detected = self._detect_evaluation_result_json(raw_text)
            if detected is not None:
                logger.info(
                    "TaskReminder[iter=%s][task=%s]: evaluation_result JSON "
                    "detected, sending end signal",
                    iteration, task_id,
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
                iteration, task_id,
            )
            return OutputResult()

        if state.get("conversation_mode"):
            logger.info(
                "TaskReminder[iter=%s][task=%s]: skip, conversation mode active",
                iteration, task_id,
            )
            return OutputResult()

        # 注：_has_active_children 已在 execute 入口提前检查，此处不再重复。

        reminder_count = state.get("evaluate_reminder_count", 0)
        if reminder_count >= self._max_reminders:
            logger.warning(
                "TaskReminder[iter=%s][task=%s]: max_reminders reached "
                "(%d >= %d), sending end signal to prevent infinite loop",
                iteration, task_id, reminder_count, self._max_reminders,
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
            "TaskReminder[iter=%s][task=%s]: injecting reminder #%d/%d, "
            "triggering next_llm",
            iteration, task_id, reminder_count + 1, self._max_reminders,
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
        """检测文本中是否包含有效的 evaluation_result JSON。

        从文本中提取所有 JSON 候选块，尝试解析并检查是否包含
        evaluation_result 或直接的 passed/score 字段。

        Args:
            text: LLM 输出的原始文本

        Returns:
            解析后的评估结果字典（含 passed/score/feedback/suggestions），
            未检测到时返回 None
        """
        candidates = []

        brace_depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == '{':
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
                if brace_depth == 0 and start >= 0:
                    candidates.append(text[start:i + 1])
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
        self, task_id: str, ctx: PluginContext,
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
        """检查 messages 中最后一条 assistant 消息是否有文本内容。

        当 output_repetition_guard 等前置插件清空 raw_result 后，
        messages 中仍保留 LLM 的原始文本输出，作为回退检测手段。

        Args:
            state: 管道状态字典

        Returns:
            最后一条 assistant 消息是否有非空文本
        """
        messages = state.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    return True
                break
        return False

    def _is_evaluation_mode(self, state: dict[str, Any]) -> bool:
        """判断当前是否为评估者模式。

        由 agent YAML 中 plugins.enabled.task_reminder.evaluation_mode
        控制，通过 PipelineEngine._apply_agent_plugin_configs 合并到
        插件构造配置。
        """
        return bool(self._evaluation_mode)

    def _build_reminder(self, state: dict[str, Any], count: int) -> str:
        """根据任务模式构建提醒内容。

        Args:
            state: 管道状态字典
            count: 当前提醒次数

        Returns:
            提醒消息字符串
        """
        task_id = state.get("task_id", "")

        if self._is_evaluation_mode(state):
            return self._build_evaluator_reminder(state, task_id, count)
        return self._build_executor_reminder(state, task_id, count)

    def _build_executor_reminder(
        self, state: dict[str, Any], task_id: str, count: int,
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
            f'- 如果已完成所有验收标准：调用 task_evaluate(action="auto_complete", '
            f'task_id="{task_id}") 提交评估',
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
        """从 Agent 配置覆盖运行时参数。

        优先使用 Agent YAML 中配置的 max_reminders / max_iterations
        覆盖构造时的默认值。

        Args:
            ctx: 插件执行上下文
        """
        agent_max_reminders = ctx.state.get("max_reminders")
        if agent_max_reminders is not None and agent_max_reminders > 0:
            self._max_reminders = agent_max_reminders

        plugin_configs = ctx.state.get("plugin_configs", {})
        task_reminder_config = plugin_configs.get("task_reminder", {})
        if "evaluation_mode" in task_reminder_config:
            self._evaluation_mode = task_reminder_config["evaluation_mode"]

    def _build_evaluator_reminder(
        self, state: dict[str, Any], task_id: str, count: int,
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

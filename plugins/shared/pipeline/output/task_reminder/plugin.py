"""任务评估提醒 Output 插件。"""

from __future__ import annotations

import json
import logging
from typing import Any

from enum_utils import safe_enum_value
from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ACTIVE_TASK_STATUSES, RouteSignal

logger = logging.getLogger(__name__)


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

        短路判定级联（顺序即优先级）：
        1. 任务状态推进：pending → running（幂等）
        2. 适用面门槛：llm_call 轮 + 引擎注入的 ``task.id`` + 非 L1 调度层
        3. 活跃子任务存在 → 不催（纯文本是等待/协调行为）
        4. 评估模式连续无文本 → 达阈值强制提醒，未达阈值只计数
        5. 有文本 → evaluation_result JSON 检测收束 / task_evaluate 成功证据放行
        6. 会话模式跳过 → 提醒耗尽走 pending_evaluation 裁决 → 注入续跑提醒

        步骤 2 各门槛仅数行且为同一职责（"本轮该不该由本插件裁决"），拆出检查
        函数只会制造跳转噪音；步骤 4/5/6 为各自可独立理解的行为段，拆为私有方法。
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

        # task.id 由引擎在管道出生时注入（== pipeline_id，见 chat_send_handler
        # "调用方预传的 task.id 一律覆盖为引擎 id"）；缺失即会话管道，跳过。
        task_id = state.get("task.id")
        if not task_id:
            logger.debug(
                "TaskReminder[iter=%s]: skip, no task_id in state",
                iteration,
            )
            return OutputResult()

        # ── 规则 3：L1 调度层永不触发 ──
        # L1（灵汐）的纯文本输出是正常的调度/沟通汇报，不代表"忘了提交评估"。
        # 层级单一真值：顶层 agent_level（context_build 以实际 Agent 层级
        # 无条件覆盖，见 context_build/plugin.py）。reminder 只对叶子执行者有意义。
        if state.get("agent_level", "") == "L1":
            logger.debug(
                "TaskReminder[iter=%s][task=%s]: skip, L1 调度层不触发 reminder",
                iteration,
                task_id,
            )
            return OutputResult()

        # ── 任务存在性：state 单一真值 ──
        # state 有 task.id 键即视为任务存在——不查跨进程 task_service（多 sidecar
        # 下进程内单例可能读不到其他进程刚创建的任务，误判而提前 end）。

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

        raw_tool_calls = state.get("raw_tool_calls", [])
        raw_result = state.get("raw_result", "")
        has_tool_calls = bool(raw_tool_calls)
        has_text = bool(raw_result and str(raw_result).strip())
        if not has_text and has_tool_calls:
            has_text = self._last_assistant_has_text(state)

        eval_counted = self._eval_no_text_tracking(
            state, iteration=iteration, task_id=task_id, has_text=has_text,
        )
        if eval_counted is not None:
            return eval_counted

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
        task_id: str,
        has_text: bool,
    ) -> OutputResult | None:
        """评估模式连续无文本追踪（仅工具调用/空输出场景）。

        未达强制阈值：计数并返回计数结果；达阈值且有提醒余量：注入强制提醒；
        其余情形返回 None（交回主级联继续判定）。
        """
        if not (self._is_evaluation_mode(state) and not has_text):
            return None

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

    def _resolve_text_output_gates(
        self,
        state: dict[str, Any],
        *,
        iteration: Any,
        task_id: str,
    ) -> OutputResult | None:
        """有文本输出时的两个收束闸门：evaluation JSON 检测 / 成功评估证据放行。

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
                    state_updates={"evaluation.detected_result": detected},
                    route_signal=RouteSignal(
                        route_type="end",
                        reason="task_reminder: evaluation_result JSON detected in output",
                    ),
                )

        # 评估闸门放行：已成功调用 task_evaluate 即放行结束；未评估则继续
        # 走提醒，文案要求先提交评估。成功后清除续跑标志（防残留标志把后续
        # 纯文本轮误路由回 LLM 造成死循环）。
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

    def _reminder_exhausted_result(
        self,
        state: dict[str, Any],
        *,
        reminder_count: int,
        iteration: Any,
        task_id: str,
    ) -> OutputResult:
        """提醒耗尽（评估闸门的插件裁决，内核只落库不判定）。

        无任何评估证据 → task.status 标 pending_evaluation（不落 completed，
        task_completed 通知照发携带该状态，上级可催评估/重派）；有证据 → 内核
        补默认 completed。两者都结束本轮并发 end。
        """
        state_updates: dict[str, Any] = {}
        # 本方法仅在主级联的成功证据闸门未放行时可达（无成功的 task_evaluate），
        # 唯一剩余证据源 = evaluation.detected_result（评估模式 JSON 检测产物）。
        if not state.get("evaluation.detected_result"):
            state_updates["task.status"] = "pending_evaluation"
            logger.warning(
                "TaskReminder[iter=%s][task=%s]: max_reminders reached without evaluation, "
                "task.status -> pending_evaluation",
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
        return OutputResult(
            state_updates=state_updates,
            route_signal=RouteSignal(
                route_type="end",
                reason=f"task_reminder: max_reminders reached ({reminder_count}/{self._max_reminders}), task may be stuck",
            ),
        )

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
            if isinstance(parsed, dict) and parsed.get("success") is True:
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

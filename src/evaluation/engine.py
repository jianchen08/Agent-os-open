"""统一评估引擎 — 根据指标类型分发到对应评估器。

评估引擎是评估系统的核心协调器，职责：
1. 接收评估请求（指标 ID 列表 + 输入参数）
2. 从 MetricLoader 获取指标定义
3. 根据指标类型分发评估：
   - tool → 通过 ToolRegistry 调用真实工具执行评估
   - agent → 调用 LLM Agent 评估（当前 Mock 实现）
   - human → 等待人工审核（当前 Mock 实现）
4. 使用 ExpectEvaluator 对评估输出进行期望判定
5. 收集评估结果返回

tool 类型评估器通过注入 ToolRegistry 实现真实工具调用；
当 ToolRegistry 不可用时自动 fallback 到 Mock 实现。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from evaluation.collecting_sink import CollectingSink
from evaluation.expect import ExpectEvaluator
from evaluation.loader import MetricLoader
from evaluation.types import (
    EvaluationConfig,
    EvaluationResult,
    MetricDefinition,
    MetricResult,
    MetricType,
)

logger = logging.getLogger(__name__)

# 评估器函数签名：接收指标定义和输入参数，返回输出字典（异步）
EvaluatorFunc = Callable[..., Awaitable[dict[str, Any]]]

# 指标类型优先级排序映射（TOOL → AGENT → HUMAN）
_TYPE_PRIORITY: dict[MetricType, int] = {
    MetricType.TOOL: 1,
    MetricType.AGENT: 2,
    MetricType.HUMAN: 3,
}


class EvaluationEngine:
    """统一评估引擎。

    根据指标类型分发评估到对应评估器，并使用期望评估器判定结果。

    用法：
        loader = MetricLoader()
        loader.load_all()
        engine = EvaluationEngine(loader=loader)
        result = engine.evaluate(
            task_id="abc123",
            config=EvaluationConfig(metric_ids=["format_valid"]),
        )

    Attributes:
        _loader: 指标加载器
        _expect_evaluator: 期望值评估器
        _evaluators: 各类型的评估器函数注册表
    """

    def __init__(
        self,
        loader: MetricLoader,
        expect_evaluator: ExpectEvaluator | None = None,
        tool_registry: Any | None = None,
        agent_registry: Any | None = None,
        main_loop: Any | None = None,
    ) -> None:
        """初始化评估引擎。

        Args:
            loader: 指标加载器（必须已加载指标）
            expect_evaluator: 期望值评估器，None 时创建默认实例
            tool_registry: 工具注册表，None 时工具型评估器 fallback 到 Mock
            agent_registry: AgentRegistry 实例，用于获取 evaluator_agent 配置
            main_loop: 主事件循环引用，human_interaction 等需要与主循环
                       交互的工具通过 run_coroutine_threadsafe 回调
        """
        self._loader = loader
        self._expect_evaluator = expect_evaluator or ExpectEvaluator()
        self._tool_registry = tool_registry
        self._agent_registry = agent_registry
        self._main_loop = main_loop
        self._evaluators: dict[MetricType, EvaluatorFunc] = {
            MetricType.TOOL: self._evaluate_tool,
            MetricType.AGENT: self._evaluate_agent,
            MetricType.HUMAN: self._evaluate_tool,
        }

    def register_evaluator(self, metric_type: MetricType, func: EvaluatorFunc) -> None:
        """注册自定义评估器函数。

        用于替换默认的 Mock 评估器或添加新类型。

        Args:
            metric_type: 指标类型
            func: 评估器函数
        """
        self._evaluators[metric_type] = func
        logger.info("Registered evaluator for %s", metric_type.value)

    async def evaluate(
        self,
        task_id: str,
        config: EvaluationConfig | None = None,
    ) -> EvaluationResult:
        """执行评估。

        Args:
            task_id: 关联的任务 ID
            config: 评估配置，None 时使用默认配置

        Returns:
            评估结果
        """
        config = config or EvaluationConfig()

        if config.metric_ids:
            metrics_to_run = [self._loader.get(mid) for mid in config.metric_ids if self._loader.get(mid) is not None]
        else:
            metrics_to_run = list(self._loader.metrics.values())

        return await self._evaluate_core(
            task_id=task_id,
            metrics_to_run=metrics_to_run,
            fail_fast=config.fail_fast,
            resolve_params=lambda m: config.input_params.get(m.id, {}),
        )

    async def _evaluate_core(
        self,
        task_id: str,
        metrics_to_run: list[MetricDefinition],
        fail_fast: bool,
        resolve_params: Callable[[MetricDefinition], dict[str, Any]],
    ) -> EvaluationResult:
        """公共评估核心流程。

        按指标类型优先级排序、逐个执行评估、收集结果并计算总体结果。
        evaluate 和 evaluate_with_metrics 共享此方法，避免逻辑重复。

        Args:
            task_id: 关联的任务 ID
            metrics_to_run: 待评估的指标列表（排序前）
            fail_fast: 是否在首次失败时中断
            resolve_params: 为每个指标解析输入参数的回调

        Returns:
            评估结果
        """
        if not metrics_to_run:
            logger.warning("No metrics to evaluate for task %s", task_id)
            return EvaluationResult(
                task_id=task_id,
                overall_passed=False,
                summary="无可评估指标",
            )

        metrics_to_run = sorted(metrics_to_run, key=lambda m: _TYPE_PRIORITY.get(m.metric_type, 99))

        type_order = [m.metric_type.value for m in metrics_to_run]
        logger.info(
            "Evaluation order for task %s: %s (sorted by type priority)",
            task_id,
            type_order,
        )

        results: list[MetricResult] = []
        for metric_def in metrics_to_run:
            result = await self._evaluate_metric(
                metric_def=metric_def,
                input_params=resolve_params(metric_def),
                task_id=task_id,
            )
            results.append(result)

            if fail_fast and not result.passed:
                logger.info(
                    "Fail-fast triggered: %s failed, stopping evaluation",
                    metric_def.id,
                )
                break

        eval_result = EvaluationResult(
            task_id=task_id,
            results=results,
        )
        eval_result.compute_overall()
        return eval_result

    async def evaluate_with_metrics(
        self,
        task_id: str,
        metrics: list[MetricDefinition],
        input_params: dict[str, Any] | None = None,
    ) -> EvaluationResult:
        """使用动态指标列表执行评估（不依赖 loader）。

        适用于模板评估等场景，指标由运行时动态生成而非从 YAML 文件加载。

        Args:
            task_id: 关联的任务 ID
            metrics: 动态指标定义列表
            input_params: 全局输入参数，每个指标共享

        Returns:
            评估结果
        """
        params = input_params or {}
        return await self._evaluate_core(
            task_id=task_id,
            metrics_to_run=metrics,
            fail_fast=False,
            resolve_params=lambda m: {**m.default_config, **params},
        )

    async def evaluate_single(
        self,
        task_id: str,
        metric_id: str,
        input_params: dict[str, Any] | None = None,
    ) -> MetricResult:
        """评估单个指标。

        Args:
            task_id: 关联的任务 ID
            metric_id: 指标 ID
            input_params: 评估输入参数

        Returns:
            单个指标的评估结果

        Raises:
            KeyError: 指标不存在
        """
        metric_def = self._loader.get(metric_id)
        if metric_def is None:
            raise KeyError(f"Metric '{metric_id}' not found")

        return await self._evaluate_metric(
            metric_def=metric_def,
            input_params=input_params or {},
            task_id=task_id,
        )

    @staticmethod
    def _resolve_input_mapping(
        metric_def: MetricDefinition,
    ) -> dict[str, Any]:
        """解析 input_mapping 模板，将指标上下文映射到工具输入参数。

        支持 {{ metric.xxx }} 占位符，用 str.format 风格替换。
        非字符串值（如 list、dict）直接透传。

        Args:
            metric_def: 指标定义（含 input_mapping 模板）

        Returns:
            解析后的参数字典
        """
        mapping = metric_def.input_mapping
        if not mapping:
            return {}

        context = {
            "metric": {
                "id": metric_def.id,
                "name": metric_def.name,
                "description": metric_def.description,
                "default_config": metric_def.default_config,
            },
        }

        resolved: dict[str, Any] = {}
        for key, value in mapping.items():
            if isinstance(value, str):
                resolved[key] = _resolve_template_typed(value, context)
            elif isinstance(value, list):
                resolved[key] = [
                    (
                        {_resolve_template(k, context): _resolve_template(v, context) for k, v in item.items()}
                        if isinstance(item, dict)
                        else _resolve_template(item, context)
                        if isinstance(item, str)
                        else item
                    )
                    for item in value
                ]
            elif isinstance(value, dict):
                resolved[key] = {_resolve_template(k, context): _resolve_template(v, context) for k, v in value.items()}
            else:
                resolved[key] = value
        return resolved

    async def _evaluate_metric(
        self,
        metric_def: MetricDefinition,
        input_params: dict[str, Any],
        task_id: str = "",
    ) -> MetricResult:
        """执行单个指标的评估流程。

        1. 根据指标类型获取评估器
        2. 调用评估器获取输出
        3. 使用期望评估器判定结果

        Args:
            metric_def: 指标定义
            input_params: 输入参数

        Returns:
            评估结果
        """
        evaluator = self._evaluators.get(metric_def.metric_type)
        if evaluator is None:
            return MetricResult(
                metric_id=metric_def.id,
                passed=False,
                message=f"无对应评估器: {metric_def.metric_type.value}",
                error=f"No evaluator registered for type {metric_def.metric_type}",
            )

        try:
            # 合并默认配置、input_mapping 解析结果和输入参数
            mapped = self._resolve_input_mapping(metric_def)
            merged_params = {
                **metric_def.default_config,
                **mapped,
                **input_params,
            }

            # 调用评估器获取输出
            output = await evaluator(
                metric_def,
                merged_params,
                task_id,
            )

            # 使用期望评估器判定
            result = self._expect_evaluator.evaluate(
                metric_id=metric_def.id,
                expect=metric_def.expect,
                output=output,
            )

            logger.info(
                "Expect evaluation: %s -> passed=%s, score=%s, message=%s",
                metric_def.id,
                result.passed,
                result.score,
                result.message[:100] if result.message else "",
            )
            if not result.passed and result.details:
                failed = result.details.get("failed_conditions", [])
                if failed:
                    logger.info(
                        "Failed conditions for %s: %s",
                        metric_def.id,
                        failed,
                    )

            # 尝试提取 score（agent/human 类型可能返回 score）
            if "score" in output:
                result.score = float(output["score"])
            elif "passed" in output and isinstance(output["passed"], bool):
                result.score = 100.0 if output["passed"] else 0.0

            # Agent 评估返回了动态 feedback → 覆盖静态 fail_message
            agent_feedback = output.get("feedback")
            if agent_feedback and isinstance(agent_feedback, str) and agent_feedback.strip():
                result.message = agent_feedback

            # 记录评估器输入/输出
            result.evaluator_input = merged_params
            result.evaluator_output = output

            # Agent 类型评估器返回的子管道 ID
            _pid = output.get("pipeline_run_id")
            if _pid:
                result.pipeline_run_id = str(_pid)

            return result

        except Exception as e:
            logger.error("Evaluation failed for metric %s: %s", metric_def.id, e)
            return MetricResult(
                metric_id=metric_def.id,
                passed=False,
                message=f"评估执行异常: {e}",
                error=str(e),
                evaluator_input=input_params,
            )

    @staticmethod
    def _pre_register_eval_pipeline(
        pipeline_id: str,
        task_id: str,
    ) -> None:
        """在评估子管道运行前立即注册到根任务子目录。

        与 task_worker.py 对主管道的 early binding 逻辑对称，
        确保评估管道记录从一开始就写入正确的子目录，
        避免后续 _register_eval_pipelines 因异常静默失败导致记录留在扁平位置。
        """
        if not task_id or not pipeline_id:
            return
        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            provider = get_service_provider()
            exec_storage = provider.get("execution_record_storage")
            if not exec_storage:
                return
            from tasks.service import TaskService  # noqa: PLC0415

            ts = provider.get_or_create(
                "task_service",
                TaskService,
            )
            if ts is None:
                return
            root_id = ts.get_root_task_id(task_id)
            if root_id:
                exec_storage.register_pipeline(pipeline_id, root_id)
                logger.debug(
                    "Eval pipeline pre-registered: %s -> root=%s",
                    pipeline_id,
                    root_id,
                )
        except Exception as exc:
            logger.debug(
                "Eval pipeline pre-registration skipped (non-critical): %s",
                exc,
            )

    # ── 默认评估器实现（Mock） ────────────────────────────

    async def _evaluate_tool(  # noqa: PLR0912
        self,
        metric_def: MetricDefinition,
        params: dict[str, Any],
        task_id: str = "",
    ) -> dict[str, Any]:
        """工具型评估器 — 通过 ToolRegistry 调用真实工具。

        当 tool_registry 不可用或工具不存在时，fallback 到 Mock 实现。

        Args:
            metric_def: 指标定义（evaluator_id 指定要调用的工具）
            params: 合并后的输入参数

        Returns:
            工具执行结果字典，格式：{"success": bool, "data": dict, "error": str}
        """
        evaluator_id = metric_def.evaluator_id
        logger.info(
            "Tool evaluation: %s (evaluator_id=%s)",
            metric_def.id,
            evaluator_id,
        )

        handler = None
        if self._tool_registry is not None and evaluator_id:
            handler = self._tool_registry.get_handler(evaluator_id)

        if handler is None and evaluator_id:
            handler = self._get_builtin_evaluator_handler(evaluator_id)

        if handler is not None:
            try:
                import asyncio  # noqa: PLC0415

                if evaluator_id == "human_interaction":
                    params["pipeline_id"] = f"__eval__{task_id or 'unknown'}"

                _is_human_interaction = evaluator_id == "human_interaction"
                try:
                    _running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    _running_loop = None

                logger.info(
                    "[EvalEngine] _evaluate_tool | metric=%s | evaluator=%s | pipeline_id=%s | params_keys=%s | running_loop=%s | main_loop=%s",
                    metric_def.id,
                    evaluator_id,
                    params.get("pipeline_id"),
                    list(params.keys()),
                    id(_running_loop) if _running_loop else None,
                    id(self._main_loop) if self._main_loop else None,
                )

                _needs_main_loop = (
                    _is_human_interaction
                    and self._main_loop is not None
                    and _running_loop is not None
                    and self._main_loop is not _running_loop
                    and not self._main_loop.is_closed()
                )

                if _needs_main_loop:
                    logger.info(
                        "[EvalEngine] human_interaction 跨事件循环检测 | running=%s | main=%s | 使用 run_coroutine_threadsafe",
                        id(_running_loop),
                        id(self._main_loop),
                    )
                    coro = handler(params)
                    future = asyncio.run_coroutine_threadsafe(coro, self._main_loop)
                    tool_result = future.result()
                else:
                    tool_result = handler(params)
                    if asyncio.iscoroutine(tool_result):
                        tool_result = await tool_result

                if hasattr(tool_result, "to_dict"):
                    result_dict = tool_result.to_dict()
                elif isinstance(tool_result, dict):
                    result_dict = tool_result
                else:
                    result_dict = {"success": True, "data": tool_result}

                if "success" not in result_dict:
                    status = result_dict.get("status", "completed")
                    result_dict["success"] = status == "completed"

                actual_status = result_dict.get("data", result_dict).get("status", result_dict.get("status"))
                actual_exit = result_dict.get("data", result_dict).get("exit_code")
                logger.info(
                    "Tool evaluation completed: %s -> success=%s, cmd_status=%s, exit_code=%s",
                    metric_def.id,
                    result_dict.get("success"),
                    actual_status,
                    actual_exit,
                )
                return result_dict

            except Exception as e:
                logger.error(
                    "Tool execution failed for %s (evaluator_id=%s): %s",
                    metric_def.id,
                    evaluator_id,
                    e,
                )
                return {
                    "success": False,
                    "error": str(e),
                }

        raise RuntimeError(f"Tool '{evaluator_id}' not found in registry. Metric: {metric_def.id}")

    @staticmethod
    def _get_builtin_evaluator_handler(evaluator_id: str) -> Any | None:
        """通过 DynamicToolLoader 动态发现并加载评估器 handler。

        复用 tools.loader.DynamicToolLoader 的自动发现机制，
        扫描 src/tools/builtin/ 目录，按工具名匹配 evaluator_id。
        配置文件中写什么 evaluator_id，就自动找对应的工具，
        无需在此处硬编码映射。

        Args:
            evaluator_id: 评估器 ID（对应工具 name，如 file_read、bash_execute）

        Returns:
            可调用的 handler，或 None
        """
        handler = _DynamicToolResolver.resolve(evaluator_id)
        if handler is not None:
            return handler
        return _EvaluatorComponentResolver.resolve(evaluator_id)

    async def _evaluate_agent(
        self,
        metric_def: MetricDefinition,
        params: dict[str, Any],
        task_id: str = "",
    ) -> dict[str, Any]:
        """Agent 型评估器 — 创建子管道运行 evaluator_agent。

        通过 EngineRegistry.register_pipeline 创建独立的 PipelineEngine（I1：
        引擎必须经注册表创建），加载 evaluator_agent 配置运行评估管道。
        管道中的 task_reminder 插件（评估者模式）会自动在 Agent 未输出正确
        格式时发送提醒。

        当 agent_registry 不可用时，fallback 到 Mock。

        Args:
            metric_def: 指标定义
            params: 合并后的输入参数

        Returns:
            评估输出字典
        """
        evaluator_id = metric_def.evaluator_id

        if self._agent_registry is None:
            raise RuntimeError(
                f"Agent evaluation requires agent_registry but it is None. "
                f"Metric: {metric_def.id}, evaluator: {evaluator_id}"
            )

        agent_config = self._agent_registry.get(evaluator_id)
        if agent_config is None:
            for cfg in self._agent_registry.list_all():
                cfg_name = getattr(cfg, "name", None) or getattr(cfg, "display_name", None)
                if cfg_name == evaluator_id:
                    agent_config = cfg
                    break
        if agent_config is None:
            raise RuntimeError(
                f"Agent '{evaluator_id}' not found in registry "
                f"(tried config_id and name). "
                f"Available agents: {[getattr(c, 'config_id', '?') for c in self._agent_registry.list_all()]}"
            )

        logger.info(
            "Agent evaluation: %s (evaluator_id=%s) — launching sub-pipeline",
            metric_def.id,
            evaluator_id,
        )

        eval_prompt = self._build_agent_eval_prompt(metric_def, params)

        # 评估子管道走标准 send 路径（I1~I5 不变量）：
        # 外部只 register + send + 经注册表读公开状态，不持有 engine 引用、
        # 不调 engine.run。evaluator_agent 的 plugin_configs（如
        # task_reminder.evaluation_mode）由 agent YAML 的 plugins.enabled 配置，
        # to_state() 自动注入 state，不再运行时硬编码。
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415
        from pipeline.registry import get_engine_registry  # noqa: PLC0415

        _provider = get_service_provider()
        _registry = get_engine_registry()

        # workspace：让 evaluator 的 file_read 工具读到任务产出（相对路径基于
        # state["workspace"]，见 WorkspaceAwareMixin）。写进 tags，
        # _start_idle_engine 会兜底注入 engine.run。
        workspace = _resolve_eval_project_root(task_id, params) or ""

        _entry = _registry.register_pipeline(
            tags={
                "agent_id": evaluator_id,
                "source": "evaluation",
                "workspace": workspace,
            },
            input_route_table=_provider.get("input_route_table"),
            output_route_table=_provider.get("output_route_table"),
            plugin_registry=_provider.get("plugin_registry"),
            services=_provider.get_all_services(),
        )
        if _entry is None:
            raise RuntimeError("评估管道注册失败（路由表/插件注册表不可用）")
        pipeline_id = _entry.engine.pipeline_id

        # 评估管道创建后立即注册到根任务子目录（与 task_worker 主管道对称）
        EvaluationEngine._pre_register_eval_pipeline(pipeline_id, task_id)

        _sink = CollectingSink(pipeline_id=pipeline_id)

        try:
            from pipeline.message_bus import send_pipeline_message  # noqa: PLC0415
            from pipeline.message_types import (  # noqa: PLC0415
                MessageSource,
                MessageType,
                PipelineMessage,
            )

            msg = PipelineMessage(
                type=MessageType.CHAT,
                content=eval_prompt,
                source=MessageSource.SYSTEM,
                pipeline_id=pipeline_id,
                metadata={"source": MessageSource.SYSTEM.value},
            )
            inject_result = await send_pipeline_message(
                msg,
                agent_config=agent_config,
                output_sink=_sink,
                workspace=workspace,
                task_id=f"__eval__{metric_def.id}",
            )
            if not inject_result.success:
                raise RuntimeError(f"评估消息注入失败: {inject_result.error or inject_result.method}")

            # 阻塞等待 evaluator_agent 流式结束。evaluator_agent.timeout_seconds
            # （evaluator YAML，由 stop_check 插件执行）是真实超时安全阀；
            # 此处取一个略大于它的上限作为 await 兜底，防止 stop_check 失效时
            # 评估挂死整个 evaluate() 调用。
            _await_timeout = (float(getattr(agent_config, "timeout_seconds", 0) or 0) + 60) or None
            output_text, sink_error = await _sink.result(timeout=_await_timeout)

            logger.info(
                "Agent evaluation raw output: metric=%s, pipeline=%s, output_text len=%d first200=%s",
                metric_def.id,
                pipeline_id,
                len(output_text or ""),
                (output_text or "")[:200],
            )

            if sink_error:
                return {
                    "passed": False,
                    "score": 0.0,
                    "feedback": f"评估管道流式错误: {sink_error}",
                    "pipeline_run_id": pipeline_id,
                }

            eval_result = self._parse_evaluation_result(output_text or "")
            if eval_result is not None:
                logger.info(
                    "Agent evaluation completed: %s -> passed=%s, score=%s",
                    metric_def.id,
                    eval_result.get("passed"),
                    eval_result.get("score"),
                )
                eval_result["pipeline_run_id"] = pipeline_id
                return eval_result

            # JSON 未解析出来：从注册表 entry.engine.last_state（公开 property）
            # 读终止原因，给出细分失败反馈。经注册表访问，不穿透私有成员。
            _state = self._read_eval_state(pipeline_id)
            stop_reason = _state.get("router.stop_reason", "")
            max_reminders = _state.get("evaluate_reminder_count", 0)
            if "timeout" in stop_reason:
                return {
                    "passed": False,
                    "score": 0.0,
                    "feedback": f"评估管道超时（指标: {metric_def.id}）: {stop_reason}",
                    "pipeline_run_id": pipeline_id,
                }
            if max_reminders > 0:
                return {
                    "passed": False,
                    "score": 0.0,
                    "feedback": (f"evaluator_agent 经 {max_reminders} 次提醒后仍未输出有效评估结论"),
                    "pipeline_run_id": pipeline_id,
                }
            return {
                "passed": False,
                "score": 0.0,
                "feedback": ("evaluator_agent 未能输出有效的 evaluation_result JSON"),
                "pipeline_run_id": pipeline_id,
            }

        except Exception as e:
            logger.error(
                "Agent evaluation pipeline failed for %s: %s",
                metric_def.id,
                e,
            )
            return {
                "passed": False,
                "score": 0.0,
                "feedback": f"评估管道执行异常: {e}",
                "pipeline_run_id": pipeline_id,
            }
        finally:
            # 一次性评估：无论成功失败都终结管道（cancel engine_task + 停 bridge
            # + engine.cleanup + unregister），避免 entry 在 registry 堆积。
            from pipeline.message_bus import stop as _stop_pipeline  # noqa: PLC0415

            try:
                await _stop_pipeline(pipeline_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Eval pipeline stop failed (non-critical): %s",
                    exc,
                )

    @staticmethod
    def _read_eval_state(pipeline_id: str) -> dict[str, Any]:
        """从注册表读评估管道的 last_state（公开 property）。

        经 get_engine_registry().get(pipeline_id).engine.last_state 访问，
        不穿透私有成员。entry 或 engine 不存在时返回空 dict。
        """
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            entry = get_engine_registry().get(pipeline_id)
            if entry is None or entry.engine is None:
                return {}
            return getattr(entry.engine, "last_state", None) or {}
        except Exception:
            return {}

    @staticmethod
    def _parse_evaluation_result(text: str) -> dict[str, Any] | None:
        """从 evaluator_agent 的输出文本中提取 evaluation_result JSON。

        支持多种格式：
        - 嵌套：{"evaluation_result": {"passed": true, "score": 85, ...}}
        - 直接：{"passed": true, "score": 85, ...}
        - Markdown code block 包裹的 JSON

        使用括号配对计数提取 JSON 块，支持嵌套结构。

        Args:
            text: evaluator_agent 的输出文本

        Returns:
            解析后的评估结果字典，解析失败返回 None
        """
        import json  # noqa: PLC0415
        import re  # noqa: PLC0415

        def _extract_json_blocks(s: str) -> list[str]:
            """通过括号配对计数从文本中提取所有顶层 JSON 对象"""
            blocks = []
            i = 0
            while i < len(s):
                if s[i] == "{":
                    depth = 0
                    start = i
                    in_string = False
                    escape_next = False
                    while i < len(s):
                        ch = s[i]
                        if escape_next:
                            escape_next = False
                        elif ch == "\\" and in_string:
                            escape_next = True
                        elif ch == '"' and not escape_next:
                            in_string = not in_string
                        elif not in_string:
                            if ch == "{":
                                depth += 1
                            elif ch == "}":
                                depth -= 1
                                if depth == 0:
                                    blocks.append(s[start : i + 1])
                                    break
                        i += 1
                i += 1
            return blocks

        code_block_pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
        for match in code_block_pattern.finditer(text):
            candidate = match.group(1).strip()
            if candidate.startswith("{"):
                try:
                    parsed = json.loads(candidate)
                    result = EvaluationEngine._extract_eval_from_parsed(parsed)
                    if result is not None:
                        return result
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

        for block in _extract_json_blocks(text):
            try:
                parsed = json.loads(block)
                result = EvaluationEngine._extract_eval_from_parsed(parsed)
                if result is not None:
                    return result
            except (json.JSONDecodeError, ValueError, TypeError):
                continue

        return None

    @staticmethod
    def _extract_eval_from_parsed(parsed: dict[str, Any]) -> dict[str, Any] | None:
        """从已解析的 JSON 对象中提取评估结果

        支持嵌套格式（evaluation_result 键）和直接格式（passed 键在顶层）。

        Args:
            parsed: 已解析的 JSON 对象

        Returns:
            标准化的评估结果字典，不符合格式返回 None
        """
        if not isinstance(parsed, dict):
            return None

        if "evaluation_result" in parsed and isinstance(parsed["evaluation_result"], dict):
            inner = parsed["evaluation_result"]
            if "passed" in inner:
                return {
                    "passed": bool(inner["passed"]),
                    "score": float(inner.get("score", 0)),
                    "feedback": str(inner.get("feedback", "")),
                    "issues": inner.get("issues", []),
                    "suggestions": inner.get("suggestions", []),
                    "report_path": inner.get("report_path", ""),
                }

        if "passed" in parsed:
            return {
                "passed": bool(parsed["passed"]),
                "score": float(parsed.get("score", 0)),
                "feedback": str(parsed.get("feedback", "")),
                "issues": parsed.get("issues", []),
                "suggestions": parsed.get("suggestions", []),
                "report_path": parsed.get("report_path", ""),
            }

        return None

    @staticmethod
    def _build_agent_eval_prompt(
        metric_def: MetricDefinition,
        params: dict[str, Any],
    ) -> str:
        """构建发给 evaluator_agent 的评估指令。

        Args:
            metric_def: 指标定义
            params: 合并后的输入参数

        Returns:
            评估指令文本
        """
        parts = [
            "请执行以下评估任务：",
            "",
            f"## 评估指标：{metric_def.name or metric_def.id}",
        ]

        if metric_def.description:
            parts.append(f"## 指标描述：{metric_def.description}")

        criteria = params.get("criteria", "")
        if criteria:
            parts.append(f"## 评估标准：{criteria}")

        content = params.get("content", "")
        if content:
            parts.append(f"## 待评估内容：\n{content}")

        summary = params.get("summary", "")
        if summary:
            parts.append(f"## 任务执行摘要：{summary}")

        parts.append("")
        parts.append(
            "请根据以上信息进行评估验证，并在完成后输出评估结论 JSON：\n"
            "```json\n"
            '{"evaluation_result": {\n'
            '  "passed": true/false,\n'
            '  "score": 0-100,\n'
            '  "feedback": "简要总结评估结论",\n'
            '  "issues": ["文件:行号 — 具体问题描述", ...],\n'
            '  "suggestions": ["具体修复建议", ...],\n'
            '  "report_path": "评估报告文件的相对路径"\n'
            "}}\n"
            "```\n"
            "\n要求：\n"
            "- issues: 逐条列出每个不通过项，包含文件路径和行号\n"
            "- suggestions: 针对每个 issue 给出可操作的修复建议\n"
            "- report_path: 将详细评估报告写入文件（如 "
            f"eval_report_{metric_def.id}.md），填入相对路径\n"
            "- 如果评估通过，issues 和 suggestions 为空数组即可"
        )

        return "\n".join(parts)


def _resolve_template(value: str, context: dict[str, Any]) -> str:
    """解析 {{ a.b.c }} 风格的简单模板占位符（返回字符串）。"""
    import re  # noqa: PLC0415

    def _replacer(match: re.Match) -> str:
        expr = match.group(1).strip()
        parts = expr.split("|")
        path = parts[0].strip()
        default_val = ""
        if len(parts) > 1:
            default_expr = parts[1].strip()
            if default_expr.startswith("default("):
                default_val = default_expr[8:].rstrip(")").strip().strip("'\"")

        current: Any = context
        for key in path.split("."):
            if isinstance(current, dict):
                current = current.get(key)
            else:
                current = None
                break

        if current is None:
            return default_val
        return str(current)

    return re.sub(r"\{\{\s*(.+?)\s*\}\}", _replacer, value)


def _resolve_template_typed(
    value: str,
    context: dict[str, Any],
) -> Any:
    """解析模板，如果整个值是单个 {{ expr }}，保留原始类型（int/float/bool）。

    避免数字字段（如 timeout_seconds）被转为字符串。
    """
    import re  # noqa: PLC0415

    stripped = value.strip()
    m = re.fullmatch(r"\{\{\s*(.+?)\s*\}\}", stripped)
    if m:
        expr = m.group(1).strip()
        parts = expr.split("|")
        path = parts[0].strip()
        current: Any = context
        for key in path.split("."):
            if isinstance(current, dict):
                current = current.get(key)
            else:
                current = None
                break
        if current is not None:
            return current
        # 回退到 default
        if len(parts) > 1:
            default_expr = parts[1].strip()
            if default_expr.startswith("default("):
                raw = default_expr[8:].rstrip(")").strip().strip("'\"")
                try:
                    return int(raw)
                except ValueError:
                    try:
                        return float(raw)
                    except ValueError:
                        return raw
        return value

    # 混合模板（含文本和占位符），返回字符串
    return _resolve_template(value, context)


def _resolve_eval_project_root(
    task_id: str,
    params: dict[str, Any],
) -> str | None:
    """Resolve the project root for evaluator agent pipelines."""
    workspace = params.get("workspace")
    if workspace:
        from pathlib import Path  # noqa: PLC0415

        p = Path(workspace)
        if p.is_absolute() and p.exists():
            return str(p)
        abs_p = Path.cwd() / workspace
        if abs_p.exists():
            return str(abs_p)

    if not task_id:
        return None

    try:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()
        ts = provider.get_or_create(
            "task_service",
            lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService(),
        )
        if ts is None:
            return None
        task = ts.get_task(task_id)
        if task and task.metadata:
            ws = task.metadata.get("workspace")
            if ws:
                from pathlib import Path  # noqa: PLC0415

                abs_ws = Path.cwd() / ws
                if abs_ws.exists():
                    return str(abs_ws)
    except Exception:
        pass

    return None


class _DynamicToolResolver:
    """通过 DynamicToolLoader 动态发现内置工具的 handler。

    复用 tools.loader.DynamicToolLoader 的自动发现机制，
    扫描 src/tools/builtin/ 目录中所有 BuiltinTool 子类，
    按 get_tool_definition().name 匹配 evaluator_id。
    """

    _cache: dict[str, Any | None] = {}

    @classmethod
    def resolve(cls, evaluator_id: str) -> Any | None:
        if evaluator_id in cls._cache:
            return cls._cache[evaluator_id]

        handler = cls._do_resolve(evaluator_id)
        cls._cache[evaluator_id] = handler
        return handler

    @classmethod
    def _do_resolve(cls, evaluator_id: str) -> Any | None:
        try:
            import importlib  # noqa: PLC0415
            import inspect  # noqa: PLC0415

            from tools.loader import get_dynamic_tool_loader, init_dynamic_tool_loader  # noqa: PLC0415
            from tools.registry import ToolRegistry  # noqa: PLC0415

            loader = get_dynamic_tool_loader()
            if loader is None:
                registry = ToolRegistry()
                loader = init_dynamic_tool_loader(registry)

            if not loader._discovered:
                loader._discover_tools()

            entry = loader._tool_classes.get(evaluator_id)
            if entry is None:
                return None

            module_path, class_name = entry
            mod = importlib.import_module(module_path)
            tool_cls = getattr(mod, class_name)

            sig = inspect.signature(tool_cls.__init__)
            required_params = [
                p for p in sig.parameters.values() if p.name != "self" and p.default is inspect.Parameter.empty
            ]
            if required_params:
                logger.debug(
                    "Evaluator '%s' requires injection params %s, skipped",
                    evaluator_id,
                    [p.name for p in required_params],
                )
                return None

            inst = tool_cls()
            return inst.execute
        except Exception as e:
            logger.debug(
                "DynamicToolResolver failed for '%s': %s",
                evaluator_id,
                e,
            )
            return None


class _EvaluatorComponentResolver:
    """扫描评估专用组件目录，按命名约定匹配 evaluator_id。

    处理 tools/builtin/evaluators/ 等非标准工具目录中的评估组件。
    命名约定：evaluator_id → {evaluator_id}.py 中同名类（SnakeCase → PascalCase）。
    """

    _EVALUATOR_DIRS: list[str] = [
        "tools.builtin.evaluators",
    ]

    _cache: dict[str, Any | None] = {}

    @classmethod
    def resolve(cls, evaluator_id: str) -> Any | None:
        if evaluator_id in cls._cache:
            return cls._cache[evaluator_id]

        handler = cls._do_resolve(evaluator_id)
        cls._cache[evaluator_id] = handler
        return handler

    @classmethod
    def _do_resolve(cls, evaluator_id: str) -> Any | None:
        try:
            import importlib  # noqa: PLC0415

            class_name = "".join(word.capitalize() for word in evaluator_id.split("_"))

            for pkg in cls._EVALUATOR_DIRS:
                module_path = f"{pkg}.{evaluator_id}"
                try:
                    mod = importlib.import_module(module_path)
                except ImportError:
                    continue

                candidate = getattr(mod, class_name, None)
                if candidate is None:
                    continue

                if not (isinstance(candidate, type) and hasattr(candidate, "execute")):
                    continue

                inst = candidate()
                return inst.execute

        except Exception as e:
            logger.debug(
                "EvaluatorComponentResolver failed for '%s': %s",
                evaluator_id,
                e,
            )
        return None

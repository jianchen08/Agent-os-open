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
import os
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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


@contextmanager
def _chdir(path: str | Path) -> Iterator[None]:
    """临时切换进程工作目录。

    评估器子管道运行期间将 cwd 切到任务 workspace，使评估 Agent 用相对路径
    即可读到任务产出（评估器空间 = 任务空间）。3.10 无 contextlib.chdir，此处自实现。
    """
    previous = Path.cwd()
    target = Path(path).resolve()
    if target == previous:
        yield
        return
    os.chdir(target)
    try:
        yield
    finally:
        os.chdir(previous)


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
        pipeline_factory: Callable[[], Any] | None = None,
        agent_registry: Any | None = None,
        main_loop: Any | None = None,
    ) -> None:
        """初始化评估引擎。

        Args:
            loader: 指标加载器（必须已加载指标）
            expect_evaluator: 期望值评估器，None 时创建默认实例
            tool_registry: 工具注册表，None 时工具型评估器 fallback 到 Mock
            pipeline_factory: 创建 PipelineEngine 实例的可调用对象
            agent_registry: AgentRegistry 实例，用于获取 evaluator_agent 配置
            main_loop: 主事件循环引用，human_interaction 等需要与主循环
                       交互的工具通过 run_coroutine_threadsafe 回调
        """
        self._loader = loader
        self._expect_evaluator = expect_evaluator or ExpectEvaluator()
        self._tool_registry = tool_registry
        self._pipeline_factory = pipeline_factory
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
            metrics_to_run = [
                self._loader.get(mid)
                for mid in config.metric_ids
                if self._loader.get(mid) is not None
            ]
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

        metrics_to_run = sorted(
            metrics_to_run, key=lambda m: _TYPE_PRIORITY.get(m.metric_type, 99)
        )

        type_order = [m.metric_type.value for m in metrics_to_run]
        logger.info(
            "Evaluation order for task %s: %s (sorted by type priority)",
            task_id, type_order,
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
                        else _resolve_template(item, context) if isinstance(item, str) else item
                    )
                    for item in value
                ]
            elif isinstance(value, dict):
                resolved[key] = {
                    _resolve_template(k, context): _resolve_template(v, context)
                    for k, v in value.items()
                }
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
                metric_def, merged_params, task_id,
            )

            # 使用期望评估器判定
            result = self._expect_evaluator.evaluate(
                metric_id=metric_def.id,
                expect=metric_def.expect,
                output=output,
            )

            logger.info(
                "Expect evaluation: %s -> passed=%s, score=%s, "
                "message=%s",
                metric_def.id, result.passed, result.score,
                result.message[:100] if result.message else "",
            )
            if not result.passed and result.details:
                failed = result.details.get("failed_conditions", [])
                if failed:
                    logger.info(
                        "Failed conditions for %s: %s",
                        metric_def.id, failed,
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
            logger.error(
                "Evaluation failed for metric %s: %s", metric_def.id, e
            )
            return MetricResult(
                metric_id=metric_def.id,
                passed=False,
                message=f"评估执行异常: {e}",
                error=str(e),
                evaluator_input=input_params,
            )

    @staticmethod
    def _pre_register_eval_pipeline(
        pipeline_id: str, task_id: str,
    ) -> None:
        """在评估子管道运行前立即注册到根任务子目录。

        与 task_worker.py 对主管道的 early binding 逻辑对称，
        确保评估管道记录从一开始就写入正确的子目录，
        避免后续 _register_eval_pipelines 因异常静默失败导致记录留在扁平位置。
        """
        if not task_id or not pipeline_id:
            return
        try:
            from infrastructure.service_provider import get_service_provider
            provider = get_service_provider()
            exec_storage = provider.get("execution_record_storage")
            if not exec_storage:
                return
            from tasks.service import TaskService
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
                    pipeline_id, root_id,
                )
        except Exception as exc:
            logger.debug(
                "Eval pipeline pre-registration skipped (non-critical): %s", exc,
            )

    # ── 默认评估器实现（Mock） ────────────────────────────

    async def _evaluate_tool(
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
            metric_def.id, evaluator_id,
        )

        handler = None
        if self._tool_registry is not None and evaluator_id:
            handler = self._tool_registry.get_handler(evaluator_id)

        if handler is None and evaluator_id:
            handler = self._get_builtin_evaluator_handler(evaluator_id)

        if handler is not None:
            try:
                import asyncio

                if evaluator_id == "human_interaction":
                    params["pipeline_id"] = f"__eval__{task_id or 'unknown'}"

                _is_human_interaction = (evaluator_id == "human_interaction")
                try:
                    _running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    _running_loop = None

                logger.info(
                    "[EvalEngine] _evaluate_tool | metric=%s | evaluator=%s | pipeline_id=%s | params_keys=%s | running_loop=%s | main_loop=%s",
                    metric_def.id, evaluator_id, params.get("pipeline_id"), list(params.keys()),
                    id(_running_loop) if _running_loop else None, id(self._main_loop) if self._main_loop else None,
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
                        id(_running_loop), id(self._main_loop),
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

                actual_status = result_dict.get("data", result_dict).get(
                    "status", result_dict.get("status")
                )
                actual_exit = result_dict.get("data", result_dict).get("exit_code")
                logger.info(
                    "Tool evaluation completed: %s -> success=%s, "
                    "cmd_status=%s, exit_code=%s",
                    metric_def.id, result_dict.get("success"),
                    actual_status, actual_exit,
                )
                return result_dict

            except Exception as e:
                logger.error(
                    "Tool execution failed for %s (evaluator_id=%s): %s",
                    metric_def.id, evaluator_id, e,
                )
                return {
                    "success": False,
                    "error": str(e),
                }

        raise RuntimeError(
            f"Tool '{evaluator_id}' not found in registry. "
            f"Metric: {metric_def.id}"
        )

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

        通过 pipeline_factory 创建独立的 PipelineEngine，加载 evaluator_agent
        配置，运行评估管道。管道中的 task_reminder 插件（评估者模式）会自动
        在 Agent 未输出正确格式时发送提醒。

        当 pipeline_factory 或 agent_registry 不可用时，fallback 到 Mock。

        Args:
            metric_def: 指标定义
            params: 合并后的输入参数

        Returns:
            评估输出字典
        """
        evaluator_id = metric_def.evaluator_id

        if self._pipeline_factory is None:
            raise RuntimeError(
                f"Agent evaluation requires pipeline_factory but it is None. "
                f"Metric: {metric_def.id}, evaluator: {evaluator_id}"
            )
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
            metric_def.id, evaluator_id,
        )

        eval_prompt = self._build_agent_eval_prompt(metric_def, params)

        # 在 pipeline 运行前捕获 engine ID，确保异常时也能注册管道记录
        _captured_pid: list[str] = [""]

        try:

            async def _run_eval_pipeline() -> dict[str, Any]:
                engine = self._pipeline_factory()
                _captured_pid[0] = engine.pipeline_id
                from pathlib import Path
                project_root = _resolve_eval_project_root(
                    task_id, params,
                ) or str(
                    Path(__file__).resolve().parent.parent.parent
                )
                _plugin_configs = {
                    "task_reminder": {"evaluation_mode": True},
                }

                # 评估管道创建后立即注册到根任务子目录
                # 确保即使管道运行异常，记录文件也会分组（与 task_worker 对主管道的做法一致）
                EvaluationEngine._pre_register_eval_pipeline(
                    engine.pipeline_id, task_id,
                )

                # 评估器空间与任务空间保持一致：子管道运行期间 cwd 切到任务 workspace，
                # 评估 Agent 用相对路径即可读到任务产出文件（如 docs/xxx_report.md）。
                # 之前 cwd 停留在进程主目录，worktree/shared 模式下相对路径读不到产出。
                with _chdir(project_root):
                    state = await engine.run(
                        user_input=eval_prompt,
                        agent_config=agent_config,
                        task_id=f"__eval__{metric_def.id}",
                        project_root=project_root,
                        workspace=project_root,
                        plugin_configs=_plugin_configs,
                    )
                return state

            pipeline_state = await _run_eval_pipeline()

            # 提取子管道 ID
            _pipeline_run_id = pipeline_state.get(
                "pipeline_id", ""
            )

            raw_output = pipeline_state.get("raw_result", "")
            final_output = pipeline_state.get(
                "final_output", raw_output
            )
            output_text = str(final_output) if final_output else ""

            logger.info(
                "Agent evaluation raw output: metric=%s, pipeline=%s, "
                "raw_result type=%s len=%d, final_output type=%s len=%d, "
                "output_text len=%d first200=%s",
                metric_def.id, _pipeline_run_id,
                type(raw_output).__name__, len(str(raw_output)),
                type(final_output).__name__, len(str(final_output)) if final_output else 0,
                len(output_text), output_text[:200],
            )

            eval_result = self._parse_evaluation_result(output_text)
            if eval_result is not None:
                logger.info(
                    "Agent evaluation completed: %s -> passed=%s, score=%s",
                    metric_def.id,
                    eval_result.get("passed"),
                    eval_result.get("score"),
                )
                eval_result["pipeline_run_id"] = _pipeline_run_id
                return eval_result

            stop_reason = pipeline_state.get(
                "router.stop_reason", ""
            )
            max_reminders = pipeline_state.get(
                "evaluate_reminder_count", 0
            )
            if "timeout" in stop_reason:
                return {
                    "passed": False,
                    "score": 0.0,
                    "feedback": f"评估管道超时（指标: {metric_def.id}）: {stop_reason}",
                    "pipeline_run_id": _pipeline_run_id,
                }
            if max_reminders > 0:
                return {
                    "passed": False,
                    "score": 0.0,
                    "feedback": (
                        f"evaluator_agent 经 {max_reminders}"
                        " 次提醒后仍未输出有效评估结论"
                    ),
                    "pipeline_run_id": _pipeline_run_id,
                }
            return {
                "passed": False,
                "score": 0.0,
                "feedback": (
                    "evaluator_agent 未能输出有效的"
                    " evaluation_result JSON"
                ),
                "pipeline_run_id": _pipeline_run_id,
            }

        except Exception as e:
            logger.error(
                "Agent evaluation pipeline failed for %s: %s",
                metric_def.id, e,
            )
            return {
                "passed": False,
                "score": 0.0,
                "feedback": f"评估管道执行异常: {e}",
                "pipeline_run_id": _captured_pid[0] or None,
            }

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
        import json
        import re

        def _extract_json_blocks(s: str) -> list[str]:
            """通过括号配对计数从文本中提取所有顶层 JSON 对象"""
            blocks = []
            i = 0
            while i < len(s):
                if s[i] == '{':
                    depth = 0
                    start = i
                    in_string = False
                    escape_next = False
                    while i < len(s):
                        ch = s[i]
                        if escape_next:
                            escape_next = False
                        elif ch == '\\' and in_string:
                            escape_next = True
                        elif ch == '"' and not escape_next:
                            in_string = not in_string
                        elif not in_string:
                            if ch == '{':
                                depth += 1
                            elif ch == '}':
                                depth -= 1
                                if depth == 0:
                                    blocks.append(s[start:i + 1])
                                    break
                        i += 1
                i += 1
            return blocks

        code_block_pattern = re.compile(r'```(?:json)?\s*\n?(.*?)\n?\s*```', re.DOTALL)
        for match in code_block_pattern.finditer(text):
            candidate = match.group(1).strip()
            if candidate.startswith('{'):
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
            '```json\n'
            '{"evaluation_result": {\n'
            '  "passed": true/false,\n'
            '  "score": 0-100,\n'
            '  "feedback": "简要总结评估结论",\n'
            '  "issues": ["文件:行号 — 具体问题描述", ...],\n'
            '  "suggestions": ["具体修复建议", ...],\n'
            '  "report_path": "评估报告文件的相对路径"\n'
            '}}\n'
            '```\n'
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
    import re

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
    import re

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
        from pathlib import Path
        p = Path(workspace)
        if p.is_absolute() and p.exists():
            return str(p)
        abs_p = Path.cwd() / workspace
        if abs_p.exists():
            return str(abs_p)

    if not task_id:
        return None

    try:
        from infrastructure.service_provider import get_service_provider
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
                from pathlib import Path
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
            import importlib
            import inspect

            from tools.loader import get_dynamic_tool_loader, init_dynamic_tool_loader
            from tools.registry import ToolRegistry

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
                p for p in sig.parameters.values()
                if p.name != "self" and p.default is inspect.Parameter.empty
            ]
            if required_params:
                logger.debug(
                    "Evaluator '%s' requires injection params %s, skipped",
                    evaluator_id, [p.name for p in required_params],
                )
                return None

            inst = tool_cls()
            return inst.execute
        except Exception as e:
            logger.debug(
                "DynamicToolResolver failed for '%s': %s", evaluator_id, e,
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
            import importlib

            class_name = "".join(
                word.capitalize() for word in evaluator_id.split("_")
            )

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
                evaluator_id, e,
            )
        return None

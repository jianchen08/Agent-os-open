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
from typing import Any, Callable

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

# 评估器函数签名：接收指标定义和输入参数，返回输出字典
EvaluatorFunc = Callable[[MetricDefinition, dict[str, Any]], dict[str, Any]]


class EvaluationEngine:
    """统一评估引擎。

    根据指标类型分发评估到对应评估器，并使用期望评估器判定结果。

    用法：
        loader = MetricLoader()
        loader.load_all()
        engine = EvaluationEngine(loader=loader)
        result = engine.evaluate(
            task_id="abc123",
            config=EvaluationConfig(metric_ids=["code_check"]),
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
    ) -> None:
        """初始化评估引擎。

        Args:
            loader: 指标加载器（必须已加载指标）
            expect_evaluator: 期望值评估器，None 时创建默认实例
            tool_registry: 工具注册表，None 时工具型评估器 fallback 到 Mock
            pipeline_factory: 创建 PipelineEngine 实例的可调用对象
            agent_registry: AgentRegistry 实例，用于获取 evaluator_agent 配置
        """
        self._loader = loader
        self._expect_evaluator = expect_evaluator or ExpectEvaluator()
        self._tool_registry = tool_registry
        self._pipeline_factory = pipeline_factory
        self._agent_registry = agent_registry
        self._evaluators: dict[MetricType, EvaluatorFunc] = {
            MetricType.TOOL: self._evaluate_tool,
            MetricType.AGENT: self._evaluate_agent,
            MetricType.HUMAN: self._evaluate_human,
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

    def evaluate(
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

        # 确定要评估的指标列表
        if config.metric_ids:
            metrics_to_run = [
                self._loader.get(mid)
                for mid in config.metric_ids
                if self._loader.get(mid) is not None
            ]
        else:
            metrics_to_run = list(self._loader.metrics.values())

        if not metrics_to_run:
            logger.warning("No metrics to evaluate for task %s", task_id)
            return EvaluationResult(
                task_id=task_id,
                overall_passed=False,
                summary="无可评估指标",
            )

        # 逐指标评估
        results: list[MetricResult] = []
        for metric_def in metrics_to_run:
            result = self._evaluate_metric(
                metric_def=metric_def,
                input_params=config.input_params.get(metric_def.id, {}),
            )
            results.append(result)

            if config.fail_fast and not result.passed:
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

    def evaluate_with_metrics(
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
        if not metrics:
            logger.warning("No metrics to evaluate for task %s", task_id)
            return EvaluationResult(
                task_id=task_id,
                overall_passed=False,
                summary="无可评估指标",
            )

        input_params = input_params or {}
        results: list[MetricResult] = []

        for metric_def in metrics:
            # 合并全局输入参数和指标默认配置
            merged_params = {**metric_def.default_config, **input_params}
            result = self._evaluate_metric(
                metric_def=metric_def,
                input_params=merged_params,
            )
            results.append(result)

        eval_result = EvaluationResult(
            task_id=task_id,
            results=results,
        )
        eval_result.compute_overall()
        return eval_result

    def evaluate_single(
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

        return self._evaluate_metric(
            metric_def=metric_def,
            input_params=input_params or {},
        )

    def _evaluate_metric(
        self,
        metric_def: MetricDefinition,
        input_params: dict[str, Any],
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
            # 合并默认配置和输入参数
            merged_params = {**metric_def.default_config, **input_params}

            # 调用评估器获取输出
            output = evaluator(metric_def, merged_params)

            # 使用期望评估器判定
            result = self._expect_evaluator.evaluate(
                metric_id=metric_def.id,
                expect=metric_def.expect,
                output=output,
            )

            # 尝试提取 score（agent/human 类型可能返回 score）
            if "score" in output:
                result.score = float(output["score"])
            elif "passed" in output and isinstance(output["passed"], bool):
                result.score = 100.0 if output["passed"] else 0.0

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
            )

    # ── 默认评估器实现（Mock） ────────────────────────────

    def _evaluate_tool(
        self,
        metric_def: MetricDefinition,
        params: dict[str, Any],
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

                tool_result = handler(params)
                if asyncio.iscoroutine(tool_result):
                    import concurrent.futures

                    def _run_async(coro):
                        return asyncio.run(coro)

                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        tool_result = pool.submit(
                            _run_async, tool_result
                        ).result()

                if hasattr(tool_result, "to_dict"):
                    result_dict = tool_result.to_dict()
                elif isinstance(tool_result, dict):
                    result_dict = tool_result
                else:
                    result_dict = {"success": True, "data": tool_result}

                if "success" not in result_dict:
                    status = result_dict.get("status", "completed")
                    result_dict["success"] = status == "completed"

                logger.info(
                    "Tool evaluation completed: %s -> success=%s",
                    metric_def.id, result_dict.get("success"),
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
        """获取内置评估器的 handler。

        当 tool_registry 中找不到评估器时，尝试直接从
        evaluators 子模块中实例化。

        Args:
            evaluator_id: 评估器 ID（如 schema_evaluator）

        Returns:
            可调用的 handler，或 None
        """
        try:
            if evaluator_id == "schema_evaluator":
                from tools.builtin.evaluators.schema_evaluator import SchemaEvaluator
                inst = SchemaEvaluator()
                return inst.execute
            elif evaluator_id == "resource_evaluator":
                from tools.builtin.evaluators.resource_evaluator import ResourceEvaluator
                inst = ResourceEvaluator()
                return inst.execute
        except Exception as e:
            logger.debug("Failed to load builtin evaluator %s: %s", evaluator_id, e)
        return None

    def _evaluate_agent(
        self,
        metric_def: MetricDefinition,
        params: dict[str, Any],
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

        try:
            import asyncio
            import json
            import re

            async def _run_eval_pipeline() -> dict[str, Any]:
                engine = self._pipeline_factory()
                state = await engine.run(
                    user_input=eval_prompt,
                    agent_config=agent_config,
                    task_id=f"__eval__{metric_def.id}",
                )
                return state

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    pipeline_state = pool.submit(
                        lambda: asyncio.run(_run_eval_pipeline())
                    ).result()
            else:
                pipeline_state = asyncio.run(_run_eval_pipeline())

            raw_output = pipeline_state.get("raw_result", "")
            final_output = pipeline_state.get("final_output", raw_output)
            output_text = str(final_output) if final_output else ""

            eval_result = self._parse_evaluation_result(output_text)
            if eval_result is not None:
                logger.info(
                    "Agent evaluation completed: %s -> passed=%s, score=%s",
                    metric_def.id, eval_result.get("passed"), eval_result.get("score"),
                )
                return eval_result

            stop_reason = pipeline_state.get("router.stop_reason", "")
            max_reminders = pipeline_state.get("evaluate_reminder_count", 0)
            if "timeout" in stop_reason:
                return {
                    "passed": False,
                    "score": 0.0,
                    "feedback": f"评估管道超时: {stop_reason}",
                }
            if max_reminders > 0:
                return {
                    "passed": False,
                    "score": 0.0,
                    "feedback": f"evaluator_agent 经 {max_reminders} 次提醒后仍未输出有效评估结论",
                }
            return {
                "passed": False,
                "score": 0.0,
                "feedback": "evaluator_agent 未能输出有效的 evaluation_result JSON",
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
                    "suggestions": inner.get("suggestions", []),
                }

        if "passed" in parsed:
            return {
                "passed": bool(parsed["passed"]),
                "score": float(parsed.get("score", 0)),
                "feedback": str(parsed.get("feedback", "")),
                "suggestions": parsed.get("suggestions", []),
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
            '{"evaluation_result": {"passed": true/false, "score": 0-100, "feedback": "评估说明..."}}\n'
            '```'
        )

        return "\n".join(parts)

    def _evaluate_human(
        self,
        metric_def: MetricDefinition,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """人工审核型评估器（未实现）。

        TODO: 通过 human_interaction 工具等待人工审核。
        当前为占位实现，返回 passed=True 避免阻塞流程。
        上层调用方应检查 evaluator_type != human 再使用。

        Args:
            metric_def: 指标定义
            params: 合并后的输入参数

        Returns:
            占位评估输出
        """
        logger.warning(
            "Human evaluation NOT IMPLEMENTED: %s (evaluator_id=%s) "
            "— returning placeholder passed result",
            metric_def.id, metric_def.evaluator_id,
        )
        return {
            "passed": True,
            "score": 100.0,
            "feedback": "人工审核尚未实现，返回占位结果",
        }

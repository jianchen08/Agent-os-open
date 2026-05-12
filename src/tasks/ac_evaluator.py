"""
验收标准评估器

提供程序化评估验收标准的核心逻辑：
- 支持三种评估器类型：tool/workflow/human
- 并行评估多个 AC
- 超时控制和重试机制
- 详细的评估日志

核心设计原则：
- 评估器是独立执行的单元
- 支持异步并行评估
- 统一的评估结果格式

重构说明：
- 使用 UnifiedEvaluationEngine 作为底层评估引擎
- 使用统一的类型定义（src.evaluation.types）
- 保留向后兼容的公共 API
- 简化内部实现，移除冗余代码
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundException, ValidationException
from src.core.results import EvaluationStatus
from src.db.models import Task
from src.evaluation import ContextBuilder, UnifiedEvaluationEngine
from src.evaluation.types import EvaluatorType
from src.services.tool_service import ToolService
from src.services.workflow_service import WorkflowService
from src.tools.executor import ToolExecutor
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class EvaluationContext:
    """评估上下文"""

    task_id: str
    task_goal: dict[str, Any]
    criteria: dict[str, Any]
    evidence: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_artifacts(self) -> list[str]:
        """获取产物列表"""
        return self.evidence.get("artifacts", [])

    def get_output(self) -> str:
        """获取执行输出"""
        return self.evidence.get("output", "")

    def get_description(self) -> str:
        """获取完成说明"""
        return self.evidence.get("description", "")


@dataclass
class EvaluationOutput:
    """评估输出"""

    passed: bool
    score: float
    feedback: str
    details: dict[str, Any] = field(default_factory=dict)
    execution_time: float = 0.0
    evaluator_type: str = ""
    evaluator_id: str = ""


class ACEvaluator:
    """
    验收标准评估器

    核心职责：
    1. 根据评估器类型执行相应的评估逻辑
    2. 支持三种评估器：tool/workflow/human
    3. 按优先级顺序评估：tool → workflow → human
    4. 并行评估多个 AC
    5. 超时控制和错误处理
    6. 详细的评估日志

    评估器优先级设计：
    - tool (优先级 1): 最快，零 Token，100% 确定
    - workflow (优先级 2): 复杂，多步骤
    - human (优先级 3): 最慢，需要人工介入

    重构后架构：
    - 使用 UnifiedEvaluationEngine 处理底层评估逻辑
    - 保留原有公共 API 以确保向后兼容
    """

    # 默认配置
    DEFAULT_TIMEOUT = 300  # 默认超时时间（秒）
    MAX_PARALLEL_EVALUATIONS = 5  # 最大并行评估数
    MAX_RETRY_ATTEMPTS = 3  # 最大重试次数

    # 评估器类型优先级（数值越小优先级越高）
    EVALUATOR_PRIORITY: dict[str, int] = {
        EvaluatorType.TOOL.value: 1,  # 工具评估器优先
        EvaluatorType.WORKFLOW.value: 2,  # 工作流评估器次之（包括语义评估）
        EvaluatorType.HUMAN.value: 3,  # 人工评估器最后
    }

    def __init__(
        self,
        session: AsyncSession,
        workflow_executor: Callable | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        """
        初始化验收标准评估器

        Args:
            session: 数据库会话
            workflow_executor: 工作流执行回调函数
            tool_registry: 工具注册表（可选，默认创建新实例）
        """
        self.session = session
        self.workflow_executor = workflow_executor

        # 初始化工具执行器
        if tool_registry is None:
            tool_registry = ToolRegistry()
        self.tool_executor = ToolExecutor(tool_registry)

        # 初始化服务
        self.tool_service = ToolService()
        self.workflow_service = WorkflowService(session)

        # 初始化统一评估引擎
        self.evaluation_engine = UnifiedEvaluationEngine(
            session=session,
            tool_registry=tool_registry,
            workflow_executor=workflow_executor,
            default_timeout=self.DEFAULT_TIMEOUT,
            max_concurrent=self.MAX_PARALLEL_EVALUATIONS,
        )

    # ========================================================================
    # 单个评估
    # ========================================================================

    async def evaluate_single(
        self,
        ac_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """
        评估单个验收标准

        Args:
            ac_id: 验收标准 ID
            task_id: 任务 ID

        Returns:
            评估结果

        Raises:
            NotFoundException: 任务或验收标准不存在
            ValidationException: 评估器配置无效
        """
        # 获取任务
        task = await self._get_task(task_id)
        if not task:
            raise NotFoundException(
                message=f"任务不存在: {task_id}",
                resource_type="Task",
                resource_id=task_id,
                code="TASK_001",
            )

        # 查找目标 AC
        acceptance_criteria = task.acceptance_criteria or []
        target_ac = None
        target_index = -1

        for i, ac in enumerate(acceptance_criteria):
            if ac.get("id") == ac_id:
                target_ac = ac
                target_index = i
                break

        if not target_ac:
            raise NotFoundException(
                message=f"验收标准不存在: {ac_id}",
                resource_type="AcceptanceCriteria",
                resource_id=ac_id,
                code="TASK_002",
            )

        # 检查是否已通过
        if target_ac.get("status") == "passed":
            return {
                "task_id": task_id,
                "ac_id": ac_id,
                "status": "already_passed",
                "message": "该验收标准已通过",
            }

        # 获取评估器
        evaluator = self.get_evaluator(target_ac)
        if not evaluator:
            raise ValidationException(
                message=f"无效的评估器配置: {target_ac.get('evaluator_type')}",
                field="evaluator_type",
                details={"evaluator_type": target_ac.get("evaluator_type")},
            )

        # 构建评估上下文
        context = EvaluationContext(
            task_id=task_id,
            task_goal=task.goal or {},
            criteria=target_ac,
            evidence=target_ac.get("last_evidence", {}),
            metadata={
                "task_title": task.title,
                "task_description": task.description,
            },
        )

        # 执行评估
        logger.info(f"开始评估 AC {ac_id}，评估器类型: {evaluator['type']}")
        start_time = datetime.now()

        try:
            result = await self._run_evaluator(
                evaluator_type=evaluator["type"],
                evaluator_id=evaluator["id"],
                context=context,
                timeout=target_ac.get("timeout", self.DEFAULT_TIMEOUT),
            )

            execution_time = (datetime.now() - start_time).total_seconds()
            evaluated_at = datetime.now().isoformat()

            # 准备 AC 状态更新（由调用方更新数据库）
            ac_update = {
                "index": target_index,
                "evaluated_at": evaluated_at,
                "status": "passed" if result.passed else "failed",
                "evaluation_result": {
                    "passed": result.passed,
                    "score": result.score,
                    "feedback": result.feedback,
                    "details": result.details,
                    "execution_time": execution_time,
                    "evaluator_type": result.evaluator_type,
                    "evaluator_id": result.evaluator_id,
                },
            }

            if result.passed:
                ac_update["passed_at"] = evaluated_at
            else:
                ac_update["retry_count"] = target_ac.get("retry_count", 0) + 1

            logger.info(
                f"AC {ac_id} 评估完成: {'通过' if result.passed else '失败'} "
                f"(分数: {result.score}, 耗时: {execution_time:.2f}s)"
            )

            return {
                "success": True,
                "task_id": task_id,
                "ac_id": ac_id,
                "ac_update": ac_update,
                "passed": result.passed,
                "score": result.score,
                "feedback": result.feedback,
                "details": result.details,
                "execution_time": execution_time,
                "evaluated_at": evaluated_at,
            }

        except TimeoutError:
            logger.error(f"AC {ac_id} 评估超时")
            evaluated_at = datetime.now().isoformat()

            # 准备 AC 状态更新（超时）
            ac_update = {
                "index": target_index,
                "evaluated_at": evaluated_at,
                "status": "timeout",
                "evaluation_result": {
                    "passed": False,
                    "score": 0,
                    "feedback": "评估超时",
                    "error": "timeout",
                },
            }

            return {
                "success": False,
                "task_id": task_id,
                "ac_id": ac_id,
                "ac_update": ac_update,
                "error": "评估超时",
                "error_code": "EVALUATION_TIMEOUT",
            }

        except Exception as e:
            logger.exception(f"AC {ac_id} 评估失败: {e}")
            evaluated_at = datetime.now().isoformat()

            # 准备 AC 状态更新（异常）
            ac_update = {
                "index": target_index,
                "evaluated_at": evaluated_at,
                "status": "error",
                "evaluation_result": {
                    "passed": False,
                    "score": 0,
                    "feedback": f"评估异常: {str(e)}",
                    "error": str(e),
                },
            }

            return {
                "success": False,
                "task_id": task_id,
                "ac_id": ac_id,
                "ac_update": ac_update,
                "error": f"评估异常: {str(e)}",
                "error_code": "EVALUATION_ERROR",
            }

    # ========================================================================
    # 依赖关系处理（新增）
    # ========================================================================

    def _sort_metrics_by_level(self, metrics: list[Any]) -> list[Any]:
        """
        按 level 从低到高排序评估指标

        Args:
            metrics: 评估指标列表

        Returns:
            排序后的指标列表
        """
        return sorted(metrics, key=lambda m: getattr(m, "level", 1))

    def _check_prerequisites(
        self, metric: Any, completed_checks: list[dict[str, Any]]
    ) -> bool:
        """
        检查前置依赖是否满足

        如果 metric.includes 中的任何指标失败，返回 False

        Args:
            metric: 当前评估指标
            completed_checks: 已完成的检查列表

        Returns:
            前置依赖是否满足
        """
        includes = getattr(metric, "includes", None)
        if not includes:
            return True

        for included_metric in includes:
            # 查找已执行的检查
            check = next(
                (c for c in completed_checks if c.get("metric") == included_metric),
                None,
            )
            if check and not check.get("passed", False):
                return False

        return True

    # ========================================================================
    # 批量评估（基于 EvaluationMetric）
    # ========================================================================

    async def evaluate_metrics(
        self,
        task_id: str,
        metric_ids: list[str],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """
        评估任务的评估指标（支持依赖关系）

        改进：支持依赖关系
        1. 按 level 从低到高排序
        2. 逐层执行
        3. 前置失败自动跳过后续

        Args:
            task_id: 任务 ID
            metric_ids: 评估指标 ID 列表
            evidence: 评估证据（summary, artifacts, output）

        Returns:
            评估结果汇总

        Raises:
            NotFoundException: 任务不存在或未找到有效的评估指标
        """
        from sqlalchemy import select

        from src.db.models import EvaluationMetric

        # 获取任务
        task = await self._get_task(task_id)
        if not task:
            raise NotFoundException(
                message=f"任务不存在: {task_id}",
                resource_type="Task",
                resource_id=task_id,
                code="TASK_001",
            )

        if not metric_ids:
            return {
                "task_id": task_id,
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "all_passed": True,
                "results": [],
            }

        # 获取评估指标定义
        result = await self.session.execute(
            select(EvaluationMetric).where(EvaluationMetric.id.in_(metric_ids))
        )
        metrics = list(result.scalars().all())

        if not metrics:
            raise NotFoundException(
                message="未找到有效的评估指标",
                resource_type="EvaluationMetric",
                resource_id=",".join(metric_ids),
                code="TASK_003",
            )

        # 按 level 从低到高排序
        sorted_metrics = self._sort_metrics_by_level(metrics)

        logger.info(
            f"开始评估任务 {task_id} 的 {len(sorted_metrics)} 个指标 "
            f"(层级顺序: {[m.level for m in sorted_metrics]})"
        )

        start_time = datetime.now()
        results = []
        completed_checks = []
        passed_count = 0
        failed_count = 0
        skipped_count = 0

        # 逐个执行评估
        for metric in sorted_metrics:
            # 检查前置依赖
            if not self._check_prerequisites(metric, completed_checks):
                # 前置依赖失败，跳过此指标
                skipped_result = {
                    "metric_id": metric.id,
                    "metric_name": metric.name,
                    "level": metric.level,
                    "passed": False,
                    "skipped": True,
                    "feedback": f"跳过评估：前置依赖 {metric.includes} 未通过",
                    "evaluated_at": datetime.now().isoformat(),
                }
                results.append(skipped_result)
                completed_checks.append(
                    {
                        "metric": metric.name,
                        "passed": False,
                        "skipped": True,
                    }
                )
                skipped_count += 1
                logger.info(
                    f"跳过指标 {metric.name} (level={metric.level})：前置依赖未通过"
                )
                continue

            # 构建评估上下文
            context = EvaluationContext(
                task_id=task_id,
                task_goal=task.goal or {},
                criteria={
                    "id": metric.id,
                    "name": metric.name,
                    "description": metric.description,
                    "evaluator_type": metric.evaluator_type,
                    "evaluator_id": metric.evaluator_id,
                    "input_params": metric.default_config or {},
                },
                evidence=evidence,
                metadata={
                    "task_title": task.title,
                    "task_description": task.description,
                },
            )

            # 执行评估
            logger.info(
                f"评估指标 {metric.name} (level={metric.level}, "
                f"type={metric.evaluator_type})"
            )

            try:
                eval_result = await self._run_evaluator(
                    evaluator_type=metric.evaluator_type,
                    evaluator_id=metric.evaluator_id,
                    context=context,
                    timeout=300,  # 默认 5 分钟超时
                )

                result_dict = {
                    "metric_id": metric.id,
                    "metric_name": metric.name,
                    "level": metric.level,
                    "passed": eval_result.passed,
                    "score": eval_result.score,
                    "feedback": eval_result.feedback,
                    "details": eval_result.details,
                    "execution_time": eval_result.execution_time,
                    "evaluator_type": eval_result.evaluator_type,
                    "evaluator_id": eval_result.evaluator_id,
                    "evaluated_at": datetime.now().isoformat(),
                }

                results.append(result_dict)
                completed_checks.append(
                    {
                        "metric": metric.name,
                        "passed": eval_result.passed,
                        "skipped": False,
                    }
                )

                if eval_result.passed:
                    passed_count += 1
                    logger.info(f"指标 {metric.name} 通过 (分数: {eval_result.score})")
                else:
                    failed_count += 1
                    logger.warning(f"指标 {metric.name} 失败: {eval_result.feedback}")

            except TimeoutError:
                logger.error(f"指标 {metric.name} 评估超时")
                result_dict = {
                    "metric_id": metric.id,
                    "metric_name": metric.name,
                    "level": metric.level,
                    "passed": False,
                    "score": 0,
                    "feedback": "评估超时",
                    "error": "timeout",
                    "evaluated_at": datetime.now().isoformat(),
                }
                results.append(result_dict)
                completed_checks.append(
                    {
                        "metric": metric.name,
                        "passed": False,
                        "skipped": False,
                    }
                )
                failed_count += 1

            except Exception as e:
                logger.exception(f"指标 {metric.name} 评估异常: {e}")
                result_dict = {
                    "metric_id": metric.id,
                    "metric_name": metric.name,
                    "level": metric.level,
                    "passed": False,
                    "score": 0,
                    "feedback": f"评估异常: {str(e)}",
                    "error": str(e),
                    "evaluated_at": datetime.now().isoformat(),
                }
                results.append(result_dict)
                completed_checks.append(
                    {
                        "metric": metric.name,
                        "passed": False,
                        "skipped": False,
                    }
                )
                failed_count += 1

        execution_time = (datetime.now() - start_time).total_seconds()
        all_passed = failed_count == 0 and skipped_count == 0

        logger.info(
            f"任务 {task_id} 评估完成: {passed_count} 通过, {failed_count} 失败, "
            f"{skipped_count} 跳过 (耗时: {execution_time:.2f}s)"
        )

        return {
            "task_id": task_id,
            "total": len(sorted_metrics),
            "passed": passed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "all_passed": all_passed,
            "execution_time": execution_time,
            "evaluated_at": datetime.now().isoformat(),
            "results": results,
        }

    # ========================================================================
    # 批量评估（旧版，基于 acceptance_criteria）
    # ========================================================================

    async def evaluate_all(
        self,
        task_id: str,
        parallel: bool = True,
    ) -> dict[str, Any]:
        """
        评估任务的所有 AC

        改进：支持依赖关系
        1. 按 level 从低到高排序
        2. 逐层执行
        3. 前置失败自动跳过后续

        Args:
            task_id: 任务 ID
            parallel: 是否并行评估

        Returns:
            评估结果汇总

        Raises:
            NotFoundException: 任务不存在
        """
        # 获取任务
        task = await self._get_task(task_id)
        if not task:
            raise NotFoundException(
                message=f"任务不存在: {task_id}",
                resource_type="Task",
                resource_id=task_id,
                code="TASK_001",
            )

        acceptance_criteria = task.acceptance_criteria or []

        if not acceptance_criteria:
            return {
                "task_id": task_id,
                "total": 0,
                "passed": 0,
                "failed": 0,
                "all_passed": True,
                "results": [],
            }

        # 筛选待评估的 AC
        pending_acs = [
            ac
            for ac in acceptance_criteria
            if ac.get("status") not in ("passed", "evaluating")
        ]

        # 按评估器类型优先级排序：tool → workflow → human
        # 这样可以优先执行快速廉价的自动化检查，实现快速失败
        def get_evaluator_priority(ac: dict[str, Any]) -> int:
            """获取 AC 的评估器优先级"""
            # 优先使用显式配置的 evaluator_type
            evaluator_type = ac.get("evaluator_type")
            if evaluator_type:
                return self.EVALUATOR_PRIORITY.get(evaluator_type, 99)

            # 兼容旧配置：从 validation_rule 推断
            validation_rule = ac.get("validation_rule", {})
            if validation_rule.get("tool_id"):
                return self.EVALUATOR_PRIORITY.get(EvaluatorType.TOOL.value, 99)

            # 默认低优先级
            return 99

        pending_acs = sorted(pending_acs, key=get_evaluator_priority)

        # 记录排序后的评估器类型分布
        evaluator_types = [ac.get("evaluator_type", "unknown") for ac in pending_acs]
        logger.debug(
            f"任务 {task_id} 评估顺序: {evaluator_types} "
            f"(优先级: {', '.join(f'{t}={self.EVALUATOR_PRIORITY.get(t, 99)}' for t in set(evaluator_types))})"
        )

        if not pending_acs:
            # 所有 AC 已评估
            passed_count = sum(
                1 for ac in acceptance_criteria if ac.get("status") == "passed"
            )
            return {
                "task_id": task_id,
                "total": len(acceptance_criteria),
                "passed": passed_count,
                "failed": len(acceptance_criteria) - passed_count,
                "all_passed": passed_count == len(acceptance_criteria),
                "results": acceptance_criteria,
            }

        logger.info(
            f"开始评估任务 {task_id} 的 {len(pending_acs)} 个 AC (并行: {parallel})"
        )

        start_time = datetime.now()

        if parallel and len(pending_acs) > 1:
            # 并行评估
            results = await self._parallel_evaluate(task_id, pending_acs)
        else:
            # 串行评估
            results = await self._serial_evaluate(task_id, pending_acs)

        execution_time = (datetime.now() - start_time).total_seconds()

        # 统计结果
        total_passed = sum(1 for r in results if r.get("passed", False))
        total_failed = len(results) - total_passed
        all_passed = total_passed == len(pending_acs)

        logger.info(
            f"任务 {task_id} 评估完成: {total_passed}/{len(pending_acs)} 通过 "
            f"(耗时: {execution_time:.2f}s)"
        )

        return {
            "task_id": task_id,
            "total": len(pending_acs),
            "passed": total_passed,
            "failed": total_failed,
            "all_passed": all_passed,
            "execution_time": execution_time,
            "evaluated_at": datetime.now().isoformat(),
            "results": results,
        }

    # ========================================================================
    # 评估器获取
    # ========================================================================

    def get_evaluator(self, ac: dict[str, Any]) -> dict[str, str] | None:
        """
        根据 AC 配置获取评估器

        Args:
            ac: 验收标准配置

        Returns:
            评估器配置，格式: {"type": "...", "id": "..."}
        """
        # 检查是否配置了评估器
        evaluator_type = ac.get("evaluator_type")
        evaluator_id = ac.get("evaluator_id")

        if not evaluator_type:
            # 默认使用工具评估器
            validation_rule = ac.get("validation_rule", {})
            if validation_rule:
                evaluator_type = EvaluatorType.TOOL.value
                evaluator_id = validation_rule.get("tool_id", "builtin_evaluator")
            else:
                # 使用默认的语义评估
                return None

        if not evaluator_id:
            logger.warning(f"AC {ac.get('id')} 缺少 evaluator_id")
            return None

        return {
            "type": evaluator_type,
            "id": evaluator_id,
        }

    # ========================================================================
    # 内部方法
    # ========================================================================

    async def _run_evaluator(
        self,
        evaluator_type: str,
        evaluator_id: str,
        context: EvaluationContext,
        timeout: float,
    ) -> EvaluationOutput:
        """
        使用统一评估引擎执行评估

        Args:
            evaluator_type: 评估器类型
            evaluator_id: 评估器 ID
            context: 评估上下文
            timeout: 超时时间

        Returns:
            评估输出
        """
        # 构建 metric_config
        metric_config = {
            "evaluator_type": evaluator_type,
            "evaluator_id": evaluator_id,
            "timeout": timeout,
        }

        # 使用 ContextBuilder 构建上下文
        eval_context = ContextBuilder().build(
            task_goal=context.task_goal,
            criteria=context.criteria,
            evidence=context.evidence,
            metadata=context.metadata,
        )

        # 调用统一引擎
        result = await self.evaluation_engine.evaluate(
            metric_config=metric_config,
            context=eval_context,
            timeout=timeout,
        )

        # 转换为 EvaluationOutput 格式（保持向后兼容）
        return EvaluationOutput(
            passed=result.passed,
            score=result.score,
            feedback=result.message,
            details=result.details,
            execution_time=(result.execution_time_ms or 0) / 1000,  # 毫秒转秒
            evaluator_type=evaluator_type,
            evaluator_id=evaluator_id,
        )

    async def _parallel_evaluate(
        self,
        task_id: str,
        pending_acs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """并行评估多个 AC"""
        semaphore = asyncio.Semaphore(self.MAX_PARALLEL_EVALUATIONS)

        async def _evaluate_with_limit(ac: dict[str, Any]):
            async with semaphore:
                return await self.evaluate_single(ac["id"], task_id)

        tasks = [_evaluate_with_limit(ac) for ac in pending_acs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 处理异常结果
        processed_results = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                processed_results.append(
                    {
                        "ac_id": pending_acs[i]["id"],
                        "error": str(r),
                        "passed": False,
                    }
                )
            else:
                processed_results.append(r)

        return processed_results

    async def _serial_evaluate(
        self,
        task_id: str,
        pending_acs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """串行评估多个 AC"""
        results = []
        for ac in pending_acs:
            result = await self.evaluate_single(ac["id"], task_id)
            results.append(result)
        return results

    async def _get_task(self, task_id: str) -> Task | None:
        """获取任务"""
        result = await self.session.execute(select(Task).where(Task.id == task_id))
        return result.scalar_one_or_none()

"""任务评估工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TaskEvaluateTool：任务评估工具类

0.2 迁移（FP-MIGR / F-MIGR-2）：
- 顶层类型走 agentos_plugin_sdk（BuiltinTool / Tool / ToolCategory / ToolLevel /
  ToolSource / ToolExecutionResult / create_failure_result / create_success_result）；
- 评估类型面（EvaluationResult / MetricResult / sanitize_eval_paths /
  EvaluationExecutor）就地重建于同目录 _eval_core.py；
- 任务领域类型（TaskStatus）以 plugins/shared/system/tasks/ 平铺模块为权威
  （server.py 注入 sys.path）；
- 0.1 infrastructure.service_provider 已废弃 → 模块级 _get_service_provider()
  shim（get 返回 None，调用方均有降级守卫）；
- 评估执行器由外部注入（构造参数 executor=...，duck-typing）：未注入时评估
  路径返回 EVAL_ENGINE_UNAVAILABLE，不静默空转。
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

import state_fields
import worktree_merge
from _eval_core import sanitize_eval_paths
from task_types import TaskModel, TaskStatus

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


# ── GAP-1 统一：state 聚合读取器 + 任务状态写面（server.py on_load 注入）──
# 读：``() -> list[dict]``（sync 或 async，管道 state 聚合行，行为扁平点号键
# 如 {"pipeline_id": ..., "task.status": ..., "task.goal": ...}）。None = 未注入
# （读面回退 task_service YAML 只读镜像）。
# 写：``(pipeline_id, fields) -> None``（async，经 pipeline-state.update 落
# state 单一真值——任务状态由任务域插件裁决写入，内核不回写 task.status）。
# None = 未注入（写面降级：仅 YAML 镜像）。
_state_reader: Any = None
_state_writer: Any = None


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（server.py on_load 经 pipeline-state capability）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def _get_state_reader() -> Any:
    """获取 state 聚合读取器（None = 未注入，测试可 monkeypatch）。"""
    return _state_reader


def set_state_writer(writer: Any) -> None:
    """注入任务状态写面（server.py on_load 经 pipeline-state.update capability）。"""
    global _state_writer  # noqa: PLW0603
    _state_writer = writer


def _get_state_writer() -> Any:
    """获取任务状态写面（None = 未注入，写面降级仅 YAML 镜像）。"""
    return _state_writer


# ── 默认评估执行器（server.py on_load 装配）──
# PipelineEvaluationExecutor（_executor.py）：tool 型本地跑，agent 型派评估
# 子管道继承任务工作区。构造参数 ``executor=...`` 显式注入优先（测试），缺省
# 回退本注入点；两处皆无 → EVAL_ENGINE_UNAVAILABLE（文档化降级，不静默空转）。
_default_executor: Any = None


def set_default_executor(executor: Any) -> None:
    """注入默认评估执行器（server.py on_load 装配 _executor.PipelineEvaluationExecutor）。"""
    global _default_executor  # noqa: PLW0603
    _default_executor = executor


def _now_iso() -> str:
    """当前 UTC 时间 ISO 串（task.ended_at 落 state 用，与内核 chrono 同格式）。"""
    return datetime.now(UTC).isoformat()


async def _write_task_state(task_id: str, fields: dict[str, Any]) -> None:
    """把任务域字段写入管道 state 单一真值（GAP-1：task.id == pipeline_id）。

    任务状态由任务域插件裁决写入（pipeline-state update capability），内核
    不再回写 task.status。写面未注入（None）时静默降级——YAML 镜像仍由
    task_service 维护，state 保持出生值 pending。
    """
    writer = _get_state_writer()
    if writer is None:
        logger.debug(
            "[TaskEvaluate] state 写面未注入，跳过 state 写入 | task=%s fields=%s",
            task_id,
            sorted(fields),
        )
        return
    try:
        await writer(task_id, fields)
    except Exception as exc:  # noqa: BLE001 — 写面失败不阻断评估主流程
        logger.warning(
            "[TaskEvaluate] state 写入失败（评估结果仍有效）| task=%s fields=%s err=%s",
            task_id,
            sorted(fields),
            exc,
        )

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_EVAL_TIMEOUT = 1200.0
_DEFAULT_MAX_EVAL_CALLS = 15
# ws_meta 读取失败类合并门控的重试上限（耗尽判死，与评估重试同构）
_MERGE_GATE_MAX_FAILURES = 3

_VALID_EVALUATE_STATUSES = {TaskStatus.RUNNING, TaskStatus.EVALUATING}
_VALID_AUTO_COMPLETE_STATUSES = {TaskStatus.RUNNING, TaskStatus.EVALUATING}


class _ServiceProviderShim:
    """0.2 服务提供者适配：get(key) 返回 0.2 等价或 None（文档化降级）。

    0.1 的 infrastructure.service_provider 已废弃（src/ 已归档）。0.2 sidecar
    无 agent_registry / tool_registry / execution_record_storage 等价单例 →
    统一返回 None，调用方均有降级守卫（如 _register_eval_pipelines 跳过管道
    注册）。worktree 合并门控已不在此列：0.2 改经 worktree_merge 共享模块
    本地执行（数据源 state ws_meta），无跨进程服务获取。
    """

    def get(self, key: str) -> Any:
        return None


def _get_service_provider() -> Any:
    """0.2 服务提供者获取（FP-MIGR：替代已废弃的 infrastructure.service_provider）。"""
    return _ServiceProviderShim()


async def task_evaluate_func(inputs: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0911
    """同步任务评估函数（供测试和简单场景使用）。

    Args:
        inputs: 包含 action 和 task_id 的字典

    Returns:
        评估结果字典
    """
    from task_types import TaskStatus  # noqa: PLC0415

    action = inputs.get("action")
    task_id = inputs.get("task_id")

    if not action:
        return {"success": False, "error_code": "MISSING_ACTION", "error": "缺少 action 参数"}

    if not task_id:
        return {"success": False, "error_code": "MISSING_TASK_ID", "error": "缺少 task_id 参数"}

    if action not in ("evaluate_single", "auto_complete"):
        return {"success": False, "error_code": "INVALID_ACTION", "error": f"不支持的操作: {action}"}

    try:
        from service_access import get_task_service  # noqa: PLC0415

        task_service = get_task_service()
        if task_service is None:
            return {"success": False, "error_code": "SERVICE_UNAVAILABLE", "error": "TaskService 不可用"}
    except Exception:
        return {"success": False, "error_code": "SERVICE_UNAVAILABLE", "error": "TaskService 不可用"}

    task = task_service.get_task(task_id)
    if task is None:
        return {"success": False, "error_code": "TASK_NOT_FOUND", "error": "任务不存在"}

    valid_statuses = _VALID_EVALUATE_STATUSES if action == "evaluate_single" else _VALID_AUTO_COMPLETE_STATUSES

    if task.status == TaskStatus.RUNNING:
        with contextlib.suppress(Exception):
            await task_service.move_to_evaluating(task_id)
            await _write_task_state(task_id, {"task.status": "evaluating"})

    if task.status not in valid_statuses and task.status != TaskStatus.RUNNING:
        return {"success": False, "error_code": "INVALID_STATUS", "error": f"不支持的状态: {task.status}"}

    try:
        if inputs.get("result") is not None:
            task.result = inputs["result"]

        await task_service.complete_evaluation(task_id, passed=True)
        await _write_task_state(
            task_id,
            {"task.status": "completed", "task.ended_at": _now_iso()},
        )
        return {"success": True, "status": "completed"}
    except Exception as e:
        return {"success": False, "error_code": "EVAL_FAILED", "error": str(e)}


class TaskEvaluateTool(BuiltinTool):
    """任务评估工具。

    负责：
    1. 解析输入参数
    2. 调用 EvaluationExecutor 执行评估
    3. 根据评估结果处理三种情况：
       - 全部通过 → 更新状态 COMPLETED + 通知提交者
       - 失败但次数未耗尽 → 返回评估结果，Agent 继续工作
       - 失败且次数耗尽 → 更新状态 FAILED + 通知提交者
    """

    def __init__(self, executor: Any = None, **kwargs: Any) -> None:
        """初始化任务评估工具。

        Args:
            executor: 评估执行器实例（0.2 注入版，duck-typing，实现
                ``async run_evaluation(...) -> EvaluationResult``）。None 表示
                未注入 → 评估路径返回 EVAL_ENGINE_UNAVAILABLE（不静默空转）。
        """
        super().__init__()
        self._executor = executor

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="task_evaluate",
            description=(
                "任务评估工具：用于评估任务的验收指标是否满足，评估通过则自动完成任务。"
                "\n\n两种评估模式（均可完成任务）："
                "\n1. evaluate_single（单指标评估）：逐个评估指标，每次只评估一个 metric_id。"
                "当所有指标都通过后，任务自动完成。适合需要分步验证或针对性修复的场景。"
                "\n2. auto_complete（完全评估）：一次性评估所有指标。"
                "已通过的指标会自动跳过，只评估未通过的。适合首次评估或最终验证。"
                "\n\n【重要】调用前提：你必须已完成任务要求的全部工作步骤和产出物。"
                "如果你还有未完成的步骤、未输出的产出物、或未处理的待办事项，禁止调用此工具——先完成它们。"
                "无验收标准的任务会自动通过；指标重试超过上限后任务会被标记为失败。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["evaluate_single", "auto_complete"],
                        "description": (
                            "评估模式："
                            "evaluate_single-评估单个指标(需提供metric_id)，"
                            "所有指标逐一通过后任务自动完成；"
                            "auto_complete-评估所有未通过的指标(已通过的自动跳过)，默认"
                        ),
                        "default": "auto_complete",
                    },
                    "metric_id": {
                        "type": "string",
                        "description": "评估指标ID，仅在evaluate_single模式时必填",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "任务完成摘要（推荐填写）。内容应包含："
                            "1) 完成了什么工作（简要说明实现思路和做了哪些改动）；"
                            "2) 产出了什么（文件、配置、数据等产物）；"
                            "3) 产物的存放路径（相对路径，如 src/auth/login.py、config/rules.yaml）。"
                            "示例：'实现了用户登录功能，新增 JWT 认证模块。产出：src/auth/login.py、src/auth/jwt_handler.py、tests/test_login.py。'"
                            "评估器将依据此摘要了解任务成果并验证产物。"
                        ),
                    },
                },
                "required": [],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.SYSTEM,
            tags=["task", "evaluate", "metric", "completion"],
            injected_params=["session_id", "user_id", "tool_record_id", "task_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911
        """执行任务评估。

        通过 injected_params 获取 task_id 等运行时参数，
        通过 TaskService 获取任务数据，通过 EvaluationExecutor 执行评估。

        系统级错误（INJECTION_ERROR/SERVICE_UNAVAILABLE）直接 fail_task + 返回
        task_failed 标记，避免 LLM 无意义重试。
        """
        action = inputs.get("action", "auto_complete")
        task_id = inputs.get("task_id")

        # 短 id 入参解析：LLM 工具面 id 短化——LLM 回传的 task_id 可能是
        # 12 位短 id，经 state 聚合前缀唯一解析回全 id。
        task_id = await self._resolve_task_id(task_id)
        if isinstance(task_id, str) and task_id.startswith("AMBIGUOUS:"):
            return create_failure_result(
                error=f"任务 ID '{task_id[len('AMBIGUOUS:'):]}' 匹配到多个任务，请使用完整 ID 重试",
                error_code="AMBIGUOUS_TASK_ID",
            )

        logger.warning(
            "[TRACE-EVAL] execute() ENTRY | action=%s | task_id=%s | input_keys=%s",
            action,
            task_id,
            list(inputs.keys()),
        )

        # GAP-1 统一：读面 state 优先（task = pipeline，task.* 即任务数据）；
        # task_service 仅作状态写与回退（YAML 只读镜像，写通道落地前保留）。
        task_service = self._get_task_service()

        if not task_id:
            # 零推断：task_id 缺失即注入链断裂（state 未取到 run 的 task_id，
            # 未变成工具参数注入），候选任务再多也不猜——猜测活跃任务会把
            # A 任务的评估写到 B 任务上，且掩盖断裂本身。
            return create_failure_result(
                error="系统错误：task_id 未注入，请联系管理员",
                error_code="INJECTION_ERROR",
                metadata={"task_failed": True},
            )

        task = await self._get_task_from_state(task_id)
        if task is None and task_service is not None:
            task = task_service.get_task(task_id)
        if task is None:
            return create_failure_result(error="任务不存在", error_code="TASK_NOT_FOUND")

        max_eval_calls = _DEFAULT_MAX_EVAL_CALLS
        if task.metadata and isinstance(task.metadata, dict):
            max_eval_calls = task.metadata.get("max_eval_calls", _DEFAULT_MAX_EVAL_CALLS)
        eval_total_calls = self._increment_eval_call_count(task)
        await _write_task_state(
            str(task_id), {"task.eval_total_calls": eval_total_calls}
        )

        if eval_total_calls > max_eval_calls:
            logger.warning(
                "[TaskEvaluate] 全局评估调用次数超限 | task_id=%s | calls=%d | max=%d | 直接标记失败",
                task_id,
                eval_total_calls,
                max_eval_calls,
            )
            await self._save_task(task_service, task)
            return create_failure_result(
                error=(
                    f"评估调用次数已达上限（{eval_total_calls}/{max_eval_calls}），"
                    f"任务自动失败。请检查评估指标是否存在无法满足的条件。"
                ),
                error_code="EVAL_CALL_LIMIT_EXCEEDED",
                metadata={"task_failed": True},
            )

        if action == "evaluate_single":
            return await self._evaluate_single(inputs, task_service, task)
        if action == "auto_complete":
            return await self._auto_complete(inputs, task_service, task)
        return create_failure_result(error=f"不支持的操作: {action}", error_code="INVALID_ACTION")

    async def _resolve_task_id(self, task_id: Any) -> Any:
        """LLM 入参 task_id 短 id → 全 id（state 聚合前缀唯一解析；歧义报错）。"""
        if not isinstance(task_id, str) or not task_id:
            return task_id
        try:
            rows = await self._read_state_rows()
        except Exception:  # noqa: BLE001 — 解析降级：原样放行既有"任务不存在"路径
            return task_id
        if rows is None:
            return task_id
        try:
            from id_utils import resolve_id  # noqa: PLC0415

            resolved = await resolve_id(rows, task_id)
        except Exception:  # noqa: BLE001 — 解析失败降级：原样放行
            return task_id
        if resolved.startswith("AMBIGUOUS:"):
            return f"AMBIGUOUS:{task_id}"
        return resolved

    async def _evaluate_single(  # noqa: PLR0911
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
    ) -> ToolExecutionResult:
        """评估单个指标（增量模式）。

        逐个评估指标，每次只评估指定的 metric_id。
        评估后汇总历史记录：如果所有声明的指标都已通过，自动完成任务；
        否则返回当前结果，Agent 可继续评估其他指标或改进后重试。

        Args:
            inputs: 工具输入参数
            task_service: TaskService 实例
            task: TaskModel 实例
        """
        metric_id = inputs.get("metric_id")
        task_id = task.id

        logger.warning(
            "[TRACE-EVAL] _evaluate_single ENTRY | task=%s | metric_id=%s | metric_ids=%s | task_metadata_keys=%s",
            task_id,
            metric_id,
            self._get_metric_ids(task),
            list(task.metadata.keys()) if hasattr(task, "metadata") and task.metadata else "N/A",
        )

        if not metric_id:
            return create_failure_result(
                error="单指标评估模式需要提供 metric_id",
                error_code="METRIC_ID_REQUIRED",
            )

        metric_ids = self._get_metric_ids(task)
        if len(metric_ids) == 1:
            logger.info(
                "[TaskEvaluate] 单指标任务自动转为完全评估 | task_id=%s | metric_count=%d",
                task_id,
                len(metric_ids),
            )
            return await self._auto_complete(inputs, task_service, task)

        try:
            import litellm  # noqa: PLC0415
        except ImportError:  # 0.2 精简环境可选依赖：限速细粒度识别降级
            litellm = None  # type: ignore[assignment]

        executor = self._create_executor(task_service)
        if executor is None:
            logger.warning(
                "[TaskEvaluate] 评估执行器未注入（0.2 评估引擎不可用）| task_id=%s | metric_id=%s",
                task_id,
                metric_id,
            )
            return create_failure_result(
                error="评估执行器未注入（0.2 评估引擎不可用），无法执行评估",
                error_code="EVAL_ENGINE_UNAVAILABLE",
            )
        timeout = self._get_eval_timeout(task)

        single_params: dict[str, dict[str, Any]] = {}
        summary_from_input = inputs.get("summary", "")
        if summary_from_input:
            single_params[metric_id] = {"summary": summary_from_input}

        try:
            result = await asyncio.wait_for(
                executor.run_evaluation(
                    task_id=task_id,
                    metric_ids=[metric_id],
                    input_params=single_params,
                    skip_state_update=True,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[TaskEvaluate] 单指标评估超时 | task_id=%s | metric_id=%s | timeout=%ss",
                task_id,
                metric_id,
                timeout,
            )
            return create_failure_result(
                error=f"评估超时（{timeout}s）: 指标 {metric_id} 执行时间过长",
                error_code="EVAL_TIMEOUT",
            )
        except Exception as exc:
            if litellm is not None and isinstance(exc, litellm.RateLimitError):
                logger.warning(
                    "[TaskEvaluate] 评估期间 API 限速 | task_id=%s | metric_id=%s: %s",
                    task_id,
                    metric_id,
                    exc,
                )
                return create_failure_result(
                    error=f"评估期间 API 限速: {exc}",
                    error_code="RATE_LIMITED",
                )
            logger.exception("[TaskEvaluate] 单指标评估失败: %s", exc)
            return create_failure_result(error=f"评估失败: {exc}", error_code="EVAL_FAILED")

        # 注册评估子管道 + 追加历史记录
        self._register_eval_pipelines(task_service, task, result)
        self._append_eval_history(task, result)
        await self._save_task(task_service, task)

        # 无评估结果（指标未找到或未加载）→ 返回明确错误，不误导 Agent 重试
        if not result.results:
            return create_failure_result(
                error=f"指标 '{metric_id}' 未找到：该指标不存在于评估指标注册表中，请确认指标 ID 是否正确",
                error_code="METRIC_NOT_FOUND",
            )

        # 当前指标未通过 → 返回结果，Agent 继续改进
        if not result.overall_passed:
            return create_success_result(
                data=self._build_result_data(result),
                metadata={
                    "action": "evaluate_single",
                    "result": "retry",
                    "message": f"指标 {metric_id} 未通过，请根据反馈继续改进",
                },
            )

        # 当前指标通过，检查所有声明指标是否都已通过
        if self._all_metrics_passed(task, metric_ids):
            logger.info(
                "[TaskEvaluate] 所有指标已通过，完成任务 | task_id=%s",
                task_id,
            )
            return await self._complete_task(task_service, task, result)

        # 还有指标未评估，返回进度
        evaluated, remaining = self._get_eval_progress(task, metric_ids)
        return create_success_result(
            data=self._build_result_data(result),
            metadata={
                "action": "evaluate_single",
                "result": "partial_pass",
                "message": (
                    f"指标 {metric_id} 已通过。进度：{evaluated}/{len(metric_ids)}，剩余：{', '.join(remaining)}"
                ),
            },
        )

    async def _auto_complete(
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
    ) -> ToolExecutionResult:
        """自动完成评估（评估任务提交时声明的所有指标）。"""
        # 指标来源兜底：task.metadata（YAML 镜像通路）在 0.2 任务=管道架构下
        # 常为空（任务无 YAML 记录），指标实际在管道 state 的
        # task.acceptance_criteria——从 state 聚合行补齐（含 JSON 字符串还原）。
        await self._ensure_criteria_from_state(task)
        logger.warning(
            "[TRACE-EVAL] _auto_complete ENTRY | task=%s | metric_ids=%s",
            task.id,
            self._get_metric_ids(task),
        )
        metric_ids = self._get_metric_ids(task)
        try:
            input_params = self._get_input_params(task)
        except ValueError as exc:
            # 指标参数解析失败（如 {tool_id} 模板歧义）：显式失败，不带病评估
            logger.error(
                "[TaskEvaluate] 指标参数解析失败 | task_id=%s | %s",
                task.id,
                exc,
            )
            return create_failure_result(
                error=f"指标参数解析失败：{exc}",
                error_code="INVALID_INPUT_PARAMS",
            )

        if not metric_ids:
            logger.warning(
                "[TaskEvaluate] 任务 %s 未声明任何评估指标，跳过评估 | 直接标记完成",
                task.id,
            )
            return await self._complete_task(
                task_service,
                task,
                type(
                    "EvalResult",
                    (),
                    {
                        "task_id": task.id,
                        "overall_passed": True,
                        "summary": "未声明评估指标，自动通过",
                        "results": [],
                    },
                )(),
            )

        # 跳过已通过的指标，只评估未通过的
        already_passed, remaining_ids = self._get_eval_progress(
            task,
            metric_ids,
        )
        # 未配置 criteria 的指标直接通过：没有验收标准 = 视为满足（不做任务
        # 描述顶替——评估引擎按 input_params 检查语义判定，顶替只会伪造
        # "有标准"假象）；留 warning 让配置缺失可见。
        no_criteria_ids = [
            mid for mid in remaining_ids
            if not (input_params.get(mid, {}).get("criteria") or "").strip()
        ]
        if no_criteria_ids:
            already_passed += len(no_criteria_ids)
            remaining_ids = [mid for mid in remaining_ids if mid not in no_criteria_ids]
            logger.warning(
                "[TaskEvaluate] 指标未配置 criteria，直接通过 | task_id=%s | metric_ids=%s",
                task.id,
                sorted(no_criteria_ids),
            )
        if not remaining_ids:
            logger.info(
                "[TaskEvaluate] 所有指标已通过，直接完成任务 | task_id=%s | passed=%d/%d",
                task.id,
                already_passed,
                len(metric_ids),
            )
            # 通过来源如实标注：无 criteria 直接通过 ≠ 历史评估记录通过
            _pass_note = (
                f"（其中 {len(no_criteria_ids)} 个未配置 criteria 直接通过）"
                if no_criteria_ids
                else "（来自历史评估记录）"
            )
            return await self._complete_task(
                task_service,
                task,
                type(
                    "EvalResult",
                    (),
                    {
                        "task_id": task.id,
                        "overall_passed": True,
                        "summary": f"所有 {len(metric_ids)} 个指标均已通过{_pass_note}",
                        "results": [],
                    },
                )(),
            )

        summary_from_input = inputs.get("summary", "")
        if summary_from_input:
            for _mid, p in input_params.items():
                if not p.get("summary"):
                    p["summary"] = summary_from_input

        logger.info(
            "[TaskEvaluate] 自动评估 | task_id=%s | total=%d | already_passed=%d | to_eval=%s",
            task.id,
            len(metric_ids),
            already_passed,
            remaining_ids,
        )

        executor = self._create_executor(task_service)
        if executor is None:
            logger.warning(
                "[TaskEvaluate] 评估执行器未注入（0.2 评估引擎不可用）| task_id=%s | metrics=%s",
                task.id,
                remaining_ids,
            )
            return create_failure_result(
                error="评估执行器未注入（0.2 评估引擎不可用），无法执行评估",
                error_code="EVAL_ENGINE_UNAVAILABLE",
            )
        timeout = self._get_eval_timeout(task)
        try:
            result = await asyncio.wait_for(
                executor.run_evaluation(
                    task_id=task.id,
                    metric_ids=remaining_ids,
                    input_params=input_params,
                    skip_state_update=True,
                ),
                timeout=timeout,
            )
            return await self._handle_evaluation_result(inputs, task_service, task, result)
        except asyncio.TimeoutError:
            logger.warning(
                "[TaskEvaluate] 自动评估超时 | task_id=%s | metrics=%s | timeout=%ss",
                task.id,
                remaining_ids,
                timeout,
            )
            return create_failure_result(
                error=f"评估超时（{timeout}s）: 指标 {metric_ids} 执行时间过长",
                error_code="EVAL_TIMEOUT",
            )
        except Exception as e:
            logger.exception("[TaskEvaluate] 自动完成评估失败: %s", e)
            return create_failure_result(error=f"评估失败: {e}", error_code="EVAL_FAILED")

    async def _handle_evaluation_result(  # noqa: PLR0912,PLR0915
        self,
        inputs: dict[str, Any],
        task_service: Any,
        task: Any,
        eval_result: Any,
    ) -> ToolExecutionResult:
        """根据评估结果处理三种情况。

        - 全部通过 → COMPLETED + 通知
        - 失败但次数未耗尽 → 返回结果，Agent 继续
        - 失败且次数耗尽 → FAILED + 通知

        Args:
            inputs: 工具输入参数
            task_service: TaskService 实例
            task: TaskModel 实例
            eval_result: EvaluationResult 实例

        Returns:
            工具执行结果
        """
        max_retries = _DEFAULT_MAX_RETRIES
        if task.metadata and isinstance(task.metadata, dict):
            max_retries = task.metadata.get("max_eval_retries", _DEFAULT_MAX_RETRIES)

        retry_counts: dict[str, int] = {}
        if task.metadata and isinstance(task.metadata, dict):
            retry_counts = task.metadata.get("eval_retry_count", {})
            if not isinstance(retry_counts, dict):
                retry_counts = {}

        has_failure = False
        exhausted = False

        _UNRECOVERABLE_PATTERNS = (  # noqa: N806
            "command not found",
            "no such file or directory",
            "module not found",
            "is not recognized",
        )

        for r in eval_result.results:
            mid = r.metric_id
            if not r.passed:
                has_failure = True
                output_str = str(r.evaluator_output or "").lower()
                message_str = (r.message or "").lower()
                is_unrecoverable = any(p in output_str or p in message_str for p in _UNRECOVERABLE_PATTERNS)
                if is_unrecoverable:
                    retry_counts[mid] = max_retries
                    exhausted = True
                    continue
                current = retry_counts.get(mid, 0) + 1
                retry_counts[mid] = current
                if current >= max_retries:
                    exhausted = True
            else:
                # 渐进重试：指标通过时重置连续失败计数
                prev_count = retry_counts.get(mid, 0)
                if prev_count > 0:
                    logger.info(
                        "[TaskEvaluate] 指标 %s 通过，重置连续失败计数 %d → 0 (渐进重试)",
                        mid,
                        prev_count,
                    )
                retry_counts[mid] = 0

        if task.metadata is None:
            task.metadata = {}
        task.metadata["eval_retry_count"] = retry_counts

        # 计数落 state 单一真值（0.2 任务每次由 state 行重建 metadata，仅存
        # YAML 镜像时跨调用计数丢失 → 耗尽判定永不触发，反复评估无限循环）
        await _write_task_state(
            str(task.id),
            {
                "task.eval_retry_count": retry_counts,
                "task.eval_total_calls": task.metadata.get("eval_total_calls", 0),
            },
        )

        # 注册评估子管道到根任务子目录
        self._register_eval_pipelines(task_service, task, eval_result)

        # 追加本次评估记录到历史（保留所有评估尝试）
        self._append_eval_history(task, eval_result)

        await self._save_task(task_service, task)

        # 无评估结果（所有指标 ID 均未在评估指标注册表中找到）→ 不误导完成
        if not eval_result.results:
            return create_failure_result(
                error="未找到任何有效的评估指标，所有指定的指标 ID 均不存在于评估指标注册表中，请确认指标配置是否正确",
                error_code="METRIC_NOT_FOUND",
            )

        if not has_failure:
            return await self._complete_task(task_service, task, eval_result)
        if exhausted:
            return await self._fail_task(task_service, task, eval_result, max_retries)
        min_remaining = max_retries - min(retry_counts.values())
        eval_total = task.metadata.get("eval_total_calls", 0) if task.metadata else 0
        max_eval_calls = (
            task.metadata.get("max_eval_calls", _DEFAULT_MAX_EVAL_CALLS) if task.metadata else _DEFAULT_MAX_EVAL_CALLS
        )
        overall_remaining = max_eval_calls - eval_total
        failed_details = []
        for r in eval_result.results:
            if not r.passed:
                detail = f"- [{r.metric_id}] 未通过"
                if r.message:
                    detail += f": {r.message}"
                if r.score is not None:
                    detail += f" (得分: {r.score})"
                failed_details.append(detail)
        feedback = "评估未通过，请根据以下反馈继续改进：\n" + "\n".join(failed_details)
        feedback += f"\n\n指标连续失败剩余重试：{min_remaining} 次"
        feedback += f"\n全局评估调用剩余次数：{overall_remaining} 次（已调用 {eval_total}/{max_eval_calls}）"
        return create_success_result(
            data=self._build_result_data(eval_result),
            metadata={
                "action": inputs.get("action", "auto_complete"),
                "result": "retry",
                "retry_remaining": min_remaining,
                "message": feedback,
            },
        )

    async def _complete_task(self, task_service: Any, task: Any, eval_result: Any) -> ToolExecutionResult:
        """评估通过，完成任务。

        合并前置策略：对于 worktree 模式的任务，在标记 completed 之前先执行合并，
        合并成功才变更状态，合并失败则标记为 failed。
        非 worktree 模式直接完成。

        TaskService.on_state_change 回调会自动发送终态通知。

        Args:
            task_service: TaskService 实例
            task: TaskModel 实例
            eval_result: EvaluationResult 实例

        Returns:
            工具执行结果
        """
        if task.status == TaskStatus.COMPLETED:
            logger.info("[TaskEvaluate] 任务 %s 已完成，跳过状态回写", task.id)
        elif task.status == TaskStatus.FAILED:
            logger.warning(
                "[TaskEvaluate] 任务 %s 已失败但评估通过，尝试恢复为完成",
                task.id,
            )
            try:
                eval_data = self._build_result_data(eval_result)
                await task_service.recover_to_completed(task.id, result=eval_data)
                await _write_task_state(
                    task.id,
                    {
                        "task.status": "completed",
                        "task.ended_at": _now_iso(),
                        "task.eval_summary": self._build_rich_summary(eval_result),
                    },
                )
            except Exception as e:
                logger.error("[TaskEvaluate] 恢复失败状态为完成失败: %s", e)
        else:
            # worktree 合并门控（0.1 判定）：worktree 模式 completed 前先合并，
            # 合并成功才变更状态，失败则标记为 failed。合并是同步 git 子进程
            # 串（可阻塞数分钟），门控内部丢线程池执行，不冻主事件循环。
            # merge_error 自描述分类（ws_meta 读取失败 / worktree 合并失败），
            # 此处按原文透传，不得再套统一前缀。
            merge_error = await self._try_merge_before_complete(task)
            if merge_error:
                # ws_meta 读取失败 = 元数据镜像时序窗口（init 镜像早于引擎注册
                # 时只落 DB 表，聚合读面可能短暂缺键），可重试——计入
                # task.merge_gate_failures，达阈值才判死（耗尽语义，与评估
                # 重试同构）；真合并失败/元数据残缺仍直接判死（重试无意义）。
                is_meta_read_failure = "ws_meta 读取失败" in merge_error
                if is_meta_read_failure:
                    gate_failures = 0
                    if task.metadata and isinstance(task.metadata, dict):
                        gate_failures = int(task.metadata.get("merge_gate_failures", 0) or 0)
                    gate_failures += 1
                    if task.metadata is None:
                        task.metadata = {}
                    task.metadata["merge_gate_failures"] = gate_failures
                    try:
                        await _write_task_state(
                            str(task.id),
                            {"task.merge_gate_failures": gate_failures},
                        )
                    except Exception as e:
                        logger.error("[TaskEvaluate] merge_gate_failures 落 state 失败: %s", e)
                    if gate_failures < _MERGE_GATE_MAX_FAILURES:
                        logger.warning(
                            "[TaskEvaluate] 工作区元数据未就绪（合并门控第 %d/%d 次），返回重试不判死: task_id=%s",
                            gate_failures,
                            _MERGE_GATE_MAX_FAILURES,
                            task.id,
                        )
                        return create_failure_result(
                            error=(
                                f"工作区元数据尚未就绪（合并门控第 {gate_failures}/{_MERGE_GATE_MAX_FAILURES} 次），"
                                f"请稍后重新提交评估: {merge_error}"
                            ),
                            error_code="MERGE_GATE_RETRY",
                        )
                logger.error(
                    "[TaskEvaluate] 完成前工作区门控失败，任务标记为 failed: task_id=%s, error=%s",
                    task.id,
                    merge_error,
                )
                try:
                    eval_data = self._build_result_data(eval_result)
                    eval_data["overall_passed"] = False
                    eval_data["merge_failure"] = merge_error
                    eval_data["summary"] = f"评估指标已通过，但完成前工作区门控失败: {merge_error}"
                    await task_service.complete_evaluation(task.id, passed=False, result=eval_data)
                    await _write_task_state(
                        task.id,
                        {
                            "task.status": "failed",
                            "task.ended_at": _now_iso(),
                            "task.eval_summary": self._build_rich_summary(eval_result),
                            "task.error": merge_error,
                        },
                    )
                except Exception as e:
                    logger.error("[TaskEvaluate] complete_evaluation(passed=False) 失败: %s", e)
                return create_failure_result(
                    error=merge_error,
                    metadata={"task_failed": True},
                )
            try:
                eval_data = self._build_result_data(eval_result)
                await task_service.complete_evaluation(task.id, passed=True, result=eval_data)
                await _write_task_state(
                    task.id,
                    {
                        "task.status": "completed",
                        "task.ended_at": _now_iso(),
                        "task.eval_summary": self._build_rich_summary(eval_result),
                    },
                )
            except Exception as e:
                logger.error("[TaskEvaluate] complete_evaluation(passed=True) 失败: %s", e)
                return create_failure_result(
                    error=f"complete_evaluation(passed=True) 失败: {e}",
                    metadata={"eval_data": str(self._build_result_data(eval_result))[:200]},
                )

        return create_success_result(
            data=self._build_result_data(eval_result),
            metadata={
                "action": "auto_complete",
                "result": "completed",
                "message": "评估通过，任务已完成",
            },
        )

    async def _try_merge_before_complete(self, task: Any) -> str | None:
        """在标记 completed 之前执行 worktree 合并门控（0.1 判定，进程内本地执行）。

        ws_meta 数据源（0.2 适配）：从管道 state 聚合行解析（task.metadata 兜底）；
        合并 git 机制经 worktree_merge 共享模块原样执行（同步 git 子进程串，
        asyncio.to_thread 丢线程池——不冻主事件循环，合并锁跨线程有效）。
        0.1 的跨进程 ServiceProvider 获取不复存在，也无"服务不可用跳过"分支：
        ws_meta 读不到 = 门控失败（worktree 产物不能静默丢失）。

        Returns:
            None 表示合并成功或不需要合并（plain/shared 模式），
            str 表示门控失败原因（自描述分类：ws_meta 读取失败 / worktree
            合并失败），调用方按原文透传并据此标记任务 failed。
        """
        ws_meta = await self._read_task_ws_meta(task)
        return await asyncio.to_thread(
            worktree_merge.merge_worktree_before_complete, task.id, ws_meta
        )

    async def _read_task_ws_meta(self, task: Any) -> dict[str, Any]:
        """任务 ws_meta 数据源（0.2，三路优先级）：

        1. state 行 ``task.ws_meta``——workspace_lifecycle init 经
           pipeline-state.update 即时镜像（运行中合并门控的权威快路径）；
        2. state 行 ``ws_meta``——引擎 state_updates 合并的出口键（run 末回写
           快照后才可见）；
        3. ``task.metadata["ws_meta"]`` 兜底（YAML 镜像任务）。

        聚合行值可能是 JSON 字符串（DB 投影原样存储），经 state_fields.as_dict
        还原成 dict；三路皆无返回空 dict（由合并门控按失败裁决，不静默跳过）。
        返回空时留诊断日志（行数/行键），供合并失败根因定位。
        """
        rows = await self._read_state_rows()
        if rows:
            row = next(
                (r for r in rows if str(r.get("pipeline_id") or "") == str(task.id)),
                None,
            )
            if row is not None:
                for key in ("task.ws_meta", "ws_meta"):
                    meta = state_fields.as_dict(row.get(key), field=key)
                    if meta:
                        return meta
                logger.warning(
                    "[TaskEvaluate] ws_meta 诊断：state 行在但无 ws_meta 键 | task_id=%s | 行键=%s",
                    task.id,
                    sorted(row.keys()),
                )
            else:
                logger.warning(
                    "[TaskEvaluate] ws_meta 诊断：state 行数=%d，无本任务行 | task_id=%s",
                    len(rows),
                    task.id,
                )
        else:
            logger.warning(
                "[TaskEvaluate] ws_meta 诊断：state 聚合行不可用（rows=%r）| task_id=%s",
                rows,
                task.id,
            )
        meta = (task.metadata or {}).get("ws_meta") if task.metadata else None
        return meta if isinstance(meta, dict) and meta else {}

    async def _fail_task(
        self,
        task_service: Any,
        task: Any,
        eval_result: Any,
        max_retries: int,
    ) -> ToolExecutionResult:
        """评估失败且次数耗尽，标记任务失败。

        TaskService.on_state_change 回调会自动发送终态通知。

        Args:
            task_service: TaskService 实例
            task: TaskModel 实例
            eval_result: EvaluationResult 实例
            max_retries: 最大重试次数

        Returns:
            工具执行结果
        """
        try:
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                eval_data = self._build_result_data(eval_result)
                await task_service.complete_evaluation(task.id, passed=False, result=eval_data)
                # 与 complete_evaluation 内部 fail_task 的 reason 同格式（YAML 镜像
                # 与 state 单一真值一致），供终态通知带出失败原因
                _summary = self._build_rich_summary(eval_result)
                _reason = f"评估未通过: {_summary}" if _summary else "评估未通过"
                await _write_task_state(
                    task.id,
                    {
                        "task.status": "failed",
                        "task.ended_at": _now_iso(),
                        "task.eval_summary": _summary,
                        "task.error": _reason,
                    },
                )
            else:
                logger.info("[TaskEvaluate] 任务 %s 已是终态(%s)，跳过状态回写", task.id, task.status.value)
        except Exception as e:
            logger.error("[TaskEvaluate] complete_evaluation(passed=False) 失败: %s", e)
            return create_failure_result(
                error=f"complete_evaluation(passed=False) 失败: {e}",
                metadata={"eval_data": str(self._build_result_data(eval_result))[:200]},
            )

        failed_metrics = [r.metric_id for r in eval_result.results if not r.passed]
        return create_success_result(
            data=self._build_result_data(eval_result),
            metadata={
                "action": "auto_complete",
                "result": "failed",
                "message": (
                    f"评估未通过且重试次数耗尽({max_retries}次)，任务失败。未通过指标：{', '.join(failed_metrics)}"
                ),
            },
        )

    async def _save_task(self, task_service: Any, task: Any) -> None:
        """保存任务元数据更新（async，因 save_task 是 async）。

        失败向上抛不吞（去降级裁定）：评估历史/重试计数落不进存储时继续走，
        评估结果对上游不可见且假象延续。auto_complete 通路的调用方 try 会把
        异常转 EVAL_FAILED 显式失败信封，任务走 failed 可见。

        Args:
            task_service: TaskService 实例
            task: TaskModel 实例
        """
        await task_service.save_task(task)

    @staticmethod
    def _register_eval_pipelines(
        task_service: Any,
        task: Any,
        eval_result: Any,
    ) -> None:
        """将 Agent 型评估产生的子管道注册到根任务子目录。

        作为 _pre_register_eval_pipeline 的兜底：如果 engine.py
        的早期注册因 ServiceProvider 不可用等原因被跳过，
        此处会在评估完成后再次尝试。
        """
        try:
            root_id = task_service.get_root_task_id(task.id)
            if not root_id:
                logger.debug(
                    "[TaskEvaluate] 无 root_id，跳过评估管道注册 | task=%s",
                    task.id,
                )
                return
            provider = _get_service_provider()
            exec_storage = provider.get("execution_record_storage")
            if not exec_storage:
                logger.warning("[TaskEvaluate] execution_record_storage 不可用，跳过评估管道注册")
                return
            registered = 0
            skipped = 0
            for r in eval_result.results:
                pid = getattr(r, "pipeline_run_id", None)
                if pid:
                    exec_storage.register_pipeline(pid, root_id)
                    registered += 1
                elif hasattr(r, "pipeline_run_id"):
                    skipped += 1
            if registered or skipped:
                logger.info(
                    "[TaskEvaluate] 评估管道注册 | task=%s | root=%s | registered=%d | skipped=%d",
                    task.id,
                    root_id,
                    registered,
                    skipped,
                )
        except Exception as exc:
            logger.warning(
                "[TaskEvaluate] 注册评估管道分组失败 | task=%s | error=%s",
                task.id,
                exc,
            )

    @staticmethod
    def _append_eval_history(task: Any, eval_result: Any) -> None:
        """将本次评估结果追加到 task.metadata 的 evaluation_history。

        每次评估（无论通过/失败/重试）都会被记录，包含时间戳、
        评估指标详情（含评估器输入/输出和 Agent 管道 ID）。

        Args:
            task: TaskModel 实例
            eval_result: EvaluationResult 实例
        """
        from datetime import datetime  # noqa: PLC0415

        metrics = []
        for r in eval_result.results:
            m: dict[str, Any] = {
                "metric_id": r.metric_id,
                "passed": r.passed,
                "score": r.score,
                "message": r.message,
                "error": r.error,
                "evidence": getattr(r, "evidence", []),
                "suggestions": getattr(r, "suggestions", []),
                "details": getattr(r, "details", {}),
            }
            if hasattr(r, "evaluator_input") and r.evaluator_input:
                m["evaluator_input"] = r.evaluator_input
            if hasattr(r, "evaluator_output") and r.evaluator_output:
                m["evaluator_output"] = r.evaluator_output
            if hasattr(r, "pipeline_run_id") and r.pipeline_run_id:
                m["pipeline_run_id"] = r.pipeline_run_id
            metrics.append(m)

        history = task.metadata.get("evaluation_history", [])
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "passed": eval_result.overall_passed,
                "summary": getattr(eval_result, "summary", ""),
                "metrics": metrics,
            }
        )
        task.metadata["evaluation_history"] = history

    async def _read_state_rows(self) -> list[dict[str, Any]] | None:
        """读管道 state 聚合行（pipeline-state.list；None = 桥未就绪）。"""
        reader = _get_state_reader()
        if reader is None:
            return None
        try:
            rows = reader()
            if asyncio.iscoroutine(rows):
                rows = await rows
            return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None
        except Exception as exc:  # noqa: BLE001 — 读面降级不崩评估
            logger.warning("[TaskEvaluate] state 聚合读取失败: %s", exc)
            return None

    async def _ensure_criteria_from_state(self, task: Any) -> None:
        """验收标准兜底：task.metadata 无 acceptance_criteria 时从 state 聚合行补齐。

        0.2 任务=管道架构下指标落在管道 state（task.acceptance_criteria），
        YAML 镜像通路的 task.metadata 常为空——不兜底会导致评估误判
        "未声明任何评估指标"而直接标记完成（实测管道 b8b92a56ad72）。
        聚合行值可能是 JSON 字符串（DB 投影原样存储），还原成 dict。
        """
        if not getattr(task, "id", None):
            return
        if task.metadata and task.metadata.get("acceptance_criteria"):
            return
        try:
            rows = await self._read_state_rows()
        except Exception:  # noqa: BLE001 — 兜底失败按无指标继续（既有降级路径）
            return
        if not rows:
            return
        row = next(
            (r for r in rows if str(r.get("pipeline_id") or "") == str(task.id)),
            None,
        )
        if row is None:
            return
        ac = state_fields.as_dict(row.get("task.acceptance_criteria"), field="task.acceptance_criteria")
        if ac:
            task.metadata = dict(task.metadata or {})
            task.metadata["acceptance_criteria"] = ac

    async def _get_task_from_state(self, task_id: str) -> Any:
        """从 state 聚合行组装任务对象（GAP-1 统一：task = pipeline）。

        返回真实 TaskModel：save_task → storage.save 以 asdict 序列化并沿
        parent_task_id 找根落盘，缺字段的占位 namespace 会在保存路径崩
        （实测 ``'SimpleNamespace' object has no attribute 'parent_task_id'``）。
        行字段（STATE_SUMMARY_KEYS 出口）：task.goal/task.status/task.description/
        task.acceptance_criteria/task.evaluation/lineage.parent_pipeline_id 等
        扁平点号键。缺行返回 None（调用方回退 YAML 只读镜像）。
        """
        rows = await self._read_state_rows()
        if rows is None:
            return None
        row = next(
            (r for r in rows if str(r.get("pipeline_id") or "") == task_id),
            None,
        )
        if row is None:
            return None
        status_str = str(row.get("task.status") or "pending")
        try:
            status = TaskStatus(status_str)
        except (ValueError, AttributeError):
            status = TaskStatus.PENDING
        metadata: dict[str, Any] = {}
        ac = state_fields.as_dict(row.get("task.acceptance_criteria"), field="task.acceptance_criteria")
        if ac:
            metadata["acceptance_criteria"] = ac
        eval_res = row.get("task.evaluation")
        if isinstance(eval_res, dict):
            metadata["evaluation"] = eval_res
        # 评估耗尽计数读回（跨调用累积的单一真值在 state，见 _handle_eval_result）
        retry_counts = state_fields.as_dict(
            row.get("task.eval_retry_count"), field="task.eval_retry_count"
        )
        if retry_counts:
            metadata["eval_retry_count"] = retry_counts
        total_calls = row.get("task.eval_total_calls")
        if total_calls is not None and total_calls != "":
            try:
                metadata["eval_total_calls"] = int(total_calls)
            except (TypeError, ValueError):
                pass
        # 合并门控重试计数读回（跨调用累积的单一真值在 state）
        gate_failures = row.get("task.merge_gate_failures")
        if gate_failures is not None and gate_failures != "":
            try:
                metadata["merge_gate_failures"] = int(gate_failures)
            except (TypeError, ValueError):
                pass
        # ws_meta 回填（数据源适配）：workspace 注入消费点（_get_input_params）读
        # task.metadata.ws_meta，0.2 权威在管道 state——workspace_lifecycle 经
        # pipeline-state.update 即时镜像（task.ws_meta）+ 引擎出口键（ws_meta），
        # 优先级与 _read_task_ws_meta 一致。缺 ws_meta 时指标在错误目录解析相对
        # 路径（实测 file_check「不存在: result.txt」误判）。
        for key in ("task.ws_meta", "ws_meta"):
            ws_meta = state_fields.as_dict(row.get(key), field=key)
            if ws_meta:
                metadata["ws_meta"] = ws_meta
                break
        # 血缘回填：出生协议只写管道坐标（lineage.parent_pipeline_id），GAP-1 下
        # 父任务 id = 父管道 id。父坐标在 YAML 镜像无记录时 _find_root_id 截断
        # 回溯，本任务按根落盘（tree_{task.id}/），保存路径不受影响。
        # 其余字段（priority/agent_level/时间戳/dependencies 等）state 无出口键，
        # 按模型缺省落盘；updated_at 由 save_task 每次覆盖。
        parent_pipeline_id = row.get("lineage.parent_pipeline_id") or None
        return TaskModel(
            id=task_id,
            title=str(row.get("task.goal") or task_id),
            description=str(row.get("task.description") or ""),
            parent_task_id=parent_pipeline_id,
            parent_pipeline_id=parent_pipeline_id,
            status=status,
            metadata=metadata,
            result=None,
        )

    def _get_task_service(self) -> Any:
        """获取共享的 TaskService 实例。

        委托到 tasks 平铺模块（plugins/shared/system/tasks/service_access.py）的
        公共接口（M3 后插件自包含单例，不再依赖 infrastructure.service_provider）。

        Returns:
            TaskService 实例，获取失败返回 None
        """
        from service_access import get_task_service  # noqa: PLC0415

        return get_task_service()

    def _create_executor(self, task_service: Any) -> Any:
        """获取评估执行器（显式构造注入优先，回退 server 装配的默认执行器）。

        0.2 生产执行器 = _executor.PipelineEvaluationExecutor（server.py on_load
        经 set_default_executor 装配）：tool 型指标本地执行，agent 型指标派
        评估子管道（R2：继承被评估任务的 workspace/执行环境）。两处皆未注入
        返回 None → 调用方返回 EVAL_ENGINE_UNAVAILABLE（文档化降级，不静默空转）。

        Args:
            task_service: TaskService 实例（签名兼容保留，执行器不再消费）

        Returns:
            评估执行器；未注入返回 None
        """
        return self._executor or _default_executor

    @staticmethod
    def _all_metrics_passed(task: Any, metric_ids: list[str]) -> bool:
        """检查所有声明指标是否都在历史记录中通过了。

        从 task.metadata.evaluation_history 中收集每个指标最近一次评估结果，
        判断是否所有指标都已通过。

        Args:
            task: TaskModel 实例
            metric_ids: 所有声明的指标 ID 列表

        Returns:
            所有指标是否都已通过
        """
        metadata = task.metadata if task.metadata else {}
        history = metadata.get("evaluation_history", [])
        if not isinstance(history, list):
            return False

        # 收集每个指标最近一次评估结果
        latest: dict[str, bool] = {}
        for entry in history:
            metrics = entry.get("metrics", [])
            for m in metrics:
                mid = m.get("metric_id")
                if mid:
                    latest[mid] = m.get("passed", False)

        return all(latest.get(mid, False) for mid in metric_ids)

    @staticmethod
    def _get_eval_progress(
        task: Any,
        metric_ids: list[str],
    ) -> tuple[int, list[str]]:
        """获取评估进度：已通过数量和剩余未通过的指标 ID。

        Args:
            task: TaskModel 实例
            metric_ids: 所有声明的指标 ID 列表

        Returns:
            (已通过数量, 未通过的指标 ID 列表)
        """
        metadata = task.metadata if task.metadata else {}
        history = metadata.get("evaluation_history", [])

        latest: dict[str, bool] = {}
        if isinstance(history, list):
            for entry in history:
                metrics = entry.get("metrics", [])
                for m in metrics:
                    mid = m.get("metric_id")
                    if mid:
                        latest[mid] = m.get("passed", False)

        passed_count = sum(1 for mid in metric_ids if latest.get(mid, False))
        remaining = [mid for mid in metric_ids if not latest.get(mid, False)]
        return passed_count, remaining

    @staticmethod
    def _get_eval_timeout(task: Any) -> float:
        """根据任务元数据获取评估超时时间（秒）。

        优先使用 task.metadata.eval_timeout（允许单个任务自定义），
        默认 _DEFAULT_EVAL_TIMEOUT（300秒）。
        """
        metadata = task.metadata if task.metadata else {}
        custom_timeout = metadata.get("eval_timeout")
        if custom_timeout is not None:
            try:
                return float(custom_timeout)
            except (TypeError, ValueError):
                pass
        return _DEFAULT_EVAL_TIMEOUT

    @staticmethod
    def _increment_eval_call_count(task: Any) -> int:
        """递增全局评估调用计数并返回当前值。

        每次 task_evaluate 工具被调用时执行，用于实现全局调用次数上限。
        计数值存储在 task.metadata["eval_total_calls"] 中。
        超过上限直接标记任务失败，防止 Agent 无限循环调用评估工具。

        Args:
            task: TaskModel 实例

        Returns:
            递增后的调用次数
        """
        if task.metadata is None:
            task.metadata = {}
        current = task.metadata.get("eval_total_calls", 0)
        if not isinstance(current, int):
            current = 0
        current += 1
        task.metadata["eval_total_calls"] = current
        return current

    def _get_metric_ids(self, task: Any) -> list[str]:
        """从任务模型中提取评估指标 ID 列表。

        Args:
            task: TaskModel 实例

        Returns:
            指标 ID 列表
        """
        if task.metadata and "evaluation_metric_ids" in task.metadata:
            return task.metadata["evaluation_metric_ids"]
        if task.metadata and "acceptance_criteria" in task.metadata:
            ac = task.metadata["acceptance_criteria"]
            if isinstance(ac, dict):
                return list(ac.keys())
        return []

    def _get_input_params(self, task: Any) -> dict[str, dict[str, Any]]:
        """从任务模型的 acceptance_criteria 中提取各指标的输入参数。

        对于工具型评估指标（如 file_check），自动注入 workspace 参数，
        确保评估工具在正确的工作目录下解析文件路径。
        从 task.metadata 解析 workspace 绝对路径，注入到工具型评估指标的参数中，
        使 file_read 等工具在任务工作空间而非项目根目录查找文件。

        Args:
            task: TaskModel 实例

        Returns:
            key=metric_id, value=input_params 的字典
        """
        params, ac = self._extract_raw_input_params(task)

        all_metric_ids: set[str] = set()
        if task.metadata and "evaluation_metric_ids" in task.metadata:
            all_metric_ids = set(task.metadata["evaluation_metric_ids"])
        if isinstance(ac, dict):
            all_metric_ids.update(ac.keys())

        # 直接从任务数据读取 ws_meta.path 作为 workspace
        ws_meta = (task.metadata or {}).get("ws_meta") if task.metadata else None
        workspace_abs: str | None = ws_meta.get("path") if ws_meta else None

        self._inject_workspace_and_placeholders(params, all_metric_ids, task, workspace_abs)
        self._apply_tool_id_templates(params, all_metric_ids, workspace_abs)
        return params

    @staticmethod
    def _extract_raw_input_params(
        task: Any,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """提取各指标的原始输入参数（占位符替换前的形态）。

        config.input_params 存在则直接采用；否则视为 LLM 把参数平铺在顶层，
        过滤 expected_output/pass_threshold/description 等已知非参数键后整体采纳。
        返回 (params, acceptance_criteria 原始对象——非 dict 形态归一为空 dict)。
        """
        params: dict[str, dict[str, Any]] = {}
        ac: dict[str, Any] = {}
        if task.metadata and "acceptance_criteria" in task.metadata:
            candidate = task.metadata["acceptance_criteria"]
            if isinstance(candidate, dict):
                ac = candidate
                non_param_keys = {"expected_output", "pass_threshold", "description"}
                for metric_id, config in candidate.items():
                    if isinstance(config, dict):
                        if "input_params" in config:
                            params[metric_id] = config["input_params"]
                        else:
                            params[metric_id] = {
                                k: v for k, v in config.items() if k not in non_param_keys
                            }
        return params, ac

    def _inject_workspace_and_placeholders(
        self,
        params: dict[str, dict[str, Any]],
        metric_ids: set[str],
        task: Any,
        workspace_abs: str | None,
    ) -> None:
        """workspace 注入 + {{workspace}}/{{task_id}} 占位符替换。

        criteria 不做任何兜底：未配置就是未配置（评估引擎按 input_params
        的检查语义判定，不消费 criteria；拿任务描述顶替只会伪造"有标准"
        假象）。未配置 criteria 的指标在 _auto_complete 直接通过。
        """
        for metric_id in metric_ids:
            p = params.get(metric_id, {})
            if workspace_abs:
                p["workspace"] = workspace_abs
            for key, val in list(p.items()):
                if isinstance(val, str):
                    if workspace_abs:
                        val = val.replace("{{workspace}}", workspace_abs)  # noqa: PLW2901
                    val = val.replace("{{task_id}}", task.id)  # noqa: PLW2901
                    p[key] = val
            params[metric_id] = p

    def _apply_tool_id_templates(
        self,
        params: dict[str, dict[str, Any]],
        metric_ids: set[str],
        workspace_abs: str | None,
    ) -> None:
        """解析参数值中的 ``{tool_id}`` 模板并原地写回。

        唯一候选可用，0/多候选拒绝——绝不"取第一个文件"猜测被评估工具，
        猜测会把评估打到错误对象，错误放行或错误失败。
        """
        tool_id_candidates = self._resolve_tool_id_candidates(workspace_abs)
        for metric_id in metric_ids:
            p = params.get(metric_id, {})
            uses_template = any(
                isinstance(val, str) and "{tool_id}" in val for val in p.values()
            )
            if not uses_template:
                continue
            if len(tool_id_candidates) != 1:
                raise ValueError(
                    f"指标 {metric_id} 引用了 {{tool_id}} 模板，但工作区工具候选为 "
                    f"{len(tool_id_candidates)} 个（{'、'.join(tool_id_candidates) or '无'}），"
                    "无法确定被评估工具；请显式指定工具或清理工作区后重试"
                )
            for key, val in list(p.items()):
                if isinstance(val, str):
                    p[key] = val.replace("{tool_id}", tool_id_candidates[0])
            params[metric_id] = p

    @staticmethod
    def _resolve_tool_id_candidates(workspace_abs: str | None) -> list[str]:
        """收集工作空间 src/tools/builtin/ 下的工具 id 候选（排除 test_/__ 前缀）。

        返回全部候选（按文件名排序）由调用方做唯一性裁决——本函数不做"取第一个"
        猜测，歧义交给调用方显式报错。

        Args:
            workspace_abs: 任务工作空间绝对路径（ws_meta.path），无则返回空列表。

        Returns:
            按文件名排序的工具 id 候选列表。
        """
        if not workspace_abs:
            return []
        from pathlib import Path  # noqa: PLC0415

        tools_dir = Path(workspace_abs) / "src" / "tools" / "builtin"
        if not tools_dir.exists():
            return []
        return sorted(
            py_file.stem
            for py_file in tools_dir.glob("*.py")
            if not py_file.stem.startswith("test_") and not py_file.stem.startswith("__")
        )

    def _build_result_data(self, result: Any) -> dict[str, Any]:
        """将评估结果构建为工具返回数据。

        包含评估器输入/输出、Agent 评估的结构化反馈（issues/suggestions/
        report_path）和管道 ID，便于 LLM 直接定位问题并修复。

        Args:
            result: EvaluationResult 实例

        Returns:
            可序列化的结果字典
        """
        metrics = []
        for r in result.results:
            if r.passed:
                metrics.append(
                    {
                        "metric_id": r.metric_id,
                        "passed": True,
                    }
                )
            else:
                m: dict[str, Any] = {
                    "metric_id": r.metric_id,
                    "passed": False,
                    "score": r.score,
                    "message": r.message,
                    "error": r.error,
                }
                # Agent 评估的结构化反馈
                if r.evaluator_output:
                    eo = r.evaluator_output
                    if eo.get("issues"):
                        m["issues"] = eo["issues"]
                    if eo.get("suggestions"):
                        m["suggestions"] = eo["suggestions"]
                    if eo.get("report_path"):
                        m["report_path"] = sanitize_eval_paths(eo["report_path"])
                # 期望条件失败的详细信息
                if r.details and isinstance(r.details, dict):
                    failed = r.details.get("failed_conditions")
                    if failed:
                        m["failed_conditions"] = failed
                if r.evaluator_input:
                    m["evaluator_input"] = sanitize_eval_paths(r.evaluator_input)
                if r.evaluator_output:
                    m["evaluator_output"] = sanitize_eval_paths(r.evaluator_output)
                if r.pipeline_run_id:
                    m["pipeline_run_id"] = r.pipeline_run_id
                metrics.append(m)
        return {
            "task_id": result.task_id,
            "overall_passed": result.overall_passed,
            "summary": result.summary,
            "metrics": metrics,
        }

    @staticmethod
    def _build_rich_summary(result: Any) -> str:
        """构建富评估摘要（0.1 evaluation.mapper.build_summary 同款）。

        首行汇总 + 逐指标说明（agent 型评估 = 评估 Agent 提交的 feedback
        总结，脚本化评估 = 检查结果 message），供终态通知带出评估结论。
        """
        lines: list[str] = []
        total = len(result.results)
        passed = sum(1 for r in result.results if r.passed)
        lines.append(f"评估结果: {passed}/{total} 指标通过")

        for r in result.results:
            status = "✅ PASS" if r.passed else "❌ FAIL"
            msg = (r.message or "").strip()
            if msg:
                lines.append(f"  {status} {r.metric_id}: {msg}")
            else:
                lines.append(f"  {status} {r.metric_id}")

        return "\n".join(lines)

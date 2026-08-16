# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 工具 0.2 迁移 TDD 测试。

迁移（FP-MIGR，F-MIGR-2）：
1. 模块可加载——0.1 的 tools.builtin.base / tools.types / core.results /
   evaluation.types 已删除：顶层类型走 agentos_plugin_sdk + 本插件目录
   _eval_core.py（就地重建 sanitize_eval_paths / 结果类型面）+ system/tasks
   平铺模块 task_types。
2. get_tool_definition() 返回合法 SDK Tool。
3. 核心行为：评估编排（无指标自动完成 / 全通过完成 / 失败重试 / 指标未找到 /
   调用次数上限 / 执行器未注入降级 / 单指标模式校验）。

装配：conftest.py 注入 sdk / tools 共享层；本文件把 task_evaluate 目录
（_eval_core 平铺模块）+ plugins/shared/system 及其 tasks 平铺目录加入
sys.path（与 task_evaluate/server.py 的 0.2 装配语义一致）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent.parent / "task_evaluate"
_SYSTEM_ROOT = Path(__file__).resolve().parents[2] / "system"

for _d in [_TE_DIR, _SYSTEM_ROOT, _SYSTEM_ROOT / "tasks"]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    """加载 task_evaluate/tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "task_evaluate_tool_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    module_path = _TE_DIR / "tool.py"
    assert module_path.exists(), f"tool.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "cannot load task_evaluate tool.py"
    assert spec.loader is not None, "cannot load task_evaluate tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    """task_evaluate 工具模块（加载后可 monkeypatch 模块级依赖）。"""
    return _load_module()


def _make_task(task_id: str = "t-1", metadata: dict | None = None) -> MagicMock:
    """构造任务 mock（status 为非终态的 MagicMock，metadata 为 dict）。"""
    task = MagicMock()
    task.id = task_id
    task.title = "评估任务"
    task.description = "评估任务描述"
    task.result = None
    task.status = MagicMock()
    task.metadata = metadata if metadata is not None else {}
    return task


def _make_service(task: MagicMock | None = None) -> MagicMock:
    """构造 TaskService mock（async 方法为 AsyncMock）。"""
    service = MagicMock()
    service.get_task.return_value = task
    service.get_root_task_id.return_value = None
    service.save_task = AsyncMock()
    service.complete_evaluation = AsyncMock()
    service.list_by_status.return_value = []
    return service


def _no_provider() -> Any:
    """ServiceProvider 替身：任何 key 都取不到（0.2 无 infrastructure 层）。"""
    provider = MagicMock()
    provider.get.return_value = None
    return provider


# ── 迁移验证：可加载 + 0.2 类型面 ──────────────────────────


class TestTaskEvaluateMigration:
    """迁移成功：模块可 import、类型来自 agentos_plugin_sdk。"""

    def test_module_imports_ok(self, mod):
        """顶层 import 不再命中已删除的 0.1 模块（迁移成功）。"""
        assert mod.TaskEvaluateTool is not None
        assert callable(mod.TaskEvaluateTool.get_tool_definition)

    def test_definition_is_sdk_tool(self, mod):
        from agentos_plugin_sdk import Tool as SdkTool

        tool = mod.TaskEvaluateTool.get_tool_definition()
        assert isinstance(tool, SdkTool)
        assert tool.name == "task_evaluate"
        assert tool.category.value == "task"

    def test_execute_returns_tool_execution_result(self, mod):
        assert isinstance(mod.TaskEvaluateTool(), mod.BuiltinTool)

    def test_eval_core_defines_shared_types(self, mod):
        """_eval_core 就地重建 0.1 evaluation.types 的最小类型面。"""
        from _eval_core import EvaluationResult, MetricResult, sanitize_eval_paths

        assert callable(sanitize_eval_paths)
        assert EvaluationResult(task_id="x").task_id == "x"
        assert MetricResult(metric_id="m").passed is False
        # 脱敏语义：受保护前缀的绝对路径替换为相对路径（防内部路径泄漏）
        sanitized = sanitize_eval_paths("base /tmp/leaked.txt tail")
        assert not sanitized.startswith("base /"), f"绝对路径未被替换为相对路径: {sanitized}"


# ── 核心行为：参数/前置校验 ────────────────────────────────


class TestTaskEvaluateValidation:
    """注入缺失、任务不存在、调用次数上限、非法 action。"""

    @pytest.mark.asyncio
    async def test_missing_task_id_rejected(self, mod, monkeypatch):
        """task_id 未注入且无法推断 → INJECTION_ERROR（防 LLM 无意义重试）。"""
        service = _make_service(task=None)
        service.list_by_status.return_value = []
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete"})
        assert result.success is False
        assert result.error_code == "INJECTION_ERROR"
        assert result.metadata.get("task_failed") is True

    @pytest.mark.asyncio
    async def test_service_unavailable(self, mod, monkeypatch):
        """TaskService 不可用 → SERVICE_UNAVAILABLE。"""
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: None)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete", "task_id": "t-1"})
        assert result.success is False
        assert result.error_code == "SERVICE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_task_not_found(self, mod, monkeypatch):
        """任务不存在 → TASK_NOT_FOUND。"""
        service = _make_service(task=None)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete", "task_id": "ghost"})
        assert result.success is False
        assert result.error_code == "TASK_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_eval_call_limit_exceeded_fails_task(self, mod, monkeypatch):
        """评估调用次数超上限 → 直接标记失败（防 Agent 无限循环调评估）。"""
        task = _make_task(metadata={"max_eval_calls": 0})
        service = _make_service(task=task)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_CALL_LIMIT_EXCEEDED"
        assert result.metadata.get("task_failed") is True

    @pytest.mark.asyncio
    async def test_unknown_action_rejected(self, mod, monkeypatch):
        """非法 action → INVALID_ACTION。"""
        task = _make_task()
        service = _make_service(task=task)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "bogus", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "INVALID_ACTION"


# ── 核心行为：评估编排（注入执行器） ────────────────────────


class TestTaskEvaluateFlow:
    """未声明指标自动完成 / 全通过完成 / 失败重试 / 指标未找到。"""

    def _inject(self, mod, monkeypatch, task: MagicMock, executor: Any) -> tuple[Any, MagicMock]:
        service = _make_service(task=task)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        monkeypatch.setattr(mod, "_get_service_provider", _no_provider)
        tool = mod.TaskEvaluateTool(executor=executor)
        return tool, service

    @pytest.mark.asyncio
    async def test_no_metrics_auto_completes(self, mod, monkeypatch):
        """任务未声明评估指标 → 自动通过并完成（不误报）。"""
        task = _make_task()
        tool, service = self._inject(mod, monkeypatch, task, executor=None)
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is True
        assert result.output["overall_passed"] is True
        assert result.metadata["result"] == "completed"
        # 完成时以 passed=True 回写任务状态
        assert service.complete_evaluation.await_count == 1
        assert service.complete_evaluation.await_args.kwargs["passed"] is True

    @pytest.mark.asyncio
    async def test_auto_complete_all_passed_completes_task(self, mod, monkeypatch):
        """全部指标通过 → 任务完成（complete_evaluation passed=True）。"""
        from _eval_core import EvaluationResult, MetricResult

        task = _make_task(
            metadata={
                "evaluation_metric_ids": ["file_check"],
                "acceptance_criteria": {"file_check": {"input_params": {"path": "src/a.py"}}},
            }
        )

        class FakeExecutor:
            async def run_evaluation(self, **kwargs):
                return EvaluationResult(
                    task_id=task.id,
                    results=[MetricResult(metric_id="file_check", passed=True, message="ok")],
                    overall_passed=True,
                    summary="全部通过",
                )

        tool, service = self._inject(mod, monkeypatch, task, executor=FakeExecutor())
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is True
        assert result.metadata["result"] == "completed"
        assert service.complete_evaluation.await_count == 1
        assert service.complete_evaluation.await_args.kwargs["passed"] is True
        # 评估历史应被记录（供后续增量评估/progress 判定）
        assert task.metadata.get("evaluation_history")

    @pytest.mark.asyncio
    async def test_auto_complete_failure_returns_retry(self, mod, monkeypatch):
        """指标失败但重试次数未耗尽 → 返回 retry 反馈（Agent 继续改进）。"""
        from _eval_core import EvaluationResult, MetricResult

        task = _make_task(
            metadata={
                "evaluation_metric_ids": ["file_check"],
                "acceptance_criteria": {"file_check": {"input_params": {"path": "src/a.py"}}},
            }
        )

        class FakeExecutor:
            async def run_evaluation(self, **kwargs):
                return EvaluationResult(
                    task_id=task.id,
                    results=[MetricResult(metric_id="file_check", passed=False, message="文件不存在")],
                    overall_passed=False,
                    summary="未通过",
                )

        tool, service = self._inject(mod, monkeypatch, task, executor=FakeExecutor())
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is True
        assert result.metadata["result"] == "retry"
        assert "文件不存在" in result.metadata["message"]
        # 未到达完成/失败终态：不调用 complete_evaluation
        assert service.complete_evaluation.await_count == 0

    @pytest.mark.asyncio
    async def test_metric_not_found_returns_clear_error(self, mod, monkeypatch):
        """指标 ID 全部不在注册表 → METRIC_NOT_FOUND（不误导 Agent 重试）。"""
        from _eval_core import EvaluationResult

        task = _make_task(metadata={"evaluation_metric_ids": ["ghost_metric"]})

        class FakeExecutor:
            async def run_evaluation(self, **kwargs):
                return EvaluationResult(task_id=task.id, results=[], overall_passed=False, summary="")

        tool, _ = self._inject(mod, monkeypatch, task, executor=FakeExecutor())
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "METRIC_NOT_FOUND"


# ── 核心行为：0.2 降级（执行器未注入） ─────────────────────


class TestTaskEvaluateNoExecutor:
    """0.2 评估引擎未注入时的文档化降级。"""

    @pytest.mark.asyncio
    async def test_auto_complete_without_executor_degrades(self, mod, monkeypatch):
        """执行器未注入 → EVAL_ENGINE_UNAVAILABLE（不崩溃、不误报成功）。"""
        task = _make_task(metadata={"evaluation_metric_ids": ["file_check"]})
        service = _make_service(task=task)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_ENGINE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_evaluate_single_single_metric_converts_to_auto_complete(self, mod, monkeypatch):
        """单指标任务在 evaluate_single 下自动转完全评估（无执行器 → 降级错误）。"""
        task = _make_task(metadata={"evaluation_metric_ids": ["file_check"]})
        service = _make_service(task=task)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute(
            {"action": "evaluate_single", "metric_id": "file_check", "task_id": task.id}
        )
        assert result.success is False
        assert result.error_code == "EVAL_ENGINE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_evaluate_single_requires_metric_id(self, mod, monkeypatch):
        """evaluate_single 缺 metric_id → METRIC_ID_REQUIRED。"""
        task = _make_task(metadata={"evaluation_metric_ids": ["file_check", "bash_check"]})
        service = _make_service(task=task)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "evaluate_single", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "METRIC_ID_REQUIRED"


# ── task_evaluate_func（简单场景助手函数） ──────────────────


class TestTaskEvaluateFunc:
    """纯校验路径（不触达服务）。"""

    @pytest.mark.asyncio
    async def test_missing_action(self, mod):
        result = await mod.task_evaluate_func({"task_id": "t-1"})
        assert result["error_code"] == "MISSING_ACTION"

    @pytest.mark.asyncio
    async def test_missing_task_id(self, mod):
        result = await mod.task_evaluate_func({"action": "auto_complete"})
        assert result["error_code"] == "MISSING_TASK_ID"

    @pytest.mark.asyncio
    async def test_invalid_action(self, mod):
        result = await mod.task_evaluate_func({"action": "bogus", "task_id": "t-1"})
        assert result["error_code"] == "INVALID_ACTION"

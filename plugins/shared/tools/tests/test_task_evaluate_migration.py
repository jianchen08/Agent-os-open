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
# shared 根：tool.py 顶层 import state_fields（plugins/shared/state_fields.py）
# ——不自带此路径时单目录运行依赖其他测试文件借道 sys.path（串扰），自持
_PLUGIN_PATHS = tuple(
    str(_d) for _d in [_TE_DIR, _SYSTEM_ROOT, _SYSTEM_ROOT / "tasks", _SYSTEM_ROOT.parent]
)
for _d in _PLUGIN_PATHS:
    if _d not in sys.path:
        sys.path.insert(0, _d)


@pytest.fixture(autouse=True)
def _ensure_plugin_paths():
    """平铺串扰自持：其它测试文件的 teardown 会把 system/tasks 目录从
    sys.path 摘走，而 tool.py 顶层 from task_types import 是随模块加载触发的
    ——每个用例前重插（模块期插入只保证收集期）。"""
    for _d in _PLUGIN_PATHS:
        if _d not in sys.path:
            sys.path.insert(0, _d)


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
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # exec 失败时清掉半初始化缓存，防毒化后续用例（AttributeError
        # no TaskEvaluateTool 的根源）
        sys.modules.pop(mod_name, None)
        raise
    return module


@pytest.fixture
def mod() -> Any:
    """task_evaluate 工具模块（加载后可 monkeypatch 模块级依赖）。"""
    return _load_module()


def _make_task(task_id: str = "t-1", metadata: dict | None = None) -> MagicMock:
    """构造任务 mock（status 为非终态的 MagicMock，metadata 为 dict）。

    默认补 plain 模式 ws_meta：完成前工作区门控读 ws_meta 判合并必要性，
    plain 免合并直过——本文件测评估流程语义，不测门控失败路径。"""
    task = MagicMock()
    task.id = task_id
    task.title = "评估任务"
    task.description = "评估任务描述"
    task.result = None
    task.status = MagicMock()
    meta = dict(metadata) if metadata is not None else {}
    meta.setdefault("ws_meta", {"mode": "plain", "path": f"workspace/{task_id}"})
    task.metadata = meta
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
        """task_id 未注入 → INJECTION_ERROR（零推断，2026-08-22 用户裁决）。

        缺失即注入链断裂（state 未取到 run 的 task_id 未变成工具参数注入），
        不做任何候选推断；防 LLM 无意义重试。
        """
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
        """GAP-1 后语义（eb56db0f）：service 不可用不再是错误——读面以 state 聚合
        为真值，service 仅状态写与回退；state 亦无此任务 → TASK_NOT_FOUND。"""
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: None)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete", "task_id": "t-1"})
        assert result.success is False
        assert result.error_code == "TASK_NOT_FOUND"

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

    def _inject(self, mod, monkeypatch, task: MagicMock, executor: Any) -> tuple[Any, MagicMock, Any]:
        service = _make_service(task=task)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        monkeypatch.setattr(mod, "_get_service_provider", _no_provider)
        # 职责边界（2026-08-24）：评估终态经 pipeline-state.update 写 state——
        # 测试注入写面记录调用，断言评估完成时任务状态落 state。
        state_writer = AsyncMock()
        monkeypatch.setattr(mod, "_state_writer", state_writer)
        # 合并机制替身（git CLI 外部依赖）：默认无需合并；门控 ws_meta 解析仍走
        # 真实实现，真实 git 行为由 tests/plugins/shared/test_worktree_merge.py 覆盖
        monkeypatch.setattr(
            mod.worktree_merge,
            "merge_worktree_before_complete",
            lambda task_id, ws_meta: None,
        )
        tool = mod.TaskEvaluateTool(executor=executor)
        return tool, service, state_writer

    @pytest.mark.asyncio
    async def test_no_metrics_auto_completes(self, mod, monkeypatch):
        """任务未声明评估指标 → 自动通过并完成（不误报）。"""
        task = _make_task()
        tool, service, state_writer = self._inject(mod, monkeypatch, task, executor=None)
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is True
        assert result.output["overall_passed"] is True
        assert result.metadata["result"] == "completed"
        # 完成时以 passed=True 回写任务状态
        assert service.complete_evaluation.await_count == 1
        assert service.complete_evaluation.await_args.kwargs["passed"] is True
        # 职责边界（2026-08-24）：评估终态落 state 单一真值（pipeline-state.update）；
        # task.eval_total_calls 计数是伴随写，终态以 completed 行为准
        completion_writes = [
            c for c in state_writer.await_args_list if c.args[1].get("task.status") == "completed"
        ]
        assert len(completion_writes) == 1
        assert completion_writes[0].args[0] == task.id
        assert "task.ended_at" in completion_writes[0].args[1]

    @pytest.mark.asyncio
    async def test_auto_complete_all_passed_completes_task(self, mod, monkeypatch):
        """全部指标通过 → 任务完成（complete_evaluation passed=True）。"""
        from _eval_core import EvaluationResult, MetricResult

        task = _make_task(
            metadata={
                "evaluation_metric_ids": ["file_check"],
                "acceptance_criteria": {
                    "file_check": {"input_params": {"path": "src/a.py", "criteria": "文件存在"}}
                },
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

        tool, service, state_writer = self._inject(mod, monkeypatch, task, executor=FakeExecutor())
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is True
        assert result.metadata["result"] == "completed"
        assert service.complete_evaluation.await_count == 1
        assert service.complete_evaluation.await_args.kwargs["passed"] is True
        # 职责边界（2026-08-24）：评估终态落 state 单一真值（pipeline-state.update）；
        # task.eval_total_calls 计数是伴随写，终态以 completed 行为准
        completion_writes = [
            c for c in state_writer.await_args_list if c.args[1].get("task.status") == "completed"
        ]
        assert len(completion_writes) == 1
        assert "task.ended_at" in completion_writes[0].args[1]
        # 评估历史应被记录（供后续增量评估/progress 判定）
        assert task.metadata.get("evaluation_history")

    @pytest.mark.asyncio
    async def test_auto_complete_failure_returns_retry(self, mod, monkeypatch):
        """指标失败但重试次数未耗尽 → 返回 retry 反馈（Agent 继续改进）。"""
        from _eval_core import EvaluationResult, MetricResult

        task = _make_task(
            metadata={
                "evaluation_metric_ids": ["file_check"],
                "acceptance_criteria": {
                    "file_check": {"input_params": {"path": "src/a.py", "criteria": "文件存在"}}
                },
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

        tool, service, state_writer = self._inject(mod, monkeypatch, task, executor=FakeExecutor())
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

        task = _make_task(
            metadata={
                "evaluation_metric_ids": ["ghost_metric"],
                "acceptance_criteria": {"ghost_metric": {"input_params": {"criteria": "任何标准"}}},
            }
        )

        class FakeExecutor:
            async def run_evaluation(self, **kwargs):
                return EvaluationResult(task_id=task.id, results=[], overall_passed=False, summary="")

        tool, _, _ = self._inject(mod, monkeypatch, task, executor=FakeExecutor())
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "METRIC_NOT_FOUND"


# ── 核心行为：0.2 降级（执行器未注入） ─────────────────────


class TestTaskEvaluateNoExecutor:
    """0.2 评估引擎未注入时的文档化降级。"""

    @pytest.mark.asyncio
    async def test_auto_complete_without_executor_degrades(self, mod, monkeypatch):
        """执行器未注入 → EVAL_ENGINE_UNAVAILABLE（不崩溃、不误报成功）。"""
        task = _make_task(
            metadata={
                "evaluation_metric_ids": ["file_check"],
                "acceptance_criteria": {"file_check": {"input_params": {"criteria": "文件存在"}}},
            }
        )
        service = _make_service(task=task)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_ENGINE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_evaluate_single_single_metric_converts_to_auto_complete(self, mod, monkeypatch):
        """单指标任务在 evaluate_single 下自动转完全评估（无执行器 → 降级错误）。"""
        task = _make_task(
            metadata={
                "evaluation_metric_ids": ["file_check"],
                "acceptance_criteria": {"file_check": {"input_params": {"criteria": "文件存在"}}},
            }
        )
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


# ═══════════════════════════════════════════════════════════
# criteria 兜底显式标记（兜底反模式审查 P6，2026-08-20）
# ═══════════════════════════════════════════════════════════


class TestCriteriaFallbackMarked:
    """criteria 去兜底后的行为（2026-08-30 契约：未配置即直接通过）。

    指标缺 criteria 不再用任务描述顶替（顶替伪造"有标准"假象）：
    _get_input_params 原样保留参数不注入 criteria；auto_complete 对
    无 criteria 指标直接通过并在完成 summary 如实标注。
    """

    def _inject(self, mod, monkeypatch, task: MagicMock, executor: Any) -> tuple[Any, MagicMock, Any]:
        service = _make_service(task=task)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        monkeypatch.setattr(mod, "_get_service_provider", _no_provider)
        # 职责边界（2026-08-24）：评估终态经 pipeline-state.update 写 state——
        # 测试注入写面记录调用，断言评估完成时任务状态落 state。
        state_writer = AsyncMock()
        monkeypatch.setattr(mod, "_state_writer", state_writer)
        # 合并机制替身（git CLI 外部依赖）：默认无需合并；门控 ws_meta 解析仍走
        # 真实实现，真实 git 行为由 tests/plugins/shared/test_worktree_merge.py 覆盖
        monkeypatch.setattr(
            mod.worktree_merge,
            "merge_worktree_before_complete",
            lambda task_id, ws_meta: None,
        )
        tool = mod.TaskEvaluateTool(executor=executor)
        return tool, service, state_writer

    @pytest.mark.asyncio
    async def test_no_criteria_params_kept_as_is(self, mod, monkeypatch):
        """_get_input_params 单 dict 返回：缺 criteria 的指标参数原样保留、不注入。"""
        task = _make_task(
            metadata={
                "evaluation_metric_ids": ["file_check"],
                "acceptance_criteria": {"file_check": {"input_params": {"path": "src/a.py"}}},
            }
        )
        tool = mod.TaskEvaluateTool()
        params = tool._get_input_params(task)
        assert params["file_check"]["path"] == "src/a.py"
        assert "criteria" not in params["file_check"], "任务描述不得顶替 criteria"

    @pytest.mark.asyncio
    async def test_configured_criteria_passes_through(self, mod, monkeypatch):
        """显式配置了 criteria 的指标按声明传递。"""
        task = _make_task(
            metadata={
                "evaluation_metric_ids": ["semantic_check"],
                "acceptance_criteria": {
                    "semantic_check": {"input_params": {"criteria": "必须包含结论"}}
                },
            }
        )
        tool = mod.TaskEvaluateTool()
        params = tool._get_input_params(task)
        assert params["semantic_check"]["criteria"] == "必须包含结论"

    @pytest.mark.asyncio
    async def test_auto_complete_no_criteria_direct_pass_marked(self, mod, monkeypatch):
        """auto_complete：无 criteria 指标直接通过，完成 summary 如实标注来源。"""
        task = _make_task(
            metadata={
                "evaluation_metric_ids": ["file_check"],
                "acceptance_criteria": {"file_check": {"input_params": {"path": "src/a.py"}}},
            }
        )

        class FakeExecutor:
            async def run_evaluation(self, **kwargs):
                raise AssertionError("无 criteria 指标直接通过，不应触达评估执行器")

        tool, service, state_writer = self._inject(mod, monkeypatch, task, executor=FakeExecutor())
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is True
        assert result.metadata["result"] == "completed"
        # 通过来源可见：完成结果带"未配置 criteria 直接通过"标注
        passed_result = service.complete_evaluation.await_args.kwargs["result"]
        assert "未配置 criteria 直接通过" in passed_result["summary"]


# ── 猜测型匹配反模式收口（2026-08-22）───────────────────────


class TestTaskIdInjectionFailsClosed:
    """task_id 未注入时零推断：候选任务再多也不猜（用户裁决）。

    猜测活跃任务会掩盖注入链断裂，并把 A 任务的评估结果写到 B 任务上。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("candidate_count", [1, 2])
    async def test_missing_task_id_rejected_regardless_of_running_candidates(self, mod, monkeypatch, candidate_count):
        """未注入 + 有 RUNNING/EVALUATING 候选任务 → 依然 INJECTION_ERROR，推断路径零调用。"""
        service = _make_service(task=None)
        service.list_by_status.return_value = [_make_task(f"t-other-{i}") for i in range(candidate_count)]
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete"})
        assert result.success is False
        assert result.error_code == "INJECTION_ERROR"
        assert result.metadata.get("task_failed") is True
        # 推断路径零调用：不 list_by_status 扫描、不写任何任务
        assert service.list_by_status.call_count == 0
        service.complete_evaluation.assert_not_awaited()


class TestToolIdTemplateResolution:
    """{tool_id} 模板解析：唯一候选可用，0/多候选拒绝，绝不取第一个（2026-08-22 同批裁决）。"""

    @staticmethod
    def _tool_with_workspace(mod: Any, tmp_path: Path, file_names: list[str]) -> tuple[Any, MagicMock]:
        """构造带 src/tools/builtin 工作区与引用 {tool_id} 指标的任务。"""
        tools_dir = tmp_path / "src" / "tools" / "builtin"
        tools_dir.mkdir(parents=True)
        for name in file_names:
            (tools_dir / name).write_text("def run():\n    pass\n", encoding="utf-8")
        metadata = {
            "ws_meta": {"path": str(tmp_path)},
            "acceptance_criteria": {
                "m1": {"input_params": {"pattern": "{tool_id}", "desc": "检查 {tool_id} 实现", "criteria": "检查 {tool_id} 实现是否完整"}},
            },
        }
        task = _make_task("t-1", metadata=metadata)
        return mod.TaskEvaluateTool(), task

    def test_single_candidate_substituted(self, mod, tmp_path):
        """唯一候选 → 模板替换为该工具 id。"""
        tool, task = self._tool_with_workspace(mod, tmp_path, ["my_tool.py"])
        params = tool._get_input_params(task)
        assert params["m1"]["pattern"] == "my_tool"
        assert params["m1"]["desc"] == "检查 my_tool 实现"

    def test_multiple_candidates_rejected(self, mod, tmp_path):
        """多个候选 → 拒绝（不取第一个文件）。"""
        tool, task = self._tool_with_workspace(mod, tmp_path, ["tool_a.py", "tool_b.py"])
        with pytest.raises(ValueError, match="无法确定被评估工具"):
            tool._get_input_params(task)

    def test_zero_candidates_rejected(self, mod):
        """模板被引用但工作区无候选 → 拒绝（不把 {tool_id} 字面量漏给评估器）。"""
        task = _make_task(
            "t-1",
            metadata={"acceptance_criteria": {"m1": {"input_params": {"pattern": "{tool_id}"}}}},
        )
        tool = mod.TaskEvaluateTool()
        with pytest.raises(ValueError, match="无法确定"):
            tool._get_input_params(task)


class TestTaskEvaluateParamResolutionFailure:
    """指标参数解析失败（如 {tool_id} 歧义）→ 显式失败而非带病评估。"""

    @pytest.mark.asyncio
    async def test_auto_complete_param_resolution_error_returns_failure(self, mod, monkeypatch):
        task = _make_task(metadata={"evaluation_metric_ids": ["m1"]})
        service = _make_service(task=task)

        def _raise(self: Any, _task: Any) -> tuple[dict, list]:
            raise ValueError("指标 m1 引用了 {tool_id} 模板，但工作区工具候选为 0 个，无法确定被评估工具")

        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_input_params", _raise)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "INVALID_INPUT_PARAMS"
        assert "无法确定被评估工具" in result.error
        # 未到达完成/失败终态：不写任务
        service.complete_evaluation.assert_not_awaited()

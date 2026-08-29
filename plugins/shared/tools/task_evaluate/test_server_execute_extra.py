# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 服务端装配（server.py）与工具执行路径（execute 编排）补充单测。

server.py：
- on_load 装配默认执行器（PipelineEvaluationExecutor 经 set_default_executor 注入）；
- task_evaluate 工具入口：成功/失败两条返回分支（错误路径返回 {"error": ...}）。

execute 编排（沿用真实 TaskService + tmp 存储，外部依赖仅 state 读面/写面与
执行器注入替身）：
- 单指标模式：summary 透传、超时 EVAL_TIMEOUT、litellm 限速 RATE_LIMITED、
  一般异常 EVAL_FAILED、部分通过 partial_pass、全过自动完成；
- 自动评估：summary 回填未配置指标、无指标自动完成、历史全过跳过执行器；
- 结果处理：unrecoverable 模式耗尽 FAILED、失败计数耗尽 FAILED、通过重置计数、
  完整 retry 反馈（剩余次数/失败明细）、空结果 METRIC_NOT_FOUND；
- 调用次数上限 EVAL_CALL_LIMIT_EXCEEDED、AMBIGUOUS 短 id 拒绝；
- 合并门控：ws_meta 缺失即失败（不静默跳过）/ state ws_meta 透传机制层
  （worktree_merge 替身；真实 git 行为由 tests/plugins/shared/test_worktree_merge.py 覆盖）；
- 评估子管道注册：root 存在时注册/跳过/异常降级。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
_TASKS_DIR = _TE_DIR.parents[1] / "system" / "tasks"
# 共享根（plugins/shared 平铺模块 state_fields/worktree_merge/task_birth）——
# 对齐生产 sidecar sys.path 形态（自身目录 + shared 根），单文件可跑。
_SHARED_ROOT = _TE_DIR.parents[1]

for _d in (_TE_DIR, _TASKS_DIR, _SHARED_ROOT):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from task_types import TaskStatus  # noqa: E402 — 依赖上方 sys.path 注入


def _load_tool() -> Any:
    mod_name = "task_eval_tool_exec_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TE_DIR / "tool.py")
    assert spec is not None and spec.loader is not None, "cannot load tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    return _load_tool()


@pytest.fixture
def service(tmp_path: Path) -> Any:
    from service import TaskService

    return TaskService(data_dir=str(tmp_path / "tasks"))


async def _new_task(service: Any, *, metadata: dict[str, Any] | None = None) -> Any:
    return await service.create_task(title="评估任务", description="任务描述", metadata=metadata or {})


def _inject_tool(
    mod: Any,
    monkeypatch: Any,
    service: Any,
    merge_result: str | None = None,
) -> tuple[Any, MagicMock]:
    """monkeypatch 服务获取/读面/写面与合并机制替身，返回 (tool, state_writer)。

    merge_result：worktree_merge 机制替身返回值（None=合并成功/无需合并；
    str=失败原因）。门控的 ws_meta 解析仍走真实实现；机制层（git CLI）为
    外部依赖故以替身注入，真实 git 行为由 tests/plugins/shared/test_worktree_merge.py
    用真实仓库覆盖。
    """
    state_writer = AsyncMock()
    monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
    monkeypatch.setattr(mod, "_state_reader", None)
    monkeypatch.setattr(mod, "_state_writer", state_writer)

    def _merge_stub(task_id: str, ws_meta: dict[str, Any]) -> str | None:
        return merge_result

    monkeypatch.setattr(mod.worktree_merge, "merge_worktree_before_complete", _merge_stub)
    return mod.TaskEvaluateTool(), state_writer


def _metric(metric_id: str, passed: bool, **kw: Any) -> Any:
    from _eval_core import MetricResult

    return MetricResult(metric_id=metric_id, passed=passed, **kw)


def _eval_result(task_id: str, metrics: list[Any], *, summary: str = "") -> Any:
    from _eval_core import EvaluationResult

    r = EvaluationResult(task_id=task_id, results=metrics, summary=summary)
    r.compute_overall()
    return r


class _RecordingExecutor:
    """记录每次 run_evaluation 的参数并返回可编程结果。"""

    def __init__(self, result_factory: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result_factory = result_factory

    async def run_evaluation(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._result_factory(kwargs)


# ── server.py：on_load 装配与工具处理器 ──────────────────────


class TestServerAssembly:
    @staticmethod
    def _load_server() -> Any:
        name = "task_eval_server_extra_under_test"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, _TE_DIR / "server.py")
        assert spec is not None and spec.loader is not None, "cannot load server.py"
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def test_on_load_injects_default_executor(self, monkeypatch: Any) -> None:
        server_mod = self._load_server()
        # 用本测试进程内唯一别名加载 tool，避免与 server._on_load 的 `import tool` 槽位冲突
        tool_mod = _load_tool()
        # 直接把 tool 槽位指向该别名模块，使 on_load 的 `import tool as tool_mod` 命中同一实例
        monkeypatch.setitem(sys.modules, "tool", tool_mod)
        before = tool_mod._default_executor
        try:
            asyncio.run(server_mod._on_load({}))
            assert tool_mod._default_executor is not None, "on_load 应装配默认评估执行器"
            # 注入的默认执行器应带 chat_send/state_rows（真实验证它可被构造）
            ex = tool_mod._default_executor
            assert callable(ex._chat_send) and callable(ex._state_rows)
        finally:
            tool_mod.set_default_executor(before)

    @pytest.mark.asyncio
    async def test_handler_success_and_error_paths(self, monkeypatch: Any) -> None:
        from agentos_plugin_sdk import create_failure_result, create_success_result

        class _FakeTool:
            async def execute(self, inputs: dict[str, Any]) -> Any:
                if inputs.get("action") == "fail":
                    return create_failure_result(error="模拟失败", error_code="X")
                return create_success_result(data={"task_id": inputs.get("task_id")})

        fake_mod = types.ModuleType("tool")
        fake_mod.TaskEvaluateTool = _FakeTool
        monkeypatch.setitem(sys.modules, "tool", fake_mod)
        server_mod = self._load_server()
        out = await server_mod.task_evaluate(action="auto_complete", task_id="t1")
        # 9de6b56f6 起 server 返回完整 ToolExecutionResult 信封（metadata 携带
        # result=completed/task_failed 等副作用信号，剥成裸 output 会丢评估证据）
        assert out["success"] is True
        assert out["output"] == {"task_id": "t1"}
        out_err = await server_mod.task_evaluate(action="fail", task_id="t1")
        assert out_err["success"] is False
        assert out_err["error"] == "模拟失败"


# ── 单指标模式（evaluate_single） ────────────────────────────


class TestEvaluateSingle:
    @pytest.mark.asyncio
    async def test_summary_passthrough_and_partial_pass(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1", "m2"]})

        def _factory(kwargs: dict[str, Any]) -> Any:
            return _eval_result(task.id, [_metric("m1", True)], summary="ok")

        executor = _RecordingExecutor(_factory)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = executor
        result = await tool.execute({"action": "evaluate_single", "metric_id": "m1", "task_id": task.id, "summary": "我做完了"})
        assert result.success is True
        assert result.metadata["result"] == "partial_pass"
        assert "进度：1/2" in result.metadata["message"] and "m2" in result.metadata["message"]
        # summary 透传给执行器
        assert executor.calls[0]["input_params"] == {"m1": {"summary": "我做完了"}}
        assert executor.calls[0]["metric_ids"] == ["m1"]
        # 未走到完成/失败终态：不写状态
        assert task.metadata["eval_total_calls"] == 1

    @pytest.mark.asyncio
    async def test_single_metric_converts_to_auto_complete(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """单指标任务在 evaluate_single 下自动转完全评估并完成。"""
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"]})

        def _factory(kwargs: dict) -> Any:
            return _eval_result(task.id, [_metric("m1", True)], summary="全过")

        ex = _RecordingExecutor(_factory)
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        tool._executor = ex
        result = await tool.execute({"action": "evaluate_single", "metric_id": "m1", "task_id": task.id})
        assert result.success is True
        assert result.metadata["result"] == "completed"
        assert service.get_task(task.id).status.value == "completed"
        assert state_writer.await_args.args[1]["task.status"] == "completed"
        # 自动转完全评估：执行器收到全部指标
        assert ex.calls[0]["metric_ids"] == ["m1"]

    @pytest.mark.asyncio
    async def test_timeout_returns_eval_timeout(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1", "m2"]})
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_eval_timeout", staticmethod(lambda self: 0.001))

        async def _slow(**kwargs: Any) -> Any:
            await asyncio.sleep(1)

        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = _SlowExecutor(_slow)
        result = await tool.execute({"action": "evaluate_single", "metric_id": "m1", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_TIMEOUT"
        assert "评估超时" in result.error

    @pytest.mark.asyncio
    async def test_rate_limited_returns_rate_limited(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        import litellm

        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1", "m2"]})

        def _boom(_kwargs: dict[str, Any]) -> Any:
            raise litellm.RateLimitError(message="quota", llm_provider="deepseek", model="m", response=None)

        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = _RecordingExecutor(_boom)
        result = await tool.execute({"action": "evaluate_single", "metric_id": "m1", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "RATE_LIMITED"
        assert "限速" in result.error

    @pytest.mark.asyncio
    async def test_generic_exception_returns_eval_failed(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1", "m2"]})

        async def _boom(**kwargs: Any) -> Any:
            raise RuntimeError("executor blew up")

        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = _RecordingExecutor(_boom)
        result = await tool.execute({"action": "evaluate_single", "metric_id": "m1", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_FAILED"


class _SlowExecutor:
    def __init__(self, coro: Any) -> None:
        self._coro = coro

    async def run_evaluation(self, **kwargs: Any) -> Any:
        return await self._coro(**kwargs)


# ── 自动评估（auto_complete） ────────────────────────────────


class TestAutoComplete:
    @pytest.mark.asyncio
    async def test_summary_filled_into_input_params(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(
            service,
            metadata={
                "evaluation_metric_ids": ["m1", "m2"],
                "acceptance_criteria": {"m1": {"input_params": {"criteria": "要求"}}},
            },
        )

        def _factory(kwargs: dict) -> Any:
            return _eval_result(task.id, [_metric("m1", True), _metric("m2", True)], summary="ok")

        ex = _RecordingExecutor(_factory)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = ex
        result = await tool.execute({"action": "auto_complete", "task_id": task.id, "summary": "完成总结"})
        assert result.success is True
        assert result.metadata["result"] == "completed"
        params = ex.calls[0]["input_params"]
        assert params["m1"]["criteria"] == "要求"
        assert params["m1"]["summary"] == "完成总结"
        assert params["m2"]["summary"] == "完成总结"

    @pytest.mark.asyncio
    async def test_no_metrics_auto_completes_without_executor(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service)
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        tool._executor = _RecordingExecutor(lambda _kw: _eval_result(task.id, []))
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is True
        assert result.output["overall_passed"] is True
        assert result.output["summary"] == "未声明评估指标，自动通过"
        # 两次状态写：①评估调用计数（eval_total_calls，耗尽判定的跨调用真值）
        # ②完成裁决（task.status）
        assert state_writer.await_count == 2
        assert state_writer.await_args.args[1]["task.status"] == "completed"

    @pytest.mark.asyncio
    async def test_all_passed_history_skips_executor(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(
            service,
            metadata={
                "evaluation_metric_ids": ["m1", "m2"],
                "evaluation_history": [
                    {"metrics": [{"metric_id": "m1", "passed": True}, {"metric_id": "m2", "passed": True}]}
                ],
            },
        )
        ex = _RecordingExecutor(lambda _kw: _eval_result("x", []))
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        tool._executor = ex
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is True
        assert result.metadata["result"] == "completed"
        assert ex.calls == [], "历史全过不应再调用执行器"
        assert "历史评估记录" in result.output["summary"]

    @pytest.mark.asyncio
    async def test_auto_complete_timeout(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"]})
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_eval_timeout", staticmethod(lambda self: 0.001))

        async def _slow(**kwargs: Any) -> Any:
            await asyncio.sleep(1)

        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = _SlowExecutor(_slow)
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_TIMEOUT"

    @pytest.mark.asyncio
    async def test_call_limit_exceeded_fails_task(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"], "max_eval_calls": 0})
        tool, _ = _inject_tool(mod, monkeypatch, service)
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_CALL_LIMIT_EXCEEDED"
        assert result.metadata.get("task_failed") is True
        assert service.get_task(task.id).status.value == "pending"


# ── 评估结果处理（_handle_evaluation_result） ────────────────


class TestHandleEvaluationResult:
    @pytest.mark.asyncio
    async def test_unrecoverable_pattern_exhausts_immediately(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"], "max_eval_retries": 5})
        tool, _ = _inject_tool(mod, monkeypatch, service)
        result = _eval_result(task.id, [_metric("m1", False, message="command not found")])
        out = await tool._handle_evaluation_result({"action": "auto_complete"}, service, task, result)
        assert out.success is True
        assert out.metadata["result"] == "failed"
        # unrecoverable → 直接按耗尽处理（不消耗渐进重试）
        assert task.metadata["eval_retry_count"] == {"m1": 5}
        assert service.get_task(task.id).status.value == "failed"

    @pytest.mark.asyncio
    async def test_retry_count_exhausted_fails_task(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(
            service,
            metadata={"evaluation_metric_ids": ["m1"], "max_eval_retries": 3, "eval_retry_count": {"m1": 2}},
        )
        tool, _ = _inject_tool(mod, monkeypatch, service)
        result = _eval_result(task.id, [_metric("m1", False, message="继续改进")])
        out = await tool._handle_evaluation_result({"action": "auto_complete"}, service, task, result)
        assert out.success is True
        assert out.metadata["result"] == "failed"
        assert "重试次数耗尽" in out.metadata["message"]
        assert service.get_task(task.id).status.value == "failed"

    @pytest.mark.asyncio
    async def test_retry_feedback_with_remaining_counts(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(
            service,
            metadata={
                "evaluation_metric_ids": ["m1"],
                "max_eval_retries": 3,
                "eval_retry_count": {"m1": 1},
                "max_eval_calls": 15,
                "eval_total_calls": 7,
            },
        )
        tool, _ = _inject_tool(mod, monkeypatch, service)
        result = _eval_result(task.id, [_metric("m1", False, message="文件不对", score=20.0)])
        out = await tool._handle_evaluation_result({"action": "auto_complete"}, service, task, result)
        assert out.success is True
        assert out.metadata["result"] == "retry"
        assert out.metadata["retry_remaining"] == 1
        msg = out.metadata["message"]
        assert "[m1] 未通过" in msg and "文件不对" in msg and "得分: 20.0" in msg
        assert "剩余重试：1 次" in msg and "已调用 7/15" in msg

    @pytest.mark.asyncio
    async def test_passed_metric_resets_retry_count(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(
            service,
            metadata={"evaluation_metric_ids": ["m1"], "eval_retry_count": {"m1": 2}},
        )
        tool, _ = _inject_tool(mod, monkeypatch, service)
        result = _eval_result(task.id, [_metric("m1", True)])
        out = await tool._handle_evaluation_result({"action": "auto_complete"}, service, task, result)
        assert out.success is True
        assert out.metadata["result"] == "completed"
        assert task.metadata["eval_retry_count"] == {"m1": 0}

    @pytest.mark.asyncio
    async def test_no_results_metric_not_found(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["ghost"]})
        tool, _ = _inject_tool(mod, monkeypatch, service)
        out = await tool._handle_evaluation_result({"action": "auto_complete"}, service, task, _eval_result(task.id, []))
        assert out.success is False
        assert out.error_code == "METRIC_NOT_FOUND"
        assert "未找到任何有效的评估指标" in out.error

    @pytest.mark.asyncio
    async def test_fail_fast_semantics_kept_compatible(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """fail_fast/skip_state_update 兼容保留：逐指标独立回收，写面仍在工具层。"""
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"]})
        result = _eval_result(task.id, [_metric("m1", False)])
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        out = await tool._handle_evaluation_result({"action": "auto_complete"}, service, task, result)
        assert out.success is True


class TestMergeAndRegister:
    @pytest.mark.asyncio
    async def test_try_merge_missing_ws_meta_fails_not_skips(self, mod: Any, monkeypatch: Any) -> None:
        """ws_meta 拿不到 = 合并失败（worktree 产物不能静默丢失），不再有跳过分支。"""
        monkeypatch.setattr(mod, "_state_reader", None)
        err = await mod.TaskEvaluateTool()._try_merge_before_complete(MagicMock(id="t1"))
        assert err is not None
        assert "t1" in err and "ws_meta" in err

    @pytest.mark.asyncio
    async def test_try_merge_delegates_to_local_mechanism(self, mod: Any, monkeypatch: Any) -> None:
        """门控决策 + 机制执行分层：门控把 state 解析的 ws_meta 透传共享模块并回传结果。"""
        calls: list[tuple[str, dict[str, Any]]] = []

        def fake_merge(task_id: str, ws_meta: dict[str, Any]) -> str | None:
            calls.append((task_id, ws_meta))
            return "merge boom"

        monkeypatch.setattr(mod.worktree_merge, "merge_worktree_before_complete", fake_merge)
        ws_meta = {"mode": "worktree", "path": "D:/wt", "project_root": "D:/src", "branch": "task/t1"}
        monkeypatch.setattr(
            mod, "_state_reader", lambda: [{"pipeline_id": "t1", "ws_meta": ws_meta}]
        )
        err = await mod.TaskEvaluateTool()._try_merge_before_complete(MagicMock(id="t1"))
        assert err == "merge boom"
        assert calls == [("t1", ws_meta)]

    def test_register_eval_pipelines_with_root(self, mod: Any, monkeypatch: Any) -> None:
        registered: list[tuple[str, str]] = []

        class _ExecStorage:
            def register_pipeline(self, pid: str, root_id: str) -> None:
                registered.append((pid, root_id))

        class _Provider:
            def get(self, key: str) -> Any:
                return _ExecStorage() if key == "execution_record_storage" else None

        service = MagicMock()
        service.get_root_task_id.return_value = "root-1"
        monkeypatch.setattr(mod, "_get_service_provider", lambda: _Provider())
        r = _eval_result("x", [_metric("a", True, pipeline_run_id="evalP"), _metric("b", True)])
        mod.TaskEvaluateTool._register_eval_pipelines(service, MagicMock(), r)
        assert registered == [("evalP", "root-1")]

    def test_register_eval_pipelines_exception_degrades(self, mod: Any, monkeypatch: Any, caplog: Any) -> None:
        class _BoomStorage:
            def register_pipeline(self, pid: str, root_id: str) -> None:
                raise RuntimeError("storage down")

        class _Provider:
            def get(self, key: str) -> Any:
                return _BoomStorage()

        service = MagicMock()
        service.get_root_task_id.return_value = "root-1"
        monkeypatch.setattr(mod, "_get_service_provider", lambda: _Provider())
        mod.TaskEvaluateTool._register_eval_pipelines(service, MagicMock(), _eval_result("x", [_metric("a", True, pipeline_run_id="p")]))
        assert "注册评估管道分组失败" in caplog.text

def _eval(task_id: str, metrics: list[Any], *, summary: str = "") -> Any:
    from _eval_core import EvaluationResult

    r = EvaluationResult(task_id=task_id, results=metrics, summary=summary)
    r.compute_overall()
    return r


# ── execute 路径补充：错误分支与边界 ─────────────────────────


class TestExecuteExtraPaths:
    @pytest.mark.asyncio
    async def test_missing_task_id_injection_error(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """task_id 未注入 → INJECTION_ERROR（零推断，2026-08-22 用户裁决）。"""
        monkeypatch.setattr(mod, "_state_reader", None)
        monkeypatch.setattr(mod, "_state_writer", AsyncMock())
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete"})
        assert result.success is False
        assert result.error_code == "INJECTION_ERROR"
        assert result.metadata.get("task_failed") is True

    @pytest.mark.asyncio
    async def test_resolve_task_id_reader_exception_degrades(self, mod: Any, monkeypatch: Any) -> None:
        """_resolve_task_id 内读 state 抛异常 → 解析降级原样放行（不炸执行）。"""

        async def _boom(self: Any) -> list[dict[str, Any]]:
            raise RuntimeError("bridge down")

        monkeypatch.setattr(mod.TaskEvaluateTool, "_read_state_rows", _boom)
        tool = mod.TaskEvaluateTool()
        # 降级语义：异常被吞，原样返回候选 id（后续"任务不存在"路径处理）
        assert await tool._resolve_task_id("abc123") == "abc123"

    @pytest.mark.asyncio
    async def test_ambiguous_task_id_rejected(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """短 id 歧义 → AMBIGUOUS_TASK_ID（零猜测）。"""
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "abc1111111111"}, {"pipeline_id": "abc2222222222"}],
        )
        monkeypatch.setattr(mod, "_state_writer", AsyncMock())
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete", "task_id": "abc"})
        assert result.success is False
        assert result.error_code == "AMBIGUOUS_TASK_ID"

    @pytest.mark.asyncio
    async def test_evaluate_single_missing_metric_id(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1", "m2"]})
        tool, _ = _inject_tool(mod, monkeypatch, service)
        result = await tool.execute({"action": "evaluate_single", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "METRIC_ID_REQUIRED"

    @pytest.mark.asyncio
    async def test_evaluate_single_metric_not_found(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1", "m2"]})

        def _factory(kwargs: dict[str, Any]) -> Any:
            return _eval_result(task.id, [], summary="")

        ex = _RecordingExecutor(_factory)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = ex
        result = await tool.execute({"action": "evaluate_single", "metric_id": "ghost", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "METRIC_NOT_FOUND"
        assert "未找到" in result.error

    @pytest.mark.asyncio
    async def test_evaluate_single_failed_returns_retry(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1", "m2"]})

        def _factory(kwargs: dict[str, Any]) -> Any:
            return _eval_result(task.id, [_metric("m1", False, message="不对")], summary="")

        ex = _RecordingExecutor(_factory)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = ex
        result = await tool.execute({"action": "evaluate_single", "metric_id": "m1", "task_id": task.id})
        assert result.success is True
        assert result.metadata["result"] == "retry"
        assert "继续改进" in result.metadata["message"]

    @pytest.mark.asyncio
    async def test_auto_complete_fallback_summary_marked(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """criteria 兜底必须显式可见：summary 前缀带兜底标记。"""
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"], "acceptance_criteria": {"m1": {"input_params": {"path": "x"}}}})

        def _factory(kwargs: dict[str, Any]) -> Any:
            return _eval_result(task.id, [_metric("m1", True)], summary="全部通过")

        ex = _RecordingExecutor(_factory)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = ex
        ex = _RecordingExecutor(_factory)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = ex
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is True
        # 兜底标记显式可见 + 原 summary（compute_overall 重算）保留
        assert "未配置 criteria，用任务描述兜底" in result.output["summary"]
        assert "全部 1 项指标通过" in result.output["summary"]

    @pytest.mark.asyncio
    async def test_auto_complete_eval_failed(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"]})

        def _boom(kwargs: dict[str, Any]) -> Any:
            raise RuntimeError("engine died")

        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = _RecordingExecutor(_boom)
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_FAILED"

    @pytest.mark.asyncio
    async def test_auto_complete_no_criteria_task_fails_param_parse(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """{tool_id} 模板歧义 → 显式失败而非带病评估。"""
        task = await _new_task(
            service,
            metadata={"evaluation_metric_ids": ["m1"], "acceptance_criteria": {"m1": {"input_params": {"pattern": "{tool_id}"}}}},
        )
        tool, _ = _inject_tool(mod, monkeypatch, service)
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "INVALID_INPUT_PARAMS"

    @pytest.mark.asyncio
    async def test_task_not_found(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", None)
        monkeypatch.setattr(mod, "_state_writer", AsyncMock())
        service = MagicMock()
        service.get_task.return_value = None
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool()
        result = await tool.execute({"action": "auto_complete", "task_id": "ghost"})
        assert result.success is False
        assert result.error_code == "TASK_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_unknown_action_rejected(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        result = await tool.execute({"action": "bogus", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "INVALID_ACTION"

    @pytest.mark.asyncio
    async def test_single_metric_executor_not_injected(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1", "m2"]})
        tool, _ = _inject_tool(mod, monkeypatch, service)
        result = await tool.execute({"action": "evaluate_single", "metric_id": "m1", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_ENGINE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_litellm_import_degrade_keeps_eval_failed(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """litellm 不可导入（精简环境）→ 限速识别降级，仍返回 EVAL_FAILED。"""
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1", "m2"]})
        # 让 litellm 导入失败：用 import hook 模拟 ModuleNotFoundError
        import builtins

        real_import = builtins.__import__

        def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "litellm":
                raise ModuleNotFoundError("no litellm")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)

        def _boom(kwargs: dict[str, Any]) -> Any:
            raise RuntimeError("boom")

        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = _RecordingExecutor(_boom)
        result = await tool.execute({"action": "evaluate_single", "metric_id": "m1", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_FAILED"


# ── 剩余边界：恢复/汇总标记/模板唯一替换/异常降级 ─────────────


class TestEdgeBranches:
    @pytest.mark.asyncio
    async def test_recover_to_completed_exception_surfaces(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """FAILED 任务恢复完成时 recover_to_completed 异常 → 记录日志，结果仍成功。"""
        task = await _new_task(service)
        task.status = TaskStatus.FAILED
        service._storage.save(task)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        monkeypatch.setattr(service, "recover_to_completed", AsyncMock(side_effect=RuntimeError("recover failed")))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        # 恢复失败不阻断成功结果（评估已通过）
        assert out.success is True

    @pytest.mark.asyncio
    async def test_merge_fail_complete_evaluation_exception(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """合并失败标记 failed 时 complete_evaluation(passed=False) 异常 → 降级返回。"""
        task = await _new_task(service)
        tool, _ = _inject_tool(mod, monkeypatch, service, merge_result="worktree 合并失败: merge boom")
        monkeypatch.setattr(service, "complete_evaluation", AsyncMock(side_effect=RuntimeError("storage down")))
        monkeypatch.setattr(service, "complete_evaluation", AsyncMock(side_effect=RuntimeError("storage down")))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is False
        assert "worktree 合并失败" in out.error

    @pytest.mark.asyncio
    async def test_retry_counts_non_dict_normalized(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"], "eval_retry_count": "junk"})
        tool, _ = _inject_tool(mod, monkeypatch, service)
        out = await tool._handle_evaluation_result(
            {"action": "auto_complete"}, service, task, _eval_result(task.id, [_metric("m1", False)])
        )
        assert out.success is True
        # 非 dict 重试计数被归一化为空 dict → 当前为第 1 次，未耗尽
        assert task.metadata["eval_retry_count"] == {"m1": 1}

    @pytest.mark.asyncio
    async def test_metadata_none_normalized(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service)
        task.metadata = None
        tool, _ = _inject_tool(mod, monkeypatch, service)
        out = await tool._handle_evaluation_result(
            {"action": "auto_complete"}, service, task, _eval_result(task.id, [_metric("m1", False)])
        )
        assert out.success is True
        assert task.metadata is not None
        assert task.metadata["eval_retry_count"] == {"m1": 1}

    @pytest.mark.asyncio
    async def test_resolve_id_raise_degrades(self, mod: Any, monkeypatch: Any) -> None:
        """resolve_id 抛异常 → 解析降级原样放行（既有"任务不存在"路径处理）。"""
        import id_utils

        def _boom(rows: Any, candidate: str) -> Any:
            raise RuntimeError("resolve broken")

        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "abc"}])
        monkeypatch.setattr(id_utils, "resolve_id", _boom)
        tool = mod.TaskEvaluateTool()
        assert await tool._resolve_task_id("abc") == "abc"

    @pytest.mark.asyncio
    async def test_single_all_passed_completes(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """evaluate_single 当前指标通过 + 历史全过 → 自动完成任务。"""
        task = await _new_task(
            service,
            metadata={
                "evaluation_metric_ids": ["m1", "m2"],
                "evaluation_history": [
                    {"metrics": [{"metric_id": "m1", "passed": True}, {"metric_id": "m2", "passed": True}]}
                ],
            },
        )

        def _factory(kwargs: dict[str, Any]) -> Any:
            return _eval_result(task.id, [_metric("m1", True)], summary="ok")

        ex = _RecordingExecutor(_factory)
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        tool._executor = ex
        result = await tool.execute({"action": "evaluate_single", "metric_id": "m1", "task_id": task.id})
        assert result.success is True
        assert result.metadata["result"] == "completed"
        assert state_writer.await_args.args[1]["task.status"] == "completed"

    @pytest.mark.asyncio
    async def test_summary_unwritable_marked_in_log(self, mod: Any, service: Any, monkeypatch: Any, caplog: Any) -> None:
        """评估结果 summary 不可写（只读对象）→ 兜底标记仅留日志，结果照常流转。"""
        task = await _new_task(
            service,
            metadata={"evaluation_metric_ids": ["m1"], "acceptance_criteria": {"m1": {"input_params": {"path": "x"}}}},
        )

        class _ReadonlyResult:
            """模拟只读结果对象：属性写入一律抛 AttributeError。"""

            def __init__(self) -> None:
                object.__setattr__(self, "task_id", "t1")
                object.__setattr__(self, "overall_passed", True)
                object.__setattr__(self, "results", [_metric("m1", True)])
                object.__setattr__(self, "summary", "只读")

            def __setattr__(self, name: str, value: Any) -> None:
                raise AttributeError("readonly")

        ex = _RecordingExecutor(lambda kw: _ReadonlyResult())
        tool, _ = _inject_tool(mod, monkeypatch, service)
        tool._executor = ex
        with caplog.at_level("WARNING"):
            result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert "summary 不可写" in caplog.text
        assert result.success is True

    @pytest.mark.asyncio
    async def test_tool_id_template_single_candidate_substituted(self, mod: Any, tmp_path: Path) -> None:
        """{tool_id} 唯一候选 → 替换进参数（不猜测，唯一命中）。"""
        tools_dir = tmp_path / "src" / "tools" / "builtin"
        tools_dir.mkdir(parents=True)
        (tools_dir / "my_tool.py").write_text("def run():\n    pass\n", encoding="utf-8")
        task = MagicMock()
        task.id = "t-1"
        task.description = ""
        task.title = ""
        task.metadata = {
            "evaluation_metric_ids": ["m1"],
            "ws_meta": {"path": str(tmp_path)},
            "acceptance_criteria": {"m1": {"input_params": {"pattern": "{tool_id}"}}},
        }
        tool = mod.TaskEvaluateTool()
        params, fallback = tool._get_input_params(task)
        assert params["m1"]["pattern"] == "my_tool"
        # 描述/标题均为空 → 任务描述兜底不触发
        assert fallback == [] and params["m1"].get("criteria") is None


class TestStateRowExecuteMerge:
    @pytest.mark.asyncio
    async def test_state_row_task_then_merge_fail_then_complete(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """state 行兜底：读面无该任务行 → 回退 YAML 任务并完成（plain ws_meta 走 metadata 兜底）。"""
        task = await _new_task(
            service,
            metadata={
                "evaluation_metric_ids": ["m1"],
                "ws_meta": {"mode": "plain", "path": "D:/ws"},
            },
        )
        monkeypatch.setattr(mod, "_state_reader", lambda: [])
        monkeypatch.setattr(mod, "_state_writer", AsyncMock())
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        ex = _RecordingExecutor(lambda kw: _eval_result(task.id, [_metric("m1", True)], summary="ok"))
        tool = mod.TaskEvaluateTool(executor=ex)
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is True
        assert result.metadata["result"] == "completed"
        assert service.get_task(task.id).status.value == "completed"


# ── server.py 装配：读写面闭包契约 ────────────────────────────


class TestServerClosures:
    """on_load 注入的 state 读/写/chat 闭包（按真实 capability 句柄往返）。"""

    @staticmethod
    def _fresh_server() -> Any:
        name = "task_eval_server_caps_under_test"
        sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(name, _TE_DIR / "server.py")
        assert spec is not None and spec.loader is not None, "cannot load server.py"
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    @pytest.fixture(autouse=True)
    def _clean_tool_slot(self) -> Any:
        """on_load 内部 `import tool` 走本插件目录（防其它测试残留 tool 槽位）。"""
        sys.modules.pop("tool", None)
        te = str(_TE_DIR)
        if te in sys.path:
            sys.path.remove(te)
        sys.path.insert(0, te)
        yield
        sys.modules.pop("tool", None)

    @pytest.mark.asyncio
    async def test_on_load_closures_roundtrip(self) -> None:
        """state 读/写/chat 闭包经 pipeline-state/chat capability 正确转发。"""
        from agentos_plugin_sdk import CapabilityHandle

        server_mod = self._fresh_server()
        plugin = server_mod.plugin

        list_calls: list[Any] = []
        update_calls: list[Any] = []
        send_calls: list[Any] = []

        async def _call_fn(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
            # CapabilityHandle.call 原样转发 method（不带 capability 前缀）
            if method == "list":
                list_calls.append(params)
                return [{"pipeline_id": "p1", "task.status": "running"}] if len(list_calls) == 1 else "not-a-list"
            if method == "update":
                update_calls.append(params)
                return {"ok": True}
            if method == "send_message":
                send_calls.append(params)
                return {"pipeline_id": "evalP"}
            return {}

        plugin._capabilities = {
            "pipeline-state": CapabilityHandle("pipeline-state", call_fn=_call_fn),
            "chat": CapabilityHandle("chat", call_fn=_call_fn),
        }
        await server_mod._on_load({})

        import tool as tool_mod  # noqa: PLC0415 — on_load 已装配本插件 tool 槽位

        try:
            # 读面：state 聚合行可读；非 list 响应降级为空列表
            rows = await tool_mod._get_state_reader()()
            assert rows == [{"pipeline_id": "p1", "task.status": "running"}]
            rows2 = await tool_mod._get_state_reader()()
            assert rows2 == []
            # 写面：task 状态落 state（pipeline_id + fields 原样转发）
            await tool_mod._get_state_writer()("p1", {"task.status": "completed"})
            assert update_calls[0]["pipeline_id"] == "p1"
            assert update_calls[0]["fields"] == {"task.status": "completed"}
            # 默认执行器已装配：chat 闭包可派发评估子管道
            executor = tool_mod._default_executor
            assert executor is not None
            resp = await executor._chat_send({"create": True, "agent_id": "evaluator_agent"})
            assert resp == {"pipeline_id": "evalP"}
            assert send_calls == [{"create": True, "agent_id": "evaluator_agent"}]
        finally:
            tool_mod.set_default_executor(None)

    @pytest.mark.asyncio
    async def test_assembled_executor_runs_tool_metric(self, tmp_path: Path) -> None:
        """on_load 装配的默认执行器可直接本地执行 tool 型指标（无需内核）。"""
        from _executor import PipelineEvaluationExecutor

        from agentos_plugin_sdk import CapabilityHandle

        server_mod = self._fresh_server()
        plugin = server_mod.plugin

        async def _call_fn(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
            if method == "pipeline-state.list":
                return []
            return {}

        plugin._capabilities = {
            "pipeline-state": CapabilityHandle("pipeline-state", call_fn=_call_fn),
            "chat": CapabilityHandle("chat", call_fn=_call_fn),
        }
        await server_mod._on_load({})

        import tool as tool_mod  # noqa: PLC0415

        try:
            cfg = tmp_path / "metrics.yaml"
            cfg.write_text(
                "metrics:\n  - name: file_check\n    evaluator_type: tool\n",
                encoding="utf-8",
            )
            ex = PipelineEvaluationExecutor(
                chat_send=AsyncMock(return_value={"pipeline_id": "evalP"}),
                state_rows=(lambda: []),
                metrics_config_path=str(cfg),
            )
            result = await ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": "no-such-file"}})
            assert result.results[0].passed is False
            assert "不存在" in result.results[0].message
        finally:
            tool_mod.set_default_executor(None)



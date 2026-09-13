# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 工具层（tool.py）深处分支簇补充单测。

与同目录既有测试互补（test_tool_extra / test_server_execute_extra 已覆盖主干编
排与门控主干），本文件只补深处未覆盖分支：

- 已 failed 任务的恢复路径合并门控失败 → 保持 failed（不绕门恢复，不落 state）；
- 合并门控 ws_meta 读取失败重试计数的 metadata 数据源两形态
  （既有非空 dict 上递增 / metadata=None 归零并建 dict）；
- _read_task_ws_meta 诊断分支：state 行在但无可用 ws_meta（键缺失/值非法）、
  行集无本任务行——均回退 task.metadata 兜底并留诊断日志；
- _ensure_criteria_from_state 兜底链：无 id 早退不读 state、state 行回填
  （dict / JSON 字符串双形态）、行集无本任务行不动 metadata；
- _get_task_from_state 计数读回：eval_retry_count（dict/JSON 双形态）、
  eval_total_calls 与 merge_gate_failures 的合法值解析（str/int 归一 int）
  与非法值丢弃（解析失败只丢该字段，任务组装不受影响）。

任务领域走真实 TaskModel + 真实 TaskService（tmp 存储目录）；外部依赖仅
state 读面/写面与 worktree_merge 机制层（git CLI，替身注入，真实 git 行为由
tests/plugins/shared/test_worktree_merge.py 覆盖）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
_TASKS_DIR = _TE_DIR.parents[1] / "system" / "tasks"
# 共享根模块（state_fields / worktree_merge）：单文件直跑时也自足解析
_SHARED_DIR = _TE_DIR.parents[1]

for _d in (_TE_DIR, _TASKS_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from task_types import TaskStatus  # noqa: E402 — 依赖上方 sys.path 注入


def _load_module() -> Any:
    mod_name = "task_eval_tool_deep_under_test"
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
    return _load_module()


@pytest.fixture
def service(tmp_path: Path) -> Any:
    """真实 TaskService（临时 YAML 目录）。"""
    from service import TaskService

    return TaskService(data_dir=str(tmp_path / "tasks"))


async def _new_task(service: Any, *, metadata: dict[str, Any] | None = None) -> Any:
    return await service.create_task(title="评估任务", description="任务描述", metadata=metadata or {})


def _metric(metric_id: str, passed: bool, **kw: Any) -> Any:
    from _eval_core import MetricResult

    return MetricResult(metric_id=metric_id, passed=passed, **kw)


def _eval_result(task_id: str, metrics: list[Any], *, summary: str = "") -> Any:
    from _eval_core import EvaluationResult

    r = EvaluationResult(task_id=task_id, results=metrics, summary=summary)
    r.compute_overall()
    return r


def _patch_merge(monkeypatch: Any, mod: Any, merge_result: str | None) -> None:
    """合并机制层替身（git CLI 为外部依赖；真实 git 行为另有真实仓库测试覆盖）。"""

    def _merge_stub(task_id: str, ws_meta: dict[str, Any]) -> str | None:
        return merge_result

    monkeypatch.setattr(mod.worktree_merge, "merge_worktree_before_complete", _merge_stub)


# ── 已 failed 任务的恢复路径：合并门控失败保持 failed ──────────


class TestFailedTaskRecoveryMergeGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "merge_error",
        [
            "worktree 合并失败: git merge conflict",
            "ws_meta 读取失败: meta not ready",
        ],
        ids=["real_merge_failure", "meta_read_failure"],
    )
    async def test_failed_task_merge_gate_failure_stays_failed(
        self, mod: Any, service: Any, monkeypatch: Any, merge_error: str
    ) -> None:
        """FAILED 任务评估通过但合并门控失败 → 保持 failed，不绕门恢复。

        恢复路径与正常路径不同构：门控失败直接判死（无 ws_meta 读取失败
        重试计数），且不发生任何 state 回写。
        """
        task = await _new_task(service)
        task.status = TaskStatus.FAILED
        service._storage.save(task)
        monkeypatch.setattr(mod, "_state_writer", AsyncMock())
        _patch_merge(monkeypatch, mod, merge_error)
        tool = mod.TaskEvaluateTool()

        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))

        assert out.success is False
        assert out.error_code == "MERGE_GATE_FAILED"
        assert out.metadata.get("task_failed") is True
        assert merge_error in out.error
        # 门控拦下恢复：任务保持 failed，且零 state 写（未谎报任何终态迁移）
        assert service.get_task(task.id).status.value == "failed"
        assert mod._state_writer.await_count == 0


# ── 合并门控重试计数：metadata 数据源多形态 ────────────────────


class TestMergeGateRetryCounterSources:
    @pytest.mark.asyncio
    async def test_retry_counter_increments_existing_metadata_count(
        self, mod: Any, service: Any, monkeypatch: Any
    ) -> None:
        """metadata 已有计数 → 在既有值上递增（1 → 2），未达阈值返回重试不判死。"""
        task = await _new_task(service, metadata={"merge_gate_failures": 1})
        monkeypatch.setattr(mod, "_state_writer", AsyncMock())
        _patch_merge(monkeypatch, mod, "ws_meta 读取失败: meta not ready")
        tool = mod.TaskEvaluateTool()

        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))

        assert out.success is False
        assert out.error_code == "MERGE_GATE_RETRY"
        assert "2/3" in out.error
        # 计数在既有值上单调递增，并落 state 单一真值
        assert task.metadata["merge_gate_failures"] == 2
        assert mod._state_writer.await_args.args[1]["task.merge_gate_failures"] == 2

    @pytest.mark.asyncio
    async def test_retry_counter_seeds_dict_when_metadata_none(
        self, mod: Any, service: Any, monkeypatch: Any
    ) -> None:
        """metadata=None 的任务遇 ws_meta 读取失败 → 归零起计并就地建 dict。"""
        task = await _new_task(service)
        task.metadata = None
        monkeypatch.setattr(mod, "_state_writer", AsyncMock())
        _patch_merge(monkeypatch, mod, "ws_meta 读取失败: meta not ready")
        tool = mod.TaskEvaluateTool()

        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))

        assert out.success is False
        assert out.error_code == "MERGE_GATE_RETRY"
        assert "1/3" in out.error
        assert task.metadata["merge_gate_failures"] == 1


# ── ws_meta 数据源解析：诊断分支与 metadata 兜底 ───────────────


class TestReadTaskWsMetaDiagnostics:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row",
        [
            {"pipeline_id": "p1", "task.goal": "g"},
            {"pipeline_id": "p1", "task.ws_meta": "not-json", "ws_meta": 123},
        ],
        ids=["keys_absent", "values_non_dict"],
    )
    async def test_row_without_usable_ws_meta_falls_back_to_metadata(
        self, mod: Any, monkeypatch: Any, caplog: Any, row: dict[str, Any]
    ) -> None:
        """state 行在但无可用 ws_meta（键缺失/值形态非法）→ 回退 metadata 并留诊断。"""
        fallback = {"mode": "plain", "path": "/m"}
        monkeypatch.setattr(mod, "_state_reader", lambda: [row])
        task = SimpleNamespace(id="p1", metadata={"ws_meta": fallback})

        with caplog.at_level("WARNING"):
            meta = await mod.TaskEvaluateTool()._read_task_ws_meta(task)

        assert meta == fallback
        assert "state 行在但无 ws_meta 键" in caplog.text

    @pytest.mark.asyncio
    async def test_rows_without_task_row_falls_back_to_metadata(
        self, mod: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        """state 行集非空但无本任务行 → 回退 metadata 并留行数诊断。"""
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "other", "ws_meta": {"mode": "plain", "path": "/o"}}],
        )
        fallback = {"mode": "worktree", "path": "/w"}
        task = SimpleNamespace(id="p1", metadata={"ws_meta": fallback})

        with caplog.at_level("WARNING"):
            meta = await mod.TaskEvaluateTool()._read_task_ws_meta(task)

        assert meta == fallback
        assert "无本任务行" in caplog.text


# ── 验收标准兜底：_ensure_criteria_from_state ─────────────────


class TestEnsureCriteriaFromState:
    @pytest.mark.asyncio
    async def test_task_without_id_never_reads_state(self, mod: Any, monkeypatch: Any) -> None:
        """无 id 的任务早退，不触发 state 读取、不动 metadata。"""
        reads: list[int] = []

        def _reader() -> list[dict[str, Any]]:
            reads.append(1)
            return []

        monkeypatch.setattr(mod, "_state_reader", _reader)
        task = SimpleNamespace(metadata={})

        await mod.TaskEvaluateTool()._ensure_criteria_from_state(task)

        assert reads == []
        assert "acceptance_criteria" not in task.metadata

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row_value",
        [
            {"m1": {"criteria": "存在"}, "m2": {"check": "exists"}},
            '{"m1": {"criteria": "存在"}, "m2": {"check": "exists"}}',
        ],
        ids=["dict_form", "json_string_form"],
    )
    async def test_criteria_backfilled_from_state_row(
        self, mod: Any, monkeypatch: Any, row_value: Any
    ) -> None:
        """metadata 无 acceptance_criteria → 从 state 行补齐（dict / JSON 字符串双形态）。"""
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "p1", "task.acceptance_criteria": row_value}],
        )
        task = SimpleNamespace(id="p1", metadata={})

        await mod.TaskEvaluateTool()._ensure_criteria_from_state(task)

        expected = {"m1": {"criteria": "存在"}, "m2": {"check": "exists"}}
        assert task.metadata["acceptance_criteria"] == expected
        assert isinstance(task.metadata["acceptance_criteria"], dict)

    @pytest.mark.asyncio
    async def test_rows_without_task_row_leave_metadata_untouched(
        self, mod: Any, monkeypatch: Any
    ) -> None:
        """行集无本任务行 → metadata 原样保留（兜底不伪造指标）。"""
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "other", "task.acceptance_criteria": {"m1": {}}}],
        )
        task = SimpleNamespace(id="p1", metadata={"note": "keep"})

        await mod.TaskEvaluateTool()._ensure_criteria_from_state(task)

        assert task.metadata == {"note": "keep"}


# ── 计数读回：_get_task_from_state 的评估/门控计数回填 ─────────


class TestGetTaskFromStateCounterBackfill:
    async def _task_from_row(self, mod: Any, monkeypatch: Any, row: dict[str, Any]) -> Any:
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "p1", **row}])
        return await mod.TaskEvaluateTool()._get_task_from_state("p1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value", [{"m1": 2}, '{"m1": 2}'], ids=["dict_form", "json_string_form"]
    )
    async def test_eval_retry_count_backfilled(self, mod: Any, monkeypatch: Any, value: Any) -> None:
        """state 行的 eval_retry_count（dict/JSON 双形态）回填任务 metadata。"""
        task = await self._task_from_row(mod, monkeypatch, {"task.eval_retry_count": value})
        assert task.metadata["eval_retry_count"] == {"m1": 2}
        assert isinstance(task.metadata["eval_retry_count"], dict)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["7", 7], ids=["str_form", "int_form"])
    async def test_eval_total_calls_parsed_to_int(
        self, mod: Any, monkeypatch: Any, value: Any
    ) -> None:
        """eval_total_calls 的 str/int 形态均归一为 int（跨调用耗尽判定的真值）。"""
        task = await self._task_from_row(mod, monkeypatch, {"task.eval_total_calls": value})
        assert task.metadata["eval_total_calls"] == 7
        assert isinstance(task.metadata["eval_total_calls"], int)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value", ["abc", {"junk": 1}, ""], ids=["non_numeric_str", "dict_value", "empty_str"]
    )
    async def test_eval_total_calls_unparseable_dropped(
        self, mod: Any, monkeypatch: Any, value: Any
    ) -> None:
        """非法 eval_total_calls 只丢该字段，任务组装其余部分不受影响。"""
        task = await self._task_from_row(
            mod, monkeypatch, {"task.eval_total_calls": value, "task.goal": "目标g"}
        )
        assert "eval_total_calls" not in task.metadata
        assert task.title == "目标g"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["2", 2], ids=["str_form", "int_form"])
    async def test_merge_gate_failures_parsed_to_int(
        self, mod: Any, monkeypatch: Any, value: Any
    ) -> None:
        """merge_gate_failures 的 str/int 形态均归一为 int（门控耗尽判定真值）。"""
        task = await self._task_from_row(mod, monkeypatch, {"task.merge_gate_failures": value})
        assert task.metadata["merge_gate_failures"] == 2
        assert isinstance(task.metadata["merge_gate_failures"], int)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value", ["oops", []], ids=["non_numeric_str", "list_value"]
    )
    async def test_merge_gate_failures_unparseable_dropped(
        self, mod: Any, monkeypatch: Any, value: Any
    ) -> None:
        """非法 merge_gate_failures 只丢该字段，任务组装其余部分不受影响。"""
        task = await self._task_from_row(
            mod, monkeypatch, {"task.merge_gate_failures": value, "task.goal": "目标g"}
        )
        assert "merge_gate_failures" not in task.metadata
        assert task.title == "目标g"

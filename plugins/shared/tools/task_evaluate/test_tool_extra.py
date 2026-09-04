# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 工具层（tool.py）补充单测。

与 plugins/shared/tools/tests/test_task_evaluate_migration.py 互补：既有测试覆盖
迁移面/校验面/基本编排/执行器未注入降级；本文件补齐未覆盖分支——

- task_id 短 id 解析：前缀唯一命中、歧义报错、读取异常降级、非 str 原样；
- state 聚合读面：_get_task_from_state 组装真实 TaskModel（状态枚举解析/坏状态
  回退/无 acceptance_criteria 行/血缘回填/stub 经真实 save_task 落盘不崩）、
  _read_state_rows 未注入/异常/非列表降级；
- 单指标模式：summary 透传、超时 EVAL_TIMEOUT、litellm 限速 RATE_LIMITED、
  一般异常 EVAL_FAILED、部分通过 partial_pass、全部通过自动完成；
- 评估结果处理：unrecoverable 模式耗尽、失败计数耗尽 FAILED、通过重置计数、
  完整 retry 反馈（剩余次数/失败明细/得分）；
- 完成/失败路径：worktree 合并失败标记 failed、complete_evaluation 异常降级、
  已 failed 任务恢复为完成、终态任务跳过回写；
- 合并门控：ws_meta 数据源解析（state 行 dict/JSON 字符串、metadata 兜底、
  缺失空值）、非 worktree 机制层零接触、worktree 透传、缺失即失败不静默跳过
  （机制层替身注入；真实 git 行为由 tests/plugins/shared/test_worktree_merge.py 覆盖）；
- 写面降级：未注入 state writer 不写、写面异常不阻断评估主流程；
- 辅助静态方法：_get_eval_timeout 自定义/非法值回退、_all_metrics_passed、
  _get_eval_progress、_increment_eval_call_count（非 int 归零）、
  _get_input_params 的 input_params/顶层参数/workspace 注入/{{workspace}}/
  {{task_id}} 模板替换、_resolve_tool_id_candidates、_build_result_data
  （通过/失败含 issues/suggestions/report_path/failed_conditions 脱敏）。

任务领域走真实 TaskModel + 真实 TaskService（tmp 存储目录）；外部依赖仅
state 读面/写面与评估执行器（注入替身）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
    mod_name = "task_eval_tool_extra_under_test"
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
    """真实 TaskService（内存 + 临时 YAML 目录），task_id=None 门面模式。"""
    from service import TaskService

    return TaskService(data_dir=str(tmp_path / "tasks"))


async def _new_task(service: Any, *, title: str = "评估任务", description: str = "任务描述", metadata: dict[str, Any] | None = None) -> Any:
    return await service.create_task(title=title, description=description, metadata=metadata or {})


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


# ── task_id 短 id 解析 ───────────────────────────────────────


class TestResolveTaskId:
    @pytest.mark.asyncio
    async def test_short_id_prefix_resolved(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "abc1234567890"}])
        tool = mod.TaskEvaluateTool()
        assert await tool._resolve_task_id("abc123456789") == "abc1234567890"

    @pytest.mark.asyncio
    async def test_ambiguous_short_id_flags(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "abc1111111111"}, {"pipeline_id": "abc2222222222"}],
        )
        tool = mod.TaskEvaluateTool()
        # 前缀 "abc" 同时命中两行 → 歧义
        assert await tool._resolve_task_id("abc") == "AMBIGUOUS:abc"

    @pytest.mark.asyncio
    async def test_no_match_passthrough(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "other-id-123"}])
        tool = mod.TaskEvaluateTool()
        assert await tool._resolve_task_id("ghost") == "ghost"

    @pytest.mark.asyncio
    async def test_non_string_passthrough(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "abc"}])
        tool = mod.TaskEvaluateTool()
        assert await tool._resolve_task_id(42) == 42

    @pytest.mark.asyncio
    async def test_reader_raise_degrades(self, mod: Any, monkeypatch: Any) -> None:
        async def boom() -> list[dict[str, Any]]:
            raise RuntimeError("bridge down")

        monkeypatch.setattr(mod, "_state_reader", boom)
        tool = mod.TaskEvaluateTool()
        assert await tool._resolve_task_id("abc") == "abc"


# ── state 聚合读面 ───────────────────────────────────────────


class TestStateRead:
    @pytest.mark.asyncio
    async def test_reader_not_injected_returns_none(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", None)
        tool = mod.TaskEvaluateTool()
        assert await tool._read_state_rows() is None

    @pytest.mark.asyncio
    async def test_reader_exception_returns_none(self, mod: Any, monkeypatch: Any) -> None:
        async def boom() -> list[dict[str, Any]]:
            raise RuntimeError("read fail")

        monkeypatch.setattr(mod, "_state_reader", boom)
        tool = mod.TaskEvaluateTool()
        assert await tool._read_state_rows() is None

    @pytest.mark.asyncio
    async def test_reader_non_list_returns_none(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", lambda: "not-a-list")
        tool = mod.TaskEvaluateTool()
        assert await tool._read_state_rows() is None

    @pytest.mark.asyncio
    async def test_reader_async_and_filters_non_dict(self, mod: Any, monkeypatch: Any) -> None:
        async def reader() -> list[Any]:  # 真实读面契约允许非 dict 行,由调用方过滤
            return [{"pipeline_id": "p1"}, "junk", 42]

        monkeypatch.setattr(mod, "_state_reader", reader)
        tool = mod.TaskEvaluateTool()
        assert await tool._read_state_rows() == [{"pipeline_id": "p1"}]

    @pytest.mark.asyncio
    async def test_get_task_from_state_assembles_light_task(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [
                {
                    "pipeline_id": "p1",
                    "task.goal": "目标",
                    "task.description": "描述",
                    "task.status": "evaluating",
                    "task.acceptance_criteria": {"m1": {"input_params": {"path": "x"}}},
                    "task.evaluation": {"summary": "ok"},
                    "lineage.parent_pipeline_id": "parent_pipe",
                }
            ],
        )
        task = await mod.TaskEvaluateTool()._get_task_from_state("p1")
        assert task.id == "p1"
        assert task.title == "目标"
        assert task.description == "描述"
        assert task.status.value == "evaluating"
        assert task.metadata["acceptance_criteria"] == {"m1": {"input_params": {"path": "x"}}}
        assert task.metadata["evaluation"] == {"summary": "ok"}
        assert task.result is None
        # 血缘回填（GAP-1：父任务 id = 父管道 id）——save_task 落盘链消费
        assert task.parent_task_id == "parent_pipe"
        assert task.parent_pipeline_id == "parent_pipe"

    @pytest.mark.asyncio
    async def test_get_task_from_state_stub_survives_real_save_task(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """stub 字段完整性：state 行组装的 task 经真实 save_task 落盘不崩。

        既有红线（实测 A2）：占位 namespace 缺 parent_task_id（且非 dataclass，
        asdict 序列化即崩）会让评估死在保存路径。save_task 消费 id/
        parent_task_id（_find_root_id 根定位）与全字段 asdict 序列化，stub 必须
        是补全字段的真实 TaskModel。
        """
        from task_types import TaskModel

        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [
                {
                    "pipeline_id": "p1",
                    "task.goal": "目标",
                    "task.status": "evaluating",
                    "task.acceptance_criteria": {"m1": {"check": "exists"}},
                    "lineage.parent_pipeline_id": "parent_pipe",
                }
            ],
        )
        task = await mod.TaskEvaluateTool()._get_task_from_state("p1")
        assert isinstance(task, TaskModel)
        await service.save_task(task)  # 不抛即字段完整（含 _find_root_id/asdict 全链）
        stored = service.get_task("p1")
        assert stored is not None
        assert stored.metadata["acceptance_criteria"] == {"m1": {"check": "exists"}}
        yaml_path = tmp_path / "tasks" / "tree_p1" / "p1.yaml"
        assert yaml_path.exists(), "stub 应按根任务落盘（父坐标在 YAML 镜像无记录 → 截断为根）"

    @pytest.mark.asyncio
    async def test_get_task_from_state_unknown_status_falls_back_pending(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "p1", "task.status": "weird"}])
        task = await mod.TaskEvaluateTool()._get_task_from_state("p1")
        assert task.status.value == "pending"

    @pytest.mark.asyncio
    async def test_get_task_from_state_ws_meta_backfills_workspace_injection(
        self, mod: Any, monkeypatch: Any
    ) -> None:
        """ws_meta 数据源适配：state 行回填 metadata.ws_meta → workspace 注入生效。

        实测 A2 误判红线：stub 缺 ws_meta 时指标 workspace 不注入，file_check 在
        错误目录解析相对路径（「不存在: result.txt」误判失败）。
        """
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [
                {
                    "pipeline_id": "p1",
                    "task.status": "running",
                    "task.acceptance_criteria": {
                        "file_check": {"input_params": {"path": "result.txt", "check": "exists"}}
                    },
                    "task.ws_meta": {"mode": "isolated", "path": "D:/ws/p1"},
                }
            ],
        )
        tool = mod.TaskEvaluateTool()
        task = await tool._get_task_from_state("p1")
        assert task.metadata["ws_meta"] == {"mode": "isolated", "path": "D:/ws/p1"}
        params = tool._get_input_params(task)
        # 指标未配 criteria 不兜底：输入参数原样保留
        assert "criteria" not in params["file_check"]
        assert params["file_check"]["workspace"] == "D:/ws/p1"
        assert params["file_check"]["path"] == "result.txt"

    @pytest.mark.asyncio
    async def test_get_task_from_state_no_row_returns_none(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "other"}])
        assert await mod.TaskEvaluateTool()._get_task_from_state("p1") is None

    @pytest.mark.asyncio
    async def test_execute_prefers_state_row_over_yaml(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """GAP-1 统一：读面 state 优先（task = pipeline）。"""
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"]})
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": task.id, "task.status": "running", "task.acceptance_criteria": {"m1": {"input_params": {"criteria": "存在"}}}}],
        )
        monkeypatch.setattr(mod, "_state_writer", AsyncMock())
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        tool = mod.TaskEvaluateTool(executor=None)
        # 有指标但执行器未注入 → 降级错误（说明读面走了 state 行而非 YAML 任务）
        result = await tool.execute({"action": "auto_complete", "task_id": task.id})
        assert result.success is False
        assert result.error_code == "EVAL_ENGINE_UNAVAILABLE"


# ── 完成路径：合并门控与失败回退 ─────────────────────────────


class TestMergeGateAndCompletionPaths:
    @pytest.mark.asyncio
    async def test_merge_failure_marks_task_failed(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"]})
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, merge_result="worktree 合并失败: git merge conflict"
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is False
        assert "worktree 合并失败" in out.error
        assert "git merge conflict" in out.error
        assert out.metadata.get("task_failed") is True
        # 写面落了 failed 终态（职责边界：评估终态落 state）
        assert state_writer.await_count == 1
        assert state_writer.await_args.args[1]["task.status"] == "failed"
        # 富通知字段随终态落 state（富评估摘要/失败原因）
        assert state_writer.await_args.args[1]["task.eval_summary"] == "评估结果: 1/1 指标通过\n  ✅ PASS m1"
        assert state_writer.await_args.args[1]["task.error"] == "worktree 合并失败: git merge conflict"

    @pytest.mark.asyncio
    async def test_merge_failure_writer_raise_not_blocking(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"]})
        tool, _ = _inject_tool(mod, monkeypatch, service, merge_result="worktree 合并失败: merge fail")
        monkeypatch.setattr(
            mod,
            "_state_writer",
            AsyncMock(side_effect=RuntimeError("state down")),
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        # 写失败不阻断：仍返回合并失败结果
        assert out.success is False
        assert "worktree 合并失败" in out.error

    @pytest.mark.asyncio
    async def test_complete_evaluation_exception_surfaces(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service)
        tool, _ = _inject_tool(mod, monkeypatch, service)

        async def _boom(task_id: str, passed: bool, result: dict[str, Any] | None = None) -> None:
            raise RuntimeError("storage corrupt")

        monkeypatch.setattr(service, "complete_evaluation", _boom)
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is False
        assert "complete_evaluation(passed=True) 失败" in out.error
        assert out.metadata.get("eval_data") is not None

    @pytest.mark.asyncio
    async def test_already_completed_skips_state_writeback(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service)
        task.status = TaskStatus.COMPLETED
        service._storage.save(task)
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is True
        assert out.metadata["result"] == "completed"
        # 已完成任务跳过状态回写（不写 state、不重复 complete_evaluation）
        assert state_writer.await_count == 0

    @pytest.mark.asyncio
    async def test_failed_task_recovered_to_completed(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """FAILED 任务但评估通过 → recover_to_completed 恢复为完成。"""
        task = await _new_task(service)
        task.status = TaskStatus.FAILED
        service._storage.save(task)
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is True
        assert service.get_task(task.id).status.value == "completed"
        assert state_writer.await_count == 1
        assert state_writer.await_args.args[1]["task.status"] == "completed"

    @pytest.mark.asyncio
    async def test_fail_task_skips_terminal_status(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """任务已是终态（completed/failed）→ 跳过状态回写，仅返回失败结果。"""
        task = await _new_task(service)
        task.status = TaskStatus.COMPLETED
        service._storage.save(task)
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        out = await tool._fail_task(service, task, _eval_result(task.id, [_metric("m1", False)]), 3)
        assert out.success is True
        assert out.metadata["result"] == "failed"
        assert "重试次数耗尽" in out.metadata["message"]
        assert state_writer.await_count == 0

    @pytest.mark.asyncio
    async def test_fail_task_writer_exception_surfaces(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        task = await _new_task(service)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        monkeypatch.setattr(service, "complete_evaluation", AsyncMock(side_effect=RuntimeError("down")))
        out = await tool._fail_task(service, task, _eval_result(task.id, [_metric("m1", False)]), 3)
        assert out.success is False
        assert "complete_evaluation(passed=False) 失败" in out.error

    @pytest.mark.asyncio
    async def test_fail_task_writes_rich_state_fields(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """评估耗尽失败：state 落 failed 终态 + 评估结论/失败原因（富通知数据源）。"""
        task = await _new_task(service)
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        out = await tool._fail_task(
            service, task, _eval_result(task.id, [_metric("m1", False)]), 3
        )
        assert out.success is True
        assert state_writer.await_count == 1
        fields = state_writer.await_args.args[1]
        assert fields["task.status"] == "failed"
        # 富评估摘要（0.1 build_summary 同款：汇总 + 逐指标说明）
        assert fields["task.eval_summary"] == "评估结果: 0/1 指标通过\n  ❌ FAIL m1"
        assert fields["task.error"] == "评估未通过: 评估结果: 0/1 指标通过\n  ❌ FAIL m1"


# ── 合并门控：ws_meta 数据源解析与分发（机制层替身/真实判定）──


class TestMergeGate:
    @pytest.mark.asyncio
    async def test_read_task_ws_meta_from_state_rows(self, mod: Any, monkeypatch: Any) -> None:
        ws_meta = {"mode": "worktree", "path": "D:/wt", "project_root": "D:/src", "branch": "task/p1"}
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "p1", "ws_meta": ws_meta}])
        meta = await mod.TaskEvaluateTool()._read_task_ws_meta(SimpleNamespace(id="p1", metadata=None))
        assert meta == ws_meta

    @pytest.mark.asyncio
    async def test_read_task_ws_meta_json_string_row_restored(self, mod: Any, monkeypatch: Any) -> None:
        """聚合行 JSON 字符串形态（DB 投影原样存储）还原成 dict。"""
        import json as _json

        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "p1", "ws_meta": _json.dumps({"mode": "plain", "path": "/w"})}],
        )
        meta = await mod.TaskEvaluateTool()._read_task_ws_meta(SimpleNamespace(id="p1", metadata=None))
        assert meta == {"mode": "plain", "path": "/w"}

    @pytest.mark.asyncio
    async def test_read_task_ws_meta_prefers_task_key_mirror(self, mod: Any, monkeypatch: Any) -> None:
        """task.ws_meta（init 镜像，运行中即时可见）优先于出口键 ws_meta。"""
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [
                {
                    "pipeline_id": "p1",
                    "task.ws_meta": {"mode": "worktree", "path": "D:/live", "project_root": "D:/src"},
                    "ws_meta": {"mode": "plain", "path": "D:/stale"},
                }
            ],
        )
        meta = await mod.TaskEvaluateTool()._read_task_ws_meta(SimpleNamespace(id="p1", metadata=None))
        assert meta == {"mode": "worktree", "path": "D:/live", "project_root": "D:/src"}

    @pytest.mark.asyncio
    async def test_read_task_ws_meta_metadata_fallback_without_rows(self, mod: Any, monkeypatch: Any) -> None:
        """state 无行（读面未注入）→ task.metadata.ws_meta 兜底。"""
        monkeypatch.setattr(mod, "_state_reader", None)
        task = SimpleNamespace(id="p1", metadata={"ws_meta": {"mode": "plain", "path": "/m"}})
        meta = await mod.TaskEvaluateTool()._read_task_ws_meta(task)
        assert meta == {"mode": "plain", "path": "/m"}

    @pytest.mark.asyncio
    async def test_read_task_ws_meta_missing_returns_empty(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", None)
        meta = await mod.TaskEvaluateTool()._read_task_ws_meta(SimpleNamespace(id="p1", metadata=None))
        assert meta == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["plain", "shared"])
    async def test_gate_non_worktree_no_git_via_real_mechanism(
        self, mod: Any, monkeypatch: Any, mode: str
    ) -> None:
        """非 worktree：门控经真实机制层返回 None，且零 git 命令执行（0.1 判定）。"""

        def _no_git(self: Any, *args: Any, **kw: Any) -> tuple[int, str, str]:
            raise AssertionError(f"mode={mode} 不应执行任何 git 命令")

        monkeypatch.setattr(mod.worktree_merge.WorktreeMerger, "_run_git", _no_git)
        monkeypatch.setattr(
            mod, "_state_reader", lambda: [{"pipeline_id": "p1", "ws_meta": {"mode": mode, "path": "D:/w"}}]
        )
        result = await mod.TaskEvaluateTool()._try_merge_before_complete(SimpleNamespace(id="p1", metadata=None))
        assert result is None

    @pytest.mark.asyncio
    async def test_gate_worktree_delegates_with_state_ws_meta(self, mod: Any, monkeypatch: Any) -> None:
        captured: list[tuple[str, dict[str, Any]]] = []

        def fake_merge(task_id: str, ws_meta: dict[str, Any]) -> str | None:
            captured.append((task_id, ws_meta))
            return None

        monkeypatch.setattr(mod.worktree_merge, "merge_worktree_before_complete", fake_merge)
        ws_meta = {"mode": "worktree", "path": "D:/wt", "project_root": "D:/src", "branch": "task/p1"}
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "p1", "ws_meta": ws_meta}])
        result = await mod.TaskEvaluateTool()._try_merge_before_complete(SimpleNamespace(id="p1", metadata=None))
        assert result is None
        assert captured == [("p1", ws_meta)], "state 解析的 ws_meta 应原样透传机制层"

    @pytest.mark.asyncio
    async def test_gate_missing_ws_meta_fails_not_skips(self, mod: Any, monkeypatch: Any) -> None:
        """0.1 判定：ws_meta 拿不到 = 失败（worktree 产物不能静默丢失）。

        走真实机制层（不替身）：空 ws_meta 在机制入口即被判失败，绝不静默跳过。
        """
        monkeypatch.setattr(mod, "_state_reader", None)
        err = await mod.TaskEvaluateTool()._try_merge_before_complete(SimpleNamespace(id="t1", metadata=None))
        assert err is not None
        assert "t1" in err and "ws_meta" in err

    @pytest.mark.asyncio
    async def test_complete_task_plain_ws_meta_completes_through_real_gate(
        self, mod: Any, service: Any, monkeypatch: Any
    ) -> None:
        """plain 任务完成全链走真实门控与机制层（ws_meta=plain，零 git 调用）。"""

        def _no_git(self: Any, *args: Any, **kw: Any) -> tuple[int, str, str]:
            raise AssertionError("plain 模式不应执行任何 git 命令")

        monkeypatch.setattr(mod.worktree_merge.WorktreeMerger, "_run_git", _no_git)
        task = await _new_task(service)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        monkeypatch.setattr(mod, "_state_writer", AsyncMock())
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": task.id, "ws_meta": {"mode": "plain", "path": "D:/ws"}}],
        )
        out = await mod.TaskEvaluateTool()._complete_task(
            service, task, _eval_result(task.id, [_metric("m1", True)])
        )
        assert out.success is True
        assert out.metadata["result"] == "completed"


# ── 写面降级与保存 ───────────────────────────────────────────


class TestStateWriterDegrade:
    @pytest.mark.asyncio
    async def test_writer_not_injected_skips_write(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_writer", None)
        await mod._write_task_state("t1", {"task.status": "completed"})

    @pytest.mark.asyncio
    async def test_writer_exception_swallowed_with_warning(self, mod: Any, monkeypatch: Any, caplog: Any) -> None:
        async def boom(_pipeline_id: str, _fields: dict[str, Any]) -> None:
            raise RuntimeError("state down")

        monkeypatch.setattr(mod, "_state_writer", boom)
        with caplog.at_level("WARNING"):
            await mod._write_task_state("t1", {"task.status": "completed"})
        assert "state 写入失败" in caplog.text

    @pytest.mark.asyncio
    async def test_save_task_failure_propagates(self, mod: Any) -> None:
        """save_task 失败向上抛（去降级裁定）：不吞异常继续走。"""
        async def boom(_task: Any) -> None:
            raise RuntimeError("save fail")

        service = MagicMock()
        service.save_task = boom
        with pytest.raises(RuntimeError, match="save fail"):
            await mod.TaskEvaluateTool()._save_task(service, MagicMock())


# ── 辅助静态方法 ─────────────────────────────────────────────


class TestHelpers:
    def test_get_eval_timeout_priority_and_invalid(self, mod: Any) -> None:
        t1 = MagicMock()
        t1.metadata = {"eval_timeout": 42.5}
        assert mod.TaskEvaluateTool._get_eval_timeout(t1) == 42.5
        t2 = MagicMock()
        t2.metadata = {"eval_timeout": "oops"}
        assert mod.TaskEvaluateTool._get_eval_timeout(t2) == mod._DEFAULT_EVAL_TIMEOUT
        t3 = MagicMock()
        t3.metadata = None
        assert mod.TaskEvaluateTool._get_eval_timeout(t3) == mod._DEFAULT_EVAL_TIMEOUT

    def test_increment_eval_call_count(self, mod: Any) -> None:
        t1 = MagicMock()
        t1.metadata = None
        mod.TaskEvaluateTool._increment_eval_call_count(t1)
        mod.TaskEvaluateTool._increment_eval_call_count(t1)
        assert t1.metadata["eval_total_calls"] == 2
        t2 = MagicMock()
        t2.metadata = {"eval_total_calls": "5"}
        assert mod.TaskEvaluateTool._increment_eval_call_count(t2) == 1

    def test_all_metrics_passed_and_progress(self, mod: Any) -> None:
        task = MagicMock()
        task.metadata = {
            "evaluation_history": [
                {"metrics": [{"metric_id": "a", "passed": True}, {"metric_id": "b", "passed": False}]},
                {"metrics": [{"metric_id": "b", "passed": True}]},
            ]
        }
        assert mod.TaskEvaluateTool._all_metrics_passed(task, ["a", "b"]) is True
        assert mod.TaskEvaluateTool._all_metrics_passed(task, ["a", "b", "c"]) is False
        passed, remaining = mod.TaskEvaluateTool._get_eval_progress(task, ["a", "b", "c"])
        assert passed == 2
        assert remaining == ["c"]
        task.metadata = {"evaluation_history": "not-a-list"}
        assert mod.TaskEvaluateTool._all_metrics_passed(task, ["a"]) is False
        passed2, remaining2 = mod.TaskEvaluateTool._get_eval_progress(task, ["a"])
        assert passed2 == 0 and remaining2 == ["a"]

    def test_get_metric_ids_sources(self, mod: Any) -> None:
        tool = mod.TaskEvaluateTool()
        t1 = MagicMock()
        t1.metadata = {"evaluation_metric_ids": ["x", "y"]}
        assert tool._get_metric_ids(t1) == ["x", "y"]
        t2 = MagicMock()
        t2.metadata = {"acceptance_criteria": {"a": {}, "b": {}}}
        assert tool._get_metric_ids(t2) == ["a", "b"]
        t3 = MagicMock()
        t3.metadata = {}
        assert tool._get_metric_ids(t3) == []

    def test_register_eval_pipelines_no_root_skips(self, mod: Any, caplog: Any) -> None:
        service = MagicMock()
        service.get_root_task_id.return_value = None
        with caplog.at_level("DEBUG"):
            mod.TaskEvaluateTool._register_eval_pipelines(service, MagicMock(), _eval_result("x", []))
        assert "无 root_id" in caplog.text

    def test_append_eval_history_records_all_fields(self, mod: Any) -> None:
        task = MagicMock()
        task.metadata = {}
        r = _eval_result(
            "t1",
            [
                _metric(
                    "m1",
                    True,
                    score=95.0,
                    message="msg",
                    details={"failed_conditions": ["c1"]},
                    evaluator_input={"path": "x"},
                    evaluator_output={"issues": ["i1"]},
                    pipeline_run_id="eval1",
                )
            ],
            summary="s",
        )
        mod.TaskEvaluateTool._append_eval_history(task, r)
        entry = task.metadata["evaluation_history"][-1]
        # compute_overall 在构造时重算 summary：全过 → "全部 N 项指标通过"
        assert entry["passed"] is True and entry["summary"] == "全部 1 项指标通过"
        m = entry["metrics"][0]
        assert m["metric_id"] == "m1" and m["passed"] is True and m["score"] == 95.0
        assert m["message"] == "msg" and m["error"] is None
        assert m["details"] == {"failed_conditions": ["c1"]}
        assert m["evaluator_input"] == {"path": "x"}
        assert m["evaluator_output"] == {"issues": ["i1"]}
        assert m["pipeline_run_id"] == "eval1"

    def test_append_eval_history_resets_non_list_history(self, mod: Any) -> None:
        task = MagicMock()
        task.metadata = {"evaluation_history": "junk"}
        mod.TaskEvaluateTool._append_eval_history(task, _eval_result("t1", []))
        assert isinstance(task.metadata["evaluation_history"], list)
        assert len(task.metadata["evaluation_history"]) == 1

    def test_build_result_data_passed_and_failed_branches(self, mod: Any) -> None:
        result = _eval_result(
            "t1",
            [
                _metric("ok_m", passed=True),
                _metric(
                    "bad_m",
                    passed=False,
                    score=3.0,
                    message="文件不对",
                    error="expected",
                    evaluator_output={
                        "issues": ["a"],
                        "suggestions": ["fix"],
                        "report_path": "/tmp/report.md",
                    },
                    details={"failed_conditions": ["c1"]},
                    evaluator_input={"path": "D:/proj/x.txt"},
                    pipeline_run_id="evalP",
                ),
            ],
        )
        data = mod.TaskEvaluateTool()._build_result_data(result)
        assert data["task_id"] == "t1"
        assert data["overall_passed"] is False
        passed_metric = next(m for m in data["metrics"] if m["metric_id"] == "ok_m")
        bad_metric = next(m for m in data["metrics"] if m["metric_id"] == "bad_m")
        assert passed_metric == {"metric_id": "ok_m", "passed": True}
        assert bad_metric["score"] == 3.0 and bad_metric["message"] == "文件不对"
        assert bad_metric["issues"] == ["a"] and bad_metric["suggestions"] == ["fix"]
        # report_path 经 sanitize 脱敏：独立绝对路径前缀不出现，保留文件名供定位
        assert bad_metric["report_path"] == "report.md" or bad_metric["report_path"].endswith("report.md")
        assert bad_metric["failed_conditions"] == ["c1"]
        assert bad_metric["pipeline_run_id"] == "evalP"
        assert bad_metric["error"] == "expected"

    def test_build_rich_summary_includes_agent_feedback(self, mod: Any) -> None:
        """富评估摘要（0.1 build_summary 同款）：汇总 + 逐指标说明。

        agent 型评估的 message = 评估 Agent 提交的 feedback 总结；脚本化评估
        message = 检查结果说明。无 message 的指标只列状态。
        """
        result = _eval_result(
            "t1",
            [
                _metric("ok_m", passed=True, message="功能实现完整，符合验收标准"),
                _metric("bad_m", passed=False, message="边界条件未覆盖"),
                _metric("no_msg", passed=True),
            ],
        )
        summary = mod.TaskEvaluateTool._build_rich_summary(result)
        assert summary == (
            "评估结果: 2/3 指标通过\n"
            "  ✅ PASS ok_m: 功能实现完整，符合验收标准\n"
            "  ❌ FAIL bad_m: 边界条件未覆盖\n"
            "  ✅ PASS no_msg"
        )

    def test_build_rich_summary_empty_metrics_falls_back_to_summary(self, mod: Any) -> None:
        """空指标结果（"直接通过"分支）→ 回退 result.summary（LLM 提交的总结）。"""
        # compute_overall 空 results 生成"无评估指标"
        result = _eval_result("t1", [])
        assert mod.TaskEvaluateTool._build_rich_summary(result) == "无评估指标"
        # 直接通过分支 EvalResult 的 summary = LLM 提交的总结
        stub = type(
            "EvalResult",
            (),
            {
                "task_id": "t1",
                "overall_passed": True,
                "summary": "已完成所有工具测试，报告见 docs/working/tool_test_report.md",
                "results": [],
            },
        )()
        assert mod.TaskEvaluateTool._build_rich_summary(stub) == "已完成所有工具测试，报告见 docs/working/tool_test_report.md"


# ── 输入参数组装（含模板替换） ───────────────────────────────


class TestInputParams:
    def test_top_level_and_input_params_sources(self, mod: Any) -> None:
        task = MagicMock()
        task.id = "t-1"
        task.description = "做点事"
        task.metadata = {
            "evaluation_metric_ids": ["a", "b"],
            "acceptance_criteria": {
                "a": {"input_params": {"path": "src/a.py"}},
                "b": {"path": "src/b.py", "expected_output": "忽略我", "pass_threshold": 0.9},
            },
        }
        tool = mod.TaskEvaluateTool()
        params = tool._get_input_params(task)
        assert params["a"]["path"] == "src/a.py"
        assert params["b"]["path"] == "src/b.py"
        assert "expected_output" not in params["b"] and "pass_threshold" not in params["b"]
        # 未配置 criteria 不兜底：原样保留，不拿任务描述顶替
        assert "criteria" not in params["a"] and "criteria" not in params["b"]

    def test_workspace_and_templates_injected(self, mod: Any, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        task = MagicMock()
        task.id = "t-9"
        task.description = ""
        task.title = "标题兜底"
        task.metadata = {
            "evaluation_metric_ids": ["m1"],
            "ws_meta": {"path": str(ws)},
            "acceptance_criteria": {"m1": {"input_params": {"criteria": "检查 {{workspace}} 与 {{task_id}}"}}},
        }
        tool = mod.TaskEvaluateTool()
        params = tool._get_input_params(task)
        p = params["m1"]
        assert p["workspace"] == str(ws)
        assert str(ws) in p["criteria"] and "t-9" in p["criteria"]
        # 非字符串参数原样保留
        task.metadata["acceptance_criteria"]["m1"]["input_params"]["min_size"] = 5
        params2 = tool._get_input_params(task)
        assert params2["m1"]["min_size"] == 5

    def test_resolve_tool_id_candidates(self, mod: Any, tmp_path: Path) -> None:
        tools_dir = tmp_path / "src" / "tools" / "builtin"
        tools_dir.mkdir(parents=True)
        for name in ["zeta.py", "alpha.py", "test_skip.py", "__init__.py"]:
            (tools_dir / name).write_text("x", encoding="utf-8")
        got = mod.TaskEvaluateTool._resolve_tool_id_candidates(str(tmp_path))
        assert got == ["alpha", "zeta"]
        assert mod.TaskEvaluateTool._resolve_tool_id_candidates(None) == []
        assert mod.TaskEvaluateTool._resolve_tool_id_candidates(str(tmp_path / "no-dir")) == []


# ── 模块级默认执行器注入点 ───────────────────────────────────


class TestDefaultExecutorInjection:
    def test_set_get_default_executor(self, mod: Any) -> None:
        fake = object()
        mod.set_default_executor(fake)
        assert mod._default_executor is fake
        mod.set_default_executor(None)
        assert mod._default_executor is None


# ── 工具定义与 task_evaluate_func 助手 ────────────────────────


class TestToolDefinition:
    def test_definition_contract(self, mod: Any) -> None:
        tool = mod.TaskEvaluateTool.get_tool_definition()
        assert tool.name == "task_evaluate"
        assert tool.category.value == "task"
        assert tool.level.value == "system"
        assert "evaluate_single" in tool.input_schema["properties"]["action"]["enum"]
        assert "auto_complete" in tool.input_schema["properties"]["action"]["enum"]
        assert tool.input_schema["properties"]["action"]["default"] == "auto_complete"
        assert tool.injected_params == ["session_id", "user_id", "tool_record_id", "task_id"]

    def test_get_task_service_delegates_to_service_access(self, mod: Any, monkeypatch: Any) -> None:
        import service_access

        fake = object()
        monkeypatch.setattr(service_access, "get_task_service", lambda: fake)
        assert mod.TaskEvaluateTool()._get_task_service() is fake


class TestTaskEvaluateFunc:
    """task_evaluate_func 同步助手：校验 + 真实服务调用 + 状态推进。"""

    @pytest.fixture(autouse=True)
    def patch_service_access(self, monkeypatch: Any) -> Any:
        import service_access

        holder: dict[str, Any] = {"service": None}

        def _get() -> Any:
            return holder["service"]

        monkeypatch.setattr(service_access, "get_task_service", _get)
        return holder

    @pytest.mark.asyncio
    async def test_missing_action_and_task_id(self, mod: Any) -> None:
        assert await mod.task_evaluate_func({}) == {
            "success": False,
            "error_code": "MISSING_ACTION",
            "error": "缺少 action 参数",
        }
        assert await mod.task_evaluate_func({"action": "auto_complete"}) == {
            "success": False,
            "error_code": "MISSING_TASK_ID",
            "error": "缺少 task_id 参数",
        }

    @pytest.mark.asyncio
    async def test_invalid_action(self, mod: Any) -> None:
        out = await mod.task_evaluate_func({"action": "bogus", "task_id": "t1"})
        assert out["error_code"] == "INVALID_ACTION"

    @pytest.mark.asyncio
    async def test_service_unavailable(self, mod: Any, patch_service_access: Any) -> None:
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1"})
        assert out["error_code"] == "SERVICE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_service_unavailable_when_get_raises(self, mod: Any, monkeypatch: Any) -> None:
        import service_access

        def _boom() -> Any:
            raise RuntimeError("service init failed")

        monkeypatch.setattr(service_access, "get_task_service", _boom)
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1"})
        assert out["error_code"] == "SERVICE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_task_not_found(self, mod: Any, patch_service_access: Any) -> None:
        service = MagicMock()
        service.get_task.return_value = None
        patch_service_access["service"] = service
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "ghost"})
        assert out["error_code"] == "TASK_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(self, mod: Any, patch_service_access: Any) -> None:
        task = MagicMock()
        task.status = TaskStatus.PENDING
        service = MagicMock()
        service.get_task.return_value = task
        patch_service_access["service"] = service
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1"})
        assert out["error_code"] == "INVALID_STATUS"

    @pytest.mark.asyncio
    async def test_running_task_moves_to_evaluating_then_completes(
        self, mod: Any, patch_service_access: Any, monkeypatch: Any
    ) -> None:
        task = MagicMock()
        task.status = TaskStatus.RUNNING
        task.result = None
        task.metadata = {"ws_meta": {"mode": "plain", "path": "C:/ws/t1"}}
        service = MagicMock()
        service.get_task.return_value = task
        service.move_to_evaluating = AsyncMock()
        service.complete_evaluation = AsyncMock()
        patch_service_access["service"] = service
        state_writer = AsyncMock()
        monkeypatch.setattr(mod, "_state_writer", state_writer)
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1", "result": {"summary": "x"}})
        assert out == {"success": True, "status": "completed"}
        assert task.result == {"summary": "x"}
        assert service.move_to_evaluating.await_count == 1
        assert service.complete_evaluation.await_count == 1
        # 状态推进两跳都落 state：evaluating → completed（含 ended_at）
        fields_calls = [c.args[1] for c in state_writer.await_args_list]
        assert [f["task.status"] for f in fields_calls] == ["evaluating", "completed"]
        assert "task.ended_at" in fields_calls[-1]

    @pytest.mark.asyncio
    async def test_move_to_evaluating_failure_not_blocking(
        self, mod: Any, patch_service_access: Any, monkeypatch: Any
    ) -> None:
        task = MagicMock()
        task.status = TaskStatus.RUNNING
        task.metadata = {"ws_meta": {"mode": "plain", "path": "C:/ws/t1"}}
        service = MagicMock()
        service.get_task.return_value = task
        service.move_to_evaluating = AsyncMock(side_effect=RuntimeError("transition invalid"))
        service.complete_evaluation = AsyncMock()
        patch_service_access["service"] = service
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1"})
        assert out["success"] is True
        # 状态推进失败被吞（不阻断评估主流程），但完成回写照常
        assert service.complete_evaluation.await_count == 1

    @pytest.mark.asyncio
    async def test_complete_evaluation_failure_returns_eval_failed(
        self, mod: Any, patch_service_access: Any
    ) -> None:
        task = MagicMock()
        task.status = TaskStatus.EVALUATING
        task.metadata = {"ws_meta": {"mode": "plain", "path": "C:/ws/t1"}}
        service = MagicMock()
        service.get_task.return_value = task
        service.complete_evaluation = AsyncMock(side_effect=RuntimeError("write failed"))
        patch_service_access["service"] = service
        out = await mod.task_evaluate_func({"action": "evaluate_single", "task_id": "t1"})
        assert out["success"] is False
        assert out["error_code"] == "EVAL_FAILED"

    @pytest.mark.asyncio
    async def test_worktree_merge_gate_failure_marks_failed(
        self, mod: Any, patch_service_access: Any, monkeypatch: Any, tmp_path: Any
    ) -> None:
        """回归（2026-09-04 裁定）：worktree 合并门控失败 → 任务标记 failed，
        禁止绕门置完成（产物会留在未合并副本里静默丢失）。"""
        task = MagicMock()
        task.status = TaskStatus.EVALUATING
        task.metadata = {"ws_meta": {"mode": "worktree", "path": str(tmp_path / "nope_wt")}}
        service = MagicMock()
        service.get_task.return_value = task
        service.complete_evaluation = AsyncMock()
        patch_service_access["service"] = service
        state_writer = AsyncMock()
        monkeypatch.setattr(mod, "_state_writer", state_writer)
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1"})
        assert out["success"] is False
        assert out["error_code"] == "MERGE_GATE_FAILED"
        # passed=False 落库（任务标 failed），状态写含 failed 与错误
        assert service.complete_evaluation.await_count == 1
        assert service.complete_evaluation.call_args.kwargs.get("passed") is False
        assert state_writer.await_count == 1
        assert state_writer.await_args.args[1]["task.status"] == "failed"

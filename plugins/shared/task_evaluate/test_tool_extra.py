# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 工具层（tool.py）补充单测。

与 tests/plugins/tools/shared/test_task_evaluate_migration.py 互补：既有测试覆盖
迁移面/校验面/基本编排/执行器未注入降级；本文件补齐未覆盖分支——

- task_id 短 id 解析：前缀唯一命中、歧义报错、读取异常降级、非 str 原样；
- state 聚合读面：_get_task_from_state 组装真实 TaskModel（状态枚举解析/坏状态
  回退/无 acceptance_criteria 行/血缘回填/stub 经真实 save_task 落盘不崩）、
  _read_state_rows 未注入/异常/非列表降级；
- 单指标模式：summary 透传、超时 EVAL_TIMEOUT、litellm 限速 RATE_LIMITED、
  一般异常 EVAL_FAILED、部分通过 partial_pass、全部通过自动完成；
- 评估结果处理：unrecoverable 模式耗尽、失败计数耗尽 FAILED、通过重置计数、
  完整 retry 反馈（剩余次数/失败明细/得分）；
- 完成/失败路径：worktree 合并失败标记 failed、终态写 state 未落账显式失败
  （不谎报终态）、已 failed 任务恢复为完成、终态任务跳过回写；
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
_TASKS_DIR = _TE_DIR.parents[0] / "tasks"
# 共享根模块（state_fields 经共享根；worktree_merge 为本目录平铺模块）：
# 单文件直跑时也自足解析
_SHARED_DIR = _TE_DIR.parents[0]
# system/（isolation.workspace：合并门控哨兵测试注入判定 base 用）
_SYSTEM_DIR = _TE_DIR.parents[0] / "system"

for _d in (_TE_DIR, _TASKS_DIR, _SHARED_DIR, _SYSTEM_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from task_types import TaskStatus  # noqa: E402 — 依赖上方 sys.path 注入


def _load_module() -> Any:
    mod_name = "task_eval_tool_extra_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TE_DIR / "tool.py")
    assert spec is not None, "cannot load tool.py"
    assert spec.loader is not None, "cannot load tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


@pytest.fixture
def service(tmp_path: Path) -> Any:
    """真实 TaskService（内存 + 临时 YAML 目录），task_id=None 门面模式。

    导入前把 tasks 插件目录强制置顶（共跑时别插件同名 service.py 可能占位）。
    """
    _s = str(_TASKS_DIR)
    while _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)
    # 逐出已缓存的裸名槽位：共跑时 security_check 等插件的 service.py 可能已
    # 占位，仅置顶 path 不够（sys.modules 命中优先）。task_evaluate 的 tool.py
    # 不导入裸名 service（只按文件载入），故逐出不影响其类身份。
    sys.modules.pop("service", None)
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
        monkeypatch.setattr(
            mod, "_state_reader", lambda: [{"pipeline_id": "p1", "task.id": "p1", "task.status": "weird"}]
        )
        task = await mod.TaskEvaluateTool()._get_task_from_state("p1")
        assert task.status.value == "pending"

    @pytest.mark.asyncio
    async def test_get_task_from_state_rejects_session_residue_row(
        self, mod: Any, monkeypatch: Any
    ) -> None:
        """行解析身份门（ADR 2026-11-06 D5 N8）：无 task.id 且无 lineage.* 的行
        （会话主管道 + 幽灵残留）不解析为任务对象 → None（调用方按结构化
        not found 处理）——评估裁决不得把会话残留当任务评估。"""
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [
                {"pipeline_id": "pipe_chat", "task.status": "failed", "task.goal": "幽灵"},
                # 对照：真任务行（task.id 在场）照常解析
                {"pipeline_id": "p-real", "task.id": "p-real", "task.status": "running"},
            ],
        )
        tool = mod.TaskEvaluateTool()
        assert await tool._get_task_from_state("pipe_chat") is None, (
            "会话残留行不得解析为任务对象"
        )
        task = await tool._get_task_from_state("p-real")
        assert task is not None, "真任务行解析不受影响"
        assert task.id == "p-real", "真任务行解析不受影响"

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
                    "task.id": "p1",
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
    async def test_auto_complete_runs_tool_metric_with_command(
        self, mod: Any, service: Any, monkeypatch: Any
    ) -> None:
        """bash_check 带 command（无 criteria 键）不得走「未配置 criteria 直通」，
        必须落执行器真跑（SWE 种子实跑实证：直通分支让 oracle 免检放行，
        判定失真——ADR 2026-09-16-external-dataset-sourcing 执行期发现）。"""
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["bash_check"]})
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [
                {
                    "pipeline_id": task.id,
                    "task.status": "running",
                    "task.acceptance_criteria": {
                        "bash_check": {"input_params": {"command": "pytest -q"}}
                    },
                    "task.ws_meta": {"mode": "plain", "path": "D:/ws/x"},
                }
            ],
        )
        state_writer = AsyncMock()
        monkeypatch.setattr(mod, "_state_writer", state_writer)
        monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda self: service)
        monkeypatch.setattr(
            mod.worktree_merge, "merge_worktree_before_complete",
            lambda task_id, ws_meta: None,
        )
        executor = MagicMock()
        executor.run_evaluation = AsyncMock(return_value=_eval_result(
            task.id, [_metric("bash_check", True)]))
        tool = mod.TaskEvaluateTool(executor=executor)
        await tool.execute({"action": "auto_complete", "task_id": task.id})
        executor.run_evaluation.assert_awaited_once()
        call = executor.run_evaluation.await_args
        ids = call.kwargs.get("metric_ids") or call.args[1]
        assert "bash_check" in ids

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
    async def test_completion_state_write_failure_surfaces(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """终态写 state 未落账 → 显式失败结果（不谎报完成；state 是唯一写面，
        落账与否承重，ADR 2026-10-08-eval-state-single-write）。"""
        task = await _new_task(service)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        monkeypatch.setattr(mod, "_state_writer", None)
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is False
        assert out.error_code == "EVAL_STATE_WRITE_FAILED"
        assert out.metadata.get("task_failed") is True

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
        """FAILED 任务但评估通过、合并门控通过 → 终态 completed 落 state 单键
        （恢复同样是"谁终态谁随写"，YAML 状态机写面已退役）。"""
        task = await _new_task(service)
        task.status = TaskStatus.FAILED
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is True
        assert state_writer.await_count == 1
        fields = state_writer.await_args.args[1]
        assert fields["task.status"] == "completed"
        assert "task.ended_at" in fields
        assert fields["task.evaluation"]["overall_passed"] is True

    @pytest.mark.asyncio
    async def test_completion_result_carries_terminal_fields(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """完成成功结果携带终态投影字段（terminal_fields，task.* 标量）。

        评估完成写面（pipeline-state.update）只落 DB/registry，引擎在飞
        state 当轮不同步——run.completed 终态载荷带陈旧未决 task.status，
        任务域事件派生（未决态不派生）被拦、父通知断。完成结果携带终态
        字段，由 tool_core 投影进在飞 state，终态载荷/收尾投影/派生三面
        同源。"""
        task = await _new_task(service)
        tool, state_writer = _inject_tool(mod, monkeypatch, service)
        monkeypatch.setattr(
            mod.worktree_merge,
            "last_collision_report",
            lambda: [{"path": "docs/a.md", "action": "backup"}],
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is True
        fields = out.output["terminal_fields"]
        assert fields["task.status"] == "completed"
        # 合并备注随终态投影（2026-10-23 裁定：完成通知必须告知上级对账）
        assert fields["task.merge_notes"] == "docs/a.md→backup"
        # 与落库写面同值（同批终态，两面一致）
        written = state_writer.await_args.args[1]
        assert fields["task.ended_at"] == written["task.ended_at"]
        assert fields["task.eval_summary"] == written["task.eval_summary"]
        # 大对象不进在飞 state
        assert "task.evaluation" not in fields
        # 合并备注同批落 DB（对账信息不只在通知面）
        assert written["task.merge_notes"] == "docs/a.md→backup"

    @pytest.mark.asyncio
    async def test_recovered_completion_carries_terminal_fields(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """FAILED 恢复完成同样携带终态投影（恢复=谁终态谁随写）。"""
        task = await _new_task(service)
        task.status = TaskStatus.FAILED
        tool, _ = _inject_tool(mod, monkeypatch, service)
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is True
        assert out.output["terminal_fields"]["task.status"] == "completed"

    @pytest.mark.asyncio
    async def test_merge_failure_has_no_terminal_fields(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """合并门控失败不投影终态——任务保持可重试，不假完成。"""
        task = await _new_task(service)
        tool, _ = _inject_tool(mod, monkeypatch, service, merge_result="worktree 合并失败: conflict")
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is False
        assert not (out.output or {}).get("terminal_fields")

    @pytest.mark.asyncio
    async def test_recover_writer_exception_surfaces_honest(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """恢复完成写 state 抛异常 → 兜底为显式失败（task 实际仍 failed）。"""
        task = await _new_task(service)
        task.status = TaskStatus.FAILED
        tool, _ = _inject_tool(mod, monkeypatch, service)

        async def _boom(_pipeline_id: str, _fields: dict[str, Any]) -> None:
            raise RuntimeError("state down")

        monkeypatch.setattr(mod, "_state_writer", _boom)
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is False
        assert out.error_code == "RECOVER_TO_COMPLETED_FAILED"
        assert out.metadata.get("task_failed") is True

    @pytest.mark.asyncio
    async def test_recover_result_build_exception_surfaces_honest(
        self, mod: Any, service: Any, monkeypatch: Any
    ) -> None:
        """恢复完成时评估结果构建抛异常 → 终态写 try 兜底显式失败（不谎报）。"""
        task = await _new_task(service)
        task.status = TaskStatus.FAILED
        tool, _ = _inject_tool(mod, monkeypatch, service)
        monkeypatch.setattr(
            mod.TaskEvaluateTool, "_build_result_data",
            lambda self, _result: (_ for _ in ()).throw(RuntimeError("result boom")),
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is False
        assert out.error_code == "RECOVER_TO_COMPLETED_FAILED"
        assert out.metadata.get("task_failed") is True

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
    async def test_fail_task_state_write_failure_surfaces(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """评估耗尽终态写 state 未落账 → 显式失败（任务实际仍非 failed）。"""
        task = await _new_task(service)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        monkeypatch.setattr(mod, "_state_writer", None)
        out = await tool._fail_task(service, task, _eval_result(task.id, [_metric("m1", False)]), 3)
        assert out.success is False
        assert out.error_code == "EVAL_STATE_WRITE_FAILED"

    @pytest.mark.asyncio
    async def test_fail_task_writer_exception_surfaces_honest(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """写面抛异常 → 兜底为显式失败结果（异常在 _write_task_state 内转
        False 后由落账检查捕获，不谎报 failed 已落账）。"""
        task = await _new_task(service)
        tool, _ = _inject_tool(mod, monkeypatch, service)

        async def _boom(_pipeline_id: str, _fields: dict[str, Any]) -> None:
            raise RuntimeError("state down")

        monkeypatch.setattr(mod, "_state_writer", _boom)
        out = await tool._fail_task(service, task, _eval_result(task.id, [_metric("m1", False)]), 3)
        assert out.success is False
        assert "评估耗尽终态写 state 失败" in out.error

    @pytest.mark.asyncio
    async def test_fail_task_unexpected_exception_surfaces_honest(
        self, mod: Any, service: Any, monkeypatch: Any
    ) -> None:
        """终态写面外的意外异常（如摘要构建崩）→ 外层兜底显式失败，不谎报。"""
        task = await _new_task(service)
        tool, _ = _inject_tool(mod, monkeypatch, service)
        monkeypatch.setattr(
            mod.TaskEvaluateTool, "_build_rich_summary",
            lambda self, _result: (_ for _ in ()).throw(RuntimeError("summary boom")),
        )
        out = await tool._fail_task(service, task, _eval_result(task.id, [_metric("m1", False)]), 3)
        assert out.success is False
        assert "评估耗尽终态写 state 失败" in out.error

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

    @pytest.mark.asyncio
    async def test_history_state_write_failure_warns_not_lies(
        self, mod: Any, service: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        """评估历史/计数落 state 失败（写面异常）→ warning 留痕 + 重试反馈照常
        返回（历史未落账 = 下轮进度判定退化为全量重评，重评安全不死循环）。"""
        task = await _new_task(service, metadata={"evaluation_metric_ids": ["m1"]})
        tool, _ = _inject_tool(mod, monkeypatch, service)

        async def _boom(_pipeline_id: str, _fields: dict[str, Any]) -> None:
            raise RuntimeError("state down")

        monkeypatch.setattr(mod, "_state_writer", _boom)
        result = _eval_result(task.id, [_metric("m1", False, message="继续改进")])
        with caplog.at_level("WARNING"):
            out = await tool._handle_evaluation_result({"action": "auto_complete"}, service, task, result)
        assert out.success is True
        assert out.metadata["result"] == "retry"
        assert any("评估历史/计数落 state 失败" in r.message for r in caplog.records)


# ── 合并门控：ws_meta 数据源解析与分发（机制层替身/真实判定）──


class TestMergeGate:
    @pytest.mark.asyncio
    async def test_read_task_ws_meta_from_state_rows(self, mod: Any, monkeypatch: Any) -> None:
        ws_meta = {"mode": "worktree", "path": "D:/wt", "project_root": "D:/src", "branch": "task/p1"}
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "p1", "task.ws_meta": ws_meta}])
        meta = await mod.TaskEvaluateTool()._read_task_ws_meta(SimpleNamespace(id="p1", metadata=None))
        assert meta == ws_meta

    @pytest.mark.asyncio
    async def test_read_task_ws_meta_json_string_row_restored(self, mod: Any, monkeypatch: Any) -> None:
        """聚合行 JSON 字符串形态（DB 投影原样存储）还原成 dict。"""
        import json as _json

        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "p1", "task.ws_meta": _json.dumps({"mode": "plain", "path": "/w"})}],
        )
        meta = await mod.TaskEvaluateTool()._read_task_ws_meta(SimpleNamespace(id="p1", metadata=None))
        assert meta == {"mode": "plain", "path": "/w"}

    @pytest.mark.asyncio
    async def test_read_task_ws_meta_single_key_ignores_retired_exit_key(self, mod: Any, monkeypatch: Any) -> None:
        """单键单通道：行内同置退役出口键 ws_meta（历史行）与 task.ws_meta →
        只读单键值（区分度：两值不同，读到 stale 即说明还在走多路）。"""
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
    async def test_read_task_ws_meta_retired_exit_key_alone_returns_empty(self, mod: Any, monkeypatch: Any) -> None:
        """退役出口键单独在场（历史行）不再被消费 → 空（门控按失败裁决）。"""
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "p1", "ws_meta": {"mode": "plain", "path": "D:/legacy"}}],
        )
        meta = await mod.TaskEvaluateTool()._read_task_ws_meta(SimpleNamespace(id="p1", metadata=None))
        assert meta == {}

    @pytest.mark.asyncio
    async def test_read_task_ws_meta_no_metadata_fallback(self, mod: Any, monkeypatch: Any) -> None:
        """YAML 通道退役：state 无行时 task.metadata.ws_meta 不再兜底 → 空。"""
        monkeypatch.setattr(mod, "_state_reader", None)
        task = SimpleNamespace(id="p1", metadata={"ws_meta": {"mode": "plain", "path": "/m"}})
        meta = await mod.TaskEvaluateTool()._read_task_ws_meta(task)
        assert meta == {}

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
            mod, "_state_reader", lambda: [{"pipeline_id": "p1", "task.ws_meta": {"mode": mode, "path": "D:/w"}}]
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
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "p1", "task.ws_meta": ws_meta}])
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
        assert "t1" in err
        assert "ws_meta" in err

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "project_rel",
        ["ai_ws/sessions/thread-abc", "ai_ws/sessions/thread-abc/docs", "real_repo"],
        ids=["session_dir", "session_nested", "real_repo"],
    )
    async def test_gate_merges_regardless_of_project_root_world(
        self, mod: Any, monkeypatch: Any, tmp_path: Path, project_rel: str
    ) -> None:
        """锚点自主权（2026-09-19 用户裁决）：合并落点由 agent 自主选择，
        会话工作区锚定与真实仓库锚定同权——门控只管合并机制本身，不裁判
        落点世界，project_root 原样透传机制层。"""

        captured: list[str] = []

        def fake_merge(task_id: str, ws_meta: dict[str, Any]) -> str | None:
            captured.append(str(ws_meta.get("project_root")))
            return None

        monkeypatch.setattr(mod.worktree_merge, "merge_worktree_before_complete", fake_merge)
        ws_base = tmp_path / "ai_ws"
        import isolation.workspace as _ws_pkg

        monkeypatch.setattr(_ws_pkg, "get_workspace_base_dir", lambda: ws_base)
        project_root = tmp_path / project_rel
        project_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [
                {
                    "pipeline_id": "p1",
                    "task.ws_meta": {
                        "mode": "worktree",
                        "path": str(tmp_path / "wt"),
                        "project_root": str(project_root),
                        "branch": "task/p1",
                    },
                }
            ],
        )
        result = await mod.TaskEvaluateTool()._try_merge_before_complete(SimpleNamespace(id="p1", metadata=None))
        assert result is None
        assert captured == [str(project_root)]

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
            lambda: [{"pipeline_id": task.id, "task.ws_meta": {"mode": "plain", "path": "D:/ws"}}],
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
    async def test_evaluation_paths_never_persist_yaml_shadow(self, mod: Any, service: Any, monkeypatch: Any) -> None:
        """评估链去 YAML 影子写（F5）：save_task 抛错也不影响完成——评估链
        对 YAML 存储零写依赖，状态/结果/历史全走 state 单键。"""
        task = await _new_task(service)
        tool, state_writer = _inject_tool(mod, monkeypatch, service)

        service.save_task = AsyncMock(side_effect=RuntimeError("save fail"))
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": task.id, "task.ws_meta": {"mode": "plain", "path": "D:/ws"}}],
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is True
        assert state_writer.await_args.args[1]["task.status"] == "completed"
        service.save_task.assert_not_awaited()


class TestReadRowTaskWsMetaModuleHelper:
    """模块级单键读取（task_evaluate_func 数据源）：降级面四态 + 命中两形态。"""

    @pytest.mark.asyncio
    async def test_reader_raise_returns_empty(self, mod: Any, monkeypatch: Any) -> None:
        def _boom() -> list:
            raise RuntimeError("bridge down")

        monkeypatch.setattr(mod, "_state_reader", _boom)
        assert await mod._read_row_task_ws_meta("t1") == {}

    @pytest.mark.asyncio
    async def test_non_list_rows_returns_empty(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", lambda: "junk")
        assert await mod._read_row_task_ws_meta("t1") == {}

    @pytest.mark.asyncio
    async def test_row_absent_returns_empty(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "other"}])
        assert await mod._read_row_task_ws_meta("t1") == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value",
        [{"mode": "plain", "path": "/w"}, '{"mode": "plain", "path": "/w"}'],
        ids=["dict_form", "json_string_form"],
    )
    async def test_hit_single_key_both_forms(self, mod: Any, monkeypatch: Any, value: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", lambda: [{"pipeline_id": "t1", "task.ws_meta": value}])
        meta = await mod._read_row_task_ws_meta("t1")
        assert meta == {"mode": "plain", "path": "/w"}

    @pytest.mark.asyncio
    async def test_async_reader_awaited(self, mod: Any, monkeypatch: Any) -> None:
        """async 读面（生产 capability 包装形态）照常 await。"""

        async def _read() -> list:
            return [{"pipeline_id": "t1", "task.ws_meta": {"mode": "plain", "path": "/w"}}]

        monkeypatch.setattr(mod, "_state_reader", _read)
        assert await mod._read_row_task_ws_meta("t1") == {"mode": "plain", "path": "/w"}

    @pytest.mark.asyncio
    async def test_reader_not_injected_returns_empty(self, mod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(mod, "_state_reader", None)
        assert await mod._read_row_task_ws_meta("t1") == {}


# ── 辅助静态方法 ─────────────────────────────────────────────# ── 辅助静态方法 ─────────────────────────────────────────────


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
        assert passed2 == 0
        assert remaining2 == ["a"]

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
        assert entry["passed"] is True
        assert entry["summary"] == "全部 1 项指标通过"
        m = entry["metrics"][0]
        assert m["metric_id"] == "m1"
        assert m["passed"] is True
        assert m["score"] == 95.0
        assert m["message"] == "msg"
        assert m["error"] is None
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
        assert bad_metric["score"] == 3.0
        assert bad_metric["message"] == "文件不对"
        assert bad_metric["issues"] == ["a"]
        assert bad_metric["suggestions"] == ["fix"]
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
        assert "expected_output" not in params["b"]
        assert "pass_threshold" not in params["b"]
        # 未配置 criteria 不兜底：原样保留，不拿任务描述顶替
        assert "criteria" not in params["a"]
        assert "criteria" not in params["b"]

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
        assert str(ws) in p["criteria"]
        assert "t-9" in p["criteria"]
        # 非字符串参数原样保留
        task.metadata["acceptance_criteria"]["m1"]["input_params"]["min_size"] = 5
        params2 = tool._get_input_params(task)
        assert params2["m1"]["min_size"] == 5

    def test_resolve_tool_id_candidates(self, mod: Any, tmp_path: Path) -> None:
        """0.2 平铺布局：候选=plugins/shared/ 下含 plugin.json 的目录，_ 前缀排除。"""
        shared_dir = tmp_path / "plugins" / "shared"
        for name in ["zeta", "alpha", "_libs", "no_manifest"]:
            (shared_dir / name).mkdir(parents=True)
        for name in ["zeta", "alpha"]:
            (shared_dir / name / "plugin.json").write_text("{}", encoding="utf-8")
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
        task.metadata = {}
        service = MagicMock()
        service.get_task.return_value = task
        service.move_to_evaluating = AsyncMock()
        patch_service_access["service"] = service
        state_writer = AsyncMock()
        monkeypatch.setattr(mod, "_state_writer", state_writer)
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "t1", "task.ws_meta": {"mode": "plain", "path": "C:/ws/t1"}}],
        )
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1"})
        assert out == {"success": True, "status": "completed"}
        assert service.move_to_evaluating.await_count == 1
        # 状态推进两跳都落 state（单写面）：evaluating → completed（含 ended_at）
        fields_calls = [c.args[1] for c in state_writer.await_args_list]
        assert [f["task.status"] for f in fields_calls] == ["evaluating", "completed"]
        assert "task.ended_at" in fields_calls[-1]

    @pytest.mark.asyncio
    async def test_move_to_evaluating_failure_surfaces(
        self, mod: Any, patch_service_access: Any, monkeypatch: Any
    ) -> None:
        """running→evaluating 状态迁移失败 → 显式错误态（不吞），补落链不
        建立在错误前提上：迁移未发生就不得继续评估/写完成终态。"""
        task = MagicMock()
        task.status = TaskStatus.RUNNING
        task.metadata = {}
        service = MagicMock()
        service.get_task.return_value = task
        # 经被测模块的同代绑定取异常类（平铺裸名跨代际身份：直接 import 可能
        # 绑到另一代 state_machine 的同名类，tool.py 的 except 接不住）
        service.move_to_evaluating = AsyncMock(
            side_effect=mod.InvalidTransitionError("running", "evaluating", "不允许转换")
        )
        patch_service_access["service"] = service
        state_writer = AsyncMock()
        monkeypatch.setattr(mod, "_state_writer", state_writer)
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "t1", "task.ws_meta": {"mode": "plain", "path": "C:/ws/t1"}}],
        )
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1"})
        assert out["success"] is False
        assert out["error_code"] == "EVAL_TRANSITION_FAILED"
        assert "running→evaluating" in out["error"]
        # 迁移失败后零 state 写（既无 evaluating 更无 completed 补落）
        assert state_writer.await_count == 0

    @pytest.mark.asyncio
    async def test_evaluating_state_write_failure_blocks_completion(
        self, mod: Any, patch_service_access: Any, monkeypatch: Any
    ) -> None:
        """迁移成功但 evaluating 过渡态写 state 未落账 → 显式失败，不继续
        写完成终态（状态链两跳必须逐跳落账）。"""
        task = MagicMock()
        task.status = TaskStatus.RUNNING
        task.metadata = {}
        service = MagicMock()
        service.get_task.return_value = task
        service.move_to_evaluating = AsyncMock()
        patch_service_access["service"] = service
        monkeypatch.setattr(mod, "_state_writer", None)
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "t1", "task.ws_meta": {"mode": "plain", "path": "C:/ws/t1"}}],
        )
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1"})
        assert out["success"] is False
        assert out["error_code"] == "EVAL_STATE_WRITE_FAILED"
        # 失败定位在 evaluating 过渡态这一跳，而非继续走到完成腿才报错
        assert "evaluating" in out["error"]
        assert "completed" not in out["error"]
        assert service.move_to_evaluating.await_count == 1

    @pytest.mark.asyncio
    async def test_completion_state_write_failure_surfaces(
        self, mod: Any, patch_service_access: Any, monkeypatch: Any
    ) -> None:
        """完成终态写 state 未落账（写面未注入）→ 显式失败，不谎报完成。"""
        task = MagicMock()
        task.status = TaskStatus.EVALUATING
        task.metadata = {}
        service = MagicMock()
        service.get_task.return_value = task
        patch_service_access["service"] = service
        monkeypatch.setattr(mod, "_state_writer", None)
        monkeypatch.setattr(
            mod,
            "_state_reader",
            lambda: [{"pipeline_id": "t1", "task.ws_meta": {"mode": "plain", "path": "C:/ws/t1"}}],
        )
        out = await mod.task_evaluate_func({"action": "evaluate_single", "task_id": "t1"})
        assert out["success"] is False
        assert out["error_code"] == "EVAL_STATE_WRITE_FAILED"

    @pytest.mark.asyncio
    async def test_worktree_merge_gate_failure_marks_failed(
        self, mod: Any, patch_service_access: Any, monkeypatch: Any, tmp_path: Any
    ) -> None:
        """回归（2026-09-04 裁定）：worktree 合并门控失败（目录存在且合并真失败，
        如冲突）→ 任务标记 failed，禁止绕门置完成（产物会留在未合并副本里
        静默丢失）。2026-09-14 修订：目录缺失场景改为跳过合并（出生落地校验
        已保证 mode=worktree 必有真目录，缺失=外部清理，见
        test_worktree_merge_dir_missing_skips）。"""
        wt_dir = tmp_path / "real_wt"
        wt_dir.mkdir()
        task = MagicMock()
        task.status = TaskStatus.EVALUATING
        task.metadata = {"ws_meta": {"mode": "worktree", "path": str(wt_dir)}}
        service = MagicMock()
        service.get_task.return_value = task
        patch_service_access["service"] = service
        state_writer = AsyncMock()
        monkeypatch.setattr(mod, "_state_writer", state_writer)
        monkeypatch.setattr(
            mod.worktree_merge, "merge_worktree_before_complete",
            lambda tid, meta: "worktree 合并失败: conflict files=['a.txt']")
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1"})
        assert out["success"] is False
        assert out["error_code"] == "MERGE_GATE_FAILED"
        # failed 终态落 state 单写面（含失败分类），零 YAML 状态机调用
        assert state_writer.await_count == 1
        assert state_writer.await_args is not None
        fields = state_writer.await_args.args[1]
        assert fields["task.status"] == "failed"
        assert fields["task.failure_class"] == "merge_gate"

    @pytest.mark.asyncio
    async def test_merge_gate_failed_state_write_not_persisted_is_visible(
        self, mod: Any, patch_service_access: Any, monkeypatch: Any, tmp_path: Any
    ) -> None:
        """合并门控失败且 failed 终态写 state 未落账 → 错误态必须可见
        （不得静默返回"已标记 failed"——库面/state 面漂移要能被感知）。"""
        wt_dir = tmp_path / "real_wt"
        wt_dir.mkdir()
        task = MagicMock()
        task.status = TaskStatus.EVALUATING
        task.metadata = {"ws_meta": {"mode": "worktree", "path": str(wt_dir)}}
        service = MagicMock()
        service.get_task.return_value = task
        patch_service_access["service"] = service
        monkeypatch.setattr(mod, "_state_writer", None)
        monkeypatch.setattr(
            mod.worktree_merge, "merge_worktree_before_complete",
            lambda tid, meta: "worktree 合并失败: conflict files=['a.txt']")
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t1"})
        assert out["success"] is False
        assert out["error_code"] == "MERGE_GATE_FAILED"
        # 合并失败事实与"未落账"事实同时可见
        assert "worktree 合并失败" in out["error"]
        assert "未落账" in out["error"]

    async def test_worktree_merge_dir_missing_skips(
        self, mod: Any, patch_service_access: Any, monkeypatch: Any, tmp_path: Any
    ) -> None:
        """2026-09-14 用户裁定（state 真值）：mode=worktree 但目录缺失 = 外部
        清理（如批量评测前清理工作空间），合并无从谈起——跳过合并不判死，
        留 warning 痕迹；不再以 267 目录无效判任务 failed。"""
        task = MagicMock()
        task.status = TaskStatus.EVALUATING
        task.metadata = {"ws_meta": {"mode": "worktree",
                                     "path": str(tmp_path / "gone_wt")}}
        service = MagicMock()
        service.get_task.return_value = task
        patch_service_access["service"] = service
        state_writer = AsyncMock()
        monkeypatch.setattr(mod, "_state_writer", state_writer)
        monkeypatch.setattr(
            mod.worktree_merge, "merge_worktree_before_complete",
            lambda tid, meta: None)  # 门控层目录缺失→跳过合并（返回 None）
        out = await mod.task_evaluate_func({"action": "auto_complete", "task_id": "t2"})
        assert out["success"] is True
        assert state_writer.await_args is not None
        assert state_writer.await_args.args[1]["task.status"] == "completed"

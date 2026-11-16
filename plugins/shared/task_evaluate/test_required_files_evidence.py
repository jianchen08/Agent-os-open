# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 完赛前置证据校验（required_files 双闸）单测。

五态（F3 终版：声明 required_files 时文件面 + commit 面 AND 双闸）：

1. 声明 + 文件齐（存在且 mtime 晚于任务创建基线）+ 有新 commit → 完赛放行，
   与现状完成路径同输出（评估通过，任务已完成）；
2. 声明 + 文件缺失/过期 → 拒绝完赛（不落 completed），回执逐条列明细；
3. 声明 + 文件齐 + 无基线后新 commit → 拒绝，回执明示「交付未落库，commit
   后再完赛」（含空仓变体：init 后无任何提交）；
4. 未声明 required_files → 无文件面要求，commit 面照查（基线后无新 commit
   即拒绝，回执不含文件明细）；仅当工作空间坐标不可得 / 任务创建基线缺失
   或不可解析时降级放行并留 warning；
5. 非 git 仓 workspace → 文件面照查（缺文件照拒）+ commit 面跳过 + warning
   留痕（不因取证环境差异误杀）。

任务领域走真实 TaskModel + 真实 TaskService（tmp 存储目录）；文件证据用
临时目录真实文件 + os.utime 精确控制 mtime；commit 证据用真实 git 仓
（GIT_AUTHOR_DATE/GIT_COMMITTER_DATE 定格提交时刻，相对基线 ±偏移）；
外部依赖仅 state 读面/写面与 worktree 合并机制（git CLI，注入替身，同
test_tool_extra.py 口径）。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
_TASKS_DIR = _TE_DIR.parents[0] / "tasks"
_SHARED_DIR = _TE_DIR.parents[0]
_SYSTEM_DIR = _TE_DIR.parents[0] / "system"

for _d in (_TE_DIR, _TASKS_DIR, _SHARED_DIR, _SYSTEM_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from task_types import TaskStatus  # noqa: E402 — 依赖上方 sys.path 注入


def _load_module() -> Any:
    mod_name = "task_eval_required_files_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TE_DIR / "tool.py")
    assert spec is not None
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
    """真实 TaskService（临时存储目录），导入前 tasks 目录强制置顶。"""
    _s = str(_TASKS_DIR)
    while _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)
    sys.modules.pop("service", None)
    from service import TaskService

    return TaskService(data_dir=str(tmp_path / "tasks"))


def _metric(metric_id: str, passed: bool, **kw: Any) -> Any:
    from _eval_core import MetricResult

    return MetricResult(metric_id=metric_id, passed=passed, **kw)


def _eval_result(task_id: str, metrics: list[Any], *, summary: str = "") -> Any:
    from _eval_core import EvaluationResult

    r = EvaluationResult(task_id=task_id, results=metrics, summary=summary)
    r.compute_overall()
    return r


def _inject_tool(
    mod: Any,
    monkeypatch: Any,
    service: Any,
    state_rows: list[dict[str, Any]],
) -> tuple[Any, AsyncMock]:
    """注入服务获取/state 读面（含 ws_meta 行）/写面与合并机制替身。"""
    state_writer = AsyncMock()
    monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda _self: service)
    monkeypatch.setattr(mod, "_state_reader", lambda: state_rows)
    monkeypatch.setattr(mod, "_state_writer", state_writer)
    monkeypatch.setattr(
        mod.worktree_merge, "merge_worktree_before_complete", lambda _task_id, _ws_meta: None
    )
    return mod.TaskEvaluateTool(), state_writer


def _baseline_epoch(task: Any) -> float:
    """任务创建基线（ISO 文本 → epoch 秒，naive 按本地时区——与实现同口径）。"""
    return datetime.fromisoformat(task.created_at).timestamp()


def _git_date(epoch: float) -> str:
    """git 环境日期串（UTC 定格，秒粒度）。"""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%M:%S +0000")


def _git(repo: Path, *args: str, at_epoch: float | None = None) -> None:
    """跑真实 git（身份/提交时刻显式定格，不依赖全局配置）。"""
    env = dict(os.environ)
    if at_epoch is not None:
        d = _git_date(at_epoch)
        env["GIT_AUTHOR_DATE"] = d
        env["GIT_COMMITTER_DATE"] = d
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=evidence", "-c", "user.email=evidence@test", *args],
        check=True,
        capture_output=True,
        env=env,
        timeout=60,
    )


def _make_workspace(tmp_path: Path, task: Any, files: dict[str, float]) -> Path:
    """构造工作空间：相对路径 → 真实文件，mtime = 任务基线 + 偏移秒。"""
    base = _baseline_epoch(task)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    for rel, offset in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("evidence", encoding="utf-8")
        t = base + offset
        os.utime(p, (t, t))
    return ws


def _make_git_workspace(
    tmp_path: Path, task: Any, files: dict[str, float], commit_epoch: float
) -> Path:
    """构造真实 git 仓工作空间：文件证据 + 定格时刻的初始提交。"""
    ws = _make_workspace(tmp_path, task, files)
    _git(ws, "init")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "evidence", at_epoch=commit_epoch)
    return ws


def _rows_with_ws(task: Any, ws: Path | None) -> list[dict[str, Any]]:
    row: dict[str, Any] = {"pipeline_id": task.id}
    if ws is not None:
        row["task.ws_meta"] = {"mode": "plain", "path": str(ws)}
    return [row]


def _assert_completed(tool_out: Any, state_writer: Any) -> None:
    assert tool_out.success is True
    assert tool_out.metadata["result"] == "completed"
    assert tool_out.metadata["message"] == "评估通过，任务已完成"
    assert state_writer.await_args.args[1]["task.status"] == "completed"


class TestRequiredFilesEvidenceGate:
    @pytest.mark.asyncio
    async def test_files_and_new_commit_pass(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态1：文件齐（晚于基线）+ 基线后有新 commit → 完赛放行，完成输出
        与现状一致。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"required_files": ["report.md", "sub/data.json"]},
        )
        ws = _make_git_workspace(
            tmp_path, task, {"report.md": 60.0, "sub/data.json": 120.0},
            commit_epoch=_baseline_epoch(task) + 30.0,
        )
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_missing_and_expired_files_rejected_with_receipt(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态2：缺失 + 过期混列（commit 面已满足）→ 拒绝完赛，回执逐条列
        全部文件违规明细；齐备文件与 commit 面不得误列。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"required_files": ["missing.txt", "stale.txt", "fresh.txt"]},
        )
        ws = _make_git_workspace(
            tmp_path, task, {"stale.txt": -60.0, "fresh.txt": 60.0},
            commit_epoch=_baseline_epoch(task) + 30.0,
        )
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        # 拒绝完赛：任务不落 completed 终态
        assert state_writer.await_count == 0
        assert out.metadata["result"] == "retry"
        receipt = out.metadata["message"]
        assert "missing.txt" in receipt
        assert "不存在" in receipt
        assert "stale.txt" in receipt
        assert "过期" in receipt
        # 齐备文件不误列
        assert "fresh.txt" not in receipt
        # commit 面已满足，不出现交付未落库
        assert "交付未落库" not in receipt
        # output 面如实 overall_passed=False
        assert out.output["overall_passed"] is False
        assert out.output["task_id"] == task.id

    @pytest.mark.asyncio
    async def test_no_new_commit_rejected_with_receipt(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态3：文件齐但基线后无新 commit → 拒绝，回执明示「交付未落库，
        commit 后再完赛」，且不列文件明细。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["report.md"]}
        )
        ws = _make_git_workspace(
            tmp_path, task, {"report.md": 60.0},
            commit_epoch=_baseline_epoch(task) - 60.0,
        )
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert state_writer.await_count == 0
        assert out.metadata["result"] == "retry"
        receipt = out.metadata["message"]
        assert "交付未落库" in receipt
        assert "commit" in receipt
        # 文件面已满足，回执无文件明细行
        assert "report.md" not in receipt
        assert out.output["overall_passed"] is False

    @pytest.mark.asyncio
    async def test_empty_repo_no_commit_rejected(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态3变体：git 仓但零提交（init 后无任何 commit）= 无基线后新
        commit → 拒绝（补首个 commit 即自愈，非降级放行）。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["report.md"]}
        )
        ws = _make_workspace(tmp_path, task, {"report.md": 60.0})
        _git(ws, "init")
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert state_writer.await_count == 0
        assert out.metadata["result"] == "retry"
        assert "交付未落库" in out.metadata["message"]

    @pytest.mark.asyncio
    async def test_not_declared_passthrough_unchanged(
        self, mod: Any, service: Any, monkeypatch: Any
    ) -> None:
        """态4：未声明 required_files → 无文件面要求，但 commit 面照查。

        本用例 state 行无 task.ws_meta（工作空间坐标不可得）→ commit 面走
        降级放行并留 warning，因此完成输出与正常放行一致。"""
        task = await service.create_task(title="评估任务")
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, None))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_non_git_workspace_files_ok_passes_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态5a：非 git 仓 + 文件齐 → commit 面跳过（warning 留痕），文件面
        满足即放行。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["report.md"]}
        )
        ws = _make_workspace(tmp_path, task, {"report.md": 60.0})
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text
        assert "commit" in caplog.text

    @pytest.mark.asyncio
    async def test_non_git_workspace_missing_file_still_rejected(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态5b：非 git 仓 + 文件缺 → 文件面照查照拒（降级只跳 commit 面，
        不豁免文件证据）。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["missing.txt"]}
        )
        ws = _make_workspace(tmp_path, task, {})
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert state_writer.await_count == 0
        assert out.metadata["result"] == "retry"
        assert "missing.txt" in out.metadata["message"]
        assert "降级" in caplog.text

    @pytest.mark.asyncio
    async def test_workspace_missing_meta_degrades_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        """降级：state 行无 ws_meta 坐标（工作空间不可得）→ 放行 + warning。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["report.md"]}
        )
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, None))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text

    @pytest.mark.asyncio
    async def test_workspace_dir_not_exist_degrades_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """降级：ws_meta.path 目录不存在 → 放行 + warning。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["report.md"]}
        )
        gone = tmp_path / "vanished_ws"
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, gone))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text

    @pytest.mark.asyncio
    async def test_unparseable_created_at_degrades_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """降级：创建基线不可解析（两判定面共用基线）→ 放行 + warning，
        不用猜测基线拒任务。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["report.md"]}
        )
        ws = _make_workspace(tmp_path, task, {"report.md": 0.0})
        task.created_at = "not-a-timestamp"
        service._storage.save(task)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text

    @pytest.mark.asyncio
    async def test_mtime_equal_baseline_counts_expired(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """性质：「晚于」为严格比较——mtime 恰等于基线按过期计。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["a.txt"]}
        )
        ws = _make_workspace(tmp_path, task, {"a.txt": 0.0})
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert state_writer.await_count == 0
        assert out.metadata["result"] == "retry"
        assert "a.txt" in out.metadata["message"]

    @pytest.mark.asyncio
    async def test_out_of_workspace_path_rejected_as_missing(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """越界声明（../ 逃出工作空间）按缺失计并列回执（fail-closed）。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["../escape.txt"]}
        )
        ws = _make_workspace(tmp_path, task, {})
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert state_writer.await_count == 0
        assert out.metadata["result"] == "retry"
        assert "../escape.txt" in out.metadata["message"]

    @pytest.mark.asyncio
    async def test_empty_required_files_list_passthrough(
        self, mod: Any, service: Any, monkeypatch: Any
    ) -> None:
        """声明空清单 = 无文件要求 → passthrough（同未声明）。"""
        task = await service.create_task(title="评估任务", metadata={"required_files": []})
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, None))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_already_completed_task_skips_gate(
        self, mod: Any, service: Any, monkeypatch: Any
    ) -> None:
        """已完成任务的幂等短路不重开闸（缺失证据也不改写既有终态）。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["missing.txt"]}
        )
        task.status = TaskStatus.COMPLETED
        service._storage.save(task)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, None))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert out.success is True
        assert out.metadata["result"] == "completed"
        assert state_writer.await_count == 0

    @pytest.mark.asyncio
    async def test_receipt_lines_match_violation_count(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """多文件全缺：回执明细行数 = 违规声明数（逐条不吞并）。"""
        declared = ["docs/a.md", "docs/b.md", "src/c.py"]
        task = await service.create_task(title="评估任务", metadata={"required_files": declared})
        ws = _make_workspace(tmp_path, task, {})
        tool, _ = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt_lines = [
            ln for ln in out.metadata["message"].splitlines()
            if ln.startswith("- ")
        ]
        assert len(receipt_lines) == len(declared)
        for rel in declared:
            assert any(rel in ln for ln in receipt_lines)

    @pytest.mark.asyncio
    async def test_blank_entry_listed_in_receipt(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """声明清单含空白条目 → 按违规列回执（不静默吞并）。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["  ", "real.txt"]}
        )
        ws = _make_git_workspace(
            tmp_path, task, {"real.txt": 60.0},
            commit_epoch=_baseline_epoch(task) + 30.0,
        )
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        assert state_writer.await_count == 0
        assert out.metadata["result"] == "retry"
        assert "空路径" in out.metadata["message"]

    @pytest.mark.asyncio
    async def test_none_metadata_passthrough(
        self, mod: Any, service: Any, monkeypatch: Any
    ) -> None:
        """task.metadata 非 dict（None）→ 视同未声明，passthrough。"""
        task = await service.create_task(title="评估任务")
        task.metadata = None
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, None))
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_empty_created_at_degrades_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """创建基线为空串（历史任务无出生键）→ 放行 + warning（与不可解析
        同属基线缺失降级，第二组区分度输入）。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["report.md"]}
        )
        ws = _make_workspace(tmp_path, task, {"report.md": 0.0})
        task.created_at = ""
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text

    @pytest.mark.asyncio
    async def test_git_probe_timeout_degrades_commit_face(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """git 取证超时（进程边界异常）= 仓库不可用 → commit 面跳过降级，
        文件面照查（进程边界替身：仅 rev-parse 调用抛超时，其余真跑）。"""
        import subprocess as sp

        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["report.md"]}
        )
        ws = _make_workspace(tmp_path, task, {"report.md": 60.0})
        real_run = sp.run

        def _run_timeout_on_revparse(*args: Any, **kw: Any) -> Any:
            cmd = list(args[0]) if args else []
            if "rev-parse" in cmd:
                raise sp.TimeoutExpired(cmd="git", timeout=30)
            return real_run(*args, **kw)

        monkeypatch.setattr(mod.subprocess, "run", _run_timeout_on_revparse)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text

    @pytest.mark.asyncio
    async def test_git_log_failure_after_repo_probe_degrades(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """仓判活后 log 取证进程异常（超时类环境差异）= 取证不可得 → commit
        面跳过降级放行（空仓拒拒不在此列：rc!=0 走证据不成立，见态3变体）。
        进程边界替身：仅 log 调用抛超时，rev-parse 真跑判活。"""
        import subprocess as sp

        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["report.md"]}
        )
        ws = _make_git_workspace(
            tmp_path, task, {"report.md": 60.0},
            commit_epoch=_baseline_epoch(task) + 30.0,
        )
        real_run = sp.run

        def _run_timeout_on_log(*args: Any, **kw: Any) -> Any:
            cmd = list(args[0]) if args else []
            if "log" in cmd:
                raise sp.TimeoutExpired(cmd="git", timeout=30)
            return real_run(*args, **kw)

        monkeypatch.setattr(mod.subprocess, "run", _run_timeout_on_log)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows_with_ws(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text

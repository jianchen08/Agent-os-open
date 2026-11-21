# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 完赛证据闸双轨（acceptance files 豁免 commit 面）单测。

设计裁定 2026-11-11（ADR docs/decisions/2026-11-11-evidence-gate-dual-track.md）：
声明 ``acceptance_criteria.files`` 的非 worktree 任务以文件面替代 commit 面
（无 bash 命令工具的 agent 执行不了 git commit，commit 面恒查 = 结构性死锁）；
worktree 挂靠项目任务不豁免（交付语义 = commit 后合并回项目仓库）。覆盖态：

(a) 非 worktree + acceptance files（task_submit 展开形态与原始 files 形态两
    组输入）+ 文件新鲜 + 无基线后 commit → completed，且 git 进程零调用
    （commit 面整体跳过，不做探测）；
(b) 同配置但文件缺失/过期 → retry，回执为文件面提示（含缺失文件名、不含
    commit 字样）；required_files 与 acceptance files 并集同查（都声明都查）；
(c) worktree 边界：worktree + acceptance files + 无 commit → 仍被 commit 面
    拦截（边界有效）；有新 commit → 照常放行（边界只关豁免门，不拦任务）；
(d) 触发器边界：裸 ``file_check`` 指标键（无实例后缀，非 files 字段信号）与
    非 dict acceptance_criteria 不触发豁免（commit 面照查）；ws_meta.mode 键
    缺失按非 worktree 保守代理（豁免生效，独立任务缺省口径）。

任务领域走真实 TaskModel + 真实 TaskService（tmp 存储目录）；文件证据用
临时目录真实文件 + os.utime 精确控制 mtime；commit 证据用真实 git 仓
（GIT_AUTHOR_DATE/GIT_COMMITTER_DATE 定格提交时刻）；外部依赖仅 state 读
面/写面与 worktree 合并机制（注入替身，同 test_required_files_evidence.py
口径）。
"""

from __future__ import annotations

import importlib.util
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

for _d in (_TE_DIR, _TASKS_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    mod_name = "task_eval_acceptance_files_under_test"
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


def _make_workspace(
    tmp_path: Path, task: Any, files: dict[str, float], *, git_commit_epoch: float | None = None
) -> Path:
    """构造工作空间：相对路径 → 真实文件，mtime = 任务基线 + 偏移秒。

    git_commit_epoch 非空 → 再建真实 git 仓并落一枚定格时刻的初始提交
    （基线前旧提交 = 无基线后新 commit 的判据输入）。
    """
    base = _baseline_epoch(task)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    for rel, offset in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("evidence", encoding="utf-8")
        t = base + offset
        os.utime(p, (t, t))
    if git_commit_epoch is not None:
        _git(ws, "init")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-m", "evidence", at_epoch=git_commit_epoch)
    return ws


def _rows_with_ws(task: Any, ws_meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    row: dict[str, Any] = {"pipeline_id": task.id}
    if ws_meta is not None:
        row["task.ws_meta"] = ws_meta
    return [row]


def _ac_expanded(paths: list[str]) -> dict[str, Any]:
    """acceptance_criteria 白话 files 展开形态（task_submit 主管道存储形态）。"""
    return {
        f"file_check::{p}": {"input_params": {"path": p, "check": "not_empty"}}
        for p in paths
    }


def _ac_raw(paths: list[str]) -> dict[str, Any]:
    """acceptance_criteria 原始白话形态（字面 files 键，直构 metadata 场景）。"""
    return {"files": list(paths)}


def _forbid_git_calls(mod: Any, monkeypatch: Any) -> None:
    """豁免轨道性质断言替身：任何 git 进程探测即失败（commit 面须整体跳过）。"""

    def _no_git(*args: Any, **kw: Any) -> Any:
        raise AssertionError("豁免轨道不得发起 git 进程探测（commit 面应整体跳过）")

    monkeypatch.setattr(mod.subprocess, "run", _no_git)


def _assert_completed(out: Any, state_writer: Any) -> None:
    assert out.success is True
    assert out.metadata["result"] == "completed"
    assert out.metadata["message"] == "评估通过，任务已完成"
    assert state_writer.await_args.args[1]["task.status"] == "completed"


def _assert_rejected(out: Any, state_writer: Any) -> str:
    """拒绝完赛：retry + 不落 completed 终态；返回回执文本。"""
    assert state_writer.await_count == 0
    assert out.success is True
    assert out.metadata["result"] == "retry"
    assert out.output["overall_passed"] is False
    receipt = out.metadata["message"]
    assert isinstance(receipt, str)
    assert receipt
    return receipt


class TestAcceptanceFilesExemption:
    @pytest.mark.asyncio
    async def test_fresh_files_no_commit_completed_expanded_form(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态(a)主管道形态：声明 acceptance files（file_check:: 展开键）+
        文件新鲜 + 仓内仅基线前旧提交 → completed（旧闸在此必然以 commit 面
        拒绝——放行即证明豁免生效），且全程零 git 进程探测。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": _ac_expanded(["report.md", "sub/data.json"])},
        )
        ws = _make_workspace(
            tmp_path, task,
            {"report.md": 60.0, "sub/data.json": 120.0},
            git_commit_epoch=_baseline_epoch(task) - 60.0,
        )
        _forbid_git_calls(mod, monkeypatch)
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_fresh_files_no_commit_completed_raw_form(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态(a)第二形态：字面 files 键（原始白话形态）同判——两形态是同一
        signals 的存储两面，豁免触发器都认。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": _ac_raw(["report.md"])},
        )
        ws = _make_workspace(
            tmp_path, task, {"report.md": 60.0},
            git_commit_epoch=_baseline_epoch(task) - 60.0,
        )
        _forbid_git_calls(mod, monkeypatch)
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_missing_file_rejected_with_file_face_receipt(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态(b)：声明文件缺失 → retry，回执文件面提示（含缺失文件名、无
        commit 字样——回执不得再对无 bash agent 发不可执行指令），指标面
        metric_id=acceptance_files。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": _ac_expanded(["report.md", "fresh.txt"])},
        )
        ws = _make_workspace(
            tmp_path, task, {"fresh.txt": 60.0},
            git_commit_epoch=_baseline_epoch(task) - 60.0,
        )
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert "report.md" in receipt
        assert "不存在" in receipt
        # 齐备文件不误列；回执零 commit 字样（大小写不敏感）
        assert "fresh.txt" not in receipt
        assert "commit" not in receipt.lower()
        assert out.output["metrics"][0]["metric_id"] == "acceptance_files"

    @pytest.mark.asyncio
    async def test_stale_file_rejected(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态(b)第二组输入：文件存在但 mtime 早于基线（过期）→ 同样 retry，
        回执列过期明细（新鲜度判据与 required_files 面同算法）。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": _ac_expanded(["stale.txt"])},
        )
        ws = _make_workspace(
            tmp_path, task, {"stale.txt": -60.0},
            git_commit_epoch=_baseline_epoch(task) - 60.0,
        )
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert "stale.txt" in receipt
        assert "过期" in receipt
        assert "commit" not in receipt.lower()

    @pytest.mark.asyncio
    async def test_union_checks_required_files_too(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """并集语义：acceptance files 与 required_files 都声明 → 都查
        （required_files 违规照列回执，acceptance files 齐备不误列）。"""
        task = await service.create_task(
            title="评估任务",
            metadata={
                "acceptance_criteria": _ac_expanded(["report.md"]),
                "required_files": ["missing_required.txt"],
            },
        )
        ws = _make_workspace(
            tmp_path, task, {"report.md": 60.0},
            git_commit_epoch=_baseline_epoch(task) - 60.0,
        )
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert "missing_required.txt" in receipt
        assert "report.md" not in receipt

    @pytest.mark.asyncio
    async def test_union_lists_both_when_both_violate(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """并集语义第二组输入：两面声明都违规 → 回执逐条都列（不吞并）。"""
        task = await service.create_task(
            title="评估任务",
            metadata={
                "acceptance_criteria": _ac_expanded(["missing_acc.md"]),
                "required_files": ["missing_req.txt"],
            },
        )
        ws = _make_workspace(tmp_path, task, {})
        _git(ws, "init")  # 空仓（零提交）：豁免轨不做 git 探测，仓形态不影响判据
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert "missing_acc.md" in receipt
        assert "missing_req.txt" in receipt


class TestWorktreeBoundary:
    @pytest.mark.asyncio
    async def test_worktree_with_acceptance_files_still_commit_gate(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态(c)：worktree 挂靠项目 + 声明 acceptance files + 无基线后新
        commit → 仍被 commit 面拦截（豁免边界有效，交付语义 = commit 后
        合并回项目仓库），回执为 commit 面文案。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": _ac_expanded(["report.md"])},
        )
        project = tmp_path / "project"
        project.mkdir()
        ws = _make_workspace(
            tmp_path, task, {"report.md": 60.0},
            git_commit_epoch=_baseline_epoch(task) - 60.0,
        )
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service,
            _rows_with_ws(task, {
                "mode": "worktree", "path": str(ws),
                "branch": f"agentos/{task.id}", "project_root": str(project),
            }),
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert "交付未落库" in receipt
        assert "git" in receipt
        assert out.output["metrics"][0]["metric_id"] == "commit_evidence"

    @pytest.mark.asyncio
    async def test_worktree_with_new_commit_passes(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态(c)边界只关豁免门不拦任务：worktree + acceptance files + 基线后
        新 commit → 照常放行（既有 commit 面判据不受声明 acceptance files
        影响）。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": _ac_expanded(["report.md"])},
        )
        project = tmp_path / "project"
        project.mkdir()
        ws = _make_workspace(
            tmp_path, task, {"report.md": 60.0},
            git_commit_epoch=_baseline_epoch(task) + 30.0,
        )
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service,
            _rows_with_ws(task, {
                "mode": "worktree", "path": str(ws),
                "branch": f"agentos/{task.id}", "project_root": str(project),
            }),
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)


class TestTriggerBoundaries:
    @pytest.mark.asyncio
    async def test_bare_file_check_metric_not_exempt(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """触发器边界：裸 ``file_check`` 指标键（指标名 dict 形态提交，无
        实例后缀）不属 files 字段信号 → 不豁免，commit 面照查（豁免触发只认
        显式 files 声明，不做启发式扩面）。"""
        task = await service.create_task(
            title="评估任务",
            metadata={
                "acceptance_criteria": {
                    "file_check": {"input_params": {"path": "report.md", "check": "not_empty"}},
                },
            },
        )
        ws = _make_workspace(
            tmp_path, task, {"report.md": 60.0},
            git_commit_epoch=_baseline_epoch(task) - 60.0,
        )
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert "交付未落库" in receipt
        assert out.output["metrics"][0]["metric_id"] == "commit_evidence"

    @pytest.mark.asyncio
    async def test_non_dict_acceptance_criteria_not_exempt(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """触发器边界第二组输入：acceptance_criteria 非 dict（字符串形态）→
        无 files 信号 → 不豁免，commit 面照查。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": "chapters/chapter_01.md"},
        )
        ws = _make_workspace(
            tmp_path, task, {"chapters/chapter_01.md": 60.0},
            git_commit_epoch=_baseline_epoch(task) - 60.0,
        )
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert "交付未落库" in receipt

    @pytest.mark.asyncio
    async def test_missing_mode_key_treated_non_worktree(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """保守代理：ws_meta 无 mode 键（历史任务/坐标残缺形态）按非 worktree
        （独立任务缺省口径，与合并门控对同一字段 mode != "worktree" 即不合并
        的读法一致）→ 豁免生效。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": _ac_expanded(["report.md"])},
        )
        ws = _make_workspace(
            tmp_path, task, {"report.md": 60.0},
            git_commit_epoch=_baseline_epoch(task) - 60.0,
        )
        _forbid_git_calls(mod, monkeypatch)
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_no_acceptance_files_no_behavior_change(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """零回归锚：未声明 acceptance files/required_files + 无基线后新
        commit → 仍走原 commit 面拒绝（豁免轨道的存在不改变未声明者行为）。"""
        task = await service.create_task(title="评估任务")
        ws = _make_workspace(
            tmp_path, task, {"report.md": 60.0},
            git_commit_epoch=_baseline_epoch(task) - 60.0,
        )
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert "交付未落库" in receipt
        assert "请先 git 提交" in receipt

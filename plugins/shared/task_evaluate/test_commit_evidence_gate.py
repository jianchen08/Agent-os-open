# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 完赛前置 commit 证据校验（未声明 required_files 面）单测。

用户裁定 2026-11-04：任务评估时没有 git 记录 → 返回说明要 agent git，不落
completed。覆盖面（未声明 required_files 的任务 = 本次新闸；声明者的双闸
行为由 test_required_files_evidence.py 守住）：

1. git 工作空间 + 基线后无新 commit → 拒绝（result=retry），回执明示「交付
   未落库，commit 后再完赛」且含「请先 git 提交」语义；
2. git 工作空间 + 基线后有新 commit → 完赛放行（与现状完成输出一致；同轴
   第二组输入 = 基线前已有 commit 不充数）；
3. 非 git 工作空间 → 放行 + warning 留痕（不因取证环境差异误杀）；
4. 工作空间坐标不可得 / 创建基线不可解析（含空串）→ 放行 + warning 留痕；
5. git 取证超时（进程边界异常）→ 放行 + warning 留痕；
6. 回归：声明 required_files 的任务仍走原闸（文件面照查照拒），且 commit
   面单次探测（两闸共享同一探测结果，不重复执行 git log）；
7. 归属性（ADR 2026-11-04-l0-gate-scope-and-eval-git-evidence 决策 3）：工作
   空间是共享仓根的子目录（dev ``.ai_workspaces/<task_id>`` 形态）或恰是多会
   话共享仓根 → commit 面降级放行 + warning 留痕（commit 归不到单个任务，
   不得逼 agent 为造证据去动共享仓）；
8. 证据面即工作空间子树：基线后仅有空提交（无文件改动）→ 不充数，仍拒绝。

任务领域走真实 TaskModel + 真实 TaskService（tmp 存储目录）；commit 证据用
真实 git 仓（GIT_AUTHOR_DATE/GIT_COMMITTER_DATE 定格提交时刻）；外部依赖仅
state 读面/写面与 worktree 合并机制（git CLI，注入替身，同
test_required_files_evidence.py 口径）。
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

for _d in (_TE_DIR, _TASKS_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    mod_name = "task_eval_commit_evidence_under_test"
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


def _eval_result(task_id: str) -> Any:
    from _eval_core import EvaluationResult, MetricResult

    r = EvaluationResult(
        task_id=task_id,
        results=[MetricResult(metric_id="m1", passed=True)],
        summary="",
    )
    r.compute_overall()
    return r


def _baseline_epoch(task: Any) -> float:
    """任务创建基线（ISO 文本 → epoch 秒，naive 按本地时区——与实现同口径）。"""
    return datetime.fromisoformat(task.created_at).timestamp()


def _git(repo: Path, *args: str, at_epoch: float | None = None) -> None:
    """跑真实 git（身份/提交时刻显式定格，不依赖全局配置）。"""
    env = dict(os.environ)
    if at_epoch is not None:
        d = datetime.fromtimestamp(at_epoch, tz=UTC).strftime("%Y-%m-%d %H:%M:%S +0000")
        env["GIT_AUTHOR_DATE"] = d
        env["GIT_COMMITTER_DATE"] = d
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=evidence", "-c", "user.email=evidence@test", *args],
        check=True,
        capture_output=True,
        env=env,
        timeout=60,
    )


def _commit(repo: Path, name: str, at_epoch: float) -> None:
    (repo / name).write_text("evidence", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", name, at_epoch=at_epoch)


def _inject_tool(
    mod: Any,
    monkeypatch: Any,
    service: Any,
    state_rows: list[dict[str, Any]],
) -> tuple[Any, AsyncMock]:
    """注入服务获取/state 读面/写面与合并机制替身。"""
    state_writer = AsyncMock()
    monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda _self: service)
    monkeypatch.setattr(mod, "_state_reader", lambda: state_rows)
    monkeypatch.setattr(mod, "_state_writer", state_writer)
    monkeypatch.setattr(
        mod.worktree_merge, "merge_worktree_before_complete", lambda _task_id, _ws_meta: None
    )
    return mod.TaskEvaluateTool(), state_writer


def _rows(task: Any, ws: Path | None) -> list[dict[str, Any]]:
    row: dict[str, Any] = {"pipeline_id": task.id}
    if ws is not None:
        row["task.ws_meta"] = {"mode": "plain", "path": str(ws)}
    return [row]


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


class TestCommitEvidenceGateWithoutRequiredFiles:
    @pytest.mark.asyncio
    async def test_no_new_commit_after_baseline_rejected(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态1：git 工作空间但基线后无新 commit（仅有基线前提交）→ 拒绝，
        回执明示先提交再完赛，任务不落 completed。"""
        task = await service.create_task(title="评估任务")
        ws = tmp_path / "ws"
        ws.mkdir()
        _git(ws, "init")
        _commit(ws, "before.txt", _baseline_epoch(task) - 60.0)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id))
        receipt = _assert_rejected(out, state_writer)
        assert "commit" in receipt
        assert "交付未落库" in receipt
        assert "请先 git 提交" in receipt
        assert out.output["metrics"][0]["metric_id"] == "commit_evidence"

    @pytest.mark.asyncio
    async def test_empty_repo_rejected(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态1变体：git init 后零提交 = 无基线后新 commit → 拒绝（补首个
        commit 即自愈，非取证环境差异）。"""
        task = await service.create_task(title="评估任务")
        ws = tmp_path / "ws"
        ws.mkdir()
        _git(ws, "init")
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id))
        assert "交付未落库" in _assert_rejected(out, state_writer)

    @pytest.mark.asyncio
    async def test_new_commit_after_baseline_passes(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态2：基线前有提交 + 基线后新提交 → 放行（基线前提交不充数，新
        commit 才满足判据）。"""
        task = await service.create_task(title="评估任务")
        base = _baseline_epoch(task)
        ws = tmp_path / "ws"
        ws.mkdir()
        _git(ws, "init")
        _commit(ws, "before.txt", base - 60.0)
        _commit(ws, "after.txt", base + 30.0)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_non_git_workspace_passes_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态3：非 git 工作空间 → 放行 + warning 如实写原因（非 git 仓）。"""
        task = await service.create_task(title="评估任务")
        ws = tmp_path / "ws"
        ws.mkdir()
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text
        assert "非 git 仓" in caplog.text

    @pytest.mark.asyncio
    async def test_workspace_meta_missing_degrades_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        """态4a：state 行无 ws_meta 坐标（工作空间不可得）→ 放行 + warning。"""
        task = await service.create_task(title="评估任务")
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, None))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text
        assert "工作空间坐标不可得" in caplog.text

    @pytest.mark.asyncio
    async def test_workspace_dir_missing_degrades_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态4a变体：ws_meta.path 目录不存在 → 放行 + warning。"""
        task = await service.create_task(title="评估任务")
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows(task, tmp_path / "vanished")
        )
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "工作空间坐标不可得" in caplog.text

    @pytest.mark.asyncio
    async def test_unparseable_created_at_degrades_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态4b：创建基线不可解析 → 放行 + warning（不用猜测基线拒任务）。"""
        task = await service.create_task(title="评估任务")
        ws = tmp_path / "ws"
        ws.mkdir()
        task.created_at = "not-a-timestamp"
        service._storage.save(task)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text
        assert "基线缺失" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_created_at_degrades_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态4b第二组输入：created_at 空串（历史任务无出生键）→ 同属基线
        缺失降级，放行 + warning。"""
        task = await service.create_task(title="评估任务")
        ws = tmp_path / "ws"
        ws.mkdir()
        task.created_at = ""
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "基线缺失" in caplog.text

    @pytest.mark.asyncio
    async def test_git_timeout_degrades_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态5：git 取证超时（进程边界异常）→ 放行 + warning 写清超时原因。"""
        task = await service.create_task(title="评估任务")
        ws = tmp_path / "ws"
        ws.mkdir()
        real_run = subprocess.run

        def _run_timeout_on_git(*args: Any, **kw: Any) -> Any:
            cmd = list(args[0]) if args else []
            if cmd and "rev-parse" in cmd:
                raise subprocess.TimeoutExpired(cmd="git", timeout=30)
            return real_run(*args, **kw)

        monkeypatch.setattr(mod.subprocess, "run", _run_timeout_on_git)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text
        assert "超时" in caplog.text


    @pytest.mark.asyncio
    async def test_git_not_available_degrades_with_warning(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态5第二组输入：git 不可用（未安装/不可执行，FileNotFoundError）→
        放行 + warning 写清 git 不可用原因。"""
        task = await service.create_task(title="评估任务")
        ws = tmp_path / "ws"
        ws.mkdir()
        real_run = subprocess.run

        def _run_no_git(*args: Any, **kw: Any) -> Any:
            cmd = list(args[0]) if args else []
            if cmd and cmd[0] == "git":
                raise FileNotFoundError(2, "No such file or directory", "git")
            return real_run(*args, **kw)

        monkeypatch.setattr(mod.subprocess, "run", _run_no_git)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text
        assert "git 不可用" in caplog.text

    @pytest.mark.asyncio
    async def test_git_log_oserror_after_repo_probe_degrades(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态5第三组输入：仓判活后 log 取证进程异常（OSError）→ 放行 +
        warning 写清 log 取证失败（不按无新 commit 误杀）。"""
        task = await service.create_task(title="评估任务")
        ws = tmp_path / "ws"
        ws.mkdir()
        _git(ws, "init")
        real_run = subprocess.run

        def _run_oserror_on_log(*args: Any, **kw: Any) -> Any:
            cmd = list(args[0]) if args else []
            if "log" in cmd:
                raise OSError(11, "Resource temporarily unavailable")
            return real_run(*args, **kw)

        monkeypatch.setattr(mod.subprocess, "run", _run_oserror_on_log)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text
        assert "log 取证失败" in caplog.text


class TestRequiredFilesGateRegression:
    @pytest.mark.asyncio
    async def test_declared_required_files_still_file_face_rejects(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """回归：声明 required_files → 文件面照查照拒（原闸不被新闸替代），
        且 commit 面只探测一次（两闸共享单次 git 探测）。"""
        task = await service.create_task(
            title="评估任务", metadata={"required_files": ["missing.txt"]}
        )
        base = _baseline_epoch(task)
        ws = tmp_path / "ws"
        ws.mkdir()
        _git(ws, "init")
        _commit(ws, "other.txt", base + 30.0)
        git_log_calls: list[list[str]] = []
        real_run = subprocess.run

        def _count_log(*args: Any, **kw: Any) -> Any:
            cmd = list(args[0]) if args else []
            if "log" in cmd:
                git_log_calls.append(cmd)
            return real_run(*args, **kw)

        monkeypatch.setattr(mod.subprocess, "run", _count_log)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id))
        receipt = _assert_rejected(out, state_writer)
        assert "missing.txt" in receipt
        assert out.output["metrics"][0]["metric_id"] == "required_files"
        assert len(git_log_calls) == 1

class TestCommitEvidenceAttribution:
    """可归属性判定（ADR 2026-11-04 决策 3；2026-11-04 e2e 循环实证后补）。"""

    @pytest.mark.asyncio
    async def test_workspace_nested_in_shared_repo_degrades(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态7a：工作空间是共享仓根的子目录（.ai_workspaces/<id> 形态）→
        commit 面降级放行；否则 trivial 任务会被逼着为造 commit 去改共享仓
        （2026-11-04 e2e 实测：agent 查 hook/门禁、regen INDEX 后强行提交）。"""
        task = await service.create_task(title="评估任务")
        base = _baseline_epoch(task)
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init")
        _commit(repo, "before.txt", base - 60.0)
        ws = repo / ".ai_workspaces" / task.id
        ws.mkdir(parents=True)
        monkeypatch.setattr(mod, "_resolve_repo_root", lambda: None)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "降级" in caplog.text
        assert "共享仓根子目录" in caplog.text
        assert "不可归属" in caplog.text

    @pytest.mark.asyncio
    async def test_shared_repo_root_workspace_degrades(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态7b：工作空间恰是仓库锚根（多会话共享主仓）→ 即使基线后无新
        commit 也降级放行（提交归属不可判，不逼任务造证据）。"""
        task = await service.create_task(title="评估任务")
        base = _baseline_epoch(task)
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init")
        _commit(repo, "before.txt", base - 60.0)
        monkeypatch.setattr(mod, "_resolve_repo_root", lambda: repo)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, repo))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "多会话共享仓根" in caplog.text
        assert "不可归属" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_commit_after_baseline_not_counted(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态8：基线后只有空提交（无文件改动）→ 不充数（取证限工作空间
        子树，空提交不构成交付证据）。"""
        task = await service.create_task(title="评估任务")
        base = _baseline_epoch(task)
        ws = tmp_path / "ws"
        ws.mkdir()
        _git(ws, "init")
        _commit(ws, "before.txt", base - 60.0)
        _git(ws, "commit", "--allow-empty", "-m", "empty", at_epoch=base + 30.0)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        out = await tool._complete_task(service, task, _eval_result(task.id))
        receipt = _assert_rejected(out, state_writer)
        assert "交付未落库" in receipt

    @pytest.mark.asyncio
    async def test_toplevel_probe_rc_nonzero_degrades(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态7c：仓判活后 show-toplevel 取证失败（退码非零）→ 降级放行。"""
        task = await service.create_task(title="评估任务")
        ws = tmp_path / "ws"
        ws.mkdir()
        _git(ws, "init")
        real_run = subprocess.run

        def _run_toplevel_fail(*args: Any, **kw: Any) -> Any:
            cmd = list(args[0]) if args else []
            if any("show-toplevel" in part for part in cmd):
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: boom")
            return real_run(*args, **kw)

        monkeypatch.setattr(mod.subprocess, "run", _run_toplevel_fail)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "show-toplevel 无结果" in caplog.text

    @pytest.mark.asyncio
    async def test_toplevel_probe_timeout_degrades(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path, caplog: Any
    ) -> None:
        """态7d：show-toplevel 取证超时（进程边界异常）→ 降级放行。"""
        task = await service.create_task(title="评估任务")
        ws = tmp_path / "ws"
        ws.mkdir()
        _git(ws, "init")
        real_run = subprocess.run

        def _run_toplevel_timeout(*args: Any, **kw: Any) -> Any:
            cmd = list(args[0]) if args else []
            if any("show-toplevel" in part for part in cmd):
                raise subprocess.TimeoutExpired(cmd, 30)
            return real_run(*args, **kw)

        monkeypatch.setattr(mod.subprocess, "run", _run_toplevel_timeout)
        tool, state_writer = _inject_tool(mod, monkeypatch, service, _rows(task, ws))
        with caplog.at_level(logging.WARNING):
            out = await tool._complete_task(service, task, _eval_result(task.id))
        _assert_completed(out, state_writer)
        assert "git 取证失败" in caplog.text

# -*- coding: utf-8 -*-
# @feature: FP-0.2.二 内部模块 manifest（worktree gitdir 跨侧统一） | @ci: python-coverage
"""_workspace_git_ops._normalize_worktree_gitdir_links 单测（真实 git 仓）。

病根（SWE 种子实跑实证）：`git worktree add` 在哪侧执行就把双向链接写成
哪侧绝对路径（宿主 `D:/` 与 WSL `/mnt/d` 互不解析），另一侧的合并门控/
评估 oracle 会 "fatal: not a git repository"。统一为相对路径后任一侧
创建、任一侧可用（相对解析不含挂载前缀）。
"""
from __future__ import annotations

import importlib.util
import logging
import shutil
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typing import Any

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_ops_mod():
    """按唯一模块名装载 _workspace_git_ops（避开车道内裸名互覆）。"""
    path = _PLUGIN_DIR / "_workspace_git_ops.py"
    spec = importlib.util.spec_from_file_location(
        "isolation_workspace_git_ops_normalize_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _force_write(path: Path, text: str) -> None:
    """覆盖写（含 Windows 隐藏文件：CPython 无法以写模式打开隐藏文件）。"""
    tmp = path.with_name(path.name + ".tmp_test")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@local",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@local"}
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env)
    assert proc.returncode == 0, f"git {args} 失败: {proc.stderr}"
    return proc.stdout


@pytest.fixture
def ops_mod():
    return _load_ops_mod()


@pytest.fixture
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """真实仓 + 一个 worktree；返回 (repo, ws_dir, ops 类)。"""
    mod = _load_ops_mod()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("hello", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    ws = tmp_path / "wt_demo__wt_t1"
    _git("worktree", "add", "-b", "task/t1", str(ws), cwd=repo)
    return repo, ws, mod._GitOpsMixin


def _assert_relative_and_usable(repo: Path, ws: Path, ops: type) -> None:
    git_file = (ws / ".git").read_text(encoding="utf-8").strip()
    assert git_file.startswith("gitdir: "), git_file
    pointed = git_file.split(":", 1)[1].strip()
    assert not Path(pointed).is_absolute(), f"仍是绝对路径: {pointed}"
    # 相对路径从 ws_dir 出发必须真实命中 admin 目录
    assert (ws / pointed).is_dir(), f"相对 gitdir 解析失败: {pointed}"
    # admin 侧回指必须绝对（git 以 cwd 相对解析该文件，相对形态会被
    # worktree prune 判死误删 admin → worktree 悬空，BUG-53 真机实证）
    back = (repo / ".git" / "worktrees" / ws.name / "gitdir").read_text(
        encoding="utf-8").strip()
    assert Path(back).is_absolute(), f"admin 回指不是绝对路径: {back}"
    assert back.endswith(".git"), f"admin 回指应指向 worktree 的 .git 文件: {back}"
    # 本侧 git 可用（rev-parse + status 真跑）
    proc = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ws,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    proc2 = subprocess.run(["git", "status", "--short"], cwd=ws,
                           capture_output=True, text=True)
    assert proc2.returncode == 0, proc2.stderr


def test_normalized_worktree_survives_prune(
    repo_with_worktree: tuple[Path, Path, Any],
) -> None:
    """归一后的 worktree 必须能活过 git worktree prune（BUG-53 真机实证：

    双侧相对形态下，并发任务的清理 prune 会把在途 worktree 的 admin 判死
    删除 → worktree 悬空、合并门控 not a git repository 误判失败）。
    """
    repo, ws, ops = repo_with_worktree
    inst = ops.__new__(ops)
    inst._normalize_worktree_gitdir_links(ws, repo)
    proc = subprocess.run(["git", "worktree", "prune", "--dry-run", "--verbose"],
                          cwd=repo, capture_output=True, text=True)
    combined = proc.stdout + proc.stderr
    assert ws.name not in combined, (
        f"归一后的 worktree 被 prune 判死: {combined}"
    )


def test_normalize_rewrites_windows_absolute_to_relative(
    repo_with_worktree: tuple[Path, Path, Any],
) -> None:
    """宿主形态（D:/ 绝对）→ 相对化后 .git/admin 回指均相对且本侧可用。"""
    repo, ws, ops = repo_with_worktree
    admin = repo / ".git" / "worktrees" / ws.name
    _force_write(ws / ".git", f"gitdir: {repo}/.git/worktrees/{ws.name}\n")
    inst = ops.__new__(ops)
    inst._normalize_worktree_gitdir_links(ws, repo)
    _assert_relative_and_usable(repo, ws, ops)
    assert admin.is_dir()


def test_normalize_rewrites_wsl_absolute_to_relative(
    repo_with_worktree: tuple[Path, Path, Any],
) -> None:
    """WSL 形态（/mnt/d 绝对，宿主不可达）→ 仅取 worktrees 段定位 admin，
    依本侧真实位置改写为相对（不依赖外路径可读）。"""
    repo, ws, ops = repo_with_worktree
    _force_write(ws / ".git",
                 f"gitdir: /mnt/d/elsewhere/repo/.git/worktrees/{ws.name}\n")
    inst = ops.__new__(ops)
    inst._normalize_worktree_gitdir_links(ws, repo)
    _assert_relative_and_usable(repo, ws, ops)


def test_normalize_is_idempotent(
    repo_with_worktree: tuple[Path, Path, Any],
) -> None:
    repo, ws, ops = repo_with_worktree
    inst = ops.__new__(ops)
    inst._normalize_worktree_gitdir_links(ws, repo)
    first = (ws / ".git").read_text(encoding="utf-8")
    inst._normalize_worktree_gitdir_links(ws, repo)
    assert (ws / ".git").read_text(encoding="utf-8") == first


def test_normalize_tolerates_missing_git_file(
    repo_with_worktree: tuple[Path, Path, Any],
) -> None:
    repo, ws, ops = repo_with_worktree
    (ws / ".git").unlink()
    inst = ops.__new__(ops)
    inst._normalize_worktree_gitdir_links(ws, repo)  # 不抛即通过


def test_normalize_no_op_when_admin_root_missing(
    repo_with_worktree: tuple[Path, Path, Any],
) -> None:
    """repo/.git/worktrees 不存在（非 worktree 或已清理）→ 静默 no-op。"""
    repo, ws, ops = repo_with_worktree
    import shutil

    shutil.rmtree(repo / ".git" / "worktrees")
    before = (ws / ".git").read_text(encoding="utf-8")
    inst = ops.__new__(ops)
    inst._normalize_worktree_gitdir_links(ws, repo)
    assert (ws / ".git").read_text(encoding="utf-8") == before


def test_normalize_warns_and_keeps_when_admin_dir_unlocatable(
    repo_with_worktree: tuple[Path, Path, Any], caplog: Any
) -> None:
    """gitdir 指向的 admin 目录定位不到 → 告警保持原样（不阻断建树）。"""
    repo, ws, ops = repo_with_worktree
    _force_write(ws / ".git", f"gitdir: {repo}/.git/worktrees/no_such_wt\n")
    inst = ops.__new__(ops)
    with caplog.at_level(logging.WARNING, logger="agentos.isolation.workspace_git_ops"):
        inst._normalize_worktree_gitdir_links(ws, repo)
    assert any("未定位到" in r.getMessage() for r in caplog.records)
    assert "no_such_wt" in (ws / ".git").read_text(encoding="utf-8")


def test_normalize_warns_when_revparse_fails_after_relativize(
    repo_with_worktree: tuple[Path, Path, Any], caplog: Any
) -> None:
    """相对化后本侧 rev-parse 失败（admin 空壳无 HEAD）→ 告警且保留相对形态。"""
    repo, ws, ops = repo_with_worktree
    admin = repo / ".git" / "worktrees" / ws.name
    shutil.rmtree(admin)  # 换成空壳目录：is_dir 成立但不是可用 git admin
    admin.mkdir(parents=True)
    _force_write(ws / ".git", f"gitdir: {repo}/.git/worktrees/{ws.name}\n")
    inst = ops.__new__(ops)
    with caplog.at_level(logging.WARNING, logger="agentos.isolation.workspace_git_ops"):
        inst._normalize_worktree_gitdir_links(ws, repo)
    assert any("rev-parse 失败" in r.getMessage() for r in caplog.records)
    pointed = (ws / ".git").read_text(encoding="utf-8").strip().split(":", 1)[1].strip()
    assert not Path(pointed).is_absolute()


def test_resolve_admin_dir_returns_none_on_unreadable_git_file(
    tmp_path: Path, ops_mod: Any
) -> None:
    """.git 文件读不出（此处为目录形态触发 OSError）→ None。"""
    fake = tmp_path / ".git"
    fake.mkdir()
    assert ops_mod._GitOpsMixin._resolve_worktree_admin_dir(fake, tmp_path / "wt") is None


def test_resolve_admin_dir_returns_none_on_non_gitdir_text(
    tmp_path: Path, ops_mod: Any
) -> None:
    """内容不是 gitdir: 行 → None。"""
    fake = tmp_path / ".git"
    fake.write_text("hello world\n", encoding="utf-8")
    assert ops_mod._GitOpsMixin._resolve_worktree_admin_dir(fake, tmp_path / "wt") is None


@pytest.mark.parametrize(
    "text",
    [
        "gitdir: D:/repo/.git\n",  # 无 worktrees 段
        "gitdir: D:/repo/.git/worktrees\n",  # worktrees 是末段（无名可取）
        "gitdir: /mnt/d/repo/.git/worktrees/\n",
    ],
)
def test_resolve_admin_dir_returns_none_on_malformed_worktrees_segment(
    tmp_path: Path, ops_mod: Any, text: str
) -> None:
    """worktrees 段缺失/无名 → None（不做猜测映射）。"""
    fake = tmp_path / ".git"
    fake.write_text(text, encoding="utf-8")
    assert ops_mod._GitOpsMixin._resolve_worktree_admin_dir(fake, tmp_path / "wt") is None

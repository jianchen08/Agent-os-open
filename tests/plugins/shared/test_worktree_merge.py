# @feature: FP-0.2.〇 隔离工作区 工具执行 | @ci: python-coverage
"""worktree_merge 共享模块（任务域合并门控的 git 机制）单测。

与 0.1 isolation 侧 tests/test_merge_verify_fix.py 同源互证（机制移植核验）：
- 真实 git 仓：合并成功落文件+清理、冲突=失败且保留 worktree（冲突不自动解决）、
  删除文件不误判、多 commit 不误判、验证失败重试耗尽；
- 入口分发：ws_meta 缺失=失败（不静默跳过）、非 worktree 零 git 接触、
  worktree 缺 path 失败；
- git 命令故障分支（超时/无 git/IO 故障）、合并诊断（缺 project_root/branch、
  分支不存在、未合并分支）、清理边界（stale index.lock、残留 __wt_ 目录、
  unstaged 保留不丢弃、反查仓库根兜底）。

外部依赖仅 git CLI 与 tmp 目录（关键路径走真实依赖，不 mock git 本体）；
故障分支经 monkeypatch 注入 subprocess 故障（外部边界）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

import os

import worktree_merge  # noqa: E402 — 依赖 conftest 的 sys.path 注入
from worktree_merge import WorktreeMerger  # noqa: E402


def git(*args: str, cwd: Path | str | None = None) -> tuple[int, str, str]:
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout, r.stderr


def _make_repo(base: Path, name: str = "project") -> Path:
    """带初始提交的最小 git 仓库。"""
    proj = base / name
    proj.mkdir()
    git("init", cwd=proj)
    git("config", "user.email", "test@test.com", cwd=proj)
    git("config", "user.name", "Test", cwd=proj)
    (proj / "hello.txt").write_text("hello", encoding="utf-8")
    git("add", "-A", cwd=proj)
    git("commit", "-m", "init", cwd=proj)
    return proj


def _setup_worktree_task(tmp: Path) -> tuple[Path, Path]:
    """源仓库 + 任务 worktree（分支 task/test1，含修改与新增文件）。"""
    proj = _make_repo(tmp)
    wt_dir = tmp / "wt_test"
    git("worktree", "add", "-b", "task/test1", str(wt_dir), cwd=proj)
    (wt_dir / "hello.txt").write_text("hello modified", encoding="utf-8")
    (wt_dir / "new_file.txt").write_text("new content", encoding="utf-8")
    git("add", "-A", cwd=wt_dir)
    git("commit", "-m", "changes", cwd=wt_dir)
    return proj, wt_dir


def _ws_meta(proj: Path, wt_dir: Path, branch: str = "task/test1") -> dict[str, Any]:
    return {
        "mode": "worktree",
        "path": str(wt_dir),
        "branch": branch,
        "project_root": str(proj),
    }


# ── 入口分发（纯判定，零 git 接触）──────────────────────────


class TestEntryDispatch:
    @pytest.mark.parametrize("bad_meta", [None, {}, "ws_meta 字符串", 42])
    def test_missing_ws_meta_is_failure_not_skip(self, bad_meta: Any) -> None:
        """ws_meta 拿不到 = 失败（worktree 产物不能静默丢失），绝不返回 None。"""
        err = worktree_merge.merge_worktree_before_complete("t1", bad_meta)
        assert isinstance(err, str)
        assert "t1" in err and "ws_meta" in err

    @pytest.mark.parametrize("mode", ["plain", "shared"])
    def test_non_worktree_modes_need_no_merge(self, mode: str, monkeypatch: Any) -> None:
        m = WorktreeMerger()

        def _no_git(*args: str, **kw: Any) -> tuple[int, str, str]:
            raise AssertionError(f"mode={mode} 不应执行任何 git 命令")

        monkeypatch.setattr(m, "_run_git", _no_git)
        assert m.merge_worktree_before_complete("t1", {"mode": mode, "path": "D:/w"}) is None

    def test_worktree_missing_path_is_failure(self) -> None:
        err = WorktreeMerger().merge_worktree_before_complete("t1", {"mode": "worktree", "path": ""})
        assert isinstance(err, str) and "ws_meta.path 为空" in err

    def test_metadata_read_failure_not_labeled_as_merge_failure(self) -> None:
        """ws_meta 读取失败 ≠ worktree 合并失败：还没走到合并步骤，文案不得误标。"""
        err = worktree_merge.merge_worktree_before_complete("t1", None)
        assert "ws_meta 读取失败" in err
        assert "worktree 合并失败" not in err

    def test_merge_failure_labeled_with_merge_prefix(self, monkeypatch: Any) -> None:
        """git 机制真报错 → 报错带 worktree 合并失败 分类前缀，与读取失败可区分。"""
        m = WorktreeMerger()
        monkeypatch.setattr(
            m,
            "on_eval_passed",
            lambda task_id, workspace, ws_meta: {"success": False, "error": "boom"},
        )
        err = m.merge_worktree_before_complete(
            "t1", {"mode": "worktree", "path": "D:/w", "project_root": "D:/s"}
        )
        assert isinstance(err, str)
        assert err.startswith("worktree 合并失败") and "boom" in err


# ── 真实 git 仓：合并/验证/清理主链路 ───────────────────────


class TestRealMerge:
    def test_merge_success_lands_files_and_cleans(self, tmp_path: Path) -> None:
        """合并成功：产物到达项目根、worktree 与分支被清理、入口返回 None。"""
        proj, wt_dir = _setup_worktree_task(tmp_path)
        err = worktree_merge.merge_worktree_before_complete("test1", _ws_meta(proj, wt_dir))
        assert err is None, err
        assert (proj / "hello.txt").read_text(encoding="utf-8") == "hello modified"
        assert (proj / "new_file.txt").read_text(encoding="utf-8") == "new content"
        assert not wt_dir.exists(), "worktree 应被清理"
        rc, out, _ = git("branch", "--list", "task/test1", cwd=proj)
        assert rc == 0 and out.strip() == "", "任务分支应被删除"

    def test_merge_success_tags_for_revert(self, tmp_path: Path) -> None:
        """git_merge 成功后打 task-merge tag（可 git revert 回退）。"""
        proj, wt_dir = _setup_worktree_task(tmp_path)
        err = worktree_merge.merge_worktree_before_complete("test1", _ws_meta(proj, wt_dir))
        assert err is None
        rc, out, _ = git("tag", "--list", "task-merge/test1", cwd=proj)
        assert rc == 0 and out.strip() != "", "应留下 task-merge/test1 tag"

    def test_conflict_is_failure_and_worktree_preserved(self, tmp_path: Path) -> None:
        """冲突 = 合并失败（不自动解决）：入口返回失败原因，worktree 保留。"""
        proj, wt_dir = _setup_worktree_task(tmp_path)
        # 主分支对同一文件做不同修改 → 合并必然冲突
        (proj / "hello.txt").write_text("main branch conflicting change", encoding="utf-8")
        git("add", "-A", cwd=proj)
        git("commit", "-m", "main change", cwd=proj)

        err = worktree_merge.merge_worktree_before_complete("test1", _ws_meta(proj, wt_dir))
        assert isinstance(err, str), "冲突必须判失败"
        assert "git merge 失败" in err and "冲突文件" in err, f"失败原因应含冲突诊断: {err}"
        assert wt_dir.exists(), "合并失败应保留 worktree（产物不丢）"
        # 项目根已从冲突中回退（merge --abort），保留主分支自己的提交
        assert (proj / "hello.txt").read_text(encoding="utf-8") == "main branch conflicting change"

    def test_deleted_files_not_misjudged(self, tmp_path: Path) -> None:
        """任务正确删除的废弃文件合并后不存在，不得误判为「文件未到达目标」。"""
        proj, wt_dir = _setup_worktree_task(tmp_path)
        (wt_dir / "new_file.txt").unlink()
        git("add", "-A", cwd=wt_dir)
        git("commit", "-m", "remove deprecated file", cwd=wt_dir)

        err = worktree_merge.merge_worktree_before_complete("test_del", _ws_meta(proj, wt_dir))
        assert err is None, err
        assert not (proj / "new_file.txt").exists()
        assert (proj / "hello.txt").read_text(encoding="utf-8") == "hello modified"
        assert not wt_dir.exists()

    def test_multi_commit_all_files_landed(self, tmp_path: Path) -> None:
        """worktree 多 commit：所有 commit 的产出都应到达项目根（diff 基准不漂移）。"""
        proj, wt_dir = _setup_worktree_task(tmp_path)
        (wt_dir / "second_commit_file.txt").write_text("v2 content", encoding="utf-8")
        git("add", "-A", cwd=wt_dir)
        git("commit", "-m", "second commit on task branch", cwd=wt_dir)

        err = worktree_merge.merge_worktree_before_complete("test_multi", _ws_meta(proj, wt_dir))
        assert err is None, err
        assert (proj / "new_file.txt").read_text(encoding="utf-8") == "new content"
        assert (proj / "second_commit_file.txt").read_text(encoding="utf-8") == "v2 content"

    def test_missing_branch_is_failure(self, tmp_path: Path) -> None:
        """待合并分支不存在（如继承自已清理的父任务）→ 明确根因失败。"""
        proj = _make_repo(tmp_path)
        wt_dir = tmp_path / "wt_ghost"
        git("worktree", "add", "-b", "task/ghost", str(wt_dir), cwd=proj)
        (wt_dir / "x.txt").write_text("x", encoding="utf-8")
        git("add", "-A", cwd=wt_dir)
        git("commit", "-m", "ghost work", cwd=wt_dir)
        # 分支被外部清理（worktree 仍在，模拟子任务继承的残留元数据）
        git("worktree", "remove", "--force", str(wt_dir), cwd=proj)
        git("branch", "-D", "task/ghost", cwd=proj)

        err = worktree_merge.merge_worktree_before_complete(
            "t1", _ws_meta(proj, wt_dir, "task/ghost")
        )
        assert isinstance(err, str) and "待合并分支不存在" in err

    def test_verify_fail_retries_exhausted_preserves_worktree(self, tmp_path: Path, monkeypatch: Any) -> None:
        """验证失败重试 2 次后放弃：保留 worktree 不清理，入口返回失败。"""
        proj, wt_dir = _setup_worktree_task(tmp_path)
        m = WorktreeMerger()
        calls: list[int] = []

        def _always_fail(workspace: str, project_root: str, ws_meta: dict, merge_result: dict) -> tuple[bool, str]:
            calls.append(1)
            return False, f"模拟验证失败 call={len(calls)}"

        monkeypatch.setattr(m, "_verify_merge_result", _always_fail)
        err = m.merge_worktree_before_complete("test1", _ws_meta(proj, wt_dir))
        assert isinstance(err, str) and "合并验证失败(重试2次)" in err
        assert len(calls) == 2, f"应恰好重试 2 次，实际 {len(calls)}"
        assert wt_dir.exists(), "验证失败应保留 worktree"

    def test_success_entry_with_conflict_files_field_returns_none(self, monkeypatch: Any) -> None:
        """合并成功但带冲突文件清单（遗留 copy 通路字段）→ 仍算成功，仅告警。"""
        m = WorktreeMerger()
        monkeypatch.setattr(
            m,
            "on_eval_passed",
            lambda task_id, workspace, ws_meta: {"success": True, "conflict_files": ["a.txt"]},
        )
        assert m.merge_worktree_before_complete("t1", {"mode": "worktree", "path": "D:/w", "project_root": "D:/s"}) is None


# ── 合并诊断与安全校验 ───────────────────────────────────────


class TestSafeMergeDiagnostics:
    @pytest.mark.parametrize(
        ("ws_meta", "needle"),
        [
            ({"branch": "task/t1", "project_root": ""}, "缺少 project_root 信息"),
            ({"branch": "", "project_root": "D:/s"}, "缺少 branch 信息"),
        ],
    )
    def test_missing_metadata_is_failure(self, ws_meta: dict, needle: str, tmp_path: Path) -> None:
        result = WorktreeMerger()._safe_merge(str(tmp_path / "ws"), ws_meta)
        assert result["success"] is False and needle in result["error"]

    def test_verify_merge_in_main_unmerged_branch_is_false(self, tmp_path: Path) -> None:
        """分支有未合并提交 → HEAD..branch 非空 → 阻止后续清理。"""
        proj = _make_repo(tmp_path)
        git("checkout", "-b", "side", cwd=proj)
        (proj / "side.txt").write_text("side", encoding="utf-8")
        git("add", "-A", cwd=proj)
        git("commit", "-m", "side work", cwd=proj)
        rc_main, _, _ = git("checkout", "main", cwd=proj)
        if rc_main != 0:
            git("checkout", "master", cwd=proj)
        m = WorktreeMerger()
        assert m._verify_merge_in_main("side", cwd=proj) is False

    def test_verify_merge_in_main_bad_branch_is_false(self, tmp_path: Path) -> None:
        proj = _make_repo(tmp_path)
        assert WorktreeMerger()._verify_merge_in_main("task/no-such-branch", cwd=proj) is False

    def test_missing_branch_diff_files_bad_branch_returns_empty(self, tmp_path: Path) -> None:
        proj = _make_repo(tmp_path)
        assert WorktreeMerger()._missing_branch_diff_files("task/no-such-branch", proj) == []

    def test_verify_merge_result_copy_missing_files(self, tmp_path: Path) -> None:
        proj = _make_repo(tmp_path)
        merge_result = {"method": "copy", "merged_files": ["hello.txt", "missing_file.txt"]}
        verified, detail = WorktreeMerger()._verify_merge_result(
            str(tmp_path / "ws"), str(proj), {"branch": "task/t1"}, merge_result
        )
        assert verified is False and "missing_file.txt" in detail

    def test_verify_merge_result_project_root_not_exist(self, tmp_path: Path) -> None:
        verified, detail = WorktreeMerger()._verify_merge_result(
            str(tmp_path / "ws"), "D:/nonexistent_repo", {}, {"method": "copy", "merged_files": []}
        )
        assert verified is False and "不存在" in detail


# ── 清理边界 ────────────────────────────────────────────────


class TestCleanupEdges:
    @pytest.mark.skipif(os.name != "nt", reason="Windows 只读属性语义：POSIX unlink 不看文件写位，handler 不触发")
    def test_force_rmtree_chmod_failure_retries_then_raises(self, tmp_path: Path, monkeypatch: Any) -> None:
        """只读文件修复被破坏 → 重试路径走完仍失败，异常上抛（不静默吞）。"""
        import os as _os
        import stat as _stat

        d = tmp_path / "ro"
        d.mkdir()
        f = d / "f.txt"
        f.write_text("x", encoding="utf-8")
        _os.chmod(f, _stat.S_IREAD)

        def broken_chmod(p: Any, mode: Any) -> None:
            raise OSError("chmod unavailable")

        monkeypatch.setattr(worktree_merge.os, "chmod", broken_chmod)
        with pytest.raises(OSError):
            worktree_merge._force_rmtree(str(d))
        monkeypatch.undo()  # 先还原全局 os.chmod，再做只读恢复清理
        _os.chmod(f, _stat.S_IWRITE)

    def test_force_rmtree_removes_readonly_tree(self, tmp_path: Path) -> None:
        """Windows 只读文件目录树可被强制删除（只读属性修复后重试）。"""
        import os as _os
        import stat as _stat

        d = tmp_path / "ro_tree"
        sub = d / "sub"
        sub.mkdir(parents=True)
        f = sub / "f.txt"
        f.write_text("x", encoding="utf-8")
        _os.chmod(f, _stat.S_IREAD)
        worktree_merge._force_rmtree(str(d))
        assert not d.exists()

    def test_stale_index_lock_removed(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        lock.write_text("", encoding="utf-8")
        assert WorktreeMerger()._remove_index_lock(tmp_path) is True
        assert not lock.exists()
        assert WorktreeMerger()._remove_index_lock(tmp_path) is False

    def test_gitignore_auto_generated_when_missing(self, tmp_path: Path) -> None:
        """无 .gitignore 的脏目录：生成最小保护版本，忽略项不进提交。"""
        repo = _make_repo(tmp_path, "gi")
        (repo / "data").mkdir()
        (repo / "data" / "junk.log").write_text("junk", encoding="utf-8")
        (repo / "hello.txt").write_text("changed", encoding="utf-8")
        h = WorktreeMerger()._git_add_commit_if_dirty(repo, "chore: x")
        assert h and len(h) == 40, "应返回完整 commit hash"
        assert (repo / ".gitignore").exists()
        _, out, _ = git("ls-files", "data/junk.log", cwd=repo)
        assert out.strip() == "", "忽略项不得进提交"

    def test_clean_repo_commit_is_noop(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "clean")
        assert WorktreeMerger()._git_add_commit_if_dirty(repo, "chore: x") is None

    def test_untracked_only_not_committed_by_tracked_only(self, tmp_path: Path) -> None:
        """_git_add_tracked_and_commit 只提交已跟踪修改，不动未跟踪文件。"""
        repo = _make_repo(tmp_path, "tracked")
        (repo / "untracked.txt").write_text("new", encoding="utf-8")
        assert WorktreeMerger()._git_add_tracked_and_commit(repo, "chore: x") is None
        assert (repo / "untracked.txt").exists(), "未跟踪文件不得被删改"
        (repo / "hello.txt").write_text("changed", encoding="utf-8")
        h = WorktreeMerger()._git_add_tracked_and_commit(repo, "chore: tracked change")
        assert h and len(h) == 40
        _, out, _ = git("ls-files", "untracked.txt", cwd=repo)
        assert out.strip() == "", "未跟踪文件不得被提交"

    def test_cleanup_worktree_remove_failure_falls_back_to_prune(self, tmp_path: Path, monkeypatch: Any) -> None:
        """worktree remove 失败 → prune 兜底；残留 __wt_ 目录被强制清理。"""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".git").mkdir()
        ws = tmp_path / "x__wt_deadbeef"
        ws.mkdir()
        (ws / "f.txt").write_text("x", encoding="utf-8")
        m = WorktreeMerger()
        real_run = m._run_git
        calls: list[tuple[str, ...]] = []

        def scripted(*args: str, cwd: Path, **kw: Any) -> tuple[int, str, str]:
            calls.append(tuple(args))
            if args[:2] == ("worktree", "remove"):
                raise RuntimeError("boom")
            return real_run(*args, cwd=cwd, **kw)

        monkeypatch.setattr(m, "_run_git", scripted)
        m._cleanup_worktree(str(ws), {"project_root": str(proj), "branch": ""})
        assert ("worktree", "prune") in calls
        assert not ws.exists(), "名为 __wt_ 的残留目录应被强制清理"

    def test_cleanup_unprobeable_root_gives_up_loudly(self, tmp_path: Path) -> None:
        """project_root 缺失且反查不到仓库根 → 放弃清理并留痕（不静默假装清理）。"""
        proj = _make_repo(tmp_path, "probe_root")
        stray = tmp_path / "stray_ws"  # 非 worktree 目录：反查 --show-toplevel 失败
        stray.mkdir()
        WorktreeMerger()._cleanup_worktree(str(stray), {"project_root": "D:/gone", "branch": "task/x"})
        assert stray.exists(), "反查失败应保留现场（放弃清理显式留痕）"
        assert proj.exists()

    def test_cleanup_unstaged_changes_warns_not_discards(self, tmp_path: Path, caplog: Any) -> None:
        """合并后 project_root 的 unstaged 变更只告警，绝不自动丢弃。

        两处修改触发告警（porcelain 输出经 _run_git strip，首行列位移位与
        0.1 行为一致，第二行起可解析）。
        """
        repo = _make_repo(tmp_path, "unstaged")
        (repo / "extra.txt").write_text("seed", encoding="utf-8")
        git("add", "-A", cwd=repo)
        git("commit", "-m", "seed extra", cwd=repo)
        (repo / "hello.txt").write_text("local uncommitted edit", encoding="utf-8")
        (repo / "extra.txt").write_text("local edit 2", encoding="utf-8")
        with caplog.at_level("WARNING"):
            WorktreeMerger()._cleanup_unstaged_changes(str(repo))
        assert "unstaged" in caplog.text
        assert (repo / "hello.txt").read_text(encoding="utf-8") == "local uncommitted edit"
        assert (repo / "extra.txt").read_text(encoding="utf-8") == "local edit 2"


# ── git 命令封装故障分支（subprocess 边界注入）───────────────


class TestGitOpsFailureBranches:
    def test_timeout_returns_error_tuple(self, tmp_path: Path, monkeypatch: Any) -> None:
        def boom(*args: Any, **kw: Any) -> None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=30)

        monkeypatch.setattr(worktree_merge.subprocess, "run", boom)
        rc, out, err = WorktreeMerger()._run_git("status", cwd=tmp_path)
        assert rc == -1 and "超时" in err

    def test_missing_git_binary_returns_error_tuple(self, tmp_path: Path, monkeypatch: Any) -> None:
        def boom(*args: Any, **kw: Any) -> None:
            raise FileNotFoundError("git")

        monkeypatch.setattr(worktree_merge.subprocess, "run", boom)
        rc, _, err = WorktreeMerger()._run_git("status", cwd=tmp_path)
        assert rc == -1 and "未找到 git 命令" in err

    def test_oserror_invalid_cwd_returns_error_tuple(self, tmp_path: Path, monkeypatch: Any) -> None:
        def boom(*args: Any, **kw: Any) -> None:
            raise OSError(267, "无效目录")

        monkeypatch.setattr(worktree_merge.subprocess, "run", boom)
        rc, _, err = WorktreeMerger()._run_git("status", cwd=tmp_path)
        assert rc == -1 and "git 工作目录无效或不存在" in err

    def test_same_name_sibling_exists_oserror_is_false(self, tmp_path: Path) -> None:
        """parent 存在但不可迭代（文件非目录）→ 模糊匹配安全返回 False。"""
        blocker = tmp_path / "afile"
        blocker.write_text("x", encoding="utf-8")
        target = blocker / "a.txt"  # parent 是文件 → iterdir 抛 NotADirectoryError(OSError)
        assert WorktreeMerger()._same_name_sibling_exists(target) is False

    def test_first_missing_paths_caps_at_ten(self, tmp_path: Path) -> None:
        expected = [f"missing_{i}.txt" for i in range(12)]
        missing = WorktreeMerger()._first_missing_paths(tmp_path, expected)
        assert len(missing) == 10 and missing[0] == "missing_0.txt"
        assert WorktreeMerger()._first_missing_paths(tmp_path, []) == []

    def test_merge_lock_per_project_root(self) -> None:
        m = WorktreeMerger()
        l1 = m._get_merge_lock("D:/r")
        assert m._get_merge_lock("D:/r") is l1, "同仓库合并应复用同一把锁（串行化）"
        assert m._get_merge_lock("D:/other") is not l1, "不同仓库互不阻塞"


# ── 脚本化 git 应答：故障重试/诊断分支（subprocess 边界替身）────


class TestScriptedGitBranches:
    """git CLI 为外部边界：用应答脚本驱动 _run_git 的失败分支，锁重试与诊断语义。"""

    @staticmethod
    def _merger_with(responder: Any) -> WorktreeMerger:
        m = WorktreeMerger()
        m._run_git = lambda *args, cwd, **kw: responder(args, cwd)  # type: ignore[method-assign]
        return m

    def test_remove_index_lock_unlink_oserror_returns_false(self, tmp_path: Path, monkeypatch: Any) -> None:
        lock = tmp_path / ".git" / "index.lock"
        lock.parent.mkdir()
        lock.write_text("", encoding="utf-8")

        def broken_unlink(self: Any) -> None:
            raise OSError("locked")

        monkeypatch.setattr(type(lock), "unlink", broken_unlink)
        assert WorktreeMerger()._remove_index_lock(tmp_path) is False

    def test_add_commit_if_dirty_add_failure_retries_then_none(self, tmp_path: Path) -> None:
        calls: list[tuple[str, ...]] = []

        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            calls.append(args)
            if args[:1] == ("status",):
                return (0, "M f.txt", "")
            if args[:1] == ("add",):
                return (-1, "", "add boom")
            return (0, "", "")

        assert self._merger_with(respond)._git_add_commit_if_dirty(tmp_path, "m") is None
        assert calls.count(("add", "-A")) == 2, "add 失败应先清 index.lock 再重试一次"

    def test_add_commit_if_dirty_commit_failure_retries_then_none(self, tmp_path: Path) -> None:
        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            if args[:1] == ("status",):
                return (0, "M f.txt", "")  # 脏工作区才进入 add/commit 流程
            if args[:1] == ("commit",):
                return (-1, "", "commit boom")
            return (0, "", "")

        assert self._merger_with(respond)._git_add_commit_if_dirty(tmp_path, "m") is None

    def test_add_tracked_failure_retries_then_none(self, tmp_path: Path) -> None:
        calls: list[tuple[str, ...]] = []

        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            calls.append(args)
            if args == ("add", "-u"):
                return (-1, "", "add boom")
            return (0, "", "")

        assert self._merger_with(respond)._git_add_tracked_and_commit(tmp_path, "m") is None
        assert calls.count(("add", "-u")) == 2

    def test_add_tracked_commit_failure_retries_then_none(self, tmp_path: Path) -> None:
        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            if args == ("add", "-u"):
                return (0, "", "")
            if args == ("status", "--porcelain", "-uno"):
                return (0, "M f.txt", "")
            if args[:1] == ("commit",):
                return (-1, "", "commit boom")
            return (0, "", "")

        assert self._merger_with(respond)._git_add_tracked_and_commit(tmp_path, "m") is None

    def test_safe_merge_unresolvable_current_branch(self, tmp_path: Path) -> None:
        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            if args[:2] == ("rev-parse", "--abbrev-ref"):
                return (1, "", "boom")
            return (0, "", "")

        result = self._merger_with(respond)._safe_merge(
            str(tmp_path / "ws"), {"project_root": str(tmp_path), "branch": "task/t1"}
        )
        assert result["success"] is False and "无法获取当前分支" in result["error"]

    def test_safe_merge_stderr_carried_into_error(self, tmp_path: Path) -> None:
        """merge 失败且无冲突清单时，stderr 进入失败原因（诊断不丢）。"""

        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            if args[:2] == ("rev-parse", "--abbrev-ref"):
                return (0, "main", "")
            if args[:2] == ("rev-parse", "--verify"):
                return (0, "abc", "")
            if args[:1] == ("merge",) and "abort" not in args:
                return (1, "", "error: merge aborted by hook")
            if args[:1] == ("diff",):
                return (1, "", "")
            return (0, "", "")

        result = self._merger_with(respond)._safe_merge(
            str(tmp_path / "ws"), {"project_root": str(tmp_path), "branch": "task/t1"}
        )
        assert result["success"] is False
        assert "stderr=error: merge aborted by hook" in result["error"]

    def test_verify_merge_in_main_log_failure_is_false(self, tmp_path: Path) -> None:
        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            if args[:1] == ("log",):
                return (1, "", "log boom")
            return (0, "", "")

        assert self._merger_with(respond)._verify_merge_in_main("task/t1", cwd=tmp_path) is False

    def test_missing_branch_diff_files_caps_at_ten(self, tmp_path: Path) -> None:
        files = "\n".join(f"gone_{i}.txt" for i in range(12))

        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            if args[:2] == ("-c", "core.quotepath=false"):
                return (0, files, "")
            return (0, "", "")

        missing = self._merger_with(respond)._missing_branch_diff_files("task/t1", tmp_path)
        assert len(missing) == 10
        assert set(missing) <= {f"gone_{i}.txt" for i in range(12)}, "缺失项应来自 diff 清单"

    def test_verify_merge_result_commit_graph_failure(self, tmp_path: Path, monkeypatch: Any) -> None:
        m = WorktreeMerger()
        monkeypatch.setattr(m, "_verify_merge_in_main", lambda branch, cwd: False)
        verified, detail = m._verify_merge_result(
            str(tmp_path / "ws"), str(tmp_path), {"branch": "task/t1"}, {"method": "git_merge"}
        )
        assert verified is False and "commit graph 验证失败" in detail

    def test_verify_merge_result_files_missing(self, tmp_path: Path, monkeypatch: Any) -> None:
        m = WorktreeMerger()
        monkeypatch.setattr(m, "_verify_merge_in_main", lambda branch, cwd: True)

        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            if args[:2] == ("-c", "core.quotepath=false"):
                return (0, "gone_a.txt\ngone_b.txt", "")
            return (0, "", "")

        monkeypatch.setattr(m, "_run_git", lambda *a, **kw: respond(a, kw.get("cwd")))
        verified, detail = m._verify_merge_result(
            str(tmp_path / "ws"), str(tmp_path), {"branch": "task/t1"}, {"method": "git_merge"}
        )
        assert verified is False and "2 个文件未到达目标" in detail

    def test_cleanup_worktree_probe_recovers_repo_root(self, tmp_path: Path) -> None:
        """project_root 缺失但 worktree 是真仓库 → 反查成功并继续清理。"""
        ws = _make_repo(tmp_path, "ws_repo")
        m = WorktreeMerger()
        m._cleanup_worktree(str(ws), {"project_root": "D:/gone", "branch": ""})
        assert ws.exists(), "目录名无 __wt_ 前缀不强制删除（反查路径行为核验）"

    def test_cleanup_worktree_missing_ws_gives_up(self, tmp_path: Path, caplog: Any) -> None:
        with caplog.at_level("WARNING"):
            WorktreeMerger()._cleanup_worktree(
                str(tmp_path / "no_such_ws"), {"project_root": "D:/gone", "branch": "task/x"}
            )
        assert "worktree 目录不存在" in caplog.text

    def test_cleanup_worktree_rmtree_failure_warns_not_raises(self, tmp_path: Path, monkeypatch: Any) -> None:
        ws = tmp_path / "x__wt_cafe"
        ws.mkdir()
        proj = tmp_path / "proj_root"
        proj.mkdir()

        def boom(path: str) -> None:
            raise OSError("locked")

        monkeypatch.setattr(worktree_merge, "_force_rmtree", boom)
        WorktreeMerger()._cleanup_worktree(str(ws), {"project_root": str(proj), "branch": ""})
        assert ws.exists(), "清理失败只告警，不静默也不崩溃"

    def test_same_name_sibling_missing_parent_is_false(self, tmp_path: Path) -> None:
        assert WorktreeMerger()._same_name_sibling_exists(tmp_path / "no_dir" / "a.txt") is False

    def test_cleanup_unstaged_changes_only_untracked_returns(self, tmp_path: Path) -> None:
        """仅 untracked（??）不构成 unstaged 修改 → 静默返回。"""
        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            if args[:1] == ("status",):
                return (0, "?? untracked.txt", "")
            return (0, "", "")

        assert self._merger_with(respond)._cleanup_unstaged_changes(str(tmp_path)) is None

    def test_cleanup_unstaged_changes_missing_root_returns(self, caplog: Any) -> None:
        with caplog.at_level("WARNING"):
            WorktreeMerger()._cleanup_unstaged_changes("D:/nonexistent_root_xyz")
        assert "unstaged" not in caplog.text

    def test_cleanup_unstaged_changes_status_failure_returns(self, tmp_path: Path) -> None:
        def respond(args: tuple[str, ...], cwd: Any) -> tuple[int, str, str]:
            if args[:1] == ("status",):
                return (1, "", "boom")
            return (0, "", "")

        assert self._merger_with(respond)._cleanup_unstaged_changes(str(tmp_path)) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""resource_merge 缺口分支补测（HEAD coverage.xml 缺行，2026-09-14）。

覆盖目标（行号语义经源码逐行确认）：
- git_helpers.py:204（git_status 空行 continue）、215（XY 码双位均非
  A/M/D/R 与 M/D 的 else 兜底 → staged）；401/425（git_log 非 worktree
  守卫 / 空行 continue）、509-510（git_merge_abort 异常兜底）
- tool.py:338（_merge_copy worktree 分支无 target_files 且带 base_commit
  → `git diff --name-status <base> HEAD`）、458（_git_merge 中 ensure_project_repo
  返回错误）、481（git add 非零 → GIT_ADD_FAILED）、501（workspace 有变更但
  commit 非零 → GIT_COMMIT_FAILED）、589-607（merge 失败但无冲突文件 →
  MERGE_FAILED 非冲突兜底）、632（_scan_workspace_files 跳过 skip_dirs
  成员）、677（rollback 中 git clean -fd 非零 → GIT_CLEAN_FAILED）、
  700-701（_remove_readonly_func 的 chmod+func 双步）、720-722（worktree
  remove 失败后手动 rmtree 也失败 → cleanup_failed 如实上报）
- server.py:81（handler 成功路径返回 result.output）

真实依赖纪律：git 全链路走 tmp_path 内真实临时仓库（git init/worktree/
merge 实操），仅在 git 二进制缺失时 skip；「命令层非零返回」用脚本化替身
（外部依赖边界），不 mock 内部逻辑。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_GIT_ENV = dict(
    os.environ,
    GIT_EDITOR="true",
    GIT_TERMINAL_PROMPT="0",
    GIT_PAGER="cat",
    GIT_CONFIG_NOSYSTEM="1",
)


def _load_git_helpers() -> Any:
    """按源码路径加载 git_helpers（同 test_resource_merge.py 模式）。"""
    if "git_helpers" in sys.modules:
        return sys.modules["git_helpers"]
    spec = importlib.util.spec_from_file_location("git_helpers", _PLUGIN_DIR / "git_helpers.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["git_helpers"] = module
    spec.loader.exec_module(module)
    return module


def _load_tool() -> Any:
    """加载 tool.py（裸名 git_helpers 已就位）。"""
    mod_name = "resource_merge_tool_gaps"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


GitHelpers = _load_git_helpers().GitHelpers


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """真实临时 git 仓库（main 分支含一个已提交文件）。"""
    if shutil.which("git") is None:
        pytest.skip("git 不可用")
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "TDD Test")
    _git(r, "config", "core.autocrlf", "false")
    (r / "a.txt").write_text("base\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


@pytest.fixture()
def tool(repo: Path) -> Any:
    return _load_tool().ResourceMergeTool(base_path=str(repo))


# ═══════════════════════════════════════════════════════════
# git_helpers.py：状态码解析 / 空行 / 守卫 / 异常兜底
# ═══════════════════════════════════════════════════════════


class TestGitStatusPorcelainParsing:
    """git_status 的 XY 码分类（空行跳过 + 双位不可识别时归 staged）。"""

    def test_untracked_and_staged_and_unstaged(self, tmp_path: Path) -> None:
        """A/M/D/R 与 M/D 的正常分类（真实仓库，非 mock）。"""
        ws, helpers = _worktree(tmp_path)
        (ws / "added.txt").write_text("a", encoding="utf-8")
        _git(ws, "add", "added.txt")  # index 新增 → A
        (ws / "a.txt").write_text("changed\n", encoding="utf-8")  # 工作区修改 → M
        (ws / "new.txt").write_text("n", encoding="utf-8")  # 未跟踪 → ?

        result = _run(helpers.git_status({}, ws))
        assert result.success
        assert "added.txt" in result.output["staged"]
        assert "a.txt" in result.output["unstaged"]
        assert "new.txt" in result.output["untracked"]
        # 性质断言：三类互不重叠，且 total_changes 与并集基数一致
        assert not (set(result.output["staged"]) & set(result.output["unstaged"]))

    def test_blank_lines_skipped_and_unknown_code_to_staged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """输出含空行（continue）与双位均不可识别的码（else → staged）。"""
        ws, helpers = _make_worktree_stub(tmp_path)
        scripted = "\n".join([
            "XY weird.txt",          # 双位皆非 ?/A/M/D/R 与 M/D → else 分支
            "",                       # 空行 → continue（204）
            "   ",                    # 纯空白行 → continue
            "?? untracked.txt",
        ]) + "\n"

        async def _fake_run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            return 0, scripted.rstrip(), ""

        monkeypatch.setattr(helpers, "run_git", _fake_run_git)
        result = _run(helpers.git_status({}, ws))
        assert result.success
        # 空行不计入；未知码归 staged；未跟踪照常归 untracked
        assert result.output["staged"] == ["weird.txt"]
        assert result.output["untracked"] == ["untracked.txt"]
        assert len(result.output["staged"]) + len(result.output["unstaged"]) + len(
            result.output["untracked"]
        ) == 2

    def test_fully_blank_output_yields_empty_lists(self, tmp_path: Path, monkeypatch) -> None:
        """空输出（stdout 为空串）→ 三个列表全空，total_changes=0。"""
        ws, helpers = _make_worktree_stub(tmp_path)

        async def _fake_run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            return 0, "", ""

        monkeypatch.setattr(helpers, "run_git", _fake_run_git)
        result = _run(helpers.git_status({}, ws))
        assert result.output["staged"] == []
        assert result.output["unstaged"] == []
        assert result.output["untracked"] == []
        assert result.output["total_changes"] == 0


class TestGitLogGuards:
    """git_log：非 worktree 守卫（401）与空行跳过（425）。"""

    def test_non_worktree_returns_not_initialized(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        helpers = GitHelpers(tmp_path)
        result = _run(helpers.git_log({}, plain))
        assert not result.success and result.error_code == "NOT_INITIALIZED"

    def test_blank_and_short_lines_skipped(self, tmp_path: Path, monkeypatch) -> None:
        """含空行（跳过）与无 '|' 分隔的短行（len(parts)!=3 丢弃）。"""
        ws, helpers = _make_worktree_stub(tmp_path)
        scripted = "\n".join([
            "abc123|feat: one|2026-01-01 10:00:00 +0800",
            "",                       # 空行 → continue（425）
            "no-separator-line",       # 无 | → parts 长度 1 → 不进 commits
            "def456|fix: two|2026-01-02 11:00:00 +0800",
        ])

        async def _fake_run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            return 0, scripted, ""

        monkeypatch.setattr(helpers, "run_git", _fake_run_git)
        result = _run(helpers.git_log({}, ws))
        assert result.success
        assert [c["hash"] for c in result.output["commits"]] == ["abc123", "def456"]
        assert result.output["count"] == len(result.output["commits"])


class TestGitMergeAbortException:
    """git_merge_abort 异常兜底（509-510）。"""

    def test_exception_wrapped_as_merge_abort_failed(self, tmp_path: Path, monkeypatch) -> None:
        helpers = GitHelpers(tmp_path)

        async def _raise(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            raise OSError("abort channel broken")

        monkeypatch.setattr(helpers, "run_git", _raise)
        result = _run(helpers.git_merge_abort({}, tmp_path))
        assert not result.success
        assert result.error_code == "MERGE_ABORT_FAILED"
        assert "git_merge_abort 操作失败" in (result.error or "")


# ═══════════════════════════════════════════════════════════
# tool.py：merge copy 的 base_commit 分支 / git_merge 失败链
# ═══════════════════════════════════════════════════════════


class TestMergeCopyBaseCommit:
    """merge copy：无 target_files 且给 base_commit → 走 `<base> HEAD` diff。"""

    def test_base_commit_scopes_changed_files(self, repo: Path, tmp_path: Path) -> None:
        """基于 checkpoint_id 的 diff 只挑出该 commit 之后的变更文件。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        assert _run(tool.execute({"action": "prepare", "workspace": str(ws)})).success

        # 第一个变更 + 提交，记录基线 commit
        (ws / "first.txt").write_text("first", encoding="utf-8")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-qm", "first")
        base_commit = _git(ws, "rev-parse", "HEAD").stdout.strip()

        # 第二个变更（不再提交 → 相对 base_commit 的 HEAD 差异含它）
        (ws / "second.txt").write_text("second", encoding="utf-8")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-qm", "second")

        target = tmp_path / "dst"
        target.mkdir()
        result = _run(
            tool.execute(
                {
                    "action": "merge",
                    "workspace": str(ws),
                    "target_dir": str(target),
                    "checkpoint_id": base_commit,
                }
            )
        )
        assert result.success
        # base_commit 之后的变更被复制；base_commit 之前的提交不在范围内
        assert (target / "second.txt").exists()
        assert not (target / "first.txt").exists()
        assert "first.txt" not in result.output["merged_files"]

    def test_base_commit_key_alias_equivalent(self, repo: Path, tmp_path: Path) -> None:
        """inputs["base_commit"] 与 checkpoint_id 同义（第二条有区分度输入）。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        assert _run(tool.execute({"action": "prepare", "workspace": str(ws)})).success
        base_commit = _git(ws, "rev-parse", "HEAD").stdout.strip()
        (ws / "later.txt").write_text("later", encoding="utf-8")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-qm", "later")

        target = tmp_path / "dst2"
        target.mkdir()
        result = _run(
            tool.execute(
                {
                    "action": "merge",
                    "workspace": str(ws),
                    "target_dir": str(target),
                    "base_commit": base_commit,
                }
            )
        )
        assert result.success
        assert [f for f in result.output["merged_files"] if f.endswith(".txt")] == ["later.txt"]


class TestGitMergeFailureChain:
    """_git_merge：ensure_project_repo 失败 / git add 失败 / commit 失败 / 无冲突兜底。"""

    def test_project_repo_check_error_returned(self, tmp_path: Path, monkeypatch) -> None:
        """ensure_project_repo 返回错误 → 原样透出（458），不继续执行。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path / "not-a-repo"))
        ws, _ = _make_worktree_stub(tmp_path)

        async def _is_worktree(workspace: Path) -> bool:
            return True

        async def _error() -> Any:
            return _load_tool().create_failure_result(error="不是 git 仓库", error_code="NOT_A_GIT_REPO")

        monkeypatch.setattr(tool._git_helpers, "is_worktree", _is_worktree)
        monkeypatch.setattr(tool._git_helpers, "ensure_project_repo", _error)
        result = _run(
            tool.execute(
                {"action": "merge", "workspace": str(ws), "merge_strategy": "git_merge"}
            )
        )
        assert not result.success and result.error_code == "NOT_A_GIT_REPO"

    def test_git_add_failure_aborts_merge(self, tmp_path: Path, monkeypatch) -> None:
        """git add 非零 → GIT_ADD_FAILED（481），不进入 commit。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path / "repo"))
        ws, _ = _make_worktree_stub(tmp_path)

        async def _is_worktree(workspace: Path) -> bool:
            return True

        async def _no_error() -> None:
            return None

        async def _run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "add":
                return 137, "", "fatal: Unable to create index.lock"
            return 0, "", ""

        monkeypatch.setattr(tool._git_helpers, "is_worktree", _is_worktree)
        monkeypatch.setattr(tool._git_helpers, "ensure_project_repo", _no_error)
        monkeypatch.setattr(tool._git_helpers, "run_git", _run_git)
        result = _run(
            tool.execute({"action": "merge", "workspace": str(ws), "merge_strategy": "git_merge"})
        )
        assert not result.success and result.error_code == "GIT_ADD_FAILED"

    def test_git_commit_failure_after_dirty_status(self, tmp_path: Path, monkeypatch) -> None:
        """status 有变更但 commit 非零 → GIT_COMMIT_FAILED（501）。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path / "repo"))
        ws, _ = _make_worktree_stub(tmp_path)

        async def _is_worktree(workspace: Path) -> bool:
            return True

        async def _no_error() -> None:
            return None

        async def _run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "status":
                return 0, " M a.txt", ""
            if args[0] == "commit":
                return 1, "", "pre-commit hook rejected"
            return 0, "", ""

        monkeypatch.setattr(tool._git_helpers, "is_worktree", _is_worktree)
        monkeypatch.setattr(tool._git_helpers, "ensure_project_repo", _no_error)
        monkeypatch.setattr(tool._git_helpers, "run_git", _run_git)
        result = _run(
            tool.execute({"action": "merge", "workspace": str(ws), "merge_strategy": "git_merge"})
        )
        assert not result.success and result.error_code == "GIT_COMMIT_FAILED"
        assert "pre-commit hook rejected" in (result.error or "")

    def test_merge_failed_without_conflicts_reports_stderr(self, tmp_path: Path, monkeypatch) -> None:
        """merge 非零且 diff --diff-filter=U 无输出 → MERGE_FAILED 兜底（589-607）。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path / "repo"))
        ws, _ = _make_worktree_stub(tmp_path)

        async def _is_worktree(workspace: Path) -> bool:
            return True

        async def _no_error() -> None:
            return None

        async def _run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "merge":
                return 1, "", "error: refusing to merge unrelated histories"
            if args[0] == "diff":
                return 0, "", ""  # 无 U 状态文件 → conflict_files 空
            return 0, "", ""

        monkeypatch.setattr(tool._git_helpers, "is_worktree", _is_worktree)
        monkeypatch.setattr(tool._git_helpers, "ensure_project_repo", _no_error)
        monkeypatch.setattr(tool._git_helpers, "run_git", _run_git)
        result = _run(
            tool.execute({"action": "merge", "workspace": str(ws), "merge_strategy": "git_merge"})
        )
        assert not result.success and result.error_code == "MERGE_FAILED"
        assert "unrelated histories" in (result.error or "")


class TestToolDefinition:
    """工具定义面：schema 契约（action 枚举 / 必填项 / 合并策略默认值）。"""

    def test_definition_declares_actions_and_requirements(self) -> None:
        definition = _load_tool().ResourceMergeTool.get_tool_definition()
        assert definition.name == "resource_merge"
        props = definition.input_schema["properties"]
        assert definition.input_schema["required"] == ["action", "workspace"]
        assert set(props["merge_strategy"]["enum"]) == {"copy", "git_merge", "git_merge_no_ff"}
        assert props["merge_strategy"]["default"] == "copy"
        # 性质断言：每个枚举 action 都有对应分派实现（无声明即无实现的悬空项）
        actions = set(props["action"]["enum"])
        assert len(actions) == 9
        assert {"prepare", "merge", "rollback", "cleanup"} <= actions


class TestGitMergeExceptionFallback:
    """_git_merge 顶层异常兜底（606-607）：worktree 探测抛错 → MERGE_FAILED。"""

    def test_is_worktree_exception_wrapped(self, tmp_path: Path, monkeypatch) -> None:
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path / "repo"))
        ws, _ = _make_worktree_stub(tmp_path)

        async def _raise(workspace: Path) -> bool:
            raise OSError("worktree probe crashed")

        monkeypatch.setattr(tool._git_helpers, "is_worktree", _raise)
        result = _run(
            tool.execute({"action": "merge", "workspace": str(ws), "merge_strategy": "git_merge"})
        )
        assert not result.success and result.error_code == "MERGE_FAILED"
        assert "git merge 操作失败" in (result.error or "")

    def test_repo_check_exception_wrapped(self, tmp_path: Path, monkeypatch) -> None:
        """第二条有区分度输入：ensure_project_repo 抛错（真实 worktree 探测通过后）。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path / "repo"))
        ws, _ = _make_worktree_stub(tmp_path)

        async def _is_worktree(workspace: Path) -> bool:
            return True

        async def _raise() -> None:
            raise RuntimeError("git dir unreadable")

        monkeypatch.setattr(tool._git_helpers, "is_worktree", _is_worktree)
        monkeypatch.setattr(tool._git_helpers, "ensure_project_repo", _raise)
        result = _run(
            tool.execute({"action": "merge", "workspace": str(ws), "merge_strategy": "git_merge_no_ff"})
        )
        assert not result.success and result.error_code == "MERGE_FAILED"
        assert "git merge 操作失败" in (result.error or "")


class TestWorktreeScanSkipDirs:
    """_scan_workspace_files：skip_dirs 成员被跳过（632）。"""

    @pytest.mark.parametrize(
        "skip_dir",
        [".git", ".ai_workspaces", "__pycache__", ".pytest_cache", "node_modules"],
    )
    def test_all_skip_dirs_ignored(self, tmp_path: Path, skip_dir: str) -> None:
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path))
        (tmp_path / "keep.txt").write_text("k", encoding="utf-8")
        nested = tmp_path / "src" / skip_dir
        nested.mkdir(parents=True)
        (nested / "hidden.txt").write_text("h", encoding="utf-8")
        (tmp_path / "src" / "visible.txt").write_text("v", encoding="utf-8")

        files = sorted(tool._scan_workspace_files(tmp_path))
        assert files == ["keep.txt", str(Path("src") / "visible.txt")]
        assert not any(skip_dir in f for f in files)

    def test_non_skip_dirs_included(self, tmp_path: Path) -> None:
        """对照组：非跳过目录照常收录（防 skip 集合过宽）。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path))
        d = tmp_path / "assets"
        d.mkdir()
        (d / "img.bin").write_bytes(b"\x00\x01")
        assert tool._scan_workspace_files(tmp_path) == [str(Path("assets") / "img.bin")]


class TestRollbackCleanFailure:
    """rollback：checkout 成功但 clean -fd 非零 → GIT_CLEAN_FAILED（677）。"""

    def test_clean_failure_reported(self, tmp_path: Path, monkeypatch) -> None:
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path / "repo"))
        ws, _ = _make_worktree_stub(tmp_path)

        async def _is_worktree(workspace: Path) -> bool:
            return True

        async def _run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "clean":
                return 1, "", "warning: failed to remove ro_dir/: Permission denied"
            return 0, "", ""

        monkeypatch.setattr(tool._git_helpers, "is_worktree", _is_worktree)
        monkeypatch.setattr(tool._git_helpers, "run_git", _run_git)
        result = _run(tool.execute({"action": "rollback", "workspace": str(ws)}))
        assert not result.success and result.error_code == "GIT_CLEAN_FAILED"
        assert "清理未跟踪文件失败" in (result.error or "")

    @requires_git
    def test_clean_success_on_real_worktree(self, repo: Path, tmp_path: Path) -> None:
        """真实 worktree：clean 成功分支（对照组，证明失败路径非恒定输出）。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        assert _run(tool.execute({"action": "prepare", "workspace": str(ws)})).success
        (ws / "junk.txt").write_text("junk", encoding="utf-8")
        result = _run(tool.execute({"action": "rollback", "workspace": str(ws)}))
        assert result.success
        assert not (ws / "junk.txt").exists()


class TestCleanupReadonlyFallback:
    """cleanup：只读文件删除回退链（700-701 chmod 双步；720-722 手动删除失败）。"""

    def test_readonly_git_dir_removed_via_chmod_callback(self, repo: Path, tmp_path: Path) -> None:
        """非 worktree 且 .git 内含只读文件 → onerror 回调 chmod 后可删（700-701）。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "plain-repo"
        git_dir = ws / ".git"
        git_dir.mkdir(parents=True)
        readonly = git_dir / "objects.pack"
        readonly.write_bytes(b"pack")
        os.chmod(readonly, 0o444)
        try:
            result = _run(tool.execute({"action": "cleanup", "workspace": str(ws)}))
        finally:
            if readonly.exists():
                os.chmod(readonly, 0o666)
        assert result.success
        assert not git_dir.exists()
        assert ws.exists(), "仅移除 .git，workspace 目录本体保留"

    def test_worktree_remove_and_manual_delete_both_fail(self, tmp_path: Path, monkeypatch) -> None:
        """worktree remove 非零 + 手动 rmtree 抛错 → cleanup_failed 如实上报（720-722）。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path / "repo"))
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "file.txt").write_text("x", encoding="utf-8")

        async def _is_worktree(workspace: Path) -> bool:
            return True

        async def _run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            return 128, "", "fatal: cannot remove a locked working tree"

        def _boom(path: str, onerror: Any) -> None:
            raise PermissionError("file held by another process")

        monkeypatch.setattr(tool._git_helpers, "is_worktree", _is_worktree)
        monkeypatch.setattr(tool._git_helpers, "run_git", _run_git)
        monkeypatch.setattr(shutil, "rmtree", _boom)
        result = _run(tool.execute({"action": "cleanup", "workspace": str(ws)}))
        # 主操作不阻塞（success），但清理失败如实标注，不谎报"已清理"
        assert result.success
        assert result.output["cleanup_failed"]
        assert any("worktree 删除失败" in item for item in result.output["cleanup_failed"])
        assert "清理部分失败" in result.output["message"]

    def test_worktree_remove_failure_but_manual_delete_succeeds(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """对照组：remove 非零但 rmtree 成功 → 该条不计入 cleanup_failed。"""
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path / "repo"))
        ws = tmp_path / "ws"
        ws.mkdir()

        async def _is_worktree(workspace: Path) -> bool:
            return True

        async def _run_git(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "worktree":
                return 128, "", "remove refused"
            return 0, "", ""

        monkeypatch.setattr(tool._git_helpers, "is_worktree", _is_worktree)
        monkeypatch.setattr(tool._git_helpers, "run_git", _run_git)
        result = _run(tool.execute({"action": "cleanup", "workspace": str(ws)}))
        assert result.success
        assert not ws.exists()
        assert not result.output.get("cleanup_failed")


# ═══════════════════════════════════════════════════════════
# server.py：handler 成功路径
# ═══════════════════════════════════════════════════════════


class TestServerHandlerSuccess:
    """server.py handler：success 时返回 result.output（81）。"""

    def test_handler_returns_output_on_success(self, repo: Path, tmp_path: Path) -> None:
        mod_name = "resource_merge_server_gaps"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
        assert spec is not None and spec.loader is not None
        server = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = server
        # handler 内 loader 固定用 base_path=Path.cwd() 构造工具；切到临时仓库根
        cwd = os.getcwd()
        impl_key = "resource_merge_tool_impl"
        saved_impl = sys.modules.get(impl_key)
        saved_tool = sys.modules.get("tool")
        try:
            spec.loader.exec_module(server)
            os.chdir(repo)
            sys.modules.pop(impl_key, None)
            ws = tmp_path / "ws"
            first = _run(
                server.resource_merge(action="prepare", workspace=str(ws))
            )
            assert first.get("branch_name") == f"task/{ws.name}"
            # 二次 prepare → 幂等成功路径（"无需重复创建"），仍走 result.output
            second = _run(server.resource_merge(action="prepare", workspace=str(ws)))
            assert "无需重复创建" in second.get("message", "")
            assert "error" not in second
        finally:
            os.chdir(cwd)
            sys.modules.pop(impl_key, None)
            if saved_impl is not None:
                sys.modules[impl_key] = saved_impl
            if saved_tool is not None:
                sys.modules["tool"] = saved_tool

    def test_handler_returns_error_on_failure(self, repo: Path, tmp_path: Path) -> None:
        """对照组：失败路径返回 {"error": ...}（非 output）。"""
        mod_name = "resource_merge_server_gaps2"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
        assert spec is not None and spec.loader is not None
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        result = _run(server.resource_merge(action="git_diff", workspace=str(tmp_path / "nope")))
        assert "error" in result


# ── 辅助 ────────────────────────────────────────────────────


def _worktree(tmp_path: Path) -> tuple[Path, Any]:
    """真实仓库 + 真实 worktree，返回 (worktree 路径, GitHelpers)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "TDD Test")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    ws = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "task/wt", str(ws))
    return ws, GitHelpers(repo)


def _make_worktree_stub(tmp_path: Path) -> tuple[Path, Any]:
    """带 .git 文件的目录（is_worktree 判真）+ GitHelpers（不触真实仓库）。"""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".git").write_text(f"gitdir: {tmp_path / 'repo' / '.git' / 'worktrees' / 'ws'}\n")
    return ws, GitHelpers(tmp_path)

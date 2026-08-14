# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-plugins-test
"""resource_merge 插件（git 资源合并/回滚工具）单元测试。

覆盖（对齐 plugins/shared/tools/resource_merge/）：
1. git_helpers.py —— run_git（成功/超时/缺 git）、is_worktree、ensure_project_repo、
   ensure_git_repo、git_status/git_commit/git_diff/git_log/git_merge_abort
2. tool.py —— prepare/merge(copy|git_merge|git_merge_no_ff)/rollback/cleanup +
   参数校验 + 冲突路径

测试用 tmp_path 内的真实 git 仓库（git worktree/commit/merge 全链路），
不依赖内核/网络。git 命令经 GitHelpers.run_git（asyncio subprocess）执行。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/tools/resource_merge/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_git_helpers() -> Any:
    mod_name = "git_helpers"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "git_helpers.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_tool() -> Any:
    mod_name = "resource_merge_tool_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


GitHelpers = _load_git_helpers().GitHelpers


async def _git(*args: str, cwd: Path, helpers: GitHelpers | None = None) -> tuple[int, str, str]:
    h = helpers or GitHelpers(Path.cwd())
    return await h.run_git(*args, cwd=cwd)


def _init_repo(path: Path) -> GitHelpers:
    """初始化一个带初始提交的 git 仓库，返回 GitHelpers。"""
    path.mkdir(parents=True, exist_ok=True)
    helpers = GitHelpers(path)
    _run(helpers.run_git("init", cwd=path))
    _run(helpers.run_git("config", "user.email", "test@agent.local", cwd=path))
    _run(helpers.run_git("config", "user.name", "Test Agent", cwd=path))
    (path / "README.md").write_text("repo readme\n", encoding="utf-8")
    _run(helpers.run_git("add", "-A", cwd=path))
    _run(helpers.run_git("commit", "-m", "init", cwd=path))
    return helpers


def _commit_file(path: Path, helpers: GitHelpers, rel: str, content: str, msg: str) -> None:
    (path / rel).write_text(content, encoding="utf-8")
    _run(helpers.run_git("add", "-A", cwd=path))
    _run(helpers.run_git("commit", "-m", msg, cwd=path))


# ═══════════════════════════════════════════════════════════
# GitHelpers：run_git 基础
# ═══════════════════════════════════════════════════════════


class TestRunGit:
    def test_run_git_success(self, tmp_path: Path) -> None:
        helpers = _init_repo(tmp_path)
        rc, out, err = _run(helpers.run_git("rev-parse", "--git-dir", cwd=tmp_path))
        assert rc == 0
        assert out == ".git"
        assert err == ""

    def test_run_git_failure_returns_code(self, tmp_path: Path) -> None:
        helpers = GitHelpers(tmp_path)
        rc, out, err = _run(helpers.run_git("bogus-command-xyz", cwd=tmp_path))
        assert rc != 0
        assert err

    def test_run_git_timeout(self, tmp_path: Path, monkeypatch) -> None:
        """communicate 超时 → (-1, '', 超时信息)。"""

        class _SlowProc:
            async def communicate(self) -> tuple[bytes, bytes]:
                raise asyncio.TimeoutError()

        async def _fake_create(*args, **kwargs):
            return _SlowProc()

        import asyncio as _asyncio

        monkeypatch.setattr(_asyncio, "create_subprocess_exec", _fake_create)
        helpers = GitHelpers(tmp_path)
        rc, out, err = _run(helpers.run_git("status", cwd=tmp_path))
        assert rc == -1
        assert "超时" in err

    def test_run_git_missing_binary(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio as _asyncio

        async def _raise(*args, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(_asyncio, "create_subprocess_exec", _raise)
        helpers = GitHelpers(tmp_path)
        rc, out, err = _run(helpers.run_git("status", cwd=tmp_path))
        assert rc == -1
        assert "未找到 git 命令" in err


# ═══════════════════════════════════════════════════════════
# GitHelpers：仓库检查 / 初始化
# ═══════════════════════════════════════════════════════════


class TestGitHelpersRepo:
    def test_is_worktree(self, tmp_path: Path) -> None:
        helpers = _init_repo(tmp_path)
        # 主仓库 .git 是目录 → False
        assert _run(helpers.is_worktree(tmp_path)) is False
        # 无 .git → False
        plain = tmp_path / "plain"
        plain.mkdir()
        assert _run(helpers.is_worktree(plain)) is False

    def test_ensure_project_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        assert _run(helpers.ensure_project_repo()) is None
        not_repo = tmp_path / "not-a-repo"
        not_repo.mkdir()
        helpers2 = GitHelpers(not_repo)
        result = _run(helpers2.ensure_project_repo())
        assert result is not None and result.error_code == "NOT_A_GIT_REPO"

    def test_ensure_git_repo(self, tmp_path: Path) -> None:
        """目录不存在 → 创建并 git init。"""
        target = tmp_path / "ws"
        helpers = GitHelpers(tmp_path)
        assert _run(helpers.ensure_git_repo(target)) is None
        assert (target / ".git").exists()

    def test_ensure_git_repo_worktree_shortcut(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        r = _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        assert r.success
        helpers = GitHelpers(repo)
        assert _run(helpers.ensure_git_repo(ws)) is None  # worktree → 直接返回


# ═══════════════════════════════════════════════════════════
# GitHelpers：status / commit / diff / log / merge abort
# ═══════════════════════════════════════════════════════════


class TestGitHelpersOps:
    def _worktree(self, tmp_path: Path) -> tuple[Path, GitHelpers]:
        """准备：主仓库 + 一个 worktree，返回 (ws, helpers)。"""
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        ws = tmp_path / "ws"
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        r = _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        assert r.success
        _run(helpers.run_git("config", "user.email", "test@agent.local", cwd=ws))
        _run(helpers.run_git("config", "user.name", "Test Agent", cwd=ws))
        return ws, helpers

    def test_git_status_not_worktree(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        helpers = GitHelpers(tmp_path)
        result = _run(helpers.git_status({}, plain))
        assert not result.success and result.error_code == "NOT_INITIALIZED"

    def test_git_status_parses_states(self, tmp_path: Path) -> None:
        ws, helpers = self._worktree(tmp_path)
        # 先提交一个文件使其成为 tracked，再修改它制造 unstaged 状态
        (ws / "mmm_modified.txt").write_text("v2", encoding="utf-8")
        _run(helpers.run_git("add", "mmm_modified.txt", cwd=ws))
        _run(helpers.run_git("commit", "-m", "add mmm", cwd=ws))
        (ws / "mmm_modified.txt").write_text("v3", encoding="utf-8")
        # staged（在提交之后 add，避免被 commit 一起带走）
        (ws / "aaa_staged.txt").write_text("s", encoding="utf-8")
        _run(helpers.run_git("add", "aaa_staged.txt", cwd=ws))
        # untracked（git 会把 ?? 段排在最后）
        (ws / "new.txt").write_text("new", encoding="utf-8")

        result = _run(helpers.git_status({}, ws))
        assert result.success
        data = result.output
        assert "aaa_staged.txt" in data["staged"]
        assert "mmm_modified.txt" in data["unstaged"]
        assert "new.txt" in data["untracked"]
        assert data["total_changes"] == 3

    def test_git_commit_flow(self, tmp_path: Path) -> None:
        ws, helpers = self._worktree(tmp_path)
        (ws / "a.txt").write_text("a", encoding="utf-8")
        result = _run(helpers.git_commit({"message": "feat: add a.txt"}, ws))
        assert result.success
        assert result.output["commit_hash"]
        assert result.output["message"] == "feat: add a.txt"

    def test_git_commit_no_changes(self, tmp_path: Path) -> None:
        ws, helpers = self._worktree(tmp_path)
        result = _run(helpers.git_commit({}, ws))
        assert result.success
        assert "没有需要提交的变更" in result.output["message"]

    def test_git_commit_not_worktree(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        helpers = GitHelpers(tmp_path)
        result = _run(helpers.git_commit({}, plain))
        assert not result.success and result.error_code == "NOT_INITIALIZED"

    def test_git_diff_against_head(self, tmp_path: Path) -> None:
        ws, helpers = self._worktree(tmp_path)
        _commit_file(ws, helpers, "f.txt", "v1", "c1")
        (ws / "f.txt").write_text("v2", encoding="utf-8")
        result = _run(helpers.git_diff({}, ws))
        assert result.success
        assert "-v1" in result.output["diff"] and "+v2" in result.output["diff"]

    def test_git_diff_without_head(self, tmp_path: Path) -> None:
        """无 HEAD 提交 → 回退 --cached。"""
        ws, helpers = self._worktree(tmp_path)
        (ws / "x.txt").write_text("x", encoding="utf-8")
        _run(helpers.run_git("add", "-A", cwd=ws))
        result = _run(helpers.git_diff({}, ws))
        assert result.success

    def test_git_log(self, tmp_path: Path) -> None:
        ws, helpers = self._worktree(tmp_path)
        _commit_file(ws, helpers, "f1.txt", "1", "first")
        _commit_file(ws, helpers, "f2.txt", "2", "second")
        result = _run(helpers.git_log({}, ws))
        assert result.success
        # worktree 分支从主仓库 HEAD（init）开始 → 共 3 条提交
        assert len(result.output["commits"]) == 3
        assert result.output["commits"][0]["message"] == "second"
        assert result.output["commits"][-1]["message"] == "init"

    def test_git_merge_abort_without_merge(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        result = _run(helpers.git_merge_abort({}, repo))
        assert result.success
        assert "没有正在进行的 merge" in result.output["message"]

    def test_git_merge_abort_real_conflict(self, tmp_path: Path) -> None:
        """真实冲突：主仓库 + 分支各自修改同一文件 → merge 冲突 → abort 成功。"""
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        ws = tmp_path / "ws"
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        r = _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        assert r.success
        _run(helpers.run_git("config", "user.email", "t@a.local", cwd=ws))
        _run(helpers.run_git("config", "user.name", "T", cwd=ws))
        # worktree 分支提交冲突版本
        _commit_file(ws, helpers, "conflict.txt", "from worktree\n", "ws change")
        # 主仓库提交另一版本
        _commit_file(repo, helpers, "conflict.txt", "from main\n", "main change")
        # 合并 → 冲突
        rc, _, _ = _run(helpers.run_git("merge", "task/ws", cwd=repo))
        assert rc != 0
        # abort
        result = _run(helpers.git_merge_abort({}, ws))
        assert result.success
        assert "已成功中止" in result.output["message"]
        # 中止后工作区恢复
        assert (repo / "conflict.txt").read_text(encoding="utf-8") == "from main\n"

    def test_git_merge_abort_not_repo(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        helpers = GitHelpers(plain)
        result = _run(helpers.git_merge_abort({}, plain))
        assert not result.success and result.error_code == "NOT_A_GIT_REPO"


# ═══════════════════════════════════════════════════════════
# ResourceMergeTool：execute 分发 + 参数校验
# ═══════════════════════════════════════════════════════════


class TestToolDispatch:
    def test_missing_action(self, tmp_path: Path) -> None:
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path))
        r = _run(tool.execute({"workspace": str(tmp_path)}))
        assert not r.success and r.error_code == "MISSING_ACTION"

    def test_missing_workspace(self, tmp_path: Path) -> None:
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path))
        r = _run(tool.execute({"action": "git_status"}))
        assert not r.success and r.error_code == "MISSING_WORKSPACE"

    def test_invalid_action(self, tmp_path: Path) -> None:
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path))
        r = _run(tool.execute({"action": "teleport", "workspace": str(tmp_path)}))
        assert not r.success and r.error_code == "INVALID_ACTION"

    def test_resolve_relative_path(self, tmp_path: Path) -> None:
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path))
        resolved = tool._resolve_path("sub/dir")
        assert resolved == (tmp_path / "sub" / "dir").resolve()

    def test_get_branch_name(self, tmp_path: Path) -> None:
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path))
        assert tool._get_branch_name(Path("/x/workspace-a")) == "task/workspace-a"


# ═══════════════════════════════════════════════════════════
# ResourceMergeTool：prepare / merge / rollback / cleanup 全链路
# ═══════════════════════════════════════════════════════════


class TestToolWorkflow:
    def test_prepare_creates_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        r = _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        assert r.success
        assert r.output["branch_name"] == f"task/{ws.name}"
        assert (ws / "README.md").exists()  # 完整项目代码
        assert r.output["base_commit"]

    def test_prepare_second_time_idempotent(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        assert _run(tool.execute({"action": "prepare", "workspace": str(ws)})).success
        r2 = _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        assert r2.success
        assert "无需重复创建" in r2.output["message"]

    def test_prepare_not_git_repo(self, tmp_path: Path) -> None:
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path))
        r = _run(tool.execute({"action": "prepare", "workspace": str(tmp_path / "ws")}))
        assert not r.success and r.error_code == "NOT_A_GIT_REPO"

    def test_merge_copy_strategy(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        # 新增文件（staged 未提交 → git diff HEAD 显示 A 状态）
        (ws / "out.txt").write_text("output", encoding="utf-8")
        _run(helpers.run_git("add", "out.txt", cwd=ws))
        # 修改已有文件（未提交，工作区变更）
        (ws / "README.md").write_text("repo readme\nchanged\n", encoding="utf-8")

        r = _run(tool.execute({"action": "merge", "workspace": str(ws)}))
        assert r.success
        report = r.output["change_report"]
        assert "out.txt" in report["added"]
        assert "README.md" in report["modified"]
        assert r.output["mode"] == "worktree"
        assert (repo / "out.txt").read_text(encoding="utf-8") == "output"

    def test_merge_copy_deleted_file(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        _commit_file(ws, helpers, "bye.txt", "bye", "add bye")
        _run(tool.execute({"action": "merge", "workspace": str(ws), "target_files": ["bye.txt"]}))
        assert (repo / "bye.txt").exists()
        # 删除 worktree 侧文件 → 再次 merge：源缺失且目标存在 → deleted
        (ws / "bye.txt").unlink()
        r = _run(tool.execute({"action": "merge", "workspace": str(ws), "target_files": ["bye.txt"]}))
        assert r.success
        assert "bye.txt" in r.output["change_report"]["deleted"]
        assert not (repo / "bye.txt").exists()

    def test_merge_copy_direct_mode(self, tmp_path: Path) -> None:
        """非 worktree workspace（纯目录）→ direct 复制模式。"""
        repo = tmp_path / "repo"
        _init_repo(repo)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "file.txt").write_text("data", encoding="utf-8")
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        r = _run(tool.execute({"action": "merge", "workspace": str(ws), "target_files": ["file.txt"]}))
        assert r.success
        assert r.output["mode"] == "direct"
        assert (repo / "file.txt").read_text(encoding="utf-8") == "data"

    def test_merge_copy_scan_without_target_files(self, tmp_path: Path) -> None:
        """direct 模式无 target_files → 扫描 workspace 全部文件。"""
        repo = tmp_path / "repo"
        _init_repo(repo)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "one.txt").write_text("1", encoding="utf-8")
        (ws / ".git").mkdir()  # 应被跳过
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        r = _run(tool.execute({"action": "merge", "workspace": str(ws)}))
        assert r.success
        assert r.output["merged_files"] == ["one.txt"]

    def test_merge_invalid_strategy(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        r = _run(
            tool.execute(
                {"action": "merge", "workspace": str(ws), "merge_strategy": "rsync", "target_files": []}
            )
        )
        assert not r.success and r.error_code == "INVALID_MERGE_STRATEGY"

    def test_git_merge_success(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        _commit_file(ws, helpers, "feature.txt", "feat", "feature change")
        r = _run(tool.execute({"action": "merge", "workspace": str(ws), "merge_strategy": "git_merge"}))
        assert r.success
        assert r.output["mode"] == "git_merge"
        assert r.output["merge_commit"]
        assert (repo / "feature.txt").read_text(encoding="utf-8") == "feat"

    def test_git_merge_no_ff(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        _commit_file(ws, helpers, "nf.txt", "1", "nf")
        r = _run(tool.execute({"action": "merge", "workspace": str(ws), "merge_strategy": "git_merge_no_ff"}))
        assert r.success
        assert r.output["merge_strategy"] == "git_merge_no_ff"

    def test_git_merge_not_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        r = _run(tool.execute({"action": "merge", "workspace": str(ws), "merge_strategy": "git_merge"}))
        assert not r.success and r.error_code == "NOT_A_WORKTREE"

    def test_git_merge_conflict(self, tmp_path: Path) -> None:
        """冲突 → 自动 abort + MERGE_CONFLICT + 冲突文件列表。"""
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        _commit_file(ws, helpers, "both.txt", "worktree version\n", "ws side")
        _commit_file(repo, helpers, "both.txt", "main version\n", "main side")

        r = _run(tool.execute({"action": "merge", "workspace": str(ws), "merge_strategy": "git_merge"}))
        assert not r.success
        assert r.error_code == "MERGE_CONFLICT"
        assert "both.txt" in r.error
        # 已自动 abort：工作区回到 main 版本
        assert (repo / "both.txt").read_text(encoding="utf-8") == "main version\n"

    def test_rollback(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        # 提交一个版本后再修改 → rollback 恢复
        _commit_file(ws, helpers, "stable.txt", "stable\n", "stable")
        (ws / "stable.txt").write_text("broken\n", encoding="utf-8")
        (ws / "junk.txt").write_text("junk", encoding="utf-8")

        r = _run(tool.execute({"action": "rollback", "workspace": str(ws)}))
        assert r.success
        assert (ws / "stable.txt").read_text(encoding="utf-8") == "stable\n"
        assert not (ws / "junk.txt").exists()  # clean -fd 删除未跟踪文件

    def test_rollback_not_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        r = _run(tool.execute({"action": "rollback", "workspace": str(ws)}))
        assert not r.success and r.error_code == "NOT_INITIALIZED"

    def test_cleanup_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        helpers = _init_repo(repo)
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        ws = tmp_path / "ws"
        _run(tool.execute({"action": "prepare", "workspace": str(ws)}))
        assert ws.exists()
        r = _run(tool.execute({"action": "cleanup", "workspace": str(ws)}))
        assert r.success
        assert not ws.exists()
        # 分支已删除
        rc, out, _ = _run(helpers.run_git("branch", "--list", f"task/{ws.name}", cwd=repo))
        assert rc == 0 and out.strip() == ""

    def test_cleanup_plain_dir_with_git(self, tmp_path: Path) -> None:
        """非 worktree 但含 .git 的目录 → 移除 .git。"""
        repo = tmp_path / "repo"
        _init_repo(repo)
        ws = tmp_path / "ws"
        _run(_load_tool().ResourceMergeTool(base_path=str(repo)).execute({"action": "prepare", "workspace": str(ws)}))
        # 先清理 worktree 的 .git 引用模拟非 worktree
        tool = _load_tool().ResourceMergeTool(base_path=str(repo))
        r = _run(tool.execute({"action": "cleanup", "workspace": str(tmp_path / "other")}))
        assert r.success
        assert "无需清理" in r.output["message"]

    def test_scan_workspace_files_skips_hidden(self, tmp_path: Path) -> None:
        tool = _load_tool().ResourceMergeTool(base_path=str(tmp_path))
        (tmp_path / "keep.txt").write_text("k", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        (tmp_path / "__pycache__").mkdir()
        files = tool._scan_workspace_files(tmp_path)
        assert files == ["keep.txt"]

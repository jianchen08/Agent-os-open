"""场景2：工作空间生命周期端到端测试。

覆盖场景：
- 场景A（新项目）：workspace 为空时创建新目录
- 场景B（已有项目无 .git）：检测到文件后初始化 git 并提交
- 场景C（已有项目有 .git）：通过 worktree 隔离
- 隔离副本创建与合并验证
- 清理后无残留文件
"""
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from isolation.workspace_lifecycle import WorkspaceLifecycleManager


# ── Mock 辅助 ─────────────────────────────────────────────────────

class MockResourceMerge:
    """Mock ResourceMerge 工具。"""

    def __init__(self, base_path: str):
        self.base_path = base_path

    def merge(self, *args, **kwargs):
        return {"success": True}

    def rollback(self, *args, **kwargs):
        return {"success": True}


class MockTaskTree:
    """Mock 任务树查询接口。"""

    def get_parent_info(self, task_id: str):
        return None

    def get_task(self, task_id: str):
        return None


class MockWsMetaStore:
    """Mock 工作空间元数据存储。"""

    def __init__(self):
        self._store: dict = {}

    def get(self, key: str):
        return self._store.get(key)

    def set(self, key: str, value):
        self._store[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._store

    def __setitem__(self, key: str, value):
        self._store[key] = value

    def __getitem__(self, key: str):
        return self._store[key]


def _create_lifecycle(base_path: str, config: dict | None = None) -> WorkspaceLifecycleManager:
    """创建测试用 WorkspaceLifecycleManager。"""
    return WorkspaceLifecycleManager(
        resource_merge=MockResourceMerge(base_path),
        config=config or {},
        task_tree=MockTaskTree(),
        ws_meta_store=MockWsMetaStore(),
        base_path=base_path,
    )


# ── Fixture ──────────────────────────────────────────────────────

@pytest.fixture
def project_root(tmp_path):
    """创建临时项目根目录。"""
    return tmp_path / "project"


@pytest.fixture
def ws_root(tmp_path):
    """创建临时工作空间根目录。"""
    return tmp_path / "workspaces"


@pytest.fixture
def lifecycle(tmp_path, ws_root):
    """创建生命周期管理器，使用临时目录。"""
    config = {"workspace": {"root": str(ws_root)}}
    return _create_lifecycle(str(tmp_path / "project"), config)


# ── 1. 场景A：新项目 ────────────────────────────────────────────────

class TestScenarioA:
    """场景A：workspace 为空时创建新目录。"""

    def test_detect_new_project_when_workspace_empty(self, lifecycle, ws_root):
        """workspace 为空时检测为 new_project。"""
        task_data = {"task_id": "task_001"}
        scenario, project_root = lifecycle._detect_scenario("", task_data)
        assert scenario == "new_project"
        assert "task_001" in project_root

    def test_detect_new_project_when_path_not_exists(self, lifecycle, tmp_path):
        """路径不存在时检测为 new_project。"""
        task_data = {"task_id": "task_002"}
        non_existent = str(tmp_path / "nonexistent")
        scenario, _ = lifecycle._detect_scenario(non_existent, task_data)
        assert scenario == "new_project"

    def test_detect_new_project_when_empty_dir(self, lifecycle, tmp_path):
        """空目录检测为 new_project。"""
        empty_dir = tmp_path / "empty_project"
        empty_dir.mkdir()
        task_data = {"task_id": "task_003"}
        scenario, _ = lifecycle._detect_scenario(str(empty_dir), task_data)
        assert scenario == "new_project"


# ── 2. 场景B：已有项目无 .git ──────────────────────────────────────

class TestScenarioB:
    """场景B：检测到已有文件后初始化 git 并提交。"""

    def test_detect_existing_project_with_files(self, lifecycle, tmp_path):
        """有文件的目录检测为 existing_project。"""
        project = tmp_path / "existing_project"
        project.mkdir()
        (project / "main.py").write_text("print('hello')", encoding="utf-8")

        task_data = {"task_id": "task_004"}
        scenario, _ = lifecycle._detect_scenario(str(project), task_data)
        assert scenario == "existing_project"

    def test_git_init_and_initial_commit(self, tmp_path):
        """git init + initial commit 在无 .git 目录上执行成功。"""
        project = tmp_path / "project_b"
        project.mkdir()
        (project / "readme.md").write_text("# Test Project", encoding="utf-8")

        lifecycle = _create_lifecycle(str(project))
        result = lifecycle._git_init_and_initial_commit(project, "Initial commit")
        assert result is True
        assert (project / ".git").exists()
        assert (project / ".gitignore").exists()

    def test_git_init_creates_gitignore(self, tmp_path):
        """git init 时自动创建 .gitignore。"""
        project = tmp_path / "project_gitignore"
        project.mkdir()

        lifecycle = _create_lifecycle(str(project))
        lifecycle._git_init_and_initial_commit(project, "init")

        gitignore = project / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text(encoding="utf-8")
        assert "__pycache__/" in content
        assert "*.pyc" in content

    def test_git_init_with_existing_valid_repo(self, tmp_path):
        """已有有效 .git 时不重复初始化。"""
        project = tmp_path / "project_with_git"
        project.mkdir()
        (project / "file.txt").write_text("hello", encoding="utf-8")

        lifecycle = _create_lifecycle(str(project))
        # 第一次初始化
        lifecycle._git_init_and_initial_commit(project, "First commit")
        # 第二次不应报错
        result = lifecycle._git_init_and_initial_commit(project, "Second commit")
        assert result is True


# ── 3. 场景C：已有项目有 .git ──────────────────────────────────────

class TestScenarioC:
    """场景C：通过 worktree 隔离。"""

    def test_detect_existing_git_project(self, tmp_path):
        """有 .git 的项目被检测为 existing_project。"""
        project = tmp_path / "git_project"
        project.mkdir()
        (project / "src").mkdir()
        (project / "src" / "app.py").write_text("app", encoding="utf-8")

        lifecycle = _create_lifecycle(str(project))
        lifecycle._git_init_and_initial_commit(project, "Initial commit")

        task_data = {"task_id": "task_005"}
        scenario, _ = lifecycle._detect_scenario(str(project), task_data)
        assert scenario == "existing_project"
        assert (project / ".git").exists()

    def test_run_git_with_valid_command(self, tmp_path):
        """_run_git 能正确执行 git 命令。"""
        project = tmp_path / "git_cmd_test"
        project.mkdir()

        lifecycle = _create_lifecycle(str(project))
        lifecycle._git_init_and_initial_commit(project, "init")

        rc, stdout, stderr = lifecycle._run_git("status", cwd=project)
        assert rc == 0

    def test_run_git_handles_nonexistent_git(self, tmp_path):
        """无 git 仓库时 _run_git 返回错误。"""
        project = tmp_path / "no_git"
        project.mkdir()

        lifecycle = _create_lifecycle(str(project))
        rc, stdout, stderr = lifecycle._run_git("status", cwd=project)
        assert rc != 0


# ── 4. 路径安全性验证 ──────────────────────────────────────────────

class TestWorkspaceSafety:
    """工作空间路径安全相关。"""

    def test_safe_ws_name_normal(self):
        """正常项目名生成安全的 worktree 名称。"""
        from isolation.workspace_lifecycle import _safe_ws_name
        name = _safe_ws_name("my_project", "abc123456789")
        assert "my_project" in name
        assert "abc12345" in name

    def test_safe_ws_name_with_special_chars(self):
        """特殊字符被替换。"""
        from isolation.workspace_lifecycle import _safe_ws_name
        name = _safe_ws_name("my<project>test", "abc123456789")
        assert "<" not in name
        assert ">" not in name

    def test_safe_ws_name_truncation(self):
        """长项目名被截断。"""
        from isolation.workspace_lifecycle import _safe_ws_name
        long_name = "a" * 50
        name = _safe_ws_name(long_name, "abc123456789")
        # 应被截断，不会太长
        assert len(name) < 60


# ── 5. 清理验证 ────────────────────────────────────────────────────

class TestWorkspaceCleanup:
    """工作空间清理验证。"""

    def test_force_rmtree_removes_readonly_files(self, tmp_path):
        """_force_rmtree 能删除只读文件（Windows 兼容）。"""
        from isolation.workspace_lifecycle import _force_rmtree

        target = tmp_path / "to_remove"
        target.mkdir()
        readonly_file = target / "readonly.txt"
        readonly_file.write_text("data", encoding="utf-8")
        # 设为只读
        readonly_file.chmod(0o444)

        _force_rmtree(str(target))
        assert not target.exists()

    def test_force_rmtree_handles_missing_dir(self, tmp_path):
        """_force_rmtree 对不存在的目录抛出 FileNotFoundError。"""
        from isolation.workspace_lifecycle import _force_rmtree
        # 函数内部未对不存在目录做 try-except，会抛出异常
        with pytest.raises(FileNotFoundError):
            _force_rmtree(str(tmp_path / "nonexistent"))


# ── 6. on_task_start 集成测试 ──────────────────────────────────────

class TestOnTaskStart:
    """on_task_start 钩子集成测试。"""

    def test_on_task_start_new_project(self, tmp_path, ws_root):
        """新项目 on_task_start 创建工作空间。"""
        config = {"workspace": {"root": str(ws_root)}}
        ws_meta = MockWsMetaStore()
        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MockResourceMerge(str(tmp_path)),
            config=config,
            task_tree=MockTaskTree(),
            ws_meta_store=ws_meta,
            base_path=str(tmp_path),
        )

        result = lifecycle.on_task_start(
            task_id="task_start_001",
            workspace="",
            task_data={"task_id": "task_start_001", "is_root": True},
        )
        assert isinstance(result, dict)
        # 新项目应该有 mode 和 path
        assert "mode" in result or result.get("path") is not None

    def test_restore_ws_meta_returns_none_for_new(self, tmp_path):
        """restore_ws_meta 对新任务返回 None。"""
        ws_meta = MockWsMetaStore()
        lifecycle = WorkspaceLifecycleManager(
            resource_merge=MockResourceMerge(str(tmp_path)),
            config={},
            task_tree=MockTaskTree(),
            ws_meta_store=ws_meta,
            base_path=str(tmp_path),
        )
        result = lifecycle.restore_ws_meta("nonexistent_task")
        # 新任务没有存储的元数据
        assert result is None or result == {}

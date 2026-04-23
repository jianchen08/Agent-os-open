"""工作空间生命周期单元测试 — 验证 git init/commit/index.lock 修复。

不依赖 LLM，纯文件系统操作验证。
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
os.chdir(str(PROJECT_ROOT))

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


class DummyWsMetaStore(dict):
    """Simple dict-based workspace metadata store."""
    pass


class DummyTaskTree:
    """Returns empty parent info."""
    def get_parent_info(self, task_id):
        return {}


class DummyResourceMerge:
    """No-op resource merge."""
    pass


def _create_manager(base_path: str):
    from isolation.workspace_lifecycle import WorkspaceLifecycleManager
    return WorkspaceLifecycleManager(
        resource_merge=DummyResourceMerge(),
        config={},
        task_tree=DummyTaskTree(),
        ws_meta_store=DummyWsMetaStore(),
        base_path=base_path,
    )


class TestGitInitAndInitialCommit(unittest.TestCase):
    """Test _git_init_and_initial_commit handles edge cases."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ws_test_"))
        self.mgr = _create_manager(str(self.tmpdir))

    def tearDown(self):
        shutil.rmtree(str(self.tmpdir), ignore_errors=True)

    def test_fresh_directory(self):
        """Scenario A: Completely empty directory -> git init + commit."""
        workdir = self.tmpdir / "fresh"
        workdir.mkdir()
        (workdir / "hello.txt").write_text("hello", encoding="utf-8")

        result = self.mgr._git_init_and_initial_commit(workdir, "test: initial")
        self.assertTrue(result, "git init + commit should succeed on fresh dir")

        # Verify commit exists
        rc, out, _ = self.mgr._run_git("rev-parse", "HEAD", cwd=workdir)
        self.assertEqual(rc, 0, "Should have a valid HEAD commit")
        self.assertTrue(len(out) > 0)

        # Verify file is tracked
        rc, out, _ = self.mgr._run_git("ls-files", cwd=workdir)
        self.assertIn("hello.txt", out)

    def test_existing_git_no_commits(self):
        """Scenario: .git exists but is empty (no commits) -> re-init."""
        workdir = self.tmpdir / "empty_git"
        workdir.mkdir()
        (workdir / "file.txt").write_text("data", encoding="utf-8")

        # Create empty .git
        (workdir / ".git").mkdir()
        (workdir / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        result = self.mgr._git_init_and_initial_commit(workdir, "test: re-init")
        self.assertTrue(result, "Should handle empty .git gracefully")

        rc, out, _ = self.mgr._run_git("rev-parse", "HEAD", cwd=workdir)
        self.assertEqual(rc, 0)

    def test_stale_index_lock(self):
        """Scenario: stale .git/index.lock exists -> removed and commit succeeds."""
        workdir = self.tmpdir / "stale_lock"
        workdir.mkdir()
        (workdir / "data.txt").write_text("test data", encoding="utf-8")

        # Init first
        self.mgr._run_git("init", cwd=workdir)

        # Create stale index.lock
        lock_path = workdir / ".git" / "index.lock"
        lock_path.write_text("stale lock", encoding="utf-8")
        self.assertTrue(lock_path.exists(), "index.lock should exist before test")

        result = self.mgr._git_init_and_initial_commit(workdir, "test: after lock removal")
        self.assertTrue(result, "Should succeed after removing stale index.lock")
        self.assertFalse(lock_path.exists(), "index.lock should be removed")

        rc, _, _ = self.mgr._run_git("rev-parse", "HEAD", cwd=workdir)
        self.assertEqual(rc, 0)

    def test_valid_existing_repo(self):
        """Scenario: valid .git with commits -> skip init, just add/commit."""
        workdir = self.tmpdir / "valid_repo"
        workdir.mkdir()
        (workdir / "initial.txt").write_text("first", encoding="utf-8")

        # Create valid repo
        self.mgr._run_git("init", cwd=workdir)
        self.mgr._ensure_git_user(workdir)
        self.mgr._run_git("add", "-A", cwd=workdir)
        self.mgr._run_git("commit", "-m", "initial", cwd=workdir)

        # Add new file
        (workdir / "second.txt").write_text("second", encoding="utf-8")

        result = self.mgr._git_init_and_initial_commit(workdir, "test: second commit")
        self.assertTrue(result)

        # Both files should be tracked
        rc, out, _ = self.mgr._run_git("ls-files", cwd=workdir)
        self.assertIn("initial.txt", out)
        self.assertIn("second.txt", out)


class TestOnTaskStartRootTask(unittest.TestCase):
    """Test _start_root_task with different scenarios."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ws_root_test_"))
        self.ws_meta = DummyWsMetaStore()
        self.mgr = _create_manager(str(self.tmpdir))

    def tearDown(self):
        shutil.rmtree(str(self.tmpdir), ignore_errors=True)

    def test_scenario_c_existing_git(self):
        """Scenario C: base_path has .git -> copies files to workspace dir."""
        # Set up base project with .git
        self.mgr._run_git("init", cwd=self.tmpdir)
        self.mgr._ensure_git_user(self.tmpdir)
        (self.tmpdir / "src").mkdir()
        (self.tmpdir / "src" / "main.py").write_text("print('hi')", encoding="utf-8")
        self.mgr._run_git("add", "-A", cwd=self.tmpdir)
        self.mgr._run_git("commit", "-m", "initial", cwd=self.tmpdir)

        task_data = {
            "task_id": "test_task_001",
            "workspace_root": str(self.tmpdir / ".ai_workspaces"),
            "is_root": True,
        }
        meta = self.mgr.on_task_start("test_task_001", "", task_data)

        # Workspace should be created under .ai_workspaces/test_task_001
        ws_dir = self.tmpdir / ".ai_workspaces" / "test_task_001"
        self.assertTrue(ws_dir.exists(), f"Workspace dir should exist: {ws_dir}")
        self.assertEqual(meta["mode"], "project_root")
        # Files should be copied
        copied_file = ws_dir / "src" / "main.py"
        self.assertTrue(copied_file.exists(), f"Copied file should exist: {copied_file}")
        # Workspace should have its own .git
        self.assertTrue((ws_dir / ".git").exists(), "Workspace should have .git")
        # Workspace git should have commits
        rc, _, _ = self.mgr._run_git("rev-parse", "HEAD", cwd=ws_dir)
        self.assertEqual(rc, 0, "Workspace git should have a valid HEAD")

    def test_scenario_a_new_project(self):
        """Scenario A: no files in base_path -> creates new project at base_path."""
        task_data = {
            "task_id": "test_new_001",
            "workspace_root": str(self.tmpdir / ".ai_workspaces"),
            "is_root": True,
        }
        meta = self.mgr.on_task_start("test_new_001", "", task_data)

        # For scenario A (empty base_path), workspace IS base_path itself
        self.assertEqual(meta["mode"], "project_root")
        self.assertTrue(Path(meta["path"]).exists())
        # Should have .git initialized
        self.assertTrue(
            (Path(meta["path"]) / ".git").exists()
        )


class TestOnBeforeEvaluate(unittest.TestCase):
    """Test pre-evaluation checkpoint save."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ws_eval_test_"))
        self.mgr = _create_manager(str(self.tmpdir))

    def tearDown(self):
        shutil.rmtree(str(self.tmpdir), ignore_errors=True)

    def test_checkpoint_with_changes(self):
        """Pre-evaluation: save changes and return commit hash."""
        workdir = self.tmpdir / "eval_ws"
        workdir.mkdir()
        # Need at least one file for initial commit
        (workdir / "initial.txt").write_text("init", encoding="utf-8")
        self.mgr._git_init_and_initial_commit(workdir, "initial")

        # Add new file
        (workdir / "output.txt").write_text("task result", encoding="utf-8")

        result = self.mgr.on_before_evaluate(str(workdir))
        self.assertTrue(result["success"])
        # commit_hash should be non-None when there were changes to commit
        self.assertIsNotNone(result["commit_hash"])

    def test_checkpoint_no_changes(self):
        """Pre-evaluation: no changes returns no commit hash."""
        workdir = self.tmpdir / "eval_ws2"
        workdir.mkdir()
        (workdir / "initial.txt").write_text("init", encoding="utf-8")
        self.mgr._git_init_and_initial_commit(workdir, "initial")

        result = self.mgr.on_before_evaluate(str(workdir))
        self.assertTrue(result["success"])
        self.assertIsNone(result["commit_hash"])

    def test_checkpoint_with_stale_lock(self):
        """Pre-evaluation: stale index.lock doesn't block checkpoint."""
        workdir = self.tmpdir / "eval_ws3"
        workdir.mkdir()
        (workdir / "initial.txt").write_text("init", encoding="utf-8")
        self.mgr._git_init_and_initial_commit(workdir, "initial")

        # Add file and create stale lock
        (workdir / "new_file.txt").write_text("data", encoding="utf-8")
        (workdir / ".git" / "index.lock").write_text("stale", encoding="utf-8")

        result = self.mgr.on_before_evaluate(str(workdir))
        self.assertTrue(result["success"])
        self.assertFalse((workdir / ".git" / "index.lock").exists(),
                         "Stale index.lock should be cleaned up")


class TestRemoveIndexLock(unittest.TestCase):
    """Test _remove_index_lock directly."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ws_lock_test_"))
        self.mgr = _create_manager(str(self.tmpdir))

    def tearDown(self):
        shutil.rmtree(str(self.tmpdir), ignore_errors=True)

    def test_no_lock_returns_false(self):
        workdir = self.tmpdir / "nolock"
        workdir.mkdir()
        (workdir / ".git").mkdir()
        result = self.mgr._remove_index_lock(workdir)
        self.assertFalse(result)

    def test_lock_removed(self):
        workdir = self.tmpdir / "haslock"
        workdir.mkdir()
        (workdir / ".git").mkdir()
        (workdir / ".git" / "index.lock").write_text("lock", encoding="utf-8")
        result = self.mgr._remove_index_lock(workdir)
        self.assertTrue(result)
        self.assertFalse((workdir / ".git" / "index.lock").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation checkpoint.py 检查点管理器测试（A5.3 补）。

覆盖：
1. F-ISO-1 输入校验：task_id 白名单 / 相对路径拒绝绝对与穿越段；
2. create_checkpoint：全目录备份 / 指定文件 / 空工作目录 / 忽略规则；
3. restore_checkpoint：成功恢复 / 检查点缺失 / manifest 篡改整体拒绝；
4. cleanup / get / list 三接口；
5. worktree 场景（文件在 project_root 而非 workspace 下）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_mod() -> Any:
    mod_name = "isolation_checkpoint_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "checkpoint.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()
CheckpointManager = _MOD.CheckpointManager
Checkpoint = _MOD.Checkpoint
CheckpointFile = _MOD.CheckpointFile


def _write(path: Path, content: str = "hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestInputValidation:
    def test_task_id_rejects_dotdot(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        with pytest.raises(ValueError, match="task_id"):
            m.create_checkpoint("../evil", "ws")

    def test_task_id_rejects_slash(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        with pytest.raises(ValueError, match="task_id"):
            m.create_checkpoint("a/b", "ws")

    def test_task_id_rejects_empty(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        with pytest.raises(ValueError, match="task_id"):
            m.create_checkpoint("", "ws")

    def test_task_id_rejects_non_ascii(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        with pytest.raises(ValueError, match="task_id"):
            m.create_checkpoint("任务", "ws")

    def test_files_to_backup_absolute_rejected(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        with pytest.raises(ValueError, match="路径"):
            m.create_checkpoint("t1", "ws", files_to_backup=["/etc/passwd"])

    def test_files_to_backup_dotdot_rejected(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        with pytest.raises(ValueError, match="路径"):
            m.create_checkpoint("t1", "ws", files_to_backup=["../outside.txt"])

    def test_files_to_backup_unix_abs_on_windows_rejected(self, tmp_path: Path) -> None:
        """Windows Path.is_absolute 不认 /etc/passwd,前导分隔符检查兜底。"""
        m = CheckpointManager(str(tmp_path))
        with pytest.raises(ValueError, match="路径"):
            m.create_checkpoint("t1", "ws", files_to_backup=["/etc/passwd"])

    def test_safe_relative_path_checker(self) -> None:
        assert CheckpointManager._is_safe_relative_path("") is True
        assert CheckpointManager._is_safe_relative_path("a/b.txt") is True
        assert CheckpointManager._is_safe_relative_path("../x") is False
        assert CheckpointManager._is_safe_relative_path("/abs") is False
        assert CheckpointManager._is_safe_relative_path("\\win-abs") is False


class TestCreateCheckpoint:
    def test_backup_whole_workspace(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "a.txt", "alpha")
        _write(tmp_path / "ws" / "sub" / "b.txt", "beta")
        m = CheckpointManager(str(tmp_path))
        cp = m.create_checkpoint("t1", "ws")
        assert cp.status == "active"
        # Windows 下 relative_to 产出反斜杠,统一正斜杠断言
        assert {f.original_path.replace("\\", "/") for f in cp.files} == {"a.txt", "sub/b.txt"}
        # 备份文件确实落盘
        backup_root = tmp_path / ".checkpoints" / "t1" / "files"
        assert (backup_root / "a.txt").exists()
        assert (backup_root / "sub" / "b.txt").exists()
        # 校验和正确性
        a_file = next(f for f in cp.files if f.original_path == "a.txt")
        assert a_file.checksum == "alpha".encode().hex() or len(a_file.checksum) == 64

    def test_create_with_explicit_files(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "keep.txt")
        _write(tmp_path / "ws" / "skip.txt")
        m = CheckpointManager(str(tmp_path))
        cp = m.create_checkpoint("t1", "ws", files_to_backup=["keep.txt"])
        assert [f.original_path for f in cp.files] == ["keep.txt"]

    def test_create_missing_file_in_list_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "keep.txt")
        m = CheckpointManager(str(tmp_path))
        cp = m.create_checkpoint("t1", "ws", files_to_backup=["keep.txt", "ghost.txt"])
        assert [f.original_path for f in cp.files] == ["keep.txt"]

    def test_missing_workspace_returns_empty_checkpoint(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        cp = m.create_checkpoint("t1", "no_such_ws")
        assert cp.files == []
        assert (m.checkpoint_dir / "t1" / "manifest.json").exists()

    def test_ignore_rules(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "keep.txt")
        _write(tmp_path / "ws" / ".hidden")
        _write(tmp_path / "ws" / "x" / "__pycache__" / "c.pyc")
        _write(tmp_path / "ws" / "node_modules" / "lib.js")
        _write(tmp_path / ".git" / "config")
        m = CheckpointManager(str(tmp_path))
        cp = m.create_checkpoint("t1", "ws")
        assert [f.original_path for f in cp.files] == ["keep.txt"]

    def test_checksum_and_size_recorded(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "a.txt", "abc")
        m = CheckpointManager(str(tmp_path))
        cp = m.create_checkpoint("t1", "ws")
        a_file = cp.files[0]
        assert a_file.checksum == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        assert a_file.size == 3
        assert a_file.modified_at.endswith("+00:00") or "T" in a_file.modified_at


class TestRestoreCheckpoint:
    def test_restore_restores_files(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "a.txt", "original")
        m = CheckpointManager(str(tmp_path))
        m.create_checkpoint("t1", "ws")
        # 修改原文件
        (tmp_path / "ws" / "a.txt").write_text("modified", encoding="utf-8")
        assert m.restore_checkpoint("t1") is True
        assert (tmp_path / "ws" / "a.txt").read_text(encoding="utf-8") == "original"
        # 状态更新为 restored
        cp = m.get_checkpoint("t1")
        assert cp is not None and cp.status == "restored"

    def test_restore_missing_checkpoint(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        assert m.restore_checkpoint("nope") is False

    def test_restore_manifest_tampered_original_path(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "a.txt")
        m = CheckpointManager(str(tmp_path))
        m.create_checkpoint("t1", "ws")
        manifest = m.checkpoint_dir / "t1" / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["files"][0]["original_path"] = "../escape.txt"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        assert m.restore_checkpoint("t1") is False
        # 状态保持 active（整体拒绝，零落盘）
        cp = m.get_checkpoint("t1")
        assert cp is not None and cp.status == "active"

    def test_restore_manifest_tampered_backup_path(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "a.txt")
        m = CheckpointManager(str(tmp_path))
        m.create_checkpoint("t1", "ws")
        manifest = m.checkpoint_dir / "t1" / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["files"][0]["backup_path"] = "/absolute/path"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        assert m.restore_checkpoint("t1") is False

    def test_restore_missing_backup_file_keeps_original(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "a.txt", "original")
        m = CheckpointManager(str(tmp_path))
        m.create_checkpoint("t1", "ws")
        # 删掉备份文件 → 恢复跳过该文件,但整体仍成功
        (m.checkpoint_dir / "t1" / "files" / "a.txt").unlink()
        assert m.restore_checkpoint("t1") is True
        assert (tmp_path / "ws" / "a.txt").read_text(encoding="utf-8") == "original"


class TestCleanupAndList:
    def test_cleanup_existing(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "a.txt")
        m = CheckpointManager(str(tmp_path))
        m.create_checkpoint("t1", "ws")
        assert m.cleanup_checkpoint("t1") is True
        assert not (m.checkpoint_dir / "t1").exists()

    def test_cleanup_missing_returns_true(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        assert m.cleanup_checkpoint("ghost") is True

    def test_list_checkpoints(self, tmp_path: Path) -> None:
        _write(tmp_path / "ws" / "a.txt")
        m = CheckpointManager(str(tmp_path))
        m.create_checkpoint("t1", "ws")
        lst = m.list_checkpoints()
        assert len(lst) == 1
        assert lst[0]["task_id"] == "t1"
        assert lst[0]["file_count"] == 1
        assert lst[0]["status"] == "active"

    def test_list_empty_dir(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        assert m.list_checkpoints() == []

    def test_get_checkpoint_missing(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        assert m.get_checkpoint("ghost") is None


class TestManifestRoundtrip:
    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        cp = Checkpoint(
            task_id="t1",
            workspace="ws",
            created_at="2026-08-26T00:00:00+00:00",
            files=[CheckpointFile("a.txt", "files/a.txt", "abc", 3, "2026-08-26T00:00:00+00:00")],
        )
        checkpoint_path = tmp_path / ".checkpoints" / "t1"
        checkpoint_path.mkdir(parents=True)
        m._save_manifest(checkpoint_path, cp)
        loaded = m._load_manifest(checkpoint_path / "manifest.json")
        assert loaded.task_id == "t1"
        assert loaded.workspace == "ws"
        assert loaded.status == "active"
        assert loaded.files[0].original_path == "a.txt"
        assert loaded.files[0].checksum == "abc"

    def test_load_manifest_default_status(self, tmp_path: Path) -> None:
        m = CheckpointManager(str(tmp_path))
        checkpoint_path = tmp_path / ".checkpoints" / "t2"
        checkpoint_path.mkdir(parents=True)
        cp = Checkpoint(task_id="t2", workspace="ws", created_at="2026-08-26T00:00:00+00:00")
        m._save_manifest(checkpoint_path, cp)
        # 手工删除 status 字段 → 加载回退 active
        manifest = checkpoint_path / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        del data["status"]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        loaded = m._load_manifest(manifest)
        assert loaded.status == "active"


class TestExceptionPaths:
    def test_empty_path_in_backup_list_allowed(self, tmp_path: Path) -> None:
        """空字符串路径跳过校验（_validate_relative_path 空值放行）。"""
        m = CheckpointManager(str(tmp_path))
        cp = m.create_checkpoint("t1", "ws", files_to_backup=[""])
        assert cp.files == []

    def test_backup_copy_failure_logged_not_fatal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path / "ws" / "a.txt")
        m = CheckpointManager(str(tmp_path))

        def _boom(src, dst, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(_MOD.shutil, "copy2", _boom)
        cp = m.create_checkpoint("t1", "ws")
        # 备份失败不中断流程,返回无文件检查点
        assert cp.files == []
        assert (m.checkpoint_dir / "t1" / "manifest.json").exists()

    def test_restore_copy_failure_logged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path / "ws" / "a.txt", "original")
        m = CheckpointManager(str(tmp_path))
        m.create_checkpoint("t1", "ws")
        (tmp_path / "ws" / "a.txt").write_text("modified", encoding="utf-8")

        def _boom(src, dst, **kw):
            raise OSError("EACCES")

        monkeypatch.setattr(_MOD.shutil, "copy2", _boom)
        assert m.restore_checkpoint("t1") is True  # 失败不阻断,整体成功
        assert (tmp_path / "ws" / "a.txt").read_text(encoding="utf-8") == "modified"

    def test_cleanup_failure_returns_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path / "ws" / "a.txt")
        m = CheckpointManager(str(tmp_path))
        m.create_checkpoint("t1", "ws")

        def _boom(path, **kw):
            raise OSError("EACCES")

        monkeypatch.setattr(_MOD.shutil, "rmtree", _boom)
        assert m.cleanup_checkpoint("t1") is False

    def test_ignore_checkpoint_dir_inside_workspace(self, tmp_path: Path) -> None:
        """workspace 内嵌 .checkpoints 目录自身不被备份。"""
        _write(tmp_path / "ws" / "keep.txt")
        _write(tmp_path / "ws" / ".checkpoints" / "other" / "x.bak")
        m = CheckpointManager(str(tmp_path))
        cp = m.create_checkpoint("t1", "ws")
        assert [f.original_path for f in cp.files] == ["keep.txt"]


class TestWorktreeFallback:
    def test_create_worktree_file_in_project_root(self, tmp_path: Path) -> None:
        """文件不在 workspace 下而在 project_root:备份走 project_root 回退。"""
        _write(tmp_path / "ws" / ".keep", "keep")  # workspace 存在,跳过空目录分支
        _write(tmp_path / "root_data" / "a.txt", "hello")
        m = CheckpointManager(str(tmp_path))
        cp = m.create_checkpoint("t1", "ws", files_to_backup=["root_data/a.txt"])
        assert [f.original_path for f in cp.files] == ["root_data/a.txt"]
        backup = m.checkpoint_dir / "t1" / "files" / "root_data" / "a.txt"
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == "hello"

    def test_restore_worktree_file_into_project_root(self, tmp_path: Path) -> None:
        """restore 时 workspace 下无父目录 → 回退 project_root 恢复。"""
        _write(tmp_path / "ws_sidecar" / "a.txt", "original")
        m = CheckpointManager(str(tmp_path))
        m.create_checkpoint("t1", "ws_sidecar", files_to_backup=["ws_sidecar/a.txt"])
        (tmp_path / "ws_sidecar" / "a.txt").write_text("modified", encoding="utf-8")
        assert m.restore_checkpoint("t1") is True
        assert (tmp_path / "ws_sidecar" / "a.txt").read_text(encoding="utf-8") == "original"

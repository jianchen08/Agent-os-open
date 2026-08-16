# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""Checkpoint 路径穿越安全测试（F-ISO-1）。

为什么重要（意图）：
- checkpoint 的 task_id / files_to_backup 经 MCP 工具来自 LLM 输出，属不可信输入。
  task_id 若含 ../ 或绝对路径，`.checkpoints/<task_id>` 会逃逸出项目根，cleanup 时
  shutil.rmtree 甚至能删除任意目录；files_to_backup 含 ../ 时 create 阶段会把任意
  宿主文件拷进检查点（数据泄露），restore 阶段又会把备份写到 project_root 之外
  （任意文件写）。本测试锁定「不可信路径输入必须被拒绝、且不落盘」这一安全契约。

覆盖：
- task_id 含 .. / / / \\ / 绝对路径 → create 阶段即抛 ValueError，且不产生目录；
- files_to_backup 含 ../ 或绝对路径 → 拒绝（ValueError）；
- manifest 被篡改（original_path / backup_path 越出 project_root）→ restore 拒绝且不落盘；
- 正常相对路径创建/恢复/清理全流程不受影响（回归护栏）。
"""

from __future__ import annotations

import tests._isolation_path  # noqa: F401  # isort: skip —— 须在 checkpoint import 前注入 sys.path

import json
from pathlib import Path

import pytest
from checkpoint import CheckpointManager


def _make_manager(project_root: Path) -> CheckpointManager:
    """构造指向临时项目根的检查点管理器。"""
    return CheckpointManager(str(project_root))


class TestTaskIdValidation:
    """task_id 白名单：非法字符（路径分隔符/穿越/绝对路径）在 create 阶段即拒绝。"""

    @pytest.mark.parametrize(
        "task_id",
        [
            "../evil",
            "a/../../evil",
            "a\\..\\evil",
            "a/b",
            "a\\b",
            "/etc/passwd",
            "C:/windows/evil",
            "C:\\windows\\evil",
            "..",
            ".",
            "",
        ],
        ids=[
            "dotdot",
            "nested_dotdot",
            "backslash_dotdot",
            "forward_slash",
            "backslash",
            "absolute_unix",
            "absolute_win_fwd",
            "absolute_win_back",
            "bare_dotdot",
            "bare_dot",
            "empty",
        ],
    )
    def test_malicious_task_id_rejected_at_create(self, tmp_path: Path, task_id: str) -> None:
        """恶意 task_id（穿越/分隔符/绝对路径/空）→ create_checkpoint 抛 ValueError。

        意图：task_id 直接拼接为 `.checkpoints/<task_id>` 目录名，任何路径语义字符
        都意味着检查点目录可能逃逸 project_root（cleanup 的 rmtree 即任意目录删除）。
        """
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="非法"):
            mgr.create_checkpoint(task_id=task_id, workspace=".", files_to_backup=[])

        # 不产生任何检查点目录（含逃逸路径）
        cp_dir = tmp_path / ".checkpoints"
        assert not cp_dir.exists() or not any(cp_dir.iterdir())

    def test_valid_task_id_accepted(self, tmp_path: Path) -> None:
        """合法 task_id（字母/数字/-/_）正常工作——白名单不能误伤正常契约。"""
        mgr = _make_manager(tmp_path)
        cp = mgr.create_checkpoint(task_id="task-42_abc", workspace=".", files_to_backup=[])
        assert cp.task_id == "task-42_abc"
        assert (tmp_path / ".checkpoints" / "task-42_abc" / "manifest.json").exists()

    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("restore_checkpoint", ("../evil",)),
            ("cleanup_checkpoint", ("../evil",)),
            ("get_checkpoint", ("../evil",)),
        ],
    )
    def test_malicious_task_id_rejected_in_all_entry_points(
        self, tmp_path: Path, method: str, args: tuple[str, ...]
    ) -> None:
        """restore/cleanup/get 同样拒绝非法 task_id——防绕过 create 直接调用。"""
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="非法"):
            getattr(mgr, method)(*args)


class TestFilesToBackupValidation:
    """files_to_backup 条目：含 ../ 或绝对路径 → 拒绝。"""

    @pytest.mark.parametrize(
        "files_to_backup",
        [
            ["ok.txt", "../outside.txt"],
            ["ok.txt", "sub/../../outside.txt"],
            ["/etc/passwd"],
            ["C:\\windows\\evil.txt"],
            [".."],
        ],
        ids=["traversal", "nested_traversal", "absolute_unix", "absolute_win", "bare_dotdot"],
    )
    def test_traversal_file_entry_rejected(self, tmp_path: Path, files_to_backup: list[str]) -> None:
        """files_to_backup 含 ../ 或绝对路径 → ValueError。

        意图：这些条目会被 `backup_path / file_rel_path` 与
        `project_root / file_rel_path` 直接拼接——../ 会让备份写出检查点目录
        （任意写）或把 project_root 之外的文件拷进检查点（泄露）。
        """
        (tmp_path / "ok.txt").write_text("ok", encoding="utf-8")
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="非法"):
            mgr.create_checkpoint(task_id="t1", workspace=".", files_to_backup=files_to_backup)

    def test_relative_entries_still_accepted(self, tmp_path: Path) -> None:
        """正常相对路径条目不受影响（含子目录）。"""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.txt").write_text("a", encoding="utf-8")
        mgr = _make_manager(tmp_path)
        cp = mgr.create_checkpoint(
            task_id="t1", workspace=".", files_to_backup=["docs/a.txt"]
        )
        assert len(cp.files) == 1
        assert cp.files[0].original_path == "docs/a.txt"


class TestRestoreTamperedManifest:
    """restore 阶段：manifest 中 original_path/backup_path 越出 project_root → 拒绝且不落盘。"""

    def _setup_checkpoint_with_file(self, tmp_path: Path) -> tuple[CheckpointManager, Path]:
        """创建含一个文件的正常检查点，返回 (manager, manifest_path)。"""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.txt").write_text("content-a", encoding="utf-8")
        mgr = _make_manager(tmp_path)
        mgr.create_checkpoint(task_id="t1", workspace=".", files_to_backup=["docs/a.txt"])
        return mgr, tmp_path / ".checkpoints" / "t1" / "manifest.json"

    def _tamper_manifest(self, manifest_path: Path, **fields: str) -> None:
        """覆写 manifest 中第一条文件记录的字段。"""
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["files"][0].update(fields)
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

    def test_original_path_escaping_project_root_rejected_no_write(self, tmp_path: Path) -> None:
        """original_path='../escaped.txt' → restore 返回 False 且不在 project_root 外落盘。

        意图：restore 用 `project_root / original_path` + shutil.copy2 直接写文件，
        manifest 可被篡改（或由旧版本/恶意 create 生成）——../ 即宿主任意文件写。
        必须整体拒绝且一个字节都不写。
        """
        mgr, manifest_path = self._setup_checkpoint_with_file(tmp_path)
        self._tamper_manifest(manifest_path, original_path="../escaped.txt")

        escaped = tmp_path.parent / "escaped.txt"
        escaped.unlink(missing_ok=True)  # 清理红阶段可能遗留的文件

        ok = mgr.restore_checkpoint("t1")

        assert ok is False
        assert not escaped.exists()
        # 原文件未被破坏（restore 未执行任何写）
        assert (tmp_path / "docs" / "a.txt").read_text(encoding="utf-8") == "content-a"

    def test_backup_path_escaping_checkpoint_dir_rejected_no_write(self, tmp_path: Path) -> None:
        """backup_path='../evil.bak' → restore 拒绝（备份文件同样不能越出检查点目录）。"""
        mgr, manifest_path = self._setup_checkpoint_with_file(tmp_path)
        self._tamper_manifest(manifest_path, backup_path="../evil.bak")

        escaped = tmp_path / "evil.bak"
        escaped.unlink(missing_ok=True)

        ok = mgr.restore_checkpoint("t1")

        assert ok is False
        assert not escaped.exists()

    def test_absolute_original_path_rejected_no_write(self, tmp_path: Path) -> None:
        """original_path 为绝对路径 → restore 拒绝且不落盘。"""
        mgr, manifest_path = self._setup_checkpoint_with_file(tmp_path)
        self._tamper_manifest(manifest_path, original_path=str(tmp_path / "evil.txt"))

        ok = mgr.restore_checkpoint("t1")

        assert ok is False
        assert not (tmp_path / "evil.txt").exists()


class TestNormalFlowRegression:
    """回归护栏：正常创建 → 恢复 → 清理全流程不受安全校验影响。"""

    def test_create_restore_cleanup_roundtrip(self, tmp_path: Path) -> None:
        """相对路径文件完整走一遍 create/restore/cleanup。"""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "docs" / "a.txt").write_text("hello-v2", encoding="utf-8")  # 改后内容
        mgr = _make_manager(tmp_path)

        cp = mgr.create_checkpoint(task_id="t1", workspace=".", files_to_backup=["docs/a.txt"])
        assert len(cp.files) == 1

        # 修改原文件后再恢复
        (tmp_path / "docs" / "a.txt").write_text("mutated", encoding="utf-8")
        assert mgr.restore_checkpoint("t1") is True
        assert (tmp_path / "docs" / "a.txt").read_text(encoding="utf-8") == "hello-v2"

        assert mgr.cleanup_checkpoint("t1") is True
        assert not (tmp_path / ".checkpoints" / "t1").exists()

    def test_create_without_files_list_still_works(self, tmp_path: Path) -> None:
        """files_to_backup=None（整目录备份）仍工作——默认扫描路径本身必须被允许。"""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.txt").write_text("a", encoding="utf-8")
        mgr = _make_manager(tmp_path)

        cp = mgr.create_checkpoint(task_id="t1", workspace="docs", files_to_backup=None)

        assert len(cp.files) == 1
        assert mgr.restore_checkpoint("t1") is True

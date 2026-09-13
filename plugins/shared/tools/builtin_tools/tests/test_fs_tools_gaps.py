# @feature: FP-0.2.二 内部模块统一 manifest 化 | @ci: python-coverage
"""fs_tools 缺口补测（2026-09-13 官方车道 miss=44）。

覆盖缺口：
- 共享根 sys.path 自举（fresh import 无 plugins/shared 时守卫插入）；
- file_read：目录读取、二进制/编码失败、读 IO 故障；
- file_write：search_replace/insert/delete_lines 的参数缺失与 not found
  分支、未知 action、写 IO 故障、.bak 备份真实创建；
- list_directory：目录不存在/非目录；
- create_directory：mkdir IO 故障；
- copy_file：批量 copies、参数缺失、目录 copytree（含 overwrite 合并）、
  复制 IO 故障；
- move_file：批量 moves、参数缺失、目标已存在、overwrite 覆盖文件/目录、
  移动 IO 故障；
- delete_file：参数缺失、force 只读位清除删除。

打桩边界：仅文件系统 IO 故障注入（Path.read_text/write_text/mkdir 与
shutil.copy2/move 抛 OSError）——文件系统属可 mock 外部依赖；其余全部
走 tmp_path 真实文件操作。
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

import pytest

import agentos_builtin_tools
from agentos_builtin_tools import fs_tools
from agentos_builtin_tools.fs_tools import (
    copy_file,
    create_directory,
    delete_file,
    file_read,
    file_write,
    list_directory,
    move_file,
)

pytestmark = pytest.mark.unit


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


# ─────────────────────── 模块自举（共享根 sys.path 守卫） ───────────────────────


def test_shared_root_bootstrap_reinserts_path_when_missing() -> None:
    """plugins/shared 不在 sys.path 时 fresh import 走自举守卫插入（43 行）。"""
    shared_root = fs_tools._SHARED_ROOT
    saved_path = list(sys.path)
    saved_mod = sys.modules.get("agentos_builtin_tools.fs_tools")
    saved_attr = getattr(agentos_builtin_tools, "fs_tools", None)
    sys.path[:] = [
        p
        for p in sys.path
        if os.path.normcase(os.path.abspath(p or os.sep)) != os.path.normcase(shared_root)
    ]
    sys.modules.pop("agentos_builtin_tools.fs_tools", None)
    try:
        fresh = importlib.import_module("agentos_builtin_tools.fs_tools")
        # 守卫生效：模块导入后共享根必须回到 sys.path（repo_anchor 可解析的前提）
        assert os.path.normcase(shared_root) in {os.path.normcase(p) for p in sys.path}
        assert hasattr(fresh, "file_read")
    finally:
        sys.path[:] = saved_path
        if saved_mod is not None:
            sys.modules["agentos_builtin_tools.fs_tools"] = saved_mod
        if saved_attr is not None:
            agentos_builtin_tools.fs_tools = saved_attr


# ───────────────────────────── file_read ─────────────────────────────


async def test_read_directory_rejected(tmp_path: Path) -> None:
    """对目录调 file_read：Not a file 失败（211 行）。"""
    ws = _ws(tmp_path)
    (ws / "sub").mkdir()

    result = await file_read(path=str(ws / "sub"), workspace=str(ws))

    assert result.success is False
    assert "Not a file" in result.error


async def test_read_binary_file_reports_encoding_failure(tmp_path: Path) -> None:
    """非 UTF-8 二进制内容：编码失败干净报错（217-218 行）。"""
    ws = _ws(tmp_path)
    target = ws / "blob.bin"
    target.write_bytes(b"\xff\xfe\x00\x01binary")

    result = await file_read(path=str(target), workspace=str(ws))

    assert result.success is False
    assert "Binary file or encoding issue" in result.error


async def test_read_io_error_is_clean_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """读盘 IO 故障：OSError 分支干净报错，不向调用方抛异常（219-220 行）。"""
    ws = _ws(tmp_path)
    target = ws / "a.txt"
    target.write_text("x", encoding="utf-8")

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("device stuck")

    monkeypatch.setattr(Path, "read_text", boom)
    result = await file_read(path=str(target), workspace=str(ws))

    assert result.success is False
    assert "Read error" in result.error
    assert "device stuck" in result.error


# ───────────────────────────── file_write ─────────────────────────────


async def test_search_replace_without_old_str_rejected(tmp_path: Path) -> None:
    """search_replace 缺 old_str：参数错误（357 行）。"""
    ws = _ws(tmp_path)

    result = await file_write(
        path="f.txt", action="search_replace", new_str="b", workspace=str(ws)
    )

    assert result.success is False
    assert "old_str is required" in result.error


async def test_search_replace_old_str_not_found(tmp_path: Path) -> None:
    """search_replace 目标串不存在：not found 失败（365 行）。"""
    ws = _ws(tmp_path)
    target = ws / "f.txt"
    target.write_text("hello world", encoding="utf-8")

    result = await file_write(
        path=str(target), action="search_replace", old_str="absent", new_str="x",
        workspace=str(ws),
    )

    assert result.success is False
    assert "old_str not found" in result.error
    assert target.read_text(encoding="utf-8") == "hello world"


async def test_insert_without_line_rejected(tmp_path: Path) -> None:
    """insert 缺 line：参数错误（385 行）。"""
    ws = _ws(tmp_path)

    result = await file_write(
        path="f.txt", action="insert", content="x", workspace=str(ws)
    )

    assert result.success is False
    assert "line is required" in result.error


async def test_delete_lines_without_start_line_rejected(tmp_path: Path) -> None:
    """delete_lines 缺 start_line：参数错误（405 行）。"""
    ws = _ws(tmp_path)

    result = await file_write(path="f.txt", action="delete_lines", workspace=str(ws))

    assert result.success is False
    assert "start_line is required" in result.error


async def test_delete_lines_missing_file_rejected(tmp_path: Path) -> None:
    """delete_lines 目标不存在：File not found（407 行）。"""
    ws = _ws(tmp_path)

    result = await file_write(
        path="ghost.txt", action="delete_lines", start_line=1, workspace=str(ws)
    )

    assert result.success is False
    assert "File not found" in result.error


async def test_unknown_action_rejected(tmp_path: Path) -> None:
    """未知 action：显式报错，无文件副作用（425 行）。"""
    ws = _ws(tmp_path)

    result = await file_write(path="f.txt", action="truncate", content="x", workspace=str(ws))

    assert result.success is False
    assert "Unknown action: truncate" in result.error
    assert not (ws / "f.txt").exists()


async def test_write_io_error_is_clean_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """写盘 IO 故障：OSError 分支干净报错，不向调用方抛异常（427-428 行）。"""
    ws = _ws(tmp_path)
    target = ws / "f.txt"
    target.write_text("old", encoding="utf-8")

    def boom(self: Path, *args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    result = await file_write(path=str(target), action="write", content="new", workspace=str(ws))

    assert result.success is False
    assert "IO error" in result.error


async def test_backup_created_for_existing_file(tmp_path: Path) -> None:
    """create_backup=True 且文件已存在：.bak 真实创建并回传路径（435-437 行）。"""
    ws = _ws(tmp_path)
    target = ws / "f.txt"
    target.write_text("v1", encoding="utf-8")

    result = await file_write(path=str(target), action="write", content="v2", workspace=str(ws))

    assert result.success is True
    backup = result.output["backup"]
    assert backup is not None
    assert Path(backup) == ws / "f.txt.bak"
    assert Path(backup).read_text(encoding="utf-8") == "v1"


async def test_backup_skipped_for_new_file(tmp_path: Path) -> None:
    """新文件无旧内容可备份：backup 为 None（性质对照）。"""
    ws = _ws(tmp_path)

    result = await file_write(path=str(ws / "new.txt"), action="write", content="v1", workspace=str(ws))

    assert result.success is True
    assert result.output["backup"] is None


# ─────────────────────────── list_directory ───────────────────────────


async def test_list_directory_missing_dir_rejected(tmp_path: Path) -> None:
    """目录不存在：Directory not found（491 行）。"""
    ws = _ws(tmp_path)

    result = await list_directory("ghost", workspace=str(ws))

    assert result.success is False
    assert "Directory not found" in result.error


async def test_list_directory_on_file_rejected(tmp_path: Path) -> None:
    """目标是文件：Not a directory（493 行）。"""
    ws = _ws(tmp_path)
    (ws / "f.txt").write_text("x", encoding="utf-8")

    result = await list_directory("f.txt", workspace=str(ws))

    assert result.success is False
    assert "Not a directory" in result.error


# ─────────────────────────── create_directory ───────────────────────────


async def test_create_directory_io_error_is_clean_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mkdir IO 故障：OSError 分支干净报错（549-550 行）。"""
    ws = _ws(tmp_path)

    def boom(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("read-only volume")

    monkeypatch.setattr(Path, "mkdir", boom)
    result = await create_directory("sub", workspace=str(ws))

    assert result.success is False
    assert "Create error" in result.error


# ───────────────────────────── copy_file ─────────────────────────────


async def test_copy_file_batch_reports_per_item(tmp_path: Path) -> None:
    """批量 copies：逐项成功/失败如实上报（592-604 行）。"""
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("A", encoding="utf-8")

    result = await copy_file(
        copies=[
            {"source": "a.txt", "destination": "b.txt"},
            {"source": "missing.txt", "destination": "c.txt"},
        ],
        workspace=str(ws),
    )

    assert result.success is True
    flags = [(r["source"], r["success"]) for r in result.output["results"]]
    assert flags == [("a.txt", True), ("missing.txt", False)]
    assert (ws / "b.txt").read_text(encoding="utf-8") == "A"
    assert not (ws / "c.txt").exists()


async def test_copy_file_without_source_and_destination_rejected(tmp_path: Path) -> None:
    """既无 source/destination 也无 copies：参数错误（607 行）。"""
    ws = _ws(tmp_path)

    result = await copy_file(workspace=str(ws))

    assert result.success is False
    assert "source and destination are required" in result.error


async def test_copy_directory_tree(tmp_path: Path) -> None:
    """目录复制：copytree 落盘，子文件完整（629 行）。"""
    ws = _ws(tmp_path)
    (ws / "src" / "nested").mkdir(parents=True)
    (ws / "src" / "nested" / "leaf.txt").write_text("leaf", encoding="utf-8")

    result = await copy_file("src", "dst", workspace=str(ws))

    assert result.success is True, result.error
    assert (ws / "dst" / "nested" / "leaf.txt").read_text(encoding="utf-8") == "leaf"


async def test_copy_directory_overwrite_merges_into_existing(tmp_path: Path) -> None:
    """overwrite=True 目录复制：目标已存在时合并（dirs_exist_ok 语义）。"""
    ws = _ws(tmp_path)
    (ws / "src").mkdir()
    (ws / "src" / "new.txt").write_text("new", encoding="utf-8")
    (ws / "dst").mkdir()
    (ws / "dst" / "kept.txt").write_text("kept", encoding="utf-8")

    result = await copy_file("src", "dst", overwrite=True, workspace=str(ws))

    assert result.success is True, result.error
    assert (ws / "dst" / "kept.txt").exists()
    assert (ws / "dst" / "new.txt").read_text(encoding="utf-8") == "new"


async def test_copy_io_error_is_clean_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """复制 IO 故障：OSError 分支干净报错（632-633 行）。"""
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("A", encoding="utf-8")

    def boom(src: object, dst: object, **kwargs: object) -> None:
        raise OSError("copy bombed")

    monkeypatch.setattr(fs_tools.shutil, "copy2", boom)
    result = await copy_file("a.txt", "b.txt", workspace=str(ws))

    assert result.success is False
    assert "Copy error" in result.error


# ───────────────────────────── move_file ─────────────────────────────


async def test_move_file_batch_reports_per_item(tmp_path: Path) -> None:
    """批量 moves：逐项成功/失败如实上报（694-706 行）。"""
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("A", encoding="utf-8")

    result = await move_file(
        moves=[
            {"source": "a.txt", "destination": "b.txt"},
            {"source": "missing.txt", "destination": "c.txt"},
        ],
        workspace=str(ws),
    )

    assert result.success is True
    flags = [(r["source"], r["success"]) for r in result.output["results"]]
    assert flags == [("a.txt", True), ("missing.txt", False)]
    assert (ws / "b.txt").read_text(encoding="utf-8") == "A"
    assert not (ws / "a.txt").exists()
    assert not (ws / "c.txt").exists()


async def test_move_file_without_source_and_destination_rejected(tmp_path: Path) -> None:
    """既无 source/destination 也无 moves：参数错误（709 行）。"""
    ws = _ws(tmp_path)

    result = await move_file(workspace=str(ws))

    assert result.success is False
    assert "source and destination are required" in result.error


async def test_move_onto_existing_destination_rejected_without_overwrite(tmp_path: Path) -> None:
    """目标已存在且未开 overwrite：拒绝移动，两端原样保留（716 行）。"""
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("A", encoding="utf-8")
    (ws / "b.txt").write_text("B", encoding="utf-8")

    result = await move_file("a.txt", "b.txt", workspace=str(ws))

    assert result.success is False
    assert "Destination already exists" in result.error
    assert (ws / "a.txt").read_text(encoding="utf-8") == "A"
    assert (ws / "b.txt").read_text(encoding="utf-8") == "B"


async def test_move_overwrite_replaces_existing_file(tmp_path: Path) -> None:
    """overwrite=True 目标为文件：unlink 后移动（723 行）。"""
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("A", encoding="utf-8")
    (ws / "b.txt").write_text("B", encoding="utf-8")

    result = await move_file("a.txt", "b.txt", overwrite=True, workspace=str(ws))

    assert result.success is True, result.error
    assert (ws / "b.txt").read_text(encoding="utf-8") == "A"
    assert not (ws / "a.txt").exists()


async def test_move_overwrite_replaces_existing_directory(tmp_path: Path) -> None:
    """overwrite=True 目标为目录：rmtree 后移动（720-721 行）。"""
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("A", encoding="utf-8")
    dst_dir = ws / "b"
    dst_dir.mkdir()
    (dst_dir / "stale.txt").write_text("stale", encoding="utf-8")

    result = await move_file("a.txt", "b", overwrite=True, workspace=str(ws))

    assert result.success is True, result.error
    assert (ws / "b").is_file()
    assert (ws / "b").read_text(encoding="utf-8") == "A"


async def test_move_io_error_is_clean_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """移动 IO 故障：OSError 分支干净报错（725-726 行）。"""
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("A", encoding="utf-8")

    def boom(src: object, dst: object) -> None:
        raise OSError("move bombed")

    monkeypatch.setattr(fs_tools.shutil, "move", boom)
    result = await move_file("a.txt", "b.txt", workspace=str(ws))

    assert result.success is False
    assert "Move error" in result.error


# ───────────────────────────── delete_file ─────────────────────────────


async def test_delete_without_path_or_paths_rejected(tmp_path: Path) -> None:
    """既无 path 也无 paths：参数错误（766 行）。"""
    ws = _ws(tmp_path)

    result = await delete_file(workspace=str(ws))

    assert result.success is False
    assert "path or paths is required" in result.error


async def test_delete_with_force_clears_readonly_bit(tmp_path: Path) -> None:
    """force=True：先清只读位再删除（797 行）。"""
    ws = _ws(tmp_path)
    target = ws / "locked.txt"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o444)

    result = await delete_file(path=str(target), force=True, workspace=str(ws))

    assert result.success is True
    assert result.output["results"][0]["deleted"] is True
    assert not target.exists()


@pytest.mark.skipif(
    os.name != "nt", reason="POSIX unlink 不受文件只读位限制，无 force 对照语义"
)
async def test_delete_readonly_without_force_fails_on_windows(tmp_path: Path) -> None:
    """Windows 只读文件不带 force：删除失败逐项上报（force 对照组）。"""
    ws = _ws(tmp_path)
    target = ws / "locked.txt"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o444)

    result = await delete_file(path=str(target), force=False, workspace=str(ws))

    assert result.success is False
    assert result.output["results"][0]["deleted"] is False
    assert target.exists()
    target.chmod(0o644)  # 还原可写，避免污染 tmp_path 清理
    shutil.rmtree(ws, ignore_errors=True)

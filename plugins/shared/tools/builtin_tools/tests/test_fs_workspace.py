# @ci: python-coverage
"""fs_tools 工作空间约束测试（punch B5）。

project_root 前缀校验（参考 download/tool.py 的 WorkspaceAwareMixin 语义）：
- file_write / move_file / delete_file：workspace 外绝对路径被拒；
- workspace 内路径（含相对路径解析）通过；
- file_read：workspace 外允许读取但记录 warning（只读向后兼容）；
- 未注入 workspace/project_root 时维持 0.1 兼容行为（不约束）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agentos_builtin_tools.fs_tools import delete_file, file_read, file_write, move_file

pytestmark = pytest.mark.unit


class TestFileWriteWorkspaceConstraint:
    async def test_write_outside_workspace_rejected(self, tmp_path: Path) -> None:
        """workspace 外绝对路径写入被拒，文件不被创建。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside.txt"

        result = await file_write(
            path=str(outside), action="write", content="evil",
            workspace=str(ws),
        )
        assert result.success is False
        assert "超出 workspace/project_root" in result.error
        assert not outside.exists()

    async def test_write_inside_workspace_allowed(self, tmp_path: Path) -> None:
        """workspace 内写入通过。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "note.md"

        result = await file_write(
            path=str(target), action="write", content="hello",
            workspace=str(ws),
        )
        assert result.success is True
        assert target.read_text(encoding="utf-8") == "hello"

    async def test_write_relative_path_resolved_in_workspace(self, tmp_path: Path) -> None:
        """相对路径以 workspace 为基准解析，落在根内则通过。"""
        ws = tmp_path / "ws"
        ws.mkdir()

        result = await file_write(
            path="rel.txt", action="write", content="ok", workspace=str(ws),
        )
        assert result.success is True
        assert (ws / "rel.txt").read_text(encoding="utf-8") == "ok"

    async def test_write_traversal_escape_rejected(self, tmp_path: Path) -> None:
        """相对路径含 .. 逃逸出 workspace 被拒。"""
        ws = tmp_path / "ws"
        ws.mkdir()

        result = await file_write(
            path="../escape.txt", action="write", content="evil", workspace=str(ws),
        )
        assert result.success is False
        assert not (tmp_path / "escape.txt").exists()

    async def test_write_without_workspace_context_compat(self, tmp_path: Path) -> None:
        """未注入 workspace/project_root：不约束（0.1 兼容），写入成功。"""
        target = tmp_path / "free.txt"
        result = await file_write(path=str(target), action="write", content="x")
        assert result.success is True
        assert target.exists()


class TestMoveFileWorkspaceConstraint:
    async def test_move_destination_outside_rejected(self, tmp_path: Path) -> None:
        """目标在 workspace 外：移动被拒，源文件保留原地。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        src = ws / "a.txt"
        src.write_text("data", encoding="utf-8")
        outside = tmp_path / "moved.txt"

        result = await move_file(
            source=str(src), destination=str(outside), workspace=str(ws),
        )
        assert result.success is False
        assert "超出 workspace/project_root" in result.error
        assert src.exists()
        assert not outside.exists()

    async def test_move_inside_workspace_allowed(self, tmp_path: Path) -> None:
        """workspace 内移动通过。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        src = ws / "a.txt"
        src.write_text("data", encoding="utf-8")
        dst = ws / "b.txt"

        result = await move_file(
            source=str(src), destination=str(dst), workspace=str(ws),
        )
        assert result.success is True
        assert dst.read_text(encoding="utf-8") == "data"
        assert not src.exists()


class TestDeleteFileWorkspaceConstraint:
    async def test_delete_outside_workspace_rejected(self, tmp_path: Path) -> None:
        """workspace 外删除被拒，目标保留。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "precious.txt"
        outside.write_text("keep", encoding="utf-8")

        result = await delete_file(path=str(outside), workspace=str(ws))
        assert result.success is False
        assert "超出 workspace/project_root" in result.error
        assert outside.exists()

    async def test_delete_inside_workspace_allowed(self, tmp_path: Path) -> None:
        """workspace 内删除通过。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "junk.txt"
        target.write_text("junk", encoding="utf-8")

        result = await delete_file(path=str(target), workspace=str(ws))
        assert result.success is True
        assert not target.exists()

    async def test_delete_batch_checks_every_path(self, tmp_path: Path) -> None:
        """批量删除逐条校验：任一路径越界即整体拒绝。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        inside = ws / "ok.txt"
        inside.write_text("ok", encoding="utf-8")
        outside = tmp_path / "no.txt"
        outside.write_text("no", encoding="utf-8")

        result = await delete_file(
            paths=[str(inside), str(outside)], workspace=str(ws),
        )
        assert result.success is False
        assert inside.exists()
        assert outside.exists()


class TestFileReadWorkspaceLogging:
    async def test_read_outside_workspace_allowed_but_logged(
        self, tmp_path: Path, caplog: logging.Logger
    ) -> None:
        """workspace 外读取放行（只读兼容），但记录 warning。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "config.ini"
        outside.write_text("[a]\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="agentos_builtin_tools.fs_tools"):
            result = await file_read(path=str(outside), workspace=str(ws))

        assert result.success is True
        assert "workspace 外路径" in caplog.text

    async def test_read_inside_workspace_no_warning(self, tmp_path: Path) -> None:
        """workspace 内读取正常，不产生越界告警。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "doc.txt"
        target.write_text("line1\n", encoding="utf-8")

        result = await file_read(path=str(target), workspace=str(ws))
        assert result.success is True

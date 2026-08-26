# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
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


class TestFileReadReturnsResolvedPath:
    """file_read 的 file 字段返回宿主侧绝对路径（工具卡片打开文件的坐标契约）。

    隔离任务下 agent 以容器挂载点 /workspace 为 cwd（isolation_guard 固定挂载），
    传相对路径或 /workspace/* 容器路径；前端工具卡片按 file 字段到宿主文件系统
    读取（get_file_content 绝对路径直读），原样回传将打不开。
    _check_workspace_path 已完成容器路径重映射 + 根锚定（file_write 同款消费其
    返回值），file_read 的读取与回传都必须使用该结果。
    """

    async def test_container_mount_path_reads_and_returns_host_path(self, tmp_path: Path) -> None:
        """容器挂载路径 /workspace/* 重映射到宿主工作区：读取成功且 file 为宿主路径。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "report.md").write_text("hello\n", encoding="utf-8")

        result = await file_read(path="/workspace/report.md", workspace=str(ws))

        assert result.success is True
        assert Path(result.output["file"]) == ws / "report.md"
        assert result.output["content"] == "hello\n"

    async def test_relative_path_resolved_to_workspace_root(self, tmp_path: Path) -> None:
        """相对路径锚定工作区根：file 返回宿主绝对路径（性质：绝对且落在根内）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "doc.txt").write_text("line\n", encoding="utf-8")

        result = await file_read(path="doc.txt", workspace=str(ws))

        assert result.success is True
        resolved = Path(result.output["file"])
        assert resolved.is_absolute()
        assert resolved == ws / "doc.txt"

    async def test_without_injection_path_passthrough(self, tmp_path: Path) -> None:
        """未注入 workspace/project_root（L1 主会话）：路径原样回传（前端按项目根解析）。"""
        target = tmp_path / "free.txt"
        target.write_text("x\n", encoding="utf-8")

        result = await file_read(path=str(target))

        assert result.success is True
        assert result.output["file"] == str(target)

    async def test_absolute_outside_path_readable_and_normalized(self, tmp_path: Path) -> None:
        """根外绝对路径读取放行（只读兼容），file 回传 resolve 归一后的宿主路径。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "cfg.ini"
        outside.write_text("[a]\n", encoding="utf-8")

        result = await file_read(path=str(outside), workspace=str(ws))

        assert result.success is True
        assert Path(result.output["file"]) == outside.resolve()


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

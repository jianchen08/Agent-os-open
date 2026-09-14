# @feature: FP-0.2.二 内部模块统一 manifest 化 | @ci: python-coverage
"""enhanced_search 工作空间边界 + 凭据黑名单双闸测试（A1）。

- 路径闸（fs_tools 同源 fail-closed）：根外绝对路径拒绝、``..`` 逃逸拒绝、
  /workspace 挂载点重映射仍工作；
- 凭据闸：根内 .env 等凭据文件内容/文件名均不入结果（遍历场景跳过），
  目标路径自身命中黑名单直接拒绝。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos_builtin_tools.search_tool import enhanced_search

pytestmark = pytest.mark.unit


async def _search(**kwargs: object) -> object:
    return await enhanced_search(**kwargs)  # type: ignore[arg-type]


class TestSearchPathBoundary:
    async def test_absolute_path_outside_root_rejected(self, tmp_path: Path) -> None:
        """根外绝对路径拒绝（两组有区分度输入：同级目录 / 更深层目录）。"""
        ws = tmp_path / "ws"
        (ws / "sub").mkdir(parents=True)
        (ws / "sub" / "note.txt").write_text("needle here", encoding="utf-8")

        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("needle here", encoding="utf-8")

        for outside in (outside_dir, tmp_path / "outside" / "deeper"):
            result = await enhanced_search(
                query="needle", path=str(outside), workspace=str(ws)
            )
            assert result.success is False
            assert "超出 workspace/project_root" in (result.error or "")

    async def test_relative_traversal_escape_rejected(self, tmp_path: Path) -> None:
        """``../`` 相对逃逸出根被拒绝（一级与多级逃逸）。"""
        ws = tmp_path / "ws"
        (ws / "sub").mkdir(parents=True)
        (tmp_path / "leaked.txt").write_text("needle", encoding="utf-8")

        for path in ("../leaked.txt", "../../leaked.txt"):
            result = await enhanced_search(
                query="needle", path=path, workspace=str(ws)
            )
            assert result.success is False
            assert "超出 workspace/project_root" in (result.error or "")

    async def test_workspace_prefix_remap_still_works(self, tmp_path: Path) -> None:
        """/workspace/ 前缀重映射到注入工作空间后正常搜索（容器挂载约定）。"""
        ws = tmp_path / "ws"
        (ws / "sub").mkdir(parents=True)
        (ws / "sub" / "app.py").write_text("NEEDLE = 1", encoding="utf-8")

        result = await enhanced_search(
            query="NEEDLE", path="/workspace/sub", workspace=str(ws)
        )
        assert result.success is True
        files = [r["file_path"] for r in result.output["results"]]
        assert files == [str(ws / "sub" / "app.py")]

    async def test_no_root_injected_fails_closed(self, tmp_path: Path) -> None:
        """workspace/project_root 均未注入时 fail-closed（不做 cwd 兜底）。"""
        result = await enhanced_search(query="needle", path=".")
        assert result.success is False
        assert "未注入" in (result.error or "")


class TestSearchSensitiveFileGate:
    async def test_env_content_never_in_text_results(self, tmp_path: Path) -> None:
        """根内 .env 含秘密时 text 搜索零命中（正常文件不受影响）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".env").write_text("API_KEY=super-secret-value\n", encoding="utf-8")
        (ws / "readme.md").write_text("nothing to see", encoding="utf-8")

        for query in ("super-secret-value", "API_KEY"):
            result = await enhanced_search(query=query, path=".", workspace=str(ws))
            assert result.success is True
            assert result.output["results"] == []

    async def test_sensitive_filenames_never_in_filename_results(
        self, tmp_path: Path
    ) -> None:
        """filename 搜索对凭据类文件名零命中（.env 与 .pem 两组输入）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".env").write_text("X=1", encoding="utf-8")
        (ws / "server.pem").write_text("-----BEGIN", encoding="utf-8")
        (ws / "keep.txt").write_text("placeholder", encoding="utf-8")

        for query in (".env", ".pem"):
            result = await enhanced_search(
                query=query, path=".", search_type="filename", workspace=str(ws)
            )
            assert result.success is True
            assert result.output["results"] == []

    async def test_target_path_itself_sensitive_rejected(self, tmp_path: Path) -> None:
        """目标路径自身命中凭据黑名单（单文件直指 .env）直接拒绝。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".env").write_text("API_KEY=topsecret\n", encoding="utf-8")

        result = await enhanced_search(
            query="topsecret", path=str(ws / ".env"), workspace=str(ws)
        )
        assert result.success is False
        assert "凭据类文件禁止访问" in (result.error or "")

    async def test_normal_file_hit_regression(self, tmp_path: Path) -> None:
        """正常文件命中回归：根内 sub.txt 内容与文件名搜索均正常。"""
        ws = tmp_path / "ws"
        (ws / "sub").mkdir(parents=True)
        (ws / "sub" / "sub.txt").write_text("alpha needle beta\n", encoding="utf-8")
        (ws / ".env").write_text("IGNORED=1", encoding="utf-8")

        text = await enhanced_search(query="needle", path="sub", workspace=str(ws))
        assert text.success is True
        assert [r["file_path"] for r in text.output["results"]] == [
            str((ws / "sub" / "sub.txt").resolve())
        ]
        assert text.output["results"][0]["content"] == "alpha needle beta"

        name = await enhanced_search(
            query="sub", path="sub", search_type="filename", workspace=str(ws)
        )
        assert name.success is True
        assert [r["content"] for r in name.output["results"]] == ["sub.txt"]


class TestWallClockBudget:
    @pytest.mark.asyncio
    async def test_timeout_returns_partial_with_marker(self, tmp_path: Path) -> None:
        """D6 同源墙钟护栏：timeout_seconds=0 时立即截断，结果带截断标记与说明。"""
        for i in range(50):
            (tmp_path / f"needle{i:03d}.txt").write_text("content", encoding="utf-8")
        result = await enhanced_search(
            query="needle",
            path=str(tmp_path),
            search_type="filename",
            workspace=str(tmp_path),
            project_root=str(tmp_path),
            timeout_seconds=-1.0,
        )
        assert result.success is True
        assert result.metadata.get("truncated") is True
        assert "超时" in (result.metadata.get("message") or "")
        # 截断前可能已有部分命中，但必须远小于总量（未走完全树）
        assert len(result.output["results"]) < 50

    @pytest.mark.asyncio
    async def test_generous_budget_completes(self, tmp_path: Path) -> None:
        """正常预算下不误标截断（找到全部命中且未触发超时）。"""
        for i in range(5):
            (tmp_path / f"needle{i}.txt").write_text("content", encoding="utf-8")
        result = await enhanced_search(
            query="needle",
            path=str(tmp_path),
            search_type="filename",
            workspace=str(tmp_path),
            project_root=str(tmp_path),
            timeout_seconds=30.0,
        )
        assert result.success is True
        assert result.metadata.get("truncated") is False
        assert len(result.output["results"]) == 5

# @feature: FP-0.2.二 内部模块统一 manifest 化 | @ci: python-coverage
"""仓库源码区读取锚测试（ADR 2026-09-12-read-anchor-repo-source）。

锁定契约（读 = read/search 三锚；写/删/move 维持单根；凭据黑名单恒先行）：
1. 仓库源码区（kernel/、docs/ 等非拒绝目录）纯读允许——即便在
   workspace/project_root 注入根之外；
2. 仓库运行时/产物目录（config/data/logs/.ai_workspaces/.git/.venv）
   读取拒绝，且拒绝原因指明目录与可读面；
3. 写操作对仓库源码区无豁免（单根越界仍拒绝）；
4. 凭据黑名单先于仓库锚（仓库根 .env 读取拒绝）；
5. enhanced_search 以仓库根为起点时拒绝目录被剪枝（零命中且不报错），
   直接以拒绝目录为起点时整体拒绝；
6. A1 回归：仓库外任意路径/``..`` 逃逸仍拒绝，自身工作区不受影响。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import repo_anchor

from agentos_builtin_tools.fs_tools import _check_workspace_path
from agentos_builtin_tools.search_tool import enhanced_search

pytestmark = pytest.mark.unit


@pytest.fixture()
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """构造带 config/isolation 标记的假仓库并钉住解析缓存。"""
    root = tmp_path / "repo"
    (root / "config" / "kernel").mkdir(parents=True)
    (root / "kernel" / "src").mkdir(parents=True)
    (root / "kernel" / "src" / "lib.py").write_text("REPO_NEEDLE = 1", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "note.md").write_text("doc needle here", encoding="utf-8")
    (root / "data" / "tenant").mkdir(parents=True)
    (root / "data" / "tenant" / "secret.txt").write_text("data needle", encoding="utf-8")
    (root / "config" / "llm.yaml").write_text("api_key: x", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "k.log").write_text("log needle", encoding="utf-8")
    (root / ".ai_workspaces" / "sessions" / "other").mkdir(parents=True)
    (root / ".ai_workspaces" / "sessions" / "other" / "a.txt").write_text(
        "other session needle", encoding="utf-8"
    )
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "objects" / "x.pack").write_text("git needle", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(root / "config"))
    repo_anchor.reset_cache()
    yield root
    repo_anchor.reset_cache()


async def _read(path: str, workspace: str) -> object:
    allowed, reason, _ = _check_workspace_path(path, workspace, None, operation="read")
    return allowed, reason or ""


class TestRepoReadAnchor:
    async def test_repo_source_file_readable_outside_workspace(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        """仓库源码文件在注入根之外可读（两组输入：kernel/ 与 docs/）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        for repo_file in (fake_repo / "kernel" / "src" / "lib.py", fake_repo / "docs" / "note.md"):
            allowed, reason = await _read(str(repo_file), str(ws))
            assert allowed, reason

    async def test_repo_runtime_dirs_denied(self, fake_repo: Path, tmp_path: Path) -> None:
        """仓库运行时/产物目录读取拒绝，原因指明目录与可读面。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        for denied in (
            fake_repo / "data" / "tenant" / "secret.txt",
            fake_repo / "config" / "llm.yaml",
            fake_repo / "logs" / "k.log",
            fake_repo / ".ai_workspaces" / "sessions" / "other" / "a.txt",
            fake_repo / ".git" / "objects" / "x.pack",
        ):
            allowed, reason = await _read(str(denied), str(ws))
            assert not allowed, f"{denied} 不应可读"
            assert "运行时/产物区" in reason

    async def test_write_has_no_repo_exemption(self, fake_repo: Path, tmp_path: Path) -> None:
        """写操作对仓库源码区无豁免（单根越界仍拒绝）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        allowed, reason, _ = _check_workspace_path(
            str(fake_repo / "kernel" / "src" / "lib.py"), str(ws), None, operation="write"
        )
        assert not allowed
        assert "超出 workspace/project_root" in reason

    async def test_credential_blacklist_precedes_repo_anchor(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        """仓库根 .env 被凭据黑名单拒绝（即使落在仓库可读面判定之前）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        allowed, reason = await _read(str(fake_repo / ".env"), str(ws))
        assert not allowed
        assert "凭据类文件" in reason

    async def test_own_workspace_inside_repo_unaffected(self, fake_repo: Path) -> None:
        """自身工作区在仓库内（.ai_workspaces 之外的自定义布局）照常可读。"""
        own = fake_repo / "ws"
        own.mkdir()
        (own / "mine.txt").write_text("mine", encoding="utf-8")
        allowed, reason = await _read(str(own / "mine.txt"), str(own))
        assert allowed, reason

    async def test_escape_outside_both_anchors_still_denied(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        """A1 回归：仓库与注入根之外的路径仍拒绝（仓库锚不过度放行）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "s.txt").write_text("x", encoding="utf-8")
        allowed, reason = await _read(str(outside / "s.txt"), str(ws))
        assert not allowed
        assert "超出 workspace/project_root" in reason


class TestRepoSearchPrune:
    async def test_search_at_repo_root_prunes_denied_dirs(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        """以仓库根为搜索起点（注入根为外部工作区）：源码区命中，拒绝目录零命中。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        result = await enhanced_search(query="needle", path=str(fake_repo), workspace=str(ws))
        assert result.success is True
        data = result.output["results"]
        hit_files = {Path(r["file_path"]).name for r in data}
        assert hit_files == {"lib.py", "note.md"}

    async def test_search_denied_dir_rejected(self, fake_repo: Path, tmp_path: Path) -> None:
        """拒绝目录作为搜索起点整体拒绝（仓库锚覆盖判定）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        result = await enhanced_search(
            query="needle", path=str(fake_repo / "data"), workspace=str(ws)
        )
        assert result.success is False
        assert "运行时/产物区" in (result.error or "")

    async def test_search_escape_regression(self, fake_repo: Path, tmp_path: Path) -> None:
        """A1 回归：``..`` 逃逸出注入根且不在仓库可读面时仍拒绝。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leaked.txt").write_text("needle", encoding="utf-8")
        ws = fake_repo / "ws"
        ws.mkdir()
        result = await enhanced_search(query="needle", path="../outside", workspace=str(ws))
        assert result.success is False

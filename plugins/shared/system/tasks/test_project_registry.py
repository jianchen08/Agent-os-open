# @feature: FP-0.2.〇 项目 = 文件夹 + 登记 | @ci: python-coverage
"""project_registry 项目登记测试（ADR 2026-08-27 + 2026-08-30 放宽/幂等）。

覆盖：显式已有非 git 目录自动 git init（不再拒绝）；ensure_project_registered
按路径幂等（同路径复用既有登记）；缺省路径自动生成 {ws_base}/projects/<slug>。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

# 共享层自举（plugins/shared/ —— project_registry 所在，与 service_access.py 同模式）
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from project_registry import (  # noqa: E402
    ProjectRegistry,
    ensure_project_folder,
    ensure_project_registered,
    load_project_paths,
    remove_project_folder,
)


@pytest.fixture
def reg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """隔离登记目录 + 工作空间基目录（不落仓库根 .ai_workspaces）。"""
    tasks_root = tmp_path / "tasks_data"
    ws_base = tmp_path / "ws"
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tasks_root))
    monkeypatch.setattr("project_registry.workspace_base_dir", lambda: ws_base)
    return {"tasks_root": tasks_root, "ws_base": ws_base}


class TestEnsureProjectFolder:
    def test_explicit_nongit_dir_gets_git_inited(self, reg_env: dict[str, Any]) -> None:
        """显式指定已有非空非 git 目录：不再拒绝，自动 git init（幂等不删文件）。"""
        target = reg_env["ws_base"] / "existing"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("data", encoding="utf-8")

        path = ensure_project_folder("标题", str(target))

        assert Path(path) == target
        assert (target / "keep.txt").read_text(encoding="utf-8") == "data"
        assert (target / ".git").is_dir()

    def test_explicit_git_dir_reused_as_is(self, reg_env: dict[str, Any]) -> None:
        """显式指定已是 git 仓库的目录：原样复用，不重复 init。"""
        target = reg_env["ws_base"] / "repo"
        target.mkdir(parents=True)
        import subprocess

        subprocess.run(["git", "init"], cwd=str(target), check=True, capture_output=True)

        path = ensure_project_folder("标题", str(target))

        assert Path(path) == target
        assert (target / ".git").is_dir()

    def test_default_path_slug_with_suffix(self, reg_env: dict[str, Any]) -> None:
        """缺省路径：{ws_base}/projects/<slug>；重名非空时递增后缀。"""
        first = ensure_project_folder("My Project")
        assert Path(first) == reg_env["ws_base"] / "projects" / "My_Project"

        second = ensure_project_folder("My Project")
        assert Path(second) == reg_env["ws_base"] / "projects" / "My_Project-2"
        assert first != second


class TestEnsureProjectRegistered:
    def test_creates_then_reuses_same_path(self, reg_env: dict[str, Any]) -> None:
        """同路径幂等：首次 created=True，再次 created=False 且复用同一登记行。"""
        target = reg_env["ws_base"] / "proj"
        target.mkdir(parents=True)

        first, created1 = ensure_project_registered("项目A", str(target))
        second, created2 = ensure_project_registered("项目A", str(target))

        assert created1 is True
        assert created2 is False
        assert second.id == first.id
        assert second.path == first.path
        # 登记目录只有一条记录
        assert len(list(load_project_paths())) == 1

    def test_reuse_matches_normalized_path(self, reg_env: dict[str, Any]) -> None:
        """路径归一化匹配（Windows 大小写/分隔符不敏感），重复登记复用。"""
        target = reg_env["ws_base"] / "CaseProj"
        target.mkdir(parents=True)

        first, _ = ensure_project_registered("项目A", str(target))
        import os

        variant = str(target).replace("\\", "/")
        if os.name == "nt":
            variant = variant.upper()
        second, created2 = ensure_project_registered("项目B", variant)

        assert created2 is False
        assert second.id == first.id

    def test_registered_with_session_and_user(self, reg_env: dict[str, Any]) -> None:
        """登记行携带 session_id / submitted_by（会话目录即项目链路）。"""
        target = reg_env["ws_base"] / "sess_proj"
        target.mkdir(parents=True)

        project, created = ensure_project_registered(
            "会话项目", str(target), session_id="sess-1", submitted_by="user-1"
        )

        assert created is True
        assert project.session_id == "sess-1"
        assert project.submitted_by == "user-1"
        # 独立 registry 实例（跨进程视角）也能读到
        registry = ProjectRegistry()
        loaded = registry.get(project.id)
        assert loaded is not None
        assert loaded.session_id == "sess-1"


class TestCreateProjectApi:
    """projects 域 create_project 端点（http_api）：幂等复用 + created 标志。"""

    async def test_create_project_idempotent_by_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reg_env: dict[str, Any]
    ) -> None:
        import http_api
        from project_registry import ProjectRegistry

        registry = ProjectRegistry(data_dir=reg_env["tasks_root"])
        monkeypatch.setattr(http_api, "_get_project_registry", lambda: registry)
        target = reg_env["ws_base"] / "proj"
        target.mkdir(parents=True)

        r1 = await http_api.create_project({"goal": "P", "path": str(target)})
        r2 = await http_api.create_project({"goal": "P", "path": str(target)})

        assert r1["created"] is True
        assert r2["created"] is False
        assert r2["project"]["id"] == r1["project"]["id"]
        assert r2["project"]["metadata"]["path"] == str(target)
        # 登记目录只有一条
        assert len(list(load_project_paths())) == 1

    async def test_create_project_reuses_registered_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reg_env: dict[str, Any]
    ) -> None:
        """会话目录已登记 → 复用既有登记，不新建（会话保存多次幂等）。"""
        import http_api
        from project_registry import ProjectRegistry

        registry = ProjectRegistry(data_dir=reg_env["tasks_root"])
        monkeypatch.setattr(http_api, "_get_project_registry", lambda: registry)
        target = reg_env["ws_base"] / "proj"
        target.mkdir(parents=True)

        r1 = await http_api.create_project(
            {"goal": "P", "path": str(target), "session_id": "sess-1"}
        )
        r2 = await http_api.create_project(
            {"goal": "P", "path": str(target), "session_id": "sess-2"}
        )

        assert r1["created"] is True
        assert r2["created"] is False
        assert r2["project"]["id"] == r1["project"]["id"]


class TestRemoveProjectFolder:
    def _git_repo_with_commit(self, target: Path) -> None:
        import subprocess

        target.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=str(target), check=True, capture_output=True)
        (target / "f.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(target), "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-m", "i"],
            check=True,
            capture_output=True,
        )

    def test_removes_git_repo_with_readonly_objects(self, reg_env: dict[str, Any]) -> None:
        """git 对象文件恒只读——删除须解锁后成功（Windows 删除端点实爆点）。"""
        target = reg_env["ws_base"] / "repo"
        self._git_repo_with_commit(target)
        readonly = [
            p
            for p in (target / ".git" / "objects").rglob("*")
            if p.is_file() and not (p.stat().st_mode & stat.S_IWRITE)
        ]
        if os.name == "nt":
            assert readonly, "git commit 落盘的松散对象应有只读位"

        assert remove_project_folder(str(target)) is True
        assert not target.exists()

    def test_rejects_workspace_base_and_missing_path(self, reg_env: dict[str, Any]) -> None:
        """保护路径（工作空间基目录）与不存在路径都返回 False，不抛错。"""
        assert remove_project_folder(str(reg_env["ws_base"])) is False
        assert remove_project_folder(str(reg_env["ws_base"] / "nope")) is False

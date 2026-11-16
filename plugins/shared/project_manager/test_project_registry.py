# @feature: FP-0.2.〇 项目 = 文件夹 + 登记 | @ci: python-coverage
"""project_registry 项目登记测试（ADR 2026-08-27 + 2026-08-30 放宽/幂等）。

自 tasks/test_project_registry.py 迁入（T6a：登记簿吸收进 project_manager，
模块测试随实现走）。覆盖：显式已有非 git 目录自动 git init（不再拒绝）；
ensure_project_registered 按路径幂等（同路径复用既有登记）；缺省路径自动
生成 {ws_base}/projects/<slug>；删除守卫与路径归一化分支。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

# 共享根自举（plugins/shared/ —— project_manager 包所在；包限定名导入免串扰）
_SHARED_ROOT = Path(__file__).resolve().parents[1]
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from project_manager import project_registry as pr  # noqa: E402


@pytest.fixture
def reg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """隔离登记目录 + 工作空间基目录（不落仓库根 .ai_workspaces）。"""
    tasks_root = tmp_path / "tasks_data"
    ws_base = tmp_path / "ws"
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tasks_root))
    monkeypatch.setattr("project_manager.project_registry.workspace_base_dir", lambda: ws_base)
    return {"tasks_root": tasks_root, "ws_base": ws_base}


class TestEnsureProjectFolder:
    def test_explicit_nongit_dir_gets_git_inited(self, reg_env: dict[str, Any]) -> None:
        """显式指定已有非空非 git 目录：不再拒绝，自动 git init（幂等不删文件）。"""
        target = reg_env["ws_base"] / "existing"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("data", encoding="utf-8")

        path = pr.ensure_project_folder("标题", str(target))

        assert Path(path) == target
        assert (target / "keep.txt").read_text(encoding="utf-8") == "data"
        assert (target / ".git").is_dir()

    def test_explicit_git_dir_reused_as_is(self, reg_env: dict[str, Any]) -> None:
        """显式指定已是 git 仓库的目录：原样复用，不重复 init。"""
        target = reg_env["ws_base"] / "repo"
        target.mkdir(parents=True)
        import subprocess

        subprocess.run(["git", "init"], cwd=str(target), check=True, capture_output=True)

        path = pr.ensure_project_folder("标题", str(target))

        assert Path(path) == target
        assert (target / ".git").is_dir()

    def test_default_path_slug_with_suffix(self, reg_env: dict[str, Any]) -> None:
        """缺省路径：{ws_base}/projects/<slug>；重名非空时递增后缀。"""
        first = pr.ensure_project_folder("My Project")
        assert Path(first) == reg_env["ws_base"] / "projects" / "My_Project"

        second = pr.ensure_project_folder("My Project")
        assert Path(second) == reg_env["ws_base"] / "projects" / "My_Project-2"
        assert first != second


class TestEnsureProjectRegistered:
    def test_creates_then_reuses_same_path(self, reg_env: dict[str, Any]) -> None:
        """同路径幂等：首次 created=True，再次 created=False 且复用同一登记行。"""
        target = reg_env["ws_base"] / "proj"
        target.mkdir(parents=True)

        first, created1 = pr.ensure_project_registered("项目A", str(target))
        second, created2 = pr.ensure_project_registered("项目A", str(target))

        assert created1 is True
        assert created2 is False
        assert second.id == first.id
        assert second.path == first.path
        # 登记目录只有一条记录
        assert len(list(pr.load_project_paths())) == 1

    def test_reuse_matches_normalized_path(self, reg_env: dict[str, Any]) -> None:
        """路径归一化匹配（Windows 大小写/分隔符不敏感），重复登记复用。"""
        target = reg_env["ws_base"] / "CaseProj"
        target.mkdir(parents=True)

        first, _ = pr.ensure_project_registered("项目A", str(target))
        variant = str(target).replace("\\", "/")
        if os.name == "nt":
            variant = variant.upper()
        second, created2 = pr.ensure_project_registered("项目B", variant)

        assert created2 is False
        assert second.id == first.id

    def test_registered_with_session_and_user(self, reg_env: dict[str, Any]) -> None:
        """登记行携带 session_id / submitted_by（会话目录即项目链路）。"""
        target = reg_env["ws_base"] / "sess_proj"
        target.mkdir(parents=True)

        project, created = pr.ensure_project_registered(
            "会话项目", str(target), session_id="sess-1", submitted_by="user-1"
        )

        assert created is True
        assert project.session_id == "sess-1"
        assert project.submitted_by == "user-1"
        # 独立 registry 实例（跨进程视角）也能读到
        registry = pr.ProjectRegistry()
        loaded = registry.get(project.id)
        assert loaded is not None
        assert loaded.session_id == "sess-1"


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

        assert pr.remove_project_folder(str(target)) is True
        assert not target.exists()

    def test_rejects_workspace_base_and_missing_path(self, reg_env: dict[str, Any]) -> None:
        """保护路径（工作空间基目录）与不存在路径都返回 False，不抛错。"""
        assert pr.remove_project_folder(str(reg_env["ws_base"])) is False
        assert pr.remove_project_folder(str(reg_env["ws_base"] / "nope")) is False


class TestPathNormalizationGuards:
    """路径归一/守卫分支（覆盖率收口批 2026-09-17）。"""

    def test_norm_path_lowercases_on_nt_and_absolutifies(self, tmp_path: Path) -> None:
        raw = str(tmp_path / "MiXeD_Case")
        norm = pr._norm_path(raw)
        assert os.path.isabs(norm)
        if os.name == "nt":
            assert norm == norm.lower()
            assert "mixed_case" in norm
        else:
            # 非 nt 平台保持原大小写（行为按 os.name 分叉，双向都验证）
            assert "MiXeD_Case" in norm

    def test_canonical_path_bottoms_out_at_missing_root(self) -> None:
        """目标连同全部祖先都不存在（含盘符根）→ 走 root-anchors 终止分支不死循环。"""
        free_root = next(
            (f"{L}:\\" for L in "CDEFGHIJKLMNOPQRSTUVWXYZ"
             if not Path(f"{L}:\\").exists()),
            None,
        )
        if free_root is None:
            pytest.skip("26 个盘符全部存在（异常宿主）")
        deep = free_root + "never_created_a/never_created_b"
        canon = pr._canonical_path(deep)
        assert canon == canon.lower() if os.name == "nt" else True
        assert "never_created_a" in canon

    def test_overlap_check_skips_registry_entries_without_path(
        self, reg_env: dict[str, Any]
    ) -> None:
        """已登记项缺 path（脏数据）→ 跳过不误判重叠。"""
        from types import SimpleNamespace

        registry = SimpleNamespace(
            list=lambda: [SimpleNamespace(id="dirty", path=""), SimpleNamespace(id="ok", path="")]
        )
        # 不抛即通过（空 path 条目被 continue 跳过）
        pr._assert_no_project_overlap(str(reg_env["ws_base"] / "any_project"), registry)

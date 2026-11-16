# @feature: FP-GATE git hooks 幂等安装 | @ci: python-coverage
"""git hooks 幂等安装回归（方案 §3.6 步骤 5 件 1；E0-38 修复后口径）。

断行为口径：
- install_hooks：core.hooksPath 指向 scripts/githooks（现役面）、幂等、
  钩子文件缺失即红；
- 反例（防拟合）：安装器不得指向旧面 scripts/hooks——哪怕旧面目录恰好在位
  （执法降级开关必须焊死）；旧面摘除后不得回流。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_INSTALL = _ROOT / "scripts" / "install_hooks.py"
_GITHOOK = _ROOT / "scripts" / "githooks" / "pre-commit"
_LEGACY_DIR = _ROOT / "scripts" / "hooks"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def _tmp_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def _run_install(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_INSTALL), "--repo", str(repo)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _hooks_path(repo: Path) -> str:
    return _git(repo, "config", "core.hooksPath").stdout.strip()


class TestInstallHooks:
    def test_hooks_path_set_and_idempotent(self, tmp_path: Path) -> None:
        repo = _tmp_git_repo(tmp_path)
        hooks = repo / "scripts" / "githooks"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

        first = _run_install(repo)
        assert first.returncode == 0, first.stderr
        assert _hooks_path(repo) == "scripts/githooks"

        # 幂等：重复安装无害且结果一致
        second = _run_install(repo)
        assert second.returncode == 0, second.stderr
        assert _hooks_path(repo) == "scripts/githooks"

    def test_never_points_at_legacy_hooks_surface(self, tmp_path: Path) -> None:
        """E0-38 反例：新旧两面同时在位时，安装器必须选现役面 githooks，
        绝不写回旧面 scripts/hooks（写回 = 提交执法面降级开关）。"""
        repo = _tmp_git_repo(tmp_path)
        legacy = repo / "scripts" / "hooks"
        legacy.mkdir(parents=True)
        (legacy / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        active = repo / "scripts" / "githooks"
        active.mkdir(parents=True)
        (active / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

        proc = _run_install(repo)
        assert proc.returncode == 0, proc.stderr
        assert _hooks_path(repo) == "scripts/githooks"
        assert _hooks_path(repo) != "scripts/hooks"

    def test_missing_hook_file_fails(self, tmp_path: Path) -> None:
        repo = _tmp_git_repo(tmp_path)
        (repo / "scripts" / "githooks").mkdir(parents=True)
        proc = _run_install(repo)
        assert proc.returncode != 0  # 钩子文件缺失 → fail-closed


class TestHookSurfaceContract:
    """主仓钩子面契约（E0-38 修复轮锚）：旧面摘除不回流，现役面在位且
    与安装器同源。"""

    def test_legacy_hooks_surface_removed(self) -> None:
        """旧面 scripts/hooks/ 必须不存在（摘除后防回流锚）。"""
        assert not _LEGACY_DIR.exists(), "旧面 scripts/hooks/ 已摘除，不得回流"

    def test_active_hook_surface_in_place(self) -> None:
        """现役面 githooks/pre-commit 在位，委托 check_pre_commit 车道触发器。"""
        assert _GITHOOK.is_file()
        text = _GITHOOK.read_text(encoding="utf-8")
        assert "check_pre_commit.py" in text

    def test_installer_targets_active_surface(self) -> None:
        """安装器 HOOKS_REL 与现役面同源（防安装器与生效面再次反向）。"""
        source = _INSTALL.read_text(encoding="utf-8")
        assert '"githooks"' in source
        assert '"hooks"' not in source

# @feature: FP-GATE 项目初始化脚手架 | @ci: python-coverage
"""项目初始化脚手架回归（方案 AI辅助编程体系落地AgentOS_20261022 §3.6 件 3）。

裸项目一键出 CI 骨架：run_gates 两档 + 路径→模块映射 + 受保护清单 +
frontmatter 模板 + 域规范占位 + hooks 安装。产物入库即受保护清单护住。
断行为口径：产物齐全性 / 幂等跳过 / 域选项路由 / git 目标自动装钩 /
生成物可编译（真实链路：py_compile 真编译）。
"""

from __future__ import annotations

import importlib.util
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "skill-dev-toolkit"
    / "scripts"
    / "scaffold_project.py"
)
_spec = importlib.util.spec_from_file_location("scaffold_project", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("scaffold_project", _mod)
_spec.loader.exec_module(_mod)

EXPECTED_FILES = [
    "scripts/run_gates.py",
    "scripts/hooks/pre-commit",
    "scripts/hooks/commit-msg",
    "scripts/install_hooks.py",
    "scripts/check_gate_protected.py",
    "scripts/check_rule_enforcement_tier.py",
    ".github/gate-protected-paths.txt",
    ".github/enforcement-tier-baseline.txt",
    ".github/workflows/ci.yml",
    "docs/schemas/文档frontmatter模板.md",
    "config/rules/enforcement_tiers.yaml",
    "config/rules/per_domain/coding_domain_rules.md",
]


def _scaffold(target: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(target), *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestSkeletonGeneration:
    def test_generates_full_skeleton(self, tmp_path: Path) -> None:
        target = tmp_path / "proj"
        target.mkdir()
        proc = _scaffold(target, "--name", "demo", "--domain", "coding")
        assert proc.returncode == 0, proc.stderr
        for rel in EXPECTED_FILES:
            assert (target / rel).is_file(), f"缺产物: {rel}"

    def test_generated_content_anchors(self, tmp_path: Path) -> None:
        """生成物内容锚点：核心机制必须在场（非空壳）。"""
        target = tmp_path / "proj2"
        target.mkdir()
        assert _scaffold(target, "--name", "demo", "--domain", "coding").returncode == 0
        assert "--changed-files" in (target / "scripts/run_gates.py").read_text(
            encoding="utf-8"
        )
        assert "--strict" in (target / "scripts/hooks/pre-commit").read_text(
            encoding="utf-8"
        )
        assert "scripts/run_gates.py" in (
            target / ".github/gate-protected-paths.txt"
        ).read_text(encoding="utf-8")
        assert "tier" in (target / "config/rules/enforcement_tiers.yaml").read_text(
            encoding="utf-8"
        )
        assert "--mode all" in (target / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )

    def test_domain_option_picks_placeholder(self, tmp_path: Path) -> None:
        target = tmp_path / "projw"
        target.mkdir()
        proc = _scaffold(target, "--name", "w", "--domain", "writing")
        assert proc.returncode == 0, proc.stderr
        assert (target / "config/rules/per_domain/writing_domain_rules.md").is_file()
        assert not (target / "config/rules/per_domain/coding_domain_rules.md").exists()

    def test_generated_python_compiles(self, tmp_path: Path) -> None:
        """真实链路：生成物全部可编译（语法级可用，非文本模板残缺）。"""
        target = tmp_path / "projc"
        target.mkdir()
        assert _scaffold(target, "--name", "d", "--domain", "coding").returncode == 0
        py_files = sorted((target / "scripts").rglob("*.py"))
        assert py_files, "应有生成脚本"
        for f in py_files:
            py_compile.compile(str(f), doraise=True)

    def test_generated_skeleton_self_check_passes(self, tmp_path: Path) -> None:
        """生成骨架自举：受保护检查器与执法档位检查器在骨架上真跑必绿。"""
        target = tmp_path / "projs"
        target.mkdir()
        assert _scaffold(target, "--name", "d", "--domain", "coding").returncode == 0
        for checker in ("check_gate_protected.py", "check_rule_enforcement_tier.py"):
            proc = subprocess.run(
                [sys.executable, str(target / "scripts" / checker)],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=target,
            )
            assert proc.returncode == 0, f"{checker}: {proc.stderr}{proc.stdout}"


class TestIdempotency:
    def test_second_run_reports_skipped(self, tmp_path: Path) -> None:
        target = tmp_path / "proj3"
        target.mkdir()
        assert _scaffold(target, "--name", "d", "--domain", "coding").returncode == 0
        second = _scaffold(target, "--name", "d", "--domain", "coding")
        assert second.returncode == 0, second.stderr
        assert "skipped" in (second.stdout + second.stderr).lower()

    def test_second_run_does_not_clobber(self, tmp_path: Path) -> None:
        target = tmp_path / "proj4"
        target.mkdir()
        assert _scaffold(target, "--name", "d", "--domain", "coding").returncode == 0
        marker = target / "scripts" / "hooks" / "pre-commit"
        marker.write_text("# human edited\n", encoding="utf-8")
        assert _scaffold(target, "--name", "d", "--domain", "coding").returncode == 0
        assert marker.read_text(encoding="utf-8") == "# human edited\n"  # 幂等不覆盖


class TestGitTarget:
    def test_git_repo_installs_hooks(self, tmp_path: Path) -> None:
        target = tmp_path / "projg"
        target.mkdir()
        subprocess.run(
            ["git", "init", "-q"], cwd=target, capture_output=True, timeout=60, check=True
        )
        proc = _scaffold(target, "--name", "d", "--domain", "coding")
        assert proc.returncode == 0, proc.stderr
        cfg = subprocess.run(
            ["git", "-C", str(target), "config", "core.hooksPath"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        assert cfg.stdout.strip() == "scripts/hooks"

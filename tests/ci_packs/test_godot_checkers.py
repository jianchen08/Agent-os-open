# @feature: FP-0.2.〇 项目类型 CI 包 | @ci: python-coverage
"""godot pack 五门行为测试（设计稿 §3.1 双层五门 / §9.7）。

不依赖真实 godot 环境：工具探测两路用可执行替身（win=.bat / posix=sh）驱动——
工具缺席=skip（exit 2，fail-open），违规=fail-closed 红（exit 1）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.ci_packs.conftest import GODOT_PACK, git, make_tool_shim, run_checker, tool_path

pytestmark = pytest.mark.unit

GODOT_CHECKS = GODOT_PACK / "checks"

_SCAFFOLD = (
    'config_version=5\n\n[application]\n\nconfig/name="G"\n\n[editor_plugins]\n\n'
    'enabled=PackedStringArray("res://addons/agentos/plugin.cfg")\n'
)


@pytest.fixture
def ci_project(git_project: Path) -> Path:
    """带真实 godot checks 的临时 git 项目（_common 由 base pack 提供，装配同构）。"""
    ci = git_project / ".agentos" / "ci"
    (ci / "checks").mkdir(parents=True)
    shutil.copyfile(GODOT_PACK.parent / "base" / "checks" / "_common.py", ci / "checks" / "_common.py")
    for py in GODOT_CHECKS.glob("*.py"):
        shutil.copyfile(py, ci / "checks" / py.name)
    return git_project


def _born_addons(project: Path, names: tuple[str, ...] = ("agentos", "godot_mcp")) -> None:
    for name in names:
        cfg = project / "addons" / name / "plugin.cfg"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(f'[plugin]\nname="{name}"\n', encoding="utf-8")


class TestProjectGodotHygiene:
    def test_valid_project_passes(self, ci_project: Path) -> None:
        (ci_project / "project.godot").write_text(_SCAFFOLD, encoding="utf-8")
        _born_addons(ci_project, ("agentos",))
        assert run_checker(ci_project, "project_godot_hygiene.py").returncode == 0

    def test_missing_project_file_blocks(self, ci_project: Path) -> None:
        assert run_checker(ci_project, "project_godot_hygiene.py").returncode == 1

    def test_unparseable_project_blocks(self, ci_project: Path) -> None:
        (ci_project / "project.godot").write_text("### 坏档 ###\n", encoding="utf-8")
        r = run_checker(ci_project, "project_godot_hygiene.py")
        assert r.returncode == 1
        assert "解析" in r.stdout

    def test_missing_enabled_entry_blocks(self, ci_project: Path) -> None:
        """启用条目在位检查：enabled 引用的 plugin.cfg 缺失 → 红。"""
        (ci_project / "project.godot").write_text(_SCAFFOLD, encoding="utf-8")
        r = run_checker(ci_project, "project_godot_hygiene.py")
        assert r.returncode == 1
        assert "addons/agentos/plugin.cfg" in r.stdout


class TestAddonsPresent:
    def test_promised_addons_pass(self, ci_project: Path) -> None:
        _born_addons(ci_project)
        r = run_checker(ci_project, "addons_present.py", "--addons", "agentos,godot_mcp")
        assert r.returncode == 0

    def test_missing_addon_blocks(self, ci_project: Path) -> None:
        _born_addons(ci_project, ("agentos",))
        r = run_checker(ci_project, "addons_present.py", "--addons", "agentos,godot_mcp")
        assert r.returncode == 1
        assert "godot_mcp" in r.stdout

    def test_no_promise_skips(self, ci_project: Path) -> None:
        """未承诺 addons（无 --addons 参数）= 无物可查 → skip。"""
        assert run_checker(ci_project, "addons_present.py").returncode == 2


class TestGdparseSyntax:
    def _gd(self, project: Path) -> None:
        """入一个 .gd 文件并暂存（staged 清单经 stdin 缺省回落 tracked）。"""
        (project / "scripts" / "main.gd").parent.mkdir(parents=True, exist_ok=True)
        (project / "scripts" / "main.gd").write_text("extends Node\n", encoding="utf-8")
        git(project, "add", "-A")

    def test_no_gdparse_skips(self, ci_project: Path, tmp_path: Path) -> None:
        self._gd(ci_project)
        r = run_checker(ci_project, "gdparse_syntax.py", env_extra={"PATH": tool_path(tmp_path)})
        assert r.returncode == 2
        assert "gdparse" in r.stdout

    def test_syntax_violation_blocks(self, ci_project: Path, tmp_path: Path) -> None:
        self._gd(ci_project)
        make_tool_shim(tmp_path, "gdparse", 1, stdout="scripts/main.gd:1 Parse Error")
        r = run_checker(ci_project, "gdparse_syntax.py", env_extra={"PATH": tool_path(tmp_path)})
        assert r.returncode == 1
        assert "Parse Error" in r.stdout

    def test_clean_syntax_passes(self, ci_project: Path, tmp_path: Path) -> None:
        self._gd(ci_project)
        make_tool_shim(tmp_path, "gdparse", 0)
        r = run_checker(ci_project, "gdparse_syntax.py", env_extra={"PATH": tool_path(tmp_path)})
        assert r.returncode == 0

    def test_no_gd_files_passes_with_tool(self, ci_project: Path, tmp_path: Path) -> None:
        make_tool_shim(tmp_path, "gdparse", 1)  # 工具即便会红，无 .gd 也不该调它
        r = run_checker(ci_project, "gdparse_syntax.py", env_extra={"PATH": tool_path(tmp_path)})
        assert r.returncode == 0


class TestGodotHeadlessImport:
    def test_baked_bin_ok_passes(self, ci_project: Path, tmp_path: Path) -> None:
        (ci_project / "project.godot").write_text(_SCAFFOLD, encoding="utf-8")
        bin_path = make_tool_shim(tmp_path, "fakegodot", 0)
        r = run_checker(ci_project, "godot_headless_import.py", "--godot", bin_path)
        assert r.returncode == 0

    def test_import_failure_blocks_with_output_tail(self, ci_project: Path, tmp_path: Path) -> None:
        bin_path = make_tool_shim(tmp_path, "fakegodot", 1, stdout="parse error!/res://gone.tscn")
        r = run_checker(ci_project, "godot_headless_import.py", "--godot", bin_path)
        assert r.returncode == 1
        assert "gone.tscn" in r.stdout

    def test_env_probe_found(self, ci_project: Path, tmp_path: Path) -> None:
        bin_path = make_tool_shim(tmp_path, "fakegodot", 0)
        r = run_checker(ci_project, "godot_headless_import.py", env_extra={"GODOT_BIN": bin_path})
        assert r.returncode == 0

    def test_bin_absent_skips(self, ci_project: Path, tmp_path: Path) -> None:
        r = run_checker(
            ci_project,
            "godot_headless_import.py",
            env_extra={"PATH": str(tmp_path), "GODOT_BIN": ""},
        )
        assert r.returncode == 2


class TestGodotBootSmoke:
    def test_boot_ok_passes(self, ci_project: Path, tmp_path: Path) -> None:
        bin_path = make_tool_shim(tmp_path, "fakegodot", 0)
        assert run_checker(ci_project, "godot_boot_smoke.py", "--godot", bin_path).returncode == 0

    def test_boot_failure_reports(self, ci_project: Path, tmp_path: Path) -> None:
        bin_path = make_tool_shim(tmp_path, "fakegodot", 1, stdout="SCRIPT ERROR: boot")
        r = run_checker(ci_project, "godot_boot_smoke.py", "--godot", bin_path)
        assert r.returncode == 1
        assert "SCRIPT ERROR" in r.stdout

    def test_bin_absent_skips(self, ci_project: Path, tmp_path: Path) -> None:
        r = run_checker(
            ci_project,
            "godot_boot_smoke.py",
            env_extra={"PATH": str(tmp_path), "GODOT_BIN": ""},
        )
        assert r.returncode == 2

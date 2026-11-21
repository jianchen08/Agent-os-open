# @feature: FP-0.2.PACK 包内插件种子新鲜度闸（ADR 2026-11-07 刀 4） | @ci: python-coverage
"""check_package_plugin_freshness.py 单测。

行为契约（断输入→输出，不钉实现）：
- 等版本 → 0（含共享根目录/单文件不进对账面的跳过语义）；
- 版本不等 / 包缺插件 / 包多余插件 → 1 且差异表可定位；
- pack-root 缺失 / ref 不可解析 → 2；
- 纯函数 compare_versions 三类差异行齐备。
临时 git 仓夹具构造仓侧 HEAD，包侧为普通临时树。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location("check_package_plugin_freshness", SCRIPTS / "check_package_plugin_freshness.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _write_plugin(root: Path, pid: str, version: str) -> None:
    d = root / "plugins" / "shared" / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({"id": pid, "version": version}), encoding="utf-8")


def _make_repo(tmp_path: Path) -> Path:
    """仓侧 HEAD：llm@1.0.16 + approval@1.0.0 + 共享根目录 + 根层单文件。"""
    repo = tmp_path / "repo"
    shared = repo / "plugins" / "shared"
    shared.mkdir(parents=True)
    _write_plugin(repo, "llm", "1.0.16")
    _write_plugin(repo, "approval", "1.0.0")
    (shared / "_host").mkdir()
    (shared / "_host" / "host.py").write_text("", encoding="utf-8")
    (shared / "wiring.py").write_text("", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init", "--no-gpg-sign")
    return repo


def _make_pack(tmp_path: Path, versions: dict[str, str], name: str = "pack") -> Path:
    pack = tmp_path / name
    for pid, ver in versions.items():
        _write_plugin(pack, pid, ver)
    (pack / "plugins" / "shared" / "_host").mkdir(parents=True)  # 共享根随包照搬，无 plugin.json
    return pack


class TestCompareVersions:
    def test_equal_is_empty(self):
        assert mod.compare_versions({"llm": "1.0.16"}, {"llm": "1.0.16"}, "HEAD") == []

    def test_mismatch_and_both_missing_directions(self):
        pack = {"llm": "1.0.14", "ghost": "1.0.0"}
        head = {"llm": "1.0.16", "newguy": "2.0.0"}
        lines = mod.compare_versions(pack, head, "HEAD")
        assert len(lines) == 3
        assert any("llm" in ln and "版本不等" in ln for ln in lines)
        assert any("ghost" in ln and "多余" in ln for ln in lines)
        assert any("newguy" in ln and "缺失" in ln for ln in lines)


class TestMainEndToEnd:
    def test_equal_versions_green(self, tmp_path):
        repo = _make_repo(tmp_path)
        pack = _make_pack(tmp_path, {"llm": "1.0.16", "approval": "1.0.0"})
        assert mod.main(["--pack-root", str(pack), "--repo", str(repo)]) == 0

    def test_stale_version_red_with_diff_table(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        pack = _make_pack(tmp_path, {"llm": "1.0.14", "approval": "1.0.0"})
        assert mod.main(["--pack-root", str(pack), "--repo", str(repo)]) == 1
        out = capsys.readouterr().out
        assert "版本不等：llm" in out
        assert "1.0.14" in out
        assert "1.0.16" in out

    def test_missing_plugin_in_pack_red(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        pack = _make_pack(tmp_path, {"llm": "1.0.16"})
        assert mod.main(["--pack-root", str(pack), "--repo", str(repo)]) == 1
        out = capsys.readouterr().out
        assert "包内种子缺失" in out
        assert "approval" in out

    def test_extra_plugin_in_pack_red(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        pack = _make_pack(tmp_path, {"llm": "1.0.16", "approval": "1.0.0", "ghost": "0.9.0"})
        assert mod.main(["--pack-root", str(pack), "--repo", str(repo)]) == 1
        out = capsys.readouterr().out
        assert "包内种子多余" in out
        assert "ghost" in out

    def test_ref_pinned_comparison(self, tmp_path):
        """--ref 钉参照提交（release 链传 BUILD_HEAD 的契约面）。"""
        repo = _make_repo(tmp_path)
        pack = _make_pack(tmp_path, {"llm": "1.0.16", "approval": "1.0.0"})
        assert mod.main(["--pack-root", str(pack), "--repo", str(repo), "--ref", "HEAD"]) == 0


class TestEnvironmentErrors:
    def test_missing_pack_root(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        assert mod.main(["--pack-root", str(tmp_path / "nope"), "--repo", str(repo)]) == 2
        assert "环境不可判" in capsys.readouterr().out

    def test_pack_root_without_seed_tree_red(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        pack = tmp_path / "empty_pack"
        pack.mkdir()
        assert mod.main(["--pack-root", str(pack), "--repo", str(repo)]) == 1
        assert "种子树" in capsys.readouterr().out

    def test_unresolvable_ref(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        pack = _make_pack(tmp_path, {"llm": "1.0.16", "approval": "1.0.0"})
        assert mod.main(["--pack-root", str(pack), "--repo", str(repo), "--ref", "nosuch-ref"]) == 2
        assert "环境不可判" in capsys.readouterr().out

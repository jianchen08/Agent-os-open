# @feature: FP-0.2.CFG 分级清单一致性对账闸（ADR 2026-11-07 刀 4） | @ci: python-coverage
"""check_plugin_tiers_consistency.py 单测。

行为契约（断输入→输出，不钉实现）：
- 纯函数两面：①清单条目悬空=红；安全插件缺 plugin.json=红；共享目录/单文件
  不在磁盘=红；②`_` 前缀跟踪条目未登记=红（新增共享件忘登记）；正例=空。
- 扫描面 = git 跟踪条目：临时仓夹具证明未跟踪运行时产物（_junk）不进对账面。
- 真仓绿：现清单与实际本就一致（验收判据 ② 的常驻证明；清单漂移此测即红）。
- main 退出码：一致=0；不一致=1；git 不可用/共享根缺失=2。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location("check_plugin_tiers_consistency", SCRIPTS / "check_plugin_tiers_consistency.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _buckets(plugin_dirs=(), module_dirs=(), module_files=()) -> dict[str, list[str]]:
    return {
        "plugin_dirs": list(plugin_dirs),
        "module_dirs": list(module_dirs),
        "module_files": list(module_files),
    }


def _make_shared(root: Path, plugins_with_manifest=(), module_dirs=(), module_files=()) -> Path:
    """构造 plugins/shared 磁盘面（目录 + plugin.json / 单文件）。"""
    shared = root / "plugins" / "shared"
    shared.mkdir(parents=True)
    for pid in plugins_with_manifest:
        d = shared / pid
        d.mkdir()
        (d / "plugin.json").write_text(json.dumps({"id": pid, "version": "1.0.0"}), encoding="utf-8")
    for mid in module_dirs:
        (shared / mid).mkdir()
        (shared / mid / "mod.py").write_text("", encoding="utf-8")
    for fname in module_files:
        shared.joinpath(fname).write_text("", encoding="utf-8")
    return shared


class TestCheckConsistency:
    def test_ok_all_three_classes(self, tmp_path):
        shared = _make_shared(tmp_path, ["approval"], ["_libs"], ["_llm_streaming.py"])
        buckets = _buckets(["approval"], ["_libs"], ["_llm_streaming.py"])
        tracked = {"approval", "_libs", "_llm_streaming.py"}
        assert mod.check_consistency(buckets, tracked, shared) == []

    def test_dangling_manifest_entry(self, tmp_path):
        shared = _make_shared(tmp_path, ["approval"])
        problems = mod.check_consistency(_buckets(["approval", "ghost"]), {"approval"}, shared)
        assert len(problems) == 1
        assert "ghost" in problems[0]
        assert "悬空" in problems[0]

    def test_safety_plugin_missing_plugin_json(self, tmp_path):
        # id 语义（2026-11-07）：safety_plugins 条目 = manifest id；目录无
        # plugin.json → 该 id 无法解析 → 悬空（含 id 不可解析的次生明细）。
        shared = _make_shared(tmp_path, ["approval"])
        (shared / "broken").mkdir()  # 目录在、plugin.json 缺 → id "broken" 不可解析
        problems = mod.check_consistency(_buckets(["approval", "broken"]), {"approval", "broken"}, shared)
        assert len(problems) == 1, problems
        assert "broken" in problems[0] and "悬空" in problems[0], problems[0]

    def test_module_dir_and_file_missing_on_disk(self, tmp_path):
        shared = _make_shared(tmp_path, ["approval"])
        tracked = {"approval", "_host", "atomic_io.py"}
        problems = mod.check_consistency(_buckets(["approval"], ["_host"], ["atomic_io.py"]), tracked, shared)
        assert len(problems) == 2
        assert any("_host" in p and "共享目录" in p for p in problems)
        assert any("atomic_io.py" in p and "共享单文件" in p for p in problems)

    def test_unregistered_underscore_entry(self, tmp_path):
        shared = _make_shared(tmp_path, ["approval"], ["_libs"], ["_llm_streaming.py"])
        buckets = _buckets(["approval"], ["_libs"], ["_llm_streaming.py"])
        tracked = set().union(*buckets.values()) | {"_newthing"}
        problems = mod.check_consistency(buckets, tracked, shared)
        assert len(problems) == 1
        assert "_newthing" in problems[0]
        assert "未登记" in problems[0]

    def test_non_underscore_stray_py_not_flagged(self, tmp_path):
        """清单外非 `_` 前缀条目默认 Tier2 语义，不在本闸两面内（任务口径）。"""
        shared = _make_shared(tmp_path, ["approval"])
        tracked = {"approval", "wiring.py"}
        assert mod.check_consistency(_buckets(["approval"]), tracked, shared) == []


class TestGatherTrackedEntries:
    def test_untracked_runtime_junk_excluded(self, tmp_path):
        """未跟踪产物（__pycache__ 形态）不进对账面——开发机/CI 同判定。"""
        shared = _make_shared(tmp_path, ["approval"], ["_libs"])
        _git(tmp_path, "init")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init", "--no-gpg-sign")
        # 运行时垃圾在 commit 后落盘（保持未跟踪，模拟 __pycache__/_dbg_tmp 形态）
        junk = shared / "_dbg_tmp_neighbor"
        junk.mkdir()
        (junk / "scratch.txt").write_text("", encoding="utf-8")
        tracked = mod.gather_tracked_entries(tmp_path, Path("plugins/shared"))
        assert {"approval", "_libs"} <= tracked
        assert "_dbg_tmp_neighbor" not in tracked

    def test_git_failure_raises(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(mod.GitUnavailableError):
            mod.gather_tracked_entries(tmp_path / "empty", Path("plugins/shared"))


class TestRealRepoGreen:
    def test_current_manifest_consistent(self, capsys):
        """验收判据：现清单与实际目录本就一致，闸应为绿（常驻证明）。"""
        assert mod.main(["--repo", str(REPO)]) == 0
        assert "PASS" in capsys.readouterr().out

    def test_missing_shared_root_is_env_error(self, tmp_path, capsys):
        manifest = tmp_path / "tiers.yaml"
        manifest.write_text("tier1:\n  safety_plugins: []\n  shared_modules: []\n", encoding="utf-8")
        assert mod.main(["--repo", str(tmp_path), "--manifest", str(manifest)]) == 2
        assert "环境不可判" in capsys.readouterr().out


class TestMainEndToEnd:
    def _init_repo_with_shared(self, tmp_path) -> Path:
        shared = _make_shared(tmp_path, ["approval"], ["_libs"], ["_llm_streaming.py"])
        _git(tmp_path, "init")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init", "--no-gpg-sign")
        return shared

    def _manifest(self, tmp_path, plugin_dirs, module_dirs, module_files) -> Path:
        lines = ["tier1:", "  safety_plugins:"]
        if plugin_dirs:
            lines += [f"    - {p}" for p in plugin_dirs]
        else:
            lines.append("    []")
        lines.append("  shared_modules:")
        if module_dirs or module_files:
            lines += [f"    - {m}" for m in [*module_dirs, *module_files]]
        else:
            lines.append("    []")
        path = tmp_path / "tiers.yaml"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_green_then_missing_registration_red(self, tmp_path, capsys):
        shared = self._init_repo_with_shared(tmp_path)
        manifest = self._manifest(tmp_path, ["approval"], ["_libs"], ["_llm_streaming.py"])
        argv = ["--repo", str(tmp_path), "--manifest", str(manifest), "--shared-root", str(shared)]
        assert mod.main(argv) == 0
        # 漏登记：新增共享目录只进 git、不进清单 → 红（可复现的缺登记场景）
        newmod = shared / "_fresh_module"
        newmod.mkdir()
        (newmod / "core.py").write_text("", encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "add module", "--no-gpg-sign")
        assert mod.main(argv) == 1
        assert "_fresh_module" in capsys.readouterr().out

    def test_dangling_entry_red(self, tmp_path, capsys):
        shared = self._init_repo_with_shared(tmp_path)
        manifest = self._manifest(tmp_path, ["approval", "ghost_plugin"], [], [])
        argv = ["--repo", str(tmp_path), "--manifest", str(manifest), "--shared-root", str(shared)]
        assert mod.main(argv) == 1
        assert "ghost_plugin" in capsys.readouterr().out


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

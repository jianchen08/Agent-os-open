# @feature: FP-0.2.PACK 装机基线漂移巡检（ADR 2026-11-07 刀 4） | @ci: python-coverage
"""check_plugin_baseline_drift.py 单测。

行为契约（断输入→输出，不钉实现）——验收判据 4 的三态断言：
- 一致 → 0；版本落后（种子更新）→ 1（warn 面，含「落后」定位行）；
- 基线损坏（缺文件/坏 JSON/schema≠1/条目缺 version）→ 2（error 面）；
- 漂移旁态：基线超前、种子缺条目、基线缺条目 → 1（ADR §2.3「基线 vs 包
  版本漂移」同面）；
- 只读：巡检后基线文件字节不变；
- 纯函数：点分版本方向判定（含尾零归一 1.0 vs 1.0.0 → 不等不误标落后）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location("check_plugin_baseline_drift", SCRIPTS / "check_plugin_baseline_drift.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _write_plugin(pack_root: Path, pid: str, version: str) -> None:
    d = pack_root / "plugins" / "shared" / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({"id": pid, "version": version}), encoding="utf-8")


def _make_pack(tmp_path: Path, versions: dict[str, str]) -> Path:
    pack = tmp_path / "resources"
    for pid, ver in versions.items():
        _write_plugin(pack, pid, ver)
    return pack


def _write_baseline(tmp_path: Path, versions: dict[str, str], schema: int = 1) -> Path:
    path = tmp_path / "plugin-baseline.json"
    plugins = {pid: {"tier": 2, "version": ver, "files": {}} for pid, ver in versions.items()}
    path.write_text(
        json.dumps({"schema": schema, "package_version": "1.2.3", "written_at": "2026-11-07T00:00:00Z", "plugins": plugins}),
        encoding="utf-8",
    )
    return path


class TestCompareBaseline:
    def test_equal_is_empty(self):
        assert mod.compare_baseline({"llm": "1.0.16"}, {"llm": "1.0.16"}) == []

    def test_behind_ahead_and_mismatch_directions(self):
        baseline = {"a": "1.0.14", "b": "2.0.0", "c": "1.0", "d": "beta"}
        seed = {"a": "1.0.16", "b": "1.9.0", "c": "1.0.0", "d": "1.0.0"}
        lines = mod.compare_baseline(baseline, seed)
        assert any(ln.startswith("落后 a") for ln in lines)
        assert any(ln.startswith("超前 b") for ln in lines)
        assert any(ln.startswith("不等 c") for ln in lines)  # 尾零归一：数值同不误标落后
        assert any(ln.startswith("不等 d") for ln in lines)  # 非点分整数无方向

    def test_missing_on_either_side(self):
        lines = mod.compare_baseline({"gone": "1.0.0", "kept": "1.0.0"}, {"fresh": "2.0.0", "kept": "1.0.0"})
        assert any(ln.startswith("种子缺条目 gone") for ln in lines)
        assert any(ln.startswith("基线缺条目 fresh") for ln in lines)


class TestMainThreeStates:
    def test_consistent_zero(self, tmp_path):
        pack = _make_pack(tmp_path, {"llm": "1.0.16"})
        baseline = _write_baseline(tmp_path, {"llm": "1.0.16"})
        assert mod.main(["--baseline", str(baseline), "--pack-root", str(pack)]) == 0

    def test_behind_warn_one(self, tmp_path, capsys):
        pack = _make_pack(tmp_path, {"llm": "1.0.16"})
        baseline = _write_baseline(tmp_path, {"llm": "1.0.14"})
        assert mod.main(["--baseline", str(baseline), "--pack-root", str(pack)]) == 1
        out = capsys.readouterr().out
        assert "落后 llm" in out
        assert "1.0.14" in out
        assert "1.0.16" in out

    def test_corrupt_baseline_error_two(self, tmp_path, capsys):
        pack = _make_pack(tmp_path, {"llm": "1.0.16"})
        baseline = tmp_path / "plugin-baseline.json"
        baseline.write_text("{not json", encoding="utf-8")
        assert mod.main(["--baseline", str(baseline), "--pack-root", str(pack)]) == 2
        assert "ERROR" in capsys.readouterr().out

    def test_readonly_baseline_bytes_unchanged(self, tmp_path):
        """只读不写：巡检后基线字节不变。"""
        pack = _make_pack(tmp_path, {"llm": "9.9.9"})
        baseline = _write_baseline(tmp_path, {"llm": "1.0.0"})
        before = baseline.read_bytes()
        assert mod.main(["--baseline", str(baseline), "--pack-root", str(pack)]) == 1
        assert baseline.read_bytes() == before


class TestDriftSideStates:
    def test_baseline_ahead_warns(self, tmp_path, capsys):
        pack = _make_pack(tmp_path, {"llm": "1.0.0"})
        baseline = _write_baseline(tmp_path, {"llm": "2.0.0"})
        assert mod.main(["--baseline", str(baseline), "--pack-root", str(pack)]) == 1
        assert "超前 llm" in capsys.readouterr().out

    def test_seed_missing_entry_warns(self, tmp_path, capsys):
        pack = _make_pack(tmp_path, {"llm": "1.0.0"})
        baseline = _write_baseline(tmp_path, {"llm": "1.0.0", "removed": "1.0.0"})
        assert mod.main(["--baseline", str(baseline), "--pack-root", str(pack)]) == 1
        assert "种子缺条目 removed" in capsys.readouterr().out

    def test_baseline_missing_entry_warns(self, tmp_path, capsys):
        pack = _make_pack(tmp_path, {"llm": "1.0.0", "newguy": "2.0.0"})
        baseline = _write_baseline(tmp_path, {"llm": "1.0.0"})
        assert mod.main(["--baseline", str(baseline), "--pack-root", str(pack)]) == 1
        assert "基线缺条目 newguy" in capsys.readouterr().out


class TestCorruptVariants:
    def _expect_error(self, tmp_path, baseline_text, capsys):
        pack = _make_pack(tmp_path, {"llm": "1.0.0"})
        baseline = tmp_path / "plugin-baseline.json"
        baseline.write_text(baseline_text, encoding="utf-8")
        assert mod.main(["--baseline", str(baseline), "--pack-root", str(pack)]) == 2
        assert "ERROR" in capsys.readouterr().out

    def test_schema_not_one(self, tmp_path, capsys):
        self._expect_error(tmp_path, json.dumps({"schema": 2, "plugins": {}}), capsys)

    def test_entry_missing_version(self, tmp_path, capsys):
        self._expect_error(tmp_path, json.dumps({"schema": 1, "plugins": {"llm": {"tier": 2}}}), capsys)

    def test_missing_baseline_file(self, tmp_path, capsys):
        assert mod.main(["--baseline", str(tmp_path / "absent.json"), "--pack-root", str(_make_pack(tmp_path, {}))]) == 2
        assert "不存在" in capsys.readouterr().out

    def test_missing_seed_tree(self, tmp_path, capsys):
        baseline = _write_baseline(tmp_path, {"llm": "1.0.0"})
        assert mod.main(["--baseline", str(baseline), "--pack-root", str(tmp_path / "nope")]) == 2
        assert "种子树不存在" in capsys.readouterr().out

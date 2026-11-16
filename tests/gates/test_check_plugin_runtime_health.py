# @feature: FP-0.2.PACK 打包/部署 CI 门 | @ci: python-test
"""check_plugin_runtime_health.py 单测（纯逻辑层 + 注入 runner 的汇总面）。

行为契约（断输入→输出，不起真 venv 不读真日志）：
- .pth 目标解析：路径行提取，import 行/注释/空行剔除；
- editable 越界：简单路径 pth 与 finder MAPPING 两种形态，指向允许根内
  =过、指向外=红（装机 venv SDK editable 指向开发仓第三犯的执法回归）；
- venv 解释器发现：Scripts/python.exe 优先，bin/python 兜底，双缺=None；
- boot 日志签名：命中报「行号: 片段」，干净日志=空；
- run_checks 汇总：导入金丝雀失败/editable 越界/日志签名 → failures，
  缺 venv 默认警告、--strict 升失败，runner 注入不触真解释器。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_plugin_runtime_health", SCRIPTS / "check_plugin_runtime_health.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


class TestParsePthTargets:
    def test_path_lines_extracted(self):
        text = "D:\\some\\src\n\nimport __editable___x_finder; install()\n# comment\nC:/other/src\n"
        assert mod.parse_pth_targets(text) == ["D:\\some\\src", "C:/other/src"]

    def test_empty_text_no_targets(self):
        assert mod.parse_pth_targets("") == []


class TestEditableViolations:
    def _make_site(self, tmp_path: Path, files: dict[str, str]) -> Path:
        site = tmp_path / "site-packages"
        site.mkdir(parents=True)
        for name, content in files.items():
            (site / name).write_text(content, encoding="utf-8")
        return site

    def test_outside_target_flagged(self, tmp_path: Path):
        site = self._make_site(
            tmp_path,
            {"__editable__.agentos_plugin_sdk-0.2.0.pth": "D:\\myproject\\container\\plugins\\sdk\\src\n"},
        )
        violations = mod.editable_violations(site, [tmp_path / "allowed"])
        assert len(violations) == 1
        assert "agentos_plugin_sdk" in violations[0]
        assert "myproject" in violations[0]

    def test_inside_target_passes(self, tmp_path: Path):
        inside = tmp_path / "resources" / "sdk"
        inside.mkdir(parents=True)
        site = self._make_site(
            tmp_path, {"__editable__.sdk-1.0.pth": f"{inside}\n"}
        )
        assert mod.editable_violations(site, [tmp_path / "resources"]) == []

    def test_finder_mapping_target_flagged(self, tmp_path: Path):
        inside = tmp_path / "resources" / "sdk"
        inside.mkdir(parents=True)
        mapping = {"sdk": str(tmp_path / "elsewhere"), "other": str(inside)}
        site = self._make_site(
            tmp_path,
            {"__editable___sdk_1_0_finder.py": f"MAPPING = {mapping!r}\n"},
        )
        violations = mod.editable_violations(site, [tmp_path / "resources"])
        assert len(violations) == 1
        assert "finder" in violations[0]
        assert "elsewhere" in violations[0]

    def test_regular_pth_ignored(self, tmp_path: Path):
        site = self._make_site(tmp_path, {"tools.pth": "somepkg\n"})
        assert mod.editable_violations(site, [tmp_path / "allowed"]) == []

    def test_missing_site_packages_empty(self, tmp_path: Path):
        assert mod.editable_violations(tmp_path / "nope", [tmp_path]) == []


class TestFindVenvPython:
    def test_scripts_python_preferred(self, tmp_path: Path):
        (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
        exe = tmp_path / ".venv" / "Scripts" / "python.exe"
        exe.write_bytes(b"")
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        bin_python = tmp_path / ".venv" / "bin" / "python"
        bin_python.write_bytes(b"")
        assert mod.find_venv_python(tmp_path) == exe

    def test_bin_python_fallback(self, tmp_path: Path):
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        bin_python = tmp_path / ".venv" / "bin" / "python"
        bin_python.write_bytes(b"")
        assert mod.find_venv_python(tmp_path) == bin_python

    def test_missing_returns_none(self, tmp_path: Path):
        assert mod.find_venv_python(tmp_path) is None


class TestScanBootLog:
    def test_hits_with_line_numbers(self):
        log = "ok line\nModuleNotFoundError: No module named 'agentos_plugin_sdk'\nanother ok\n"
        hits = mod.scan_boot_log(log)
        assert len(hits) == 1
        assert hits[0].startswith("行2:")
        assert "agentos_plugin_sdk" in hits[0]

    def test_all_default_signatures_hit(self):
        log = (
            "x has no attribute 'Router'\n"
            "插件 venv 解释器缺失（.venv 不存在）\n"
        )
        assert len(mod.scan_boot_log(log)) == 2

    def test_clean_log_empty(self):
        assert mod.scan_boot_log("INFO plugin loaded\nINFO boot done\n") == []


class TestRunChecks:
    def _plugins_root(self, tmp_path: Path, name: str = "llm", with_venv: bool = True) -> Path:
        root = tmp_path / "plugins"
        plugin = root / name
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text("{}", encoding="utf-8")
        if with_venv:
            (plugin / ".venv" / "Scripts").mkdir(parents=True)
            (plugin / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
        return root

    @staticmethod
    def _runner_ok(_python, _canaries, _timeout_s=120):
        return True, ""

    def test_sdk_import_failure_reported(self, tmp_path: Path):
        root = self._plugins_root(tmp_path)
        calls: list[list[str]] = []

        def runner(_python, canaries, _timeout_s=120):
            calls.append(list(canaries))
            return False, "ModuleNotFoundError: no module named 'agentos_plugin_sdk'"

        report = mod.run_checks(root, [], [], [root], {}, False, runner=runner)
        assert report["ok"] is False
        assert any("llm" in f and "agentos_plugin_sdk" in f for f in report["failures"])
        assert calls, "runner 未被调用"
        assert calls[0] == ["import agentos_plugin_sdk"]

    def test_plugin_extra_canary_injected(self, tmp_path: Path):
        root = self._plugins_root(tmp_path, name="llm")

        def runner(_python, canaries, _timeout_s=120):
            assert canaries == ["import agentos_plugin_sdk", "import litellm"]
            return True, ""

        report = mod.run_checks(
            root, [], [], [root], {"llm": "litellm"}, False, runner=runner
        )
        assert report["ok"] is True

    def test_editable_violation_in_plugin_venv(self, tmp_path: Path):
        root = self._plugins_root(tmp_path)
        site = root / "llm" / ".venv" / "Lib" / "site-packages"
        site.mkdir(parents=True)
        (site / "__editable__.sdk-1.0.pth").write_text(
            "D:\\myproject\\elsewhere\\src\n", encoding="utf-8"
        )

        def runner(python, canaries, timeout_s=120):
            return True, ""

        report = mod.run_checks(root, [], [], [root], {}, False, runner=runner)
        assert report["ok"] is False
        assert any("editable 越界" in f for f in report["failures"])

    def test_missing_venv_warning_by_default_strict_upgrades(self, tmp_path: Path):
        root = self._plugins_root(tmp_path, with_venv=False)
        report = mod.run_checks(root, [], [], [root], {}, False, runner=self._runner_ok)
        assert report["ok"] is True
        assert report["warnings"], "缺 venv 应产生警告"
        assert "缺 venv" in report["warnings"][0]
        strict = mod.run_checks(root, [], [], [root], {}, True, runner=self._runner_ok)
        assert strict["ok"] is False

    def test_boot_log_signature_fails(self, tmp_path: Path):
        log = tmp_path / "kernel.log"
        log.write_text("boot...\nNo module named 'agentos_plugin_sdk'\n", encoding="utf-8")
        report = mod.run_checks(None, [], [log], [], {}, False, runner=self._runner_ok)
        assert report["ok"] is False
        assert any("boot 日志失败签名" in f for f in report["failures"])

    def test_venv_mode_checks_given_dir(self, tmp_path: Path):
        venv = tmp_path / "host" / ".venv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "python.exe").write_bytes(b"")
        (venv / "Lib" / "site-packages").mkdir(parents=True)
        report = mod.run_checks(
            None, [venv.parent], [], [tmp_path], {}, False, runner=self._runner_ok
        )
        assert report["ok"] is True
        assert report["checked"]["venvs"] == 1

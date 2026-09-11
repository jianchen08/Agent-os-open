# @feature: FP-GATE CI 变更面分档 | @ci: python-coverage
"""CI 变更面分档与关联测试映射单测（2026-09-10 根目录运维脚本轻量车道）。

覆盖两个契约点的可单测面：
1. ci_changed_areas.compute_lanes：根目录运维脚本（*.bat/*.sh/*.ps1）变更
   → related 轻量车道点亮；kernel/scripts 子目录分档、普通 py 变更、
   docs_only 判定行为不变；
2. ci_related_tests：根目录运维脚本 → tests/test_startup_scripts_fix.py
   静态契约测试映射 + 根目录 .sh 的 bash -n 语法检查（通过/失败/dry-run）。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_scripts_module(name: str):
    """按裸名装载 scripts/ 下模块（同目录相互 import 需 ci_common 先在 sys.modules）。"""
    if "ci_common" not in sys.modules:
        spec = importlib.util.spec_from_file_location("ci_common", SCRIPTS / "ci_common.py")
        assert spec is not None
        assert spec.loader is not None
        common = importlib.util.module_from_spec(spec)
        sys.modules["ci_common"] = common
        spec.loader.exec_module(common)
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def changed_areas():
    yield _load_scripts_module("ci_changed_areas")
    sys.modules.pop("ci_changed_areas", None)


@pytest.fixture(scope="module")
def related_tests():
    yield _load_scripts_module("ci_related_tests")
    sys.modules.pop("ci_related_tests", None)
    sys.modules.pop("ci_common", None)


# ---------------------------------------------------------------------------
# compute_lanes：根目录运维脚本 → related 轻量车道
# ---------------------------------------------------------------------------
class TestComputeLanesRootOpsScripts:
    def test_root_bat_triggers_related_only(self, changed_areas):
        lanes = changed_areas.compute_lanes(["start_web_02.bat"])
        assert lanes["related"] is True
        assert lanes["full"] is False
        assert lanes["python_full"] is False
        assert lanes["docs_only"] is False, "根脚本不是文档，不得触发 docs_only 放行"

    @pytest.mark.parametrize("script", ["start_web_02.sh", "stop_web_02.sh", "wsl_health_probe.sh"])
    def test_root_sh_triggers_related(self, changed_areas, script):
        assert changed_areas.compute_lanes([script])["related"] is True

    @pytest.mark.parametrize("script", ["wsl_alive_probe.ps1", "run_kernel_supervised.bat"])
    def test_root_ps1_and_bat_triggers_related(self, changed_areas, script):
        assert changed_areas.compute_lanes([script])["related"] is True

    def test_subdir_scripts_keep_existing_lanes(self, changed_areas):
        # scripts/ 子目录脚本归 FULL（不被根脚本规则劫持）；
        # 插件目录 .sh 随其所属插件面走 related（既有前缀规则不变）。
        assert changed_areas.compute_lanes(["scripts/foo.sh"])["full"] is True
        assert changed_areas.compute_lanes(["scripts/foo.sh"])["related"] is False
        assert changed_areas.compute_lanes(["plugins/shared/tools/simple/a.sh"])["related"] is True

    def test_full_dominates_root_script(self, changed_areas):
        # 重车道互斥瀑布不变：kernel 变更 + 根脚本变更 → 只 full 不 related
        lanes = changed_areas.compute_lanes(["kernel/src/lib.rs", "start_web_02.bat"])
        assert lanes["full"] is True
        assert lanes["related"] is False

    def test_plain_python_change_unchanged(self, changed_areas):
        # 普通 py 变更行为不变：非核心插件 → related；pipeline → python_full
        lanes = changed_areas.compute_lanes(["plugins/shared/tools/memory/server.py"])
        assert lanes["related"] is True
        assert lanes["full"] is False
        assert lanes["python_full"] is False
        lanes_core = changed_areas.compute_lanes(["plugins/shared/pipeline/input/context_build/x.py"])
        assert lanes_core["python_full"] is True
        assert lanes_core["related"] is False

    def test_docker_compose_still_related(self, changed_areas):
        assert changed_areas.compute_lanes(["docker-compose.yml"])["related"] is True


# ---------------------------------------------------------------------------
# ci_related_tests：根运维脚本 → 静态契约测试映射 + bash -n
# ---------------------------------------------------------------------------
class TestRelatedTestsRootOpsMapping:
    def test_root_scripts_select_startup_contract_test(self, related_tests):
        selected, notes = related_tests.select_tests(["start_web_02.bat", "run_kernel_supervised.bat"])
        assert related_tests.TESTS_DIR / "test_startup_scripts_fix.py" in selected
        assert any("根目录运维脚本" in n for n in notes)

    def test_plain_test_change_unaffected(self, related_tests):
        selected, _ = related_tests.select_tests(["tests/test_port_config.py"])
        assert related_tests.ROOT / "tests/test_port_config.py" in selected
        assert related_tests.TESTS_DIR / "test_startup_scripts_fix.py" not in selected

    def test_bash_syntax_check_passes_good_script(self, related_tests, tmp_path, monkeypatch):
        if shutil.which("bash") is None:
            pytest.skip("bash 不可用")
        monkeypatch.setattr(related_tests, "ROOT", tmp_path)
        (tmp_path / "good.sh").write_text("echo ok\n", encoding="utf-8")
        assert related_tests._bash_syntax_check(["good.sh"]) == 0

    def test_bash_syntax_check_fails_loud_on_bad_syntax(self, related_tests, tmp_path, monkeypatch):
        if shutil.which("bash") is None:
            pytest.skip("bash 不可用")
        monkeypatch.setattr(related_tests, "ROOT", tmp_path)
        (tmp_path / "broken.sh").write_text("if [ -f x ]; then\n", encoding="utf-8")
        assert related_tests._bash_syntax_check(["broken.sh"]) == 1

    def test_bash_syntax_check_dry_run_prints_without_running(self, related_tests, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(related_tests, "ROOT", tmp_path)
        # 语法坏的脚本在 dry-run 下也不执行 bash（返回 0，只打印将执行的检查）
        (tmp_path / "broken.sh").write_text("if [ -f x ]; then\n", encoding="utf-8")
        assert related_tests._bash_syntax_check(["broken.sh"], dry_run=True) == 0
        assert "dry-run: bash -n broken.sh" in capsys.readouterr().out

    def test_bash_syntax_check_ignores_non_root_and_non_sh(self, related_tests, tmp_path, monkeypatch):
        if shutil.which("bash") is None:
            pytest.skip("bash 不可用")
        monkeypatch.setattr(related_tests, "ROOT", tmp_path)
        # 子目录 .sh（归 FULL 车道）与非 .sh 文件不进本检查
        assert related_tests._bash_syntax_check(["scripts/x.sh", "start_web_02.bat"]) == 0


class TestRelatedTestsMainWiring:
    def test_dry_run_end_to_end_root_script_change(self, related_tests, tmp_path):
        """--from-file + --dry-run 全链路：根脚本变更 → 映射到静态契约测试。

        走真实 main()（子进程），验证 heavy 排除 → bash 检查 → 选择 → argv 打印
        的接线顺序，不实际执行 pytest。
        """
        changed = tmp_path / "changed.txt"
        changed.write_text("start_web_02.bat\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "ci_related_tests.py"), "--from-file", str(changed), "--dry-run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(related_tests.ROOT),
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "test_startup_scripts_fix.py" in proc.stdout
        assert "bash -n" not in proc.stdout, "无 .sh 变更时不得出现 bash 检查"

    def test_full_lane_change_bypasses_related(self, related_tests, tmp_path):
        changed = tmp_path / "changed.txt"
        changed.write_text("kernel/src/lib.rs\nstart_web_02.bat\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "ci_related_tests.py"), "--from-file", str(changed), "--dry-run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(related_tests.ROOT),
            check=False,
        )
        assert proc.returncode == 0
        assert "本车道不适用" in proc.stdout
        assert "test_startup_scripts_fix.py" not in proc.stdout

# @feature: FP-0.2.三 宿主接入(打包布局) | @ci: python-coverage
"""check_packaged_layout 行为测试：临时目录 fixture 造打包资源根，断言 c 结构判据。

覆盖：合格结构零 error / 模式包带 .venv 报错 / 合宿成员带 .venv 报错 /
共享 venv 缺解释器报错 / independent 类 .venv 仅 info / junction 报错（win）/
--json 输出形状 / 缺失根退出码 1。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_packaged_layout", REPO / "scripts" / "check_packaged_layout.py"
)
assert _SPEC is not None and _SPEC.loader is not None  # 同仓固定路径，装配必成功
C = importlib.util.module_from_spec(_SPEC)
sys.modules["check_packaged_layout"] = C
_SPEC.loader.exec_module(C)


def make_resources(root: Path) -> Path:
    """最小合格 c 结构：_host 基座 + 共享 venv 解释器 + 瘦身模式包 + 独立插件。"""
    host = root / "plugins" / "shared" / "_host"
    (host / ".venv" / "Scripts").mkdir(parents=True)
    (host / "host.py").write_text("# host base\n", encoding="utf-8")
    (host / "pyproject.toml").write_text("[project]\nname='h'\n", encoding="utf-8")
    (host / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")

    mode = root / "plugins" / "shared" / "modes" / "mode_x"
    mode.mkdir(parents=True)
    (mode / "plugin.json").write_text(
        json.dumps({"id": "mode_x", "host_group": "light"}), encoding="utf-8"
    )

    indep = root / "plugins" / "shared" / "system" / "heavy_plugin"
    indep.mkdir(parents=True)
    (indep / "plugin.json").write_text(json.dumps({"id": "heavy_plugin"}), encoding="utf-8")
    return root


def error_codes(findings: list[C.Finding]) -> set[str]:
    return {f.code for f in findings if f.level == "error"}


def test_valid_layout_passes(tmp_path):
    root = make_resources(tmp_path / "res")
    findings = C.check_resources(root)
    assert not error_codes(findings), findings
    assert (tmp_path / "res").exists()


def test_mode_pack_with_own_venv_fails(tmp_path):
    root = make_resources(tmp_path / "res")
    mode = root / "plugins" / "shared" / "modes" / "mode_x" / ".venv"
    mode.mkdir()
    (mode / "marker.txt").write_text("x", encoding="utf-8")
    codes = error_codes(C.check_resources(root))
    assert "SLIM_MEMBER_VENV_PRESENT" in codes


def test_light_member_with_own_venv_fails(tmp_path):
    root = make_resources(tmp_path / "res")
    member = root / "plugins" / "shared" / "system" / "cohort_member"
    member.mkdir()
    (member / "plugin.json").write_text(
        json.dumps({"id": "cohort_member", "host_group": "light_stable"}), encoding="utf-8"
    )
    (member / ".venv").mkdir()
    codes = error_codes(C.check_resources(root))
    assert "SLIM_MEMBER_VENV_PRESENT" in codes


def test_host_venv_missing_fails(tmp_path):
    root = make_resources(tmp_path / "res")
    import shutil

    shutil.rmtree(root / "plugins" / "shared" / "_host" / ".venv")
    codes = error_codes(C.check_resources(root))
    assert "HOST_VENV_MISSING" in codes


def test_host_base_file_missing_fails(tmp_path):
    root = make_resources(tmp_path / "res")
    (root / "plugins" / "shared" / "_host" / "host.py").unlink()
    codes = error_codes(C.check_resources(root))
    assert "HOST_BASE_MISSING" in codes


def test_unix_layout_interpreter_accepted(tmp_path):
    root = make_resources(tmp_path / "res")
    import shutil

    venv = root / "plugins" / "shared" / "_host" / ".venv"
    shutil.rmtree(venv)
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("", encoding="utf-8")
    assert not error_codes(C.check_resources(root))


def test_independent_venv_is_info_not_error(tmp_path):
    root = make_resources(tmp_path / "res")
    indep_venv = root / "plugins" / "shared" / "system" / "heavy_plugin" / ".venv"
    indep_venv.mkdir()
    (indep_venv / "Scripts").mkdir()
    (indep_venv / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    findings = C.check_resources(root)
    assert not error_codes(findings)
    assert any(f.code == "INDEPENDENT_VENV_PRESENT" for f in findings)


@pytest.mark.skipif(sys.platform != "win32", reason="junction 为 Windows 机制")
def test_junction_venv_in_package_fails(tmp_path):
    import _winapi

    root = make_resources(tmp_path / "res")
    host_venv = root / "plugins" / "shared" / "_host" / ".venv"
    indep = root / "plugins" / "shared" / "system" / "heavy_plugin"
    _winapi.CreateJunction(str(host_venv), str(indep / ".venv"))
    codes = error_codes(C.check_resources(root))
    assert "JUNCTION_IN_PACKAGE" in codes


def test_missing_shared_dir_fails(tmp_path):
    codes = error_codes(C.check_resources(tmp_path / "empty"))
    assert "SHARED_DIR_MISSING" in codes


def test_main_cli_exit_codes_and_json(tmp_path, capsys, monkeypatch):
    root = make_resources(tmp_path / "res")

    # 合格 → 退出 0
    monkeypatch.setattr(sys, "argv", ["check_packaged_layout.py", str(root), "--json"])
    assert C.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["root"] == str(root.resolve())
    assert isinstance(payload["findings"], list)

    # 注入违规 → 退出 1
    (root / "plugins" / "shared" / "modes" / "mode_x" / ".venv").mkdir()
    monkeypatch.setattr(sys, "argv", ["check_packaged_layout.py", str(root)])
    assert C.main() == 1
    assert "SLIM_MEMBER_VENV_PRESENT" in capsys.readouterr().out

    # 资源根不存在 → 退出 1
    monkeypatch.setattr(sys, "argv", ["check_packaged_layout.py", str(tmp_path / "nope")])
    assert C.main() == 1

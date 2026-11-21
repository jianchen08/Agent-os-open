# @feature: FP-0.2.三 宿主接入(打包布局) | @ci: python-coverage
"""check_packaged_layout 行为测试：临时目录 fixture 造打包资源根，断言 c 结构判据。

覆盖：合格结构零 error / 模式包带 .venv 报错 / 合宿成员带 .venv 报错 /
共享 venv 缺解释器报错 / independent 类 .venv 仅 info / junction 报错（win）/
--json 输出形状 / 缺失根退出码 1。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_packaged_layout", REPO / "scripts" / "check_packaged_layout.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None  # 同仓固定路径，装配必成功
C = importlib.util.module_from_spec(_SPEC)
sys.modules["check_packaged_layout"] = C
_SPEC.loader.exec_module(C)


def make_resources(root: Path) -> Path:
    """最小合格 c 结构：_host 基座 + 共享 venv 解释器 + 瘦身模式包 + 独立插件。

    home 用包内绝对路径（打包位=warn 不阻断、装后位=合法；相对 home 属
    VENV_HOME_RELATIVE_UNSUPPORTED 红判据）。
    """
    prereqs = root / "prereqs" / "python"
    prereqs.mkdir(parents=True)
    host = root / "plugins" / "shared" / "_host"
    (host / ".venv" / "Scripts").mkdir(parents=True)
    (host / "host.py").write_text("# host base\n", encoding="utf-8")
    (host / "pyproject.toml").write_text("[project]\nname='h'\n", encoding="utf-8")
    (host / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    (host / ".venv" / "pyvenv.cfg").write_text(f"home = {prereqs}\n", encoding="utf-8")

    mode = root / "plugins" / "shared" / "mode_x"
    mode.mkdir(parents=True)
    (mode / "plugin.json").write_text(
        json.dumps({"id": "mode_x", "host_group": "light"}), encoding="utf-8"
    )

    indep = root / "plugins" / "shared" / "heavy_plugin"
    indep.mkdir(parents=True)
    (indep / "plugin.json").write_text(json.dumps({"id": "heavy_plugin"}), encoding="utf-8")
    return root


def error_codes(findings: list[Any]) -> set[str]:
    # 注：C 为动态装配的脚本模块，mypy 看不到 C.Finding 形状，标注退 Any
    return {f.code for f in findings if f.level == "error"}


def warn_codes(findings: list[Any]) -> set[str]:
    return {f.code for f in findings if f.level == "warn"}


def test_valid_layout_passes(tmp_path):
    root = make_resources(tmp_path / "res")
    findings = C.check_resources(root)
    assert not error_codes(findings), findings
    assert (tmp_path / "res").exists()


def test_mode_pack_with_own_venv_fails(tmp_path):
    root = make_resources(tmp_path / "res")
    mode = root / "plugins" / "shared" / "mode_x" / ".venv"
    mode.mkdir()
    (mode / "marker.txt").write_text("x", encoding="utf-8")
    codes = error_codes(C.check_resources(root))
    assert "SLIM_MEMBER_VENV_PRESENT" in codes


def test_light_member_with_own_venv_fails(tmp_path):
    root = make_resources(tmp_path / "res")
    member = root / "plugins" / "shared" / "cohort_member"
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
    (venv / "pyvenv.cfg").write_text(f"home = {root / 'prereqs' / 'python'}\n", encoding="utf-8")
    assert not error_codes(C.check_resources(root))


def test_independent_venv_is_info_not_error(tmp_path):
    root = make_resources(tmp_path / "res")
    indep_venv = root / "plugins" / "shared" / "heavy_plugin" / ".venv"
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
    indep = root / "plugins" / "shared" / "heavy_plugin"
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
    (root / "plugins" / "shared" / "mode_x" / ".venv").mkdir()
    monkeypatch.setattr(sys, "argv", ["check_packaged_layout.py", str(root)])
    assert C.main() == 1
    assert "SLIM_MEMBER_VENV_PRESENT" in capsys.readouterr().out

    # 资源根不存在 → 退出 1
    monkeypatch.setattr(sys, "argv", ["check_packaged_layout.py", str(tmp_path / "nope")])
    assert C.main() == 1


def test_main_cli_installed_flag_lenient_to_in_package_absolute(tmp_path, capsys, monkeypatch):
    """--installed（装后位）：包内绝对路径行合法；同文件默认（打包位）退出 1。"""
    root = make_resources(tmp_path / "res")
    sp = root / "plugins" / "shared" / "_host" / ".venv" / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    (root / "plugins" / "sdk" / "src").mkdir(parents=True)
    (sp / "__editable__.agentos_plugin_sdk-0.2.2.pth").write_text(
        str(root / "plugins" / "sdk" / "src"), encoding="utf-8"
    )

    monkeypatch.setattr(sys, "argv", ["check_packaged_layout.py", str(root)])
    assert C.main() == 1
    assert "EDITABLE_ABSOLUTE_IN_PACKAGE" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["check_packaged_layout.py", str(root), "--installed"])
    assert C.main() == 0


# ---------- 判据 6：成员依赖 ⊆ 包内共享 venv ----------


def _add_member_with_deps(root: Path, deps: list[str]) -> Path:
    member = root / "plugins" / "shared" / "web_like"
    member.mkdir(parents=True)
    (member / "plugin.json").write_text(
        json.dumps({"id": "web_like", "host_group": "light"}), encoding="utf-8"
    )
    dep_lines = "\n".join(f'    "{d}",' for d in deps)
    (member / "pyproject.toml").write_text(
        f"[project]\nname='web-like'\ndependencies=[\n{dep_lines}\n]\n",
        encoding="utf-8",
    )
    return member


def _add_dist(root: Path, name: str, version: str) -> None:
    sp = root / "plugins" / "shared" / "_host" / ".venv" / "Lib" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / f"{name}-{version}.dist-info").mkdir()


def test_member_dep_missing_from_host_venv_fails(tmp_path):
    """成员声明 httpx 而包内 venv 未实装 → HOST_VENV_DEP_MISSING（web_operate 实证形态）。"""
    root = make_resources(tmp_path / "res")
    _add_member_with_deps(root, ["httpx", "trafilatura>=1.8"])
    _add_dist(root, "pip", "24.0")  # site-packages 层在位，仅缺成员依赖
    codes = error_codes(C.check_resources(root))
    assert "HOST_VENV_DEP_MISSING" in codes


def test_member_dep_installed_passes(tmp_path):
    root = make_resources(tmp_path / "res")
    _add_member_with_deps(root, ["httpx"])
    _add_dist(root, "httpx", "0.28.1")
    assert not error_codes(C.check_resources(root))


def test_sdk_editable_dep_exempt(tmp_path):
    """SDK 是 editable 本地源（无 dist-info），不得误报。"""
    root = make_resources(tmp_path / "res")
    _add_member_with_deps(root, ["agentos-plugin-sdk>=0.2.0"])
    _add_dist(root, "pip", "24.0")
    assert not error_codes(C.check_resources(root))


def test_dep_name_normalized(tmp_path):
    """归一比对：dist-info 用 PEP503 异形名（Pillow/Flask_Style）也须命中。"""
    root = make_resources(tmp_path / "res")
    _add_member_with_deps(root, ["Pillow"])
    _add_dist(root, "pip", "24.0")
    _add_dist(root, "pillow", "11.0.0")
    assert not error_codes(C.check_resources(root))


def test_independent_plugin_deps_not_checked(tmp_path):
    """independent 插件自带 venv，其依赖不进共享闭包比对。"""
    root = make_resources(tmp_path / "res")
    indep = root / "plugins" / "shared" / "heavy_plugin"
    (indep / "pyproject.toml").write_text(
        "[project]\nname='heavy'\ndependencies=['torch']\n", encoding="utf-8"
    )
    assert not error_codes(C.check_resources(root))


def test_site_packages_missing_fails(tmp_path):
    """解释器在位但 site-packages 整层缺失 = 破损 venv，独立报错。"""
    root = make_resources(tmp_path / "res")
    _add_member_with_deps(root, ["httpx"])
    codes = error_codes(C.check_resources(root))
    assert "HOST_VENV_SITE_PKG_MISSING" in codes


# ── venv 身份 / editable 指向（2026-11-10 装机事故回归）────────────────


def test_host_venv_cfg_missing_fails(tmp_path):
    """解释器在位但缺 pyvenv.cfg = 失灵 venv：启动器退回外部 Python，
    venv 内全部包失效（装机实证：aiohttp 命中外部命名空间包 → sync_agent
    上行永久失败）。必须在出包前拦下。"""
    root = make_resources(tmp_path / "res")
    (root / "plugins" / "shared" / "_host" / ".venv" / "pyvenv.cfg").unlink()
    codes = error_codes(C.check_resources(root))
    assert "HOST_VENV_CFG_MISSING" in codes


def _host_site_packages(root: Path) -> Path:
    sp = root / "plugins" / "shared" / "_host" / ".venv" / "Lib" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    return sp


def _sdk_target(root: Path) -> Path:
    target = root / "plugins" / "sdk" / "src"
    target.mkdir(parents=True, exist_ok=True)
    return target


def test_editable_absolute_in_package_fails_at_pack(tmp_path):
    """打包位绝对路径（第四犯形态）：打包机上「原地对」但产物挪位即死链，出包前拦下。"""
    root = make_resources(tmp_path / "res")
    sp = _host_site_packages(root)
    (sp / "__editable__.agentos_plugin_sdk-0.2.2.pth").write_text(str(_sdk_target(root)), encoding="utf-8")
    assert "EDITABLE_ABSOLUTE_IN_PACKAGE" in error_codes(C.check_resources(root))


def test_editable_absolute_in_package_passes_when_installed(tmp_path):
    """装后位（--installed）：树不再挪位，包内绝对路径合法（只拦越界）。"""
    root = make_resources(tmp_path / "res")
    sp = _host_site_packages(root)
    (sp / "__editable__.agentos_plugin_sdk-0.2.2.pth").write_text(str(_sdk_target(root)), encoding="utf-8")
    assert not error_codes(C.check_resources(root, will_relocate=False))


def test_editable_relative_line_passes_at_pack(tmp_path):
    """相对路径行（relocate 后形态）：site.addpackage 按 .pth 所在目录解析，随树挪位成立。"""
    root = make_resources(tmp_path / "res")
    sp = _host_site_packages(root)
    relative = os.path.relpath(_sdk_target(root), sp)
    (sp / "__editable__.agentos_plugin_sdk-0.2.2.pth").write_text(relative, encoding="utf-8")
    assert not error_codes(C.check_resources(root))


def test_editable_relative_line_escaping_root_fails(tmp_path):
    """相对路径行解析越出资源根（../.. 逃逸）= 越界，拦下。"""
    root = make_resources(tmp_path / "res")
    sp = _host_site_packages(root)
    escapee = os.path.relpath(tmp_path / "outside" / "sdk" / "src", sp)
    (sp / "__editable__.esc.pth").write_text(escapee, encoding="utf-8")
    assert "EDITABLE_TARGET_OUT_OF_ROOT" in error_codes(C.check_resources(root))


def test_editable_relative_line_missing_target_fails(tmp_path):
    """相对路径行解析目标不存在：运行期被 site 静默跳过 → import 必失败，拦下。"""
    root = make_resources(tmp_path / "res")
    sp = _host_site_packages(root)
    ghost = os.path.relpath(root / "plugins" / "sdk" / "absent", sp)
    (sp / "__editable__.ghost.pth").write_text(ghost, encoding="utf-8")
    assert "EDITABLE_TARGET_MISSING" in error_codes(C.check_resources(root))


# ---------- 判据 2e：venv 基座指针（pyvenv.cfg home）形态 ----------


def _host_cfg(root: Path) -> Path:
    return root / "plugins" / "shared" / "_host" / ".venv" / "pyvenv.cfg"


def test_venv_home_out_of_package_fails(tmp_path):
    """home 指向开发机基座（用户机 uv python 形态）：净机无此路径即死，两种模式都拦。"""
    root = make_resources(tmp_path / "res")
    _host_cfg(root).write_text(
        "home = C:\\Users\\dev\\AppData\\Roaming\\uv\\python\\cpython-3.12\n", encoding="utf-8"
    )
    assert "VENV_HOME_OUT_OF_PACKAGE" in error_codes(C.check_resources(root))
    assert "VENV_HOME_OUT_OF_PACKAGE" in error_codes(C.check_resources(root, will_relocate=False))


def test_venv_home_absolute_in_package_warns_at_pack(tmp_path):
    """打包位绝对 home（产物挪位即死链）：打包位 warn（随包 venv 违规形态，
    由 strip 出包剥离——非阻断但可见），装后位合法（autoprovision 原地生成形态）。"""
    root = make_resources(tmp_path / "res")
    findings = C.check_resources(root)
    assert "VENV_HOME_ABSOLUTE_IN_PACKAGE" in warn_codes(findings)
    assert "VENV_HOME_ABSOLUTE_IN_PACKAGE" not in error_codes(findings)
    assert not error_codes(C.check_resources(root, will_relocate=False))
    assert "VENV_HOME_ABSOLUTE_IN_PACKAGE" not in warn_codes(
        C.check_resources(root, will_relocate=False)
    )


def test_venv_home_relative_unsupported_fails(tmp_path):
    """相对 home=CWD 依赖（异 cwd 启动即 No Python at，实测）：两种模式都拦。"""
    root = make_resources(tmp_path / "res")
    venv = root / "plugins" / "shared" / "_host" / ".venv"
    _host_cfg(root).write_text(
        f"home = {os.path.relpath(root / 'prereqs' / 'python', venv)}\n", encoding="utf-8"
    )
    assert "VENV_HOME_RELATIVE_UNSUPPORTED" in error_codes(C.check_resources(root))
    assert "VENV_HOME_RELATIVE_UNSUPPORTED" in error_codes(
        C.check_resources(root, will_relocate=False)
    )


def test_independent_venv_home_checked(tmp_path):
    """独立插件 venv（在场=info 合法形态）的基座指针同口径校验。"""
    root = make_resources(tmp_path / "res")
    venv = root / "plugins" / "shared" / "heavy_plugin" / ".venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        "home = C:\\Users\\dev\\uv\\python\\cpython-3.12\n", encoding="utf-8"
    )
    assert "VENV_HOME_OUT_OF_PACKAGE" in error_codes(C.check_resources(root))


def test_editable_target_outside_root_fails(tmp_path):
    """editable 指向打包资源根之外（开发机路径）＝裸机必炸，出包前拦下。"""
    root = make_resources(tmp_path / "res")
    sp = root / "plugins" / "shared" / "_host" / ".venv" / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    dev_path = tmp_path / "devrepo" / "plugins" / "sdk" / "src"
    dev_path.mkdir(parents=True)
    (sp / "__editable__.agentos_plugin_sdk-0.2.2.pth").write_text(str(dev_path), encoding="utf-8")
    assert "EDITABLE_TARGET_OUT_OF_ROOT" in error_codes(C.check_resources(root))


def test_prereqs_python_editable_outside_root_fails(tmp_path):
    """随包解释器（prereqs/python）的 editable 同样须落在资源根内。"""
    root = make_resources(tmp_path / "res")
    sp = root / "prereqs" / "python" / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    dev_path = tmp_path / "devrepo" / "plugins" / "sdk" / "src"
    dev_path.mkdir(parents=True)
    (sp / "__editable__.agentos_plugin_sdk-0.2.0.pth").write_text(str(dev_path), encoding="utf-8")
    assert "EDITABLE_TARGET_OUT_OF_ROOT" in error_codes(C.check_resources(root))


def test_editable_import_line_ignored(tmp_path):
    """`.pth` 里的 import 行（非路径行）不参与越界判定。"""
    root = make_resources(tmp_path / "res")
    sp = root / "plugins" / "shared" / "_host" / ".venv" / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    (sp / "__editable__x.pth").write_text("import sys; sys.path.append('/nope')\n", encoding="utf-8")
    assert "EDITABLE_TARGET_OUT_OF_ROOT" not in error_codes(C.check_resources(root))

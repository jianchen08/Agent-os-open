# @feature: FP-0.2.CFG 安装器用户数据/凭据保留闸（ADR 2026-11-08） | @ci: python-coverage
"""check_installer_userdata_guard.py 单测。

行为契约（断输入→输出，不钉实现）：
- 删除口令播种（DeleteRegValue/DeleteRegKey）=红；注释里的历史叙事不执法。
- RMDir /r $APPDATA =红；普通 RMDir /r $INSTDIR 清场（包内载荷）不红。
- 正面断言：ReadRegStr 保留守卫与空值守卫任一缺失=红（播种退化=轮换回潮）。
- deleteAppDataOnUninstall=true =红；缺省/false 不红。
- 真仓绿：现安装器与本就合规（保留契约常驻证明；回潮此测即红）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_installer_userdata_guard", SCRIPTS / "check_installer_userdata_guard.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def test_delete_seeded_password_is_red() -> None:
    text = '  DeleteRegValue HKCU "Environment" "AGENTOS_ADMIN_PASSWORD"\n'
    assert any("DeleteRegValue" in v or "删除口令播种" in v for v in mod.check_installer_nsh(text))


def test_delete_seeded_password_in_comment_is_exempt() -> None:
    """注释里的历史叙事（旧实现考古）不得触发执法——注释不落运行行为。"""
    text = '; 旧实现: DeleteRegValue HKCU "Environment" "AGENTOS_ADMIN_PASSWORD"\n'
    violations = [v for v in mod.check_installer_nsh(text) if "删除口令播种" in v]
    assert violations == []


def test_rmdir_appdata_is_red_but_instdir_is_fine() -> None:
    assert mod.check_installer_nsh('  RMDir /r "$APPDATA\\agentos"\n')
    # 包内载荷清场（$INSTDIR）合法：有正面守卫锚在位即只剩 RMDir 违规为空
    ok = (
        mod.PRESERVE_READ_ANCHOR + "\n"
        + mod.PRESERVE_GUARD_ANCHOR + "\n"
        + '  RMDir /r "$INSTDIR\\resources\\bin"\n'
    )
    assert mod.check_installer_nsh(ok) == []


def test_missing_preserve_guard_is_red() -> None:
    """播种守卫被整段删掉=保留契约失守（轮换回潮面）。"""
    assert mod.check_installer_nsh("  DetailPrint \"nothing\"\n")


def test_package_json_delete_app_data_is_red() -> None:
    assert mod.check_package_json({"build": {"nsis": {"deleteAppDataOnUninstall": True}}})
    assert mod.check_package_json({"build": {"nsis": {"deleteAppDataOnUninstall": False}}}) == []
    assert mod.check_package_json({"build": {"nsis": {}}}) == []


def test_package_json_per_machine_is_red() -> None:
    """perMachine=true = 管理员安装（用户裁定 2026-11-12 禁止）。"""
    assert mod.check_package_json({"build": {"nsis": {"perMachine": True}}})
    assert mod.check_package_json({"build": {"nsis": {"perMachine": False}}}) == []
    assert mod.check_package_json({"build": {"nsis": {}}}) == []


def test_real_repo_is_green() -> None:
    """真仓常驻证明：现安装器合规（保留契约在场、删除面缺席）。"""
    nsh = (REPO / "electron" / "nsis" / "installer-custom.nsh").read_text(encoding="utf-8")
    pkg = json.loads((REPO / "package.json").read_text(encoding="utf-8"))
    assert mod.check_installer_nsh(nsh) == []
    assert mod.check_package_json(pkg) == []

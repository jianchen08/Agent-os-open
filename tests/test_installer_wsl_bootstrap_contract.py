# @feature: FP-0.2.PACK 安装器 WSL 环境准备 | @ci: python-coverage
"""安装器 WSL 环境准备契约（NSIS 页 + 随包脚本 + package extraResources）。

锁点：
- 交互安装有 WSL 环境准备页（customPageAfterChangeDir / nsDialogs）；
- 勾选后调用随包 ``prereqs/installer_env_bootstrap.py --install-wsl``；
- 脚本随 electron-builder extraResources 分发（源 scripts/ → 目标 prereqs/）；
- 静默安装默认不启用（需 AGENTOS_INSTALL_WSL=1）；
- 安装器仍保持 per-user 零提权（perMachine=false / allowElevation=false）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_NSH = _ROOT / "electron" / "nsis" / "installer-custom.nsh"
_PACKAGE = _ROOT / "package.json"
_HELPER = _ROOT / "scripts" / "installer_env_bootstrap.py"


def test_nsh_has_wsl_page_and_bootstrap_call() -> None:
    text = _NSH.read_text(encoding="utf-8")
    assert "nsDialogs.nsh" in text
    assert "customPageAfterChangeDir" in text
    assert "AgentOsWslCheckbox" in text
    assert "installer_env_bootstrap.py" in text
    assert "--install-wsl" in text
    assert "AGENTOS_INSTALL_WSL" in text
    assert "3010" in text  # 待重启码透传到安装器提示


def test_package_ships_helper_in_prereqs() -> None:
    package = json.loads(_PACKAGE.read_text(encoding="utf-8"))
    resources = package["build"]["win"]["extraResources"]
    assert any(
        entry.get("from") == "scripts/installer_env_bootstrap.py"
        and entry.get("to") == "prereqs/installer_env_bootstrap.py"
        for entry in resources
    )


def test_installer_remains_per_user_no_admin() -> None:
    package = json.loads(_PACKAGE.read_text(encoding="utf-8"))
    nsis = package["build"]["nsis"]
    assert nsis.get("perMachine") is False
    assert nsis.get("allowElevation") is False


def test_helper_pins_wsl_min_version() -> None:
    text = _HELPER.read_text(encoding="utf-8")
    assert "MIN_WSL_VERSION" in text
    assert "3, 0, 5" in text
    assert "--install --no-distribution" in text or '"--install", "--no-distribution"' in text
    assert "--update" in text

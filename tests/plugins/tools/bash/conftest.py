# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""bash 工具进度推送测试 conftest——注入插件目录到 sys.path。

插件位于 plugins/shared/tools/bash/，内部用平铺 import
（from tool import BashTool / from process_manager import ...）。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "tools" / "bash"
)

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR)]

for _d in _PLUGIN_SOURCE_DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)


@pytest.fixture(scope="module", autouse=True)
def _prewarm_wsl_vm():
    """WSL VM 预热：本机 wsl.exe 在 PATH 时 ProcessManager 优先 `wsl -e bash -c`，
    VM 冷启动实测 9~15s（闲置自动终止 + 大套件内存重压），会吃光用例的
    execute/continue 等待窗口——本目录用例与 suites/plugins/test_bash_sidecar_state
    同族（2026-09-27 插桩车道实锤：该目录无预热，continue(timeout=15) 在冷 VM 下
    拿不到 exit_code）。模块首个用例前把 VM 拉热，等待窗口只度量命令本身；
    预热超时按异常传播（VM 起不来时后续用例必然全红，不如在此给出清晰错误）。"""
    if not shutil.which("wsl"):
        return
    subprocess.run(
        ["wsl", "-e", "bash", "-c", ":"],
        timeout=120,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,  # 预热只关心是否拉得动；失败细节由后续用例暴露
    )

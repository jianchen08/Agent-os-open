# @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: python-coverage
"""mode_roleplay sidecar 真实入口形态导入冒烟测试。

sidecar spawn（host_acquire）不注入 PYTHONPATH，插件模块加载期对 _libs 共享根
的可达性由 server.py 顶部 bootstrap_plugin(__file__) 时序保证（bootstrap 必须先于
rp_* 兄弟层导入——兄弟层模块加载期即需 _libs）。本测试以 sidecar 真实入口形态
（cwd=插件目录、干净子进程、无 PYTHONPATH 预注入）锁定该导入序契约；进程内
sys.path 注入形态（conftest/_PLUGIN_SOURCE_DIRS 机制）比真实环境多出共享根，
测不出此契约——这是拆分回归逃逸的直接原因，故必须走子进程真实依赖。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "shared" / "mode_roleplay"


def _run_in_sidecar_shape(code: str) -> subprocess.CompletedProcess[bytes]:
    """以 sidecar 入口形态跑子进程：cwd=插件目录、剥离 PYTHONPATH。

    SDK 可达前提与 CI python-coverage job 同源（uv pip install -e
    ./plugins/sdk 的 editable 安装使 agentos_plugin_sdk 可达，等价 sidecar
    venv 的安装形态）；spawn 不注入 PYTHONPATH（host_acquire 契约）。
    bootstrap 注入共享根的时序契约由被测代码自持，正是本测试锁定的对象。
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        cwd=str(_PLUGIN_DIR),
        capture_output=True,
        timeout=120,
        env=env,
    )


def test_sidecar_shape_import_server_succeeds() -> None:
    """sidecar 真实入口形态（cwd=插件目录、无 PYTHONPATH）下 import server 必须成功。

    锁定 bootstrap_plugin(__file__) 先于 rp_* 兄弟层导入的时序契约：兄弟层
    （rp_cards/rp_common/rp_content/rp_game）模块加载期即需 _libs 共享根，
    仅 bootstrap 注入可达；导入序倒置在此红灯。
    """
    result = _run_in_sidecar_shape("import server")
    assert result.returncode == 0, (
        "import server 在 sidecar 形态下失败"
        "（bootstrap_plugin(__file__) 必须先于 rp_* 兄弟层导入）：\n"
        f"{result.stderr.decode(errors='replace')}"
    )


def test_sidecar_shape_libs_unreachable_without_bootstrap() -> None:
    """共享根 _libs 在未 bootstrap 时不可达（干净 sidecar 环境）。

    反面锁定：若 _libs 未 bootstrap 即可达，说明共享根泄漏进了默认 sys.path，
    bootstrap 注入契约失效（env 契约面变化须显式评估而非静默依赖）。
    """
    result = _run_in_sidecar_shape("import _libs")
    assert result.returncode != 0, (
        "_libs 未 bootstrap 即可达——共享根泄漏进默认 sys.path，"
        "bootstrap 注入契约失效"
    )


def test_sidecar_shape_bootstrap_then_libs_succeeds() -> None:
    """bootstrap 注入后 _libs 可达：注入契约本身在工作（两面夹住时序契约）。"""
    entry = str(_PLUGIN_DIR / "server.py")
    result = _run_in_sidecar_shape(
        "from agentos_plugin_sdk.bootstrap import bootstrap_plugin; "
        f"bootstrap_plugin(r'{entry}'); import _libs"
    )
    assert result.returncode == 0, (
        "bootstrap 注入后 _libs 仍不可达——bootstrap 注入契约本身失效：\n"
        f"{result.stderr.decode(errors='replace')}"
    )

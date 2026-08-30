# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""self_heal.wsl_shutdown 边界分支测试。

脚本缺席分支：wsl_shutdown.ps1 不存在时跳过关机、按无操作成功返回，
不得尝试任何子进程调用（外部依赖仅 subprocess，patch 为哨兵）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_mod():
    """动态加载 self_heal.py（唯一模块名，防与其它测试的裸名模块冲突）。"""
    mod_name = "isolation_self_heal_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "self_heal.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()


def test_wsl_shutdown_script_missing_returns_true_without_subprocess():
    """脚本缺失 → 直接按无操作成功返回，不发起任何子进程调用。"""
    with (
        patch(f"{_MOD.__name__}.os.path.exists", return_value=False),
        patch(f"{_MOD.__name__}.subprocess.run", side_effect=AssertionError("脚本缺失时不应调用子进程")) as run_spy,
    ):
        assert _MOD.wsl_shutdown(timeout=1) is True
        run_spy.assert_not_called()

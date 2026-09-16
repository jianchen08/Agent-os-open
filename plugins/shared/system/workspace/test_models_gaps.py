# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""workspace models.py 缺口补测（第 18 行 plugins/shared 自举注入）。

`models.py` 依赖 plugins/shared 平铺模块 `time_iso`（裸名导入），装载时把
shared root 注入 sys.path。本文件以唯一模块名装载，装载前把 shared root 从
sys.path 移除，验证自举生效且 Workspace 的时间戳字段走真实实现（非空）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_WS_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _WS_DIR.parents[1]  # plugins/shared


def _strip_shared_root() -> None:
    target = os.path.abspath(str(_SHARED_ROOT))
    for entry in list(sys.path):
        if entry and os.path.abspath(entry) == target:
            sys.path.remove(entry)


def test_models_bootstraps_shared_root_for_time_iso() -> None:
    """18 行：shared root 缺失时装载 models.py 应自举注入，time_iso 可用。"""
    saved_time_iso = sys.modules.pop("time_iso", None)
    original_path = list(sys.path)
    try:
        _strip_shared_root()
        assert not any(
            entry and os.path.abspath(entry) == os.path.abspath(str(_SHARED_ROOT))
            for entry in sys.path
        )
        mod_name = "workspace_models_bootstrap_under_test"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, _WS_DIR / "models.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        assert any(
            entry and os.path.abspath(entry) == os.path.abspath(str(_SHARED_ROOT))
            for entry in sys.path
        )
        assert "time_iso" in sys.modules
        ws = module.Workspace(title="自举验证")
        assert ws.created_at
        # 时间戳走真实时钟：构造期取样不得晚于其后的当前时刻取样
        # （原方向反置，仅两次取样落同一微秒才偶绿——墙钟竞态预存红）
        assert ws.created_at <= module._now_iso()
    finally:
        sys.path[:] = original_path
        sys.modules.pop("time_iso", None)
        if saved_time_iso is not None:
            sys.modules["time_iso"] = saved_time_iso


def test_models_reloads_with_shared_root_already_present() -> None:
    """对照：shared root 已在路径上时照常可用（幂等，不重复注入）。"""
    original_path = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            "workspace_models_idempotent_under_test", _WS_DIR / "models.py"
        )
        assert spec is not None and spec.loader is not None
        module: Any = importlib.util.module_from_spec(spec)
        sys.modules["workspace_models_idempotent_under_test"] = module
        spec.loader.exec_module(module)

        target = os.path.abspath(str(_SHARED_ROOT))
        matches = [e for e in sys.path if e and os.path.abspath(e) == target]
        assert len(matches) == 1  # 幂等：不重复注入
        assert os.path.abspath(matches[0]) == target
        ws = module.Workspace(title="常规")
        assert ws.created_at
        assert ws.to_dict()["created_at"] == ws.created_at
    finally:
        sys.path[:] = original_path
        sys.modules.pop("workspace_models_idempotent_under_test", None)

# @feature: FP-0.2.CFG 插件配置热感知 | @ci: python-test
"""llm_service on_config_changed 配置变更感知钩子测试（2026-09-28 批次 B）。

行为契约（断输入→输出/副作用，不钉实现）：
- server 模块装载即注册了配置变更钩子（每调用配置感知接线存在）
- 钩子触发 → 注入配置刷新（get_config 可读回新视图）+ adapter 惰性单例
  清空（下次 _ensure_adapter 懒重建携新模型表/key 池）
- 钩子对 sync 调用方可用（SDK 侧 await 兼容 sync 返回）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_server() -> Any:
    """按显式路径加载 llm 插件 server 模块（唯一模块名隔离同名 server.py）。"""
    mod_name = "llm_server_cfg_hook_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def test_config_change_handler_registered() -> None:
    server = _load_server()
    assert len(server.plugin._config_change_handlers) >= 1  # noqa: SLF001


def test_handler_refreshes_config_and_resets_adapter(monkeypatch: Any) -> None:
    import _config_models

    monkeypatch.setattr(_config_models, "_config", {})
    server = _load_server()
    server._adapter = object()  # noqa: SLF001 — 预置惰性单例，钩子应清空

    handler = server.plugin._config_change_handlers[-1]  # noqa: SLF001
    handler({"llm": {"defaults": {"chat": "m-new"}}})

    assert _config_models.get_config() == {"llm": {"defaults": {"chat": "m-new"}}}
    assert server._adapter is None  # noqa: SLF001

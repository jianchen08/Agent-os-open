# @feature: FP-0.2.五 资源治理 | @ci: none-local
"""isolation server.py 生命周期测试（D6 常驻任务回收）。

覆盖：
1. on_unload 取消配置 watcher 任务并收敛等待（不取消则 while True 轮询
   在卸载后常驻事件循环，持旧 manager 引用纯泄漏）；
2. on_unload 调用 wsl_health.terminate_keepalive()（拉起者负责终止保活会话）；
3. watcher 未起时 on_unload 幂等。

隔离策略：wsl_health.terminate_keepalive 用 monkeypatch 替身（wsl.exe 是
外部依赖）；watcher 用真实任务驱动（config yaml 只 stat 不写）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_mod() -> Any:
    """动态加载 server.py（唯一模块名，防与其它测试的裸名 server 冲突）。"""
    for _bare in ("server",):
        sys.modules.pop(_bare, None)
    mod_name = "isolation_server_lifecycle_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()


def test_on_unload_cancels_config_watcher_and_terminates_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_unload：watcher 任务 cancel 收敛（done+cancelled）+ 保活句柄终止。"""
    terminated: list[bool] = []
    monkeypatch.setattr(_MOD.wsl_health, "terminate_keepalive", lambda: terminated.append(True))

    tasks: list[asyncio.Task] = []

    async def _scenario() -> None:
        watcher = asyncio.create_task(_MOD._watch_config_reload())
        tasks.append(watcher)
        _MOD._config_watcher_task = watcher
        await asyncio.sleep(0)  # watcher 进入轮询（stat → sleep(5) 窗口）
        assert not watcher.done()
        await _MOD._on_unload({})

    asyncio.run(_scenario())

    watcher = tasks[0]
    assert watcher.done(), "卸载后 watcher 任务应已收敛"
    assert watcher.cancelled(), "watcher 应经 cancel 收敛而非自然退出"
    assert _MOD._config_watcher_task is None
    assert terminated == [True], "on_unload 应调用 terminate_keepalive"


def test_on_unload_idempotent_without_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """watcher 未起/已清（_config_watcher_task=None）时 on_unload 幂等不抛。"""
    terminated: list[bool] = []
    monkeypatch.setattr(_MOD.wsl_health, "terminate_keepalive", lambda: terminated.append(True))

    _MOD._config_watcher_task = None
    _MOD._manager = None

    asyncio.run(_MOD._on_unload({}))

    assert _MOD._config_watcher_task is None
    assert terminated == [True]

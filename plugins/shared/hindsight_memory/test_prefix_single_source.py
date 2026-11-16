# @feature: FP-0.2.六 记忆检索 | @ci: python-coverage
"""B11-S5 hindsight tag/bank 前缀单源一致性测试。

前缀 "thread-"（会话 bank）与 "session:"（memory 工具注入 tag）此前三写：
routes_memory / server 各持一份字面量，memory_backend recall 硬编码 f-string。
单源后契约真源 = memory_backend（插件内最底层，wiring/测试已消费），server 与
routes_memory 经导入消费。本文件断言三模块同源（同一对象）与召回归一/扇出
入口行为不变（行为面由 test_hindsight_thread_tag / test_memory_backend 全套
回归执法）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

import memory_backend  # 真实模块名（server/routes 的导入都解析到 sys.modules 同一条目）

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SDK_SRC = Path(__file__).resolve().parents[3] / "sdk" / "src"
if _SDK_SRC.exists() and str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))


def _load_module(filename: str, unique_name: str) -> Any:
    """唯一名动态加载插件内模块（避免跨测试文件模块级状态串扰）。"""
    sys.modules.pop(unique_name, None)
    spec = importlib.util.spec_from_file_location(unique_name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def server_mod() -> Any:
    return _load_module("server.py", "_hindsight_server_prefix_ut")


@pytest.fixture(scope="module")
def routes_mod() -> Any:
    return _load_module("routes_memory.py", "_hindsight_routes_prefix_ut")


class TestPrefixSingleSource:
    def test_server与routes消费同一常量对象(
        self, server_mod: Any, routes_mod: Any
    ) -> None:
        # 同源判定：server/routes 的消费名即 memory_backend 契约常量本身
        # （三方都解析到 sys.modules 中同一条 memory_backend）
        assert server_mod._THREAD_BANK_PREFIX is memory_backend.THREAD_BANK_PREFIX
        assert server_mod._SESSION_TAG_PREFIX is memory_backend.SESSION_TAG_PREFIX
        assert routes_mod.THREAD_BANK_PREFIX is memory_backend.THREAD_BANK_PREFIX

    def test_前缀契约值(self) -> None:
        # 契约值锁：内核会话 id 形 thread-{uuid}；store 侧 tag 形 session:<id>
        assert memory_backend.THREAD_BANK_PREFIX == "thread-"
        assert memory_backend.SESSION_TAG_PREFIX == "session:"

    def test_召回归一行为不变(self, server_mod: Any) -> None:
        # server._normalize_thread_tag：双形态归一 + 非 thread tag 拒绝
        normalize = server_mod._normalize_thread_tag
        assert normalize("thread-abc123") == "thread-abc123"
        assert normalize("session:thread-abc123") == "thread-abc123"
        assert normalize("session:other-bank") == ""
        assert normalize("pipeline:p1") == ""

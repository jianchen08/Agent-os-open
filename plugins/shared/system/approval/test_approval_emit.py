# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""approval.created 事件发射链路测试（改动 A 后端）。

覆盖：
1. create_choice 成功创建 human 交互后 → emit approval.created（payload 对齐前端）
2. event-bus capability 未注入 → 不抛异常（fire-and-forget 韧性）
3. create_choice 失败 → 不 emit（只在创建成功后发）
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

# SDK 路径（agentos_plugin_sdk 未安装时）
_SDK_DIR = Path(__file__).resolve().parents[4] / "sdk" / "src"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))


def _load_server() -> Any:
    """动态加载 server.py（每次新建，避免模块级 plugin 状态跨测试污染）。"""
    mod_name = "approval_server_test"
    module_path = _PLUGIN_DIR / "server.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class FakeBus:
    """记录 emit 调用的伪 event-bus capability handle。"""

    def __init__(self) -> None:
        self.emits: list[tuple[str, dict]] = []

    async def notify(self, method: str, params: dict) -> None:
        self.emits.append((method, params))


class FakeHi:
    """伪 human-interaction capability handle。"""

    def __init__(self, create_result: dict, wait_result: dict | None = None) -> None:
        self._create = create_result
        self._wait = wait_result or {"selected_option": "批准"}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "create_choice":
            return self._create
        if method == "wait_for_choice":
            return self._wait
        return {}


def _inject_capability(module: Any, name: str, handle: Any) -> None:
    """把伪 capability 注入插件（覆盖 _get_cap 的返回）。"""
    original = module.plugin.get_capability
    module.plugin.get_capability = lambda n: handle if n == name else original(n)  # type: ignore[method-assign]


def test_create_choice_emits_approval_created() -> None:
    """create_choice 成功 → emit approval.created，payload 含 request_id/options/mode/run_id。"""
    mod = _load_server()
    bus = FakeBus()
    _inject_capability(mod, "event-bus", bus)
    hi = FakeHi(create_result={"request_id": "req-abc", "status": "pending"})
    _inject_capability(mod, "human-interaction", hi)
    mod._suspended.clear()
    mod._decisions.clear()

    result = _run(mod.create_choice(title="是否批准", options=["批准", "拒绝"], run_id="run-42"))

    assert result.get("status") == "resolved"
    # emit 记录：bus.notify("emit", {event, payload, thread_id})
    assert len(bus.emits) == 1
    method, params = bus.emits[0]
    assert method == "emit"
    assert params["event"] == "approval.created"
    payload = params["payload"]
    assert payload["request_id"] == "req-abc"
    assert payload["title"] == "是否批准"
    assert payload["options"] == ["批准", "拒绝"]
    assert payload["mode"] == "choice"
    assert payload["run_id"] == "run-42"
    assert params["thread_id"] == "run-42"


def test_emit_skipped_when_no_event_bus() -> None:
    """event-bus 未注入 → 不发事件、不抛异常（fire-and-forget 韧性）。"""
    mod = _load_server()
    # 不注入 event-bus（get_capability 抛 KeyError → _get_cap 返回 None）
    hi = FakeHi(create_result={"request_id": "req-x"})
    _inject_capability(mod, "human-interaction", hi)
    mod._suspended.clear()
    mod._decisions.clear()

    result = _run(mod.create_choice(title="t", options=["a"], run_id="run-1"))

    assert result.get("status") == "resolved"  # 主链路不受影响


def test_no_emit_when_create_fails() -> None:
    """human create_choice 失败 → 不 emit approval.created（仅创建成功后发）。"""
    mod = _load_server()
    bus = FakeBus()
    _inject_capability(mod, "event-bus", bus)
    hi = FakeHi(create_result={"error": "human unavailable"})
    _inject_capability(mod, "human-interaction", hi)

    result = _run(mod.create_choice(title="t", options=["a"], run_id="run-2"))

    assert "error" in result
    assert bus.emits == []


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

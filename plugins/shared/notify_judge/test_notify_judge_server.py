# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage

"""notify_judge server.py 适配层测试——注册一致性 / 降级契约 / handler 出入面。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))


def _load_server() -> Any:
    """按显式路径加载 server.py（裸名 plugin 全车道共跑会被劫持，先逐出）。"""
    sys.modules.pop("plugin", None)
    mod_name = "notify_judge_server_test"
    spec = importlib.util.spec_from_file_location(mod_name, str(_DIR / "server.py"))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_invoke_entry_tool_registered() -> None:
    srv = _load_server()
    assert srv.plugin._tools is not None
    assert "notify_judge.execute" in srv.plugin._tools


def test_invoke_entry_matches_manifest() -> None:
    manifest = json.loads((_DIR / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["invoke_entry"] == "notify_judge.execute"
    assert manifest["id"] == "pipeline_notify_judge"


async def test_execute_handler_writes_notify_worthy() -> None:
    """handler 出入面： worthy=true → state_updates 携 messages set-op。"""

    async def fake_caller(method: str, params: dict, timeout: float | None = None) -> dict[str, Any]:
        return {"success": True, "data": {"text": '{"worthy": true, "reason": "交付"}'}}

    srv = _load_server()
    srv.set_capability_caller(fake_caller)
    state = {
        "messages": [
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "正在处理。", "seq": 3},
        ]
    }
    result = await srv.execute(state, {"settings": {"model": "minimax-m3"}})
    ops = result["state_updates"]["messages"]["_ops"]
    assert ops[0]["seq"] == 3
    assert ops[0]["msg"]["metadata"]["notify_worthy"] is True


async def test_execute_handler_degrades_to_zero_write_on_error(monkeypatch: Any) -> None:
    """降级契约：判定器异常 → warning 直通零写入，不阻断管道。"""
    srv = _load_server()

    async def _boom(self: Any, state: dict) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(srv.NotifyJudge, "judge", _boom)
    result = await srv.execute({"messages": []}, None)
    assert result == {"state_updates": {}}


async def test_execute_handler_false_writes_nothing() -> None:
    """LLM 判否 → handler 零写入（true-only 契约）。"""

    async def fake_caller(method: str, params: dict, timeout: float | None = None) -> dict[str, Any]:
        return {"success": True, "data": {"text": '{"worthy": false, "reason": "碎语"}'}}

    srv = _load_server()
    srv.set_capability_caller(fake_caller)
    state = {
        "messages": [
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "好的。", "seq": 3},
        ]
    }
    assert await srv.execute(state, None) == {"state_updates": {}}


class _FakeCapabilityHandle:
    """tool-executor 句柄 fake：记录调用并按 tool_name 回信封。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, float | None]] = []

    async def call(self, method: str, params: dict, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append((method, params, timeout))
        if params.get("tool_name") == "model.resolve":
            return {"success": True, "data": {"model_id": "tier-small-resolved"}}
        return {"success": True, "data": {"text": '{"worthy": true, "reason": "交付"}'}}


async def test_on_load_wires_capability_caller(monkeypatch: Any) -> None:
    """on_load 接线：tool-executor 句柄 → capability caller → LLM 判定通路。"""
    srv = _load_server()
    handle = _FakeCapabilityHandle()
    monkeypatch.setattr(srv.plugin, "get_capability", lambda _name: handle)
    await srv._on_load({})
    state = {
        "messages": [
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "好的。", "seq": 3},
        ]
    }
    result = await srv.execute(state, None)
    assert result["state_updates"]["messages"]["_ops"][0]["msg"]["metadata"]["notify_worthy"] is True
    assert len(handle.calls) == 2
    method, params, _timeout = handle.calls[0]
    assert method == "invoke"
    assert params["tool_name"] == "model.resolve"
    assert params["plugin_id"] == "llm_service"
    assert params["args"] == {"model_tier": "small"}
    method, params, _timeout = handle.calls[1]
    assert params["tool_name"] == "llm.complete_stream"
    assert params["plugin_id"] == "llm_service"

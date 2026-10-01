# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-coverage
"""交互卡来源 payload 契约测试。

用户裁定：所有弹给用户的交互卡必须可见完整来源——发起 agent（名称+级别）、
所属会话、管道（id+名称）、时间。本文件钉服务端 payload 的来源字段契约：

1. ``interaction.create_choice`` 服务工具（approval/security_check 等插件经此
   建卡）：来源四元组入参 → 记录 message_data 落位 → notify_request payload
   透传（卡片族数据面）；缺项省略键。
2. LLM 工具路径（human_interaction choice）：param_inject 注入的
   ``parent_agent_level``（int）归一为 "L{n}" 落 payload。
3. notification 路径：pipeline_id/agent_level 同样落 payload（通知中心
   sourceLabel 解析同一数据面）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_TOOLS_DIR = _PLUGIN_DIR.parent


def _load_server() -> Any:
    """importlib 显式路径 + 唯一模块名加载 server.py（同 test_human_server 约定）。"""
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    sys.modules.pop("human", None)
    name = "human_origin_payload_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "server.py")
    assert spec is not None, "cannot load human/server.py"
    assert spec.loader is not None, "cannot load human/server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _FakeBus:
    """记录 notify 调用的假 event-bus capability。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.calls.append((method, params))


class _FakePlugin:
    """假插件：get_capability 按注入表返回，缺失抛 KeyError（同 SDK 语义）。"""

    def __init__(self, caps: dict[str, Any] | None = None) -> None:
        self._caps = caps or {}

    def get_capability(self, name: str) -> Any:
        if name not in self._caps:
            raise KeyError(name)
        return self._caps[name]


def _last_request_payload(bus: _FakeBus) -> dict[str, Any]:
    """取最近一条 interaction_request 事件的 payload。"""
    for _method, params in reversed(bus.calls):
        if params.get("event") == "interaction_request":
            return params["payload"]
    raise AssertionError("no interaction_request emitted")


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _FakeBus]:
    """环境收敛（choice 等待上限 0.2s 防阻塞）+ 真实 service + 假 bus。"""
    monkeypatch.setenv("HUMAN_INTERACTION_CHOICE_MAX_WAIT_SECONDS", "0.2")
    mod = _load_server()
    bus = _FakeBus()
    _run(mod._on_load({}))
    assert mod._service is not None
    mod._service.set_notifier(mod._EventBusNotifier(_FakePlugin({"event-bus": bus})))
    return mod, bus


def test_service_create_choice_full_origin_payload(seeded: tuple[Any, _FakeBus]) -> None:
    """服务工具建卡带全量来源（L3 子代理）：记录落位 + payload 五字段齐备。"""
    mod, bus = seeded
    svc = mod._service

    rid = _run(mod.interaction_create_choice(
        session_id="sess-1", thread_id="sess-1", tab_id="", title="授权写入 data/interp",
        description="工具 file_write 要写的路径在写区外",
        options=[{"id": "pipeline", "label": "仅本管道"}],
        priority="high", user_id="u-1",
        agent_id="coding_dev_agent", agent_level="L3",
        pipeline_id="pipe-abc123", agent_name="编码开发代理",
    ))["request_id"]

    record = _run(svc.get_request(rid))
    assert record is not None
    msg = record["message_data"]
    assert msg["agent_id"] == "coding_dev_agent"
    assert msg["agent_level"] == "L3"
    assert msg["pipeline_id"] == "pipe-abc123"
    assert msg["agent_name"] == "编码开发代理"

    payload = _last_request_payload(bus)
    assert payload["agent_id"] == "coding_dev_agent"
    assert payload["agent_level"] == "L3"
    assert payload["pipeline_id"] == "pipe-abc123"
    assert payload["agent_name"] == "编码开发代理"
    assert payload["created_at"]  # 时间段数据面（卡片来源行显示时分）


def test_service_create_choice_partial_origin_omits_missing_keys(
    seeded: tuple[Any, _FakeBus],
) -> None:
    """服务工具建卡缺来源（L1 主会话直批）：缺项键整体省略，不落空串污染。"""
    mod, bus = seeded
    svc = mod._service

    rid = _run(mod.interaction_create_choice(
        session_id="sess-2", thread_id="sess-2", tab_id="", title="安全审批: bash_execute",
        options=[{"id": "approve", "label": "批准"}],
        agent_level="L1",
    ))["request_id"]

    record = _run(svc.get_request(rid))
    msg = record["message_data"]
    assert msg["agent_level"] == "L1"
    # agent_id 是记录基座键（恒存在）；未传时为 None——payload 侧整体省略
    assert not msg.get("agent_id")
    assert "pipeline_id" not in msg
    assert "agent_name" not in msg

    payload = _last_request_payload(bus)
    assert payload["agent_level"] == "L1"
    assert "agent_id" not in payload
    assert "pipeline_id" not in payload
    assert "agent_name" not in payload


def test_llm_tool_path_normalizes_parent_agent_level(seeded: tuple[Any, _FakeBus]) -> None:
    """LLM 工具路径：param_inject 注入的 parent_agent_level（int）→ payload "L3"。

    阻塞等待无人应答 → 0.2s 收敛超时拒绝（等待上限由环境收敛），payload 在
    创建瞬间已发射。
    """
    mod, bus = seeded

    result = _run(mod.human_interaction(
        mode="choice", title="请确认方案", options=["批准", "拒绝"],
        session_id="sess-3", pipeline_id="pipe-llm",
        parent_agent_level=3,
        timeout_seconds=30,
    ))
    assert result["error_code"] == "INTERACTION_TIMEOUT"

    payload = _last_request_payload(bus)
    assert payload["agent_level"] == "L3"
    assert payload["pipeline_id"] == "pipe-llm"
    assert payload["title"] == "请确认方案"


def test_agent_level_normalization_variants(seeded: tuple[Any, _FakeBus]) -> None:
    """级别归一单点（_agent_level_from_kwargs）：int/字符串合法值归一，非法值 None。"""
    mod, _bus = seeded
    normalize = mod._agent_level_from_kwargs
    assert normalize({"parent_agent_level": 3}) == "L3"
    assert normalize({"parent_agent_level": "l2"}) == "L2"
    assert normalize({"parent_agent_level": "L1"}) == "L1"
    assert normalize({}) is None
    assert normalize({"parent_agent_level": ""}) is None
    assert normalize({"parent_agent_level": "abc"}) is None


def test_notification_path_carries_pipeline_and_level(seeded: tuple[Any, _FakeBus]) -> None:
    """通知模式：pipeline_id/agent_level 落 payload（通知中心 sourceLabel 同数据面）。"""
    mod, bus = seeded

    _run(mod.human_interaction(
        mode="notification", title="进度通知", description="阶段完成",
        session_id="sess-4", pipeline_id="pipe-notify",
        parent_agent_level=2,
    ))

    payload = _last_request_payload(bus)
    assert payload["interaction_mode"] == "notification"
    assert payload["pipeline_id"] == "pipe-notify"
    assert payload["agent_level"] == "L2"


def test_conversation_path_carries_pipeline_and_level(seeded: tuple[Any, _FakeBus]) -> None:
    """对话模式：pipeline_id/agent_level 同样落 payload（与 choice 同一数据面契约）。"""
    mod, bus = seeded

    result = _run(mod.human_interaction(
        mode="conversation", title="请到对话页确认", initial_message="方案已就绪",
        session_id="sess-5", pipeline_id="pipe-conv",
        parent_agent_level=2,
        timeout_seconds=0.2,
    ))
    # 无人到达对话页 → 0.2s 收敛超时（conversation 等待取调用方 timeout）；
    # payload 在创建瞬间已发射
    assert result["error_code"] == "INTERACTION_TIMEOUT"

    payload = _last_request_payload(bus)
    assert payload["interaction_mode"] == "conversation"
    assert payload["pipeline_id"] == "pipe-conv"
    assert payload["agent_level"] == "L2"

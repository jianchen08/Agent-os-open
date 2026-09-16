# @feature: FP-MIGR task_submit server 接线补测 | @ci: python-coverage
"""task_submit server.py 的 on_load 接线面行为测试。

靶面：sidecar 启动时把三个能力桥（chat.send_message 派发器 / pipeline-state
读取器 / agent_manager agent.get 查询器）注入 tool.py 的模块级注入点。

断言可观察契约：注入后经 tool.py 的取用函数（_get_chat_sender /
_get_state_reader / _get_agent_registry_lookup 语义）能拿回钩子，且钩子转发到
正确的能力方法与参数（chat.send_message 原样透传；pipeline-state.list 非 list
响应归一为空表；agent.get 的 success/found/config 信封逐级判真，任一不成立
返回 None 表示磁盘回退）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

from tests._stdlib_guard import ensure_stdlib_module

ensure_stdlib_module("types")

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "tasks"
_TOOL_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "task_submit"
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"

_SERVER_MOD = "task_submit_server_wiring_test_mod"
_TOOL_MOD = "task_submit_tool_wiring_test_mod"


@pytest.fixture(scope="module", autouse=True)
def _module_sys_path():
    added: list[str] = []
    for _p in (_SDK_DIR, _TASKS_DIR, _TOOL_DIR, _SYSTEM_DIR, _SHARED_DIR):
        s = str(_p)
        if s not in sys.path:
            sys.path.insert(0, s)
            added.append(s)
    yield
    for s in added:
        sys.path.remove(s)


@pytest.fixture
def wiring(monkeypatch):
    """装载 server.py（含 on_load 注册）+ 独立的 tool.py 实例，返回 (server, tool)。

    server 模块的 `import tool as tool_mod` 会命中 sys.path 上的 tool.py——用
    monkeypatch 把 sys.modules['tool'] 钉到本测试装载的实例，保证注入点与断言
    面是同一模块对象。
    """
    tool_spec = importlib.util.spec_from_file_location(_TOOL_MOD, _TOOL_DIR / "tool.py")
    tool_mod = importlib.util.module_from_spec(tool_spec)
    sys.modules[_TOOL_MOD] = tool_mod
    sys.modules["tool"] = tool_mod
    try:
        tool_spec.loader.exec_module(tool_mod)

        server_spec = importlib.util.spec_from_file_location(
            _SERVER_MOD, _TOOL_DIR / "server.py")
        server_mod = importlib.util.module_from_spec(server_spec)
        sys.modules[_SERVER_MOD] = server_mod
        server_spec.loader.exec_module(server_mod)
        yield server_mod, tool_mod
    finally:
        for name in (_TOOL_MOD, _SERVER_MOD, "tool"):
            sys.modules.pop(name, None)
        tool_mod._chat_sender = None
        tool_mod._agent_registry_lookup = None
        tool_mod._state_reader = None


class _FakeHandle:
    """能力句柄桩：记录 (method, params) 并回放构造时给定的响应。"""

    def __init__(self, responder):
        self._respond = responder
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, timeout=None):
        self.calls.append((method, params or {}))
        return self._respond(method, params or {})


def _install_capabilities(server_mod, monkeypatch, responders):
    """按能力名装配桩句柄；responders 缺某能力时该能力取 None（未注册场景）。"""
    handles: dict[str, _FakeHandle] = {}

    class _FakePlugin:
        def get_capability(self, name):
            if name not in responders:
                raise KeyError(f"capability 未注册: {name}")
            return handles.setdefault(name, _FakeHandle(responders[name]))

    monkeypatch.setattr(server_mod, "plugin", _FakePlugin())
    return handles


# ── on_load 注入的三个钩子都挂上 ─────────────────────────────────────────
def test_on_load_registers_all_three_bridges(wiring, monkeypatch):
    """on_load 后 tool 模块三个注入点都非空（chat / state / registry 全接线）。"""
    server_mod, tool_mod = wiring
    handles = _install_capabilities(server_mod, monkeypatch, {
        "chat": lambda m, p: {"success": True},
        "pipeline-state": lambda m, p: [],
        "tool-executor": lambda m, p: {"success": True, "data": {"found": False}},
    })

    asyncio.run(server_mod._on_load({}))

    assert tool_mod._chat_sender is not None
    assert tool_mod._state_reader is not None
    assert tool_mod._agent_registry_lookup is not None


# ── chat.send_message 派发器 ─────────────────────────────────────────────
def test_chat_sender_forwards_params_verbatim(wiring, monkeypatch):
    """chat 派发器把 params 原样交给 capability 的 send_message，返回值原样回传。"""
    server_mod, tool_mod = wiring
    _install_capabilities(server_mod, monkeypatch, {
        "chat": lambda m, p: {"pipeline_id": "pl-1", "echo": p},
        "pipeline-state": lambda m, p: [],
        "tool-executor": lambda m, p: {"success": True, "data": {"found": False}},
    })
    asyncio.run(server_mod._on_load({}))

    out = asyncio.run(tool_mod._chat_sender({"goal_title": "g", "target_id": "a"}))

    assert out["pipeline_id"] == "pl-1"
    assert out["echo"] == {"goal_title": "g", "target_id": "a"}


# ── pipeline-state 读取器 ────────────────────────────────────────────────
def test_state_reader_returns_list_response_as_is(wiring, monkeypatch):
    """pipeline-state.list 返回 list → 原样透出（rows 直接可用）。"""
    server_mod, tool_mod = wiring
    rows = [{"field_key": "task.status", "field_value": "running"}]
    _install_capabilities(server_mod, monkeypatch, {
        "chat": lambda m, p: {},
        "pipeline-state": lambda m, p: rows,
        "tool-executor": lambda m, p: {"success": True, "data": {"found": False}},
    })
    asyncio.run(server_mod._on_load({}))

    assert asyncio.run(tool_mod._state_reader()) == rows


def test_state_reader_normalizes_non_list_response_to_empty(wiring, monkeypatch):
    """非 list 响应（信封 dict / None）→ 归一为空表，调用方不必判类型。"""
    server_mod, tool_mod = wiring
    _install_capabilities(server_mod, monkeypatch, {
        "chat": lambda m, p: {},
        "pipeline-state": lambda m, p: {"rows": []},  # 信封形态，非 list
        "tool-executor": lambda m, p: {"success": True, "data": {"found": False}},
    })
    asyncio.run(server_mod._on_load({}))

    assert asyncio.run(tool_mod._state_reader()) == []


def test_state_reader_calls_list_method(wiring, monkeypatch):
    """读取器走的 capability 方法是 list（契约名，不得漂移）。"""
    server_mod, tool_mod = wiring
    handles = _install_capabilities(server_mod, monkeypatch, {
        "chat": lambda m, p: {},
        "pipeline-state": lambda m, p: [],
        "tool-executor": lambda m, p: {"success": True, "data": {"found": False}},
    })
    asyncio.run(server_mod._on_load({}))
    asyncio.run(tool_mod._state_reader())

    assert [m for m, _ in handles["pipeline-state"].calls] == ["list"]


# ── agent_registry 查询器 ────────────────────────────────────────────────
def _responder_agent_get(payload):
    return lambda m, p: payload


def test_agent_lookup_returns_config_on_success(wiring, monkeypatch):
    """success=true + found=true + config 为 dict → 返回 config。"""
    server_mod, tool_mod = wiring
    _install_capabilities(server_mod, monkeypatch, {
        "chat": lambda m, p: {},
        "pipeline-state": lambda m, p: [],
        "tool-executor": _responder_agent_get(
            {"success": True, "data": {"found": True, "config": {"level": "L2"}}}),
    })
    asyncio.run(server_mod._on_load({}))

    assert asyncio.run(tool_mod._agent_registry_lookup("orch")) == {"level": "L2"}


def test_agent_lookup_calls_agent_manager_agent_get(wiring, monkeypatch):
    """查询经 tool-executor 的 invoke，显式 plugin_id=agent_manager + tool_name=agent.get。"""
    server_mod, tool_mod = wiring
    handles = _install_capabilities(server_mod, monkeypatch, {
        "chat": lambda m, p: {},
        "pipeline-state": lambda m, p: [],
        "tool-executor": _responder_agent_get(
            {"success": True, "data": {"found": True, "config": {}}}),
    })
    asyncio.run(server_mod._on_load({}))
    asyncio.run(tool_mod._agent_registry_lookup("a1"))

    method, params = handles["tool-executor"].calls[0]
    assert method == "invoke"
    assert params["plugin_id"] == "agent_manager"
    assert params["tool_name"] == "agent.get"
    assert params["args"] == {"agent_id": "a1"}


@pytest.mark.parametrize(
    "payload,why",
    [
        ({"success": False, "data": {"found": True, "config": {}}}, "success=false"),
        ({"data": {"found": True, "config": {}}}, "缺 success 键"),
        ({"success": True, "data": {"found": False}}, "found=false"),
        ({"success": True, "data": {}}, "缺 found 键"),
        ({"success": True}, "缺 data"),
        ({"success": True, "data": {"found": True}}, "缺 config"),
        ({"success": True, "data": {"found": True, "config": "not-dict"}},
         "config 非 dict"),
        ("not-a-dict", "响应非 dict"),
    ],
)
def test_agent_lookup_degrades_to_none(wiring, monkeypatch, payload, why):
    """信封逐级判真：任一不成立 → None（调用方据此走磁盘回退）。"""
    server_mod, tool_mod = wiring
    _install_capabilities(server_mod, monkeypatch, {
        "chat": lambda m, p: {},
        "pipeline-state": lambda m, p: [],
        "tool-executor": _responder_agent_get(payload),
    })
    asyncio.run(server_mod._on_load({}))

    assert asyncio.run(tool_mod._agent_registry_lookup("a1")) is None, why


# ── task_submit 工具包装（server 暴露面） ────────────────────────────────
class _StubTool:
    """TaskSubmitTool 桩：按构造参数回放执行结果。"""

    def __init__(self, result):
        self._result = result
        self.seen: list[dict] = []

    async def execute(self, kwargs):
        self.seen.append(kwargs)
        return self._result


def _patch_tool_class(server_mod, monkeypatch, result):
    """把 server 模块的 TaskSubmitTool 换成桩（构造时回放给定结果）。"""
    stub = _StubTool(result)
    monkeypatch.setattr(server_mod, "TaskSubmitTool", lambda: stub)
    return stub


def _completed(output):
    from agentos_plugin_sdk.results import ExecutionStatus, ToolExecutionResult
    return ToolExecutionResult(status=ExecutionStatus.COMPLETED, output=output)


def _failed(error, error_code=None, metadata=None):
    from agentos_plugin_sdk.results import ExecutionStatus, ToolExecutionResult
    return ToolExecutionResult(status=ExecutionStatus.FAILED, error=error,
                               error_code=error_code, metadata=metadata or {})


def test_task_submit_success_returns_output_payload(wiring, monkeypatch):
    """成功 → 直接返回 result.output（不包额外信封）。"""
    server_mod, _ = wiring
    payload = {"task_id": "t1", "status": "pending"}
    stub = _patch_tool_class(server_mod, monkeypatch, _completed(payload))

    out = asyncio.run(server_mod.task_submit(goal_title="g"))

    assert out == payload
    assert stub.seen == [{"goal_title": "g"}]


def test_task_submit_failure_passes_error_without_code(wiring, monkeypatch):
    """失败无 error_code → 载荷只有 error 键。"""
    server_mod, _ = wiring
    _patch_tool_class(server_mod, monkeypatch, _failed("目标不存在"))

    out = asyncio.run(server_mod.task_submit(goal_title="g"))

    assert out == {"error": "目标不存在"}


def test_task_submit_failure_hoists_error_code_and_metadata(wiring, monkeypatch):
    """失败带 error_code 与结构化 metadata → 逐键提升到载荷（错误是值）。"""
    server_mod, _ = wiring
    _patch_tool_class(server_mod, monkeypatch, _failed(
        "编排键不存在", error_code="ORCHESTRATION_NOT_FOUND",
        metadata={"missing_fields": ["target_id"], "orchestration_key": "k1",
                  "suggestion": "改用 x"}))

    out = asyncio.run(server_mod.task_submit(goal_title="g"))

    assert out["error"] == "编排键不存在"
    assert out["error_code"] == "ORCHESTRATION_NOT_FOUND"
    assert out["missing_fields"] == ["target_id"]
    assert out["orchestration_key"] == "k1"
    assert out["suggestion"] == "改用 x"


def test_task_submit_failure_drops_none_valued_metadata_keys(wiring, monkeypatch):
    """metadata 中值为 None 的键被剔除（不透出空占位）。"""
    server_mod, _ = wiring
    _patch_tool_class(server_mod, monkeypatch, _failed(
        "失败", error_code="E", metadata={"a": 1, "b": None}))

    out = asyncio.run(server_mod.task_submit(goal_title="g"))

    assert out["a"] == 1
    assert "b" not in out

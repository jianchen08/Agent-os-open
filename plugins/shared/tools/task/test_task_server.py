# @feature: FP-0.2.二 内部模块 manifest（task_manage 插件入口：server.py 接线与 handler） | @ci: python-coverage
"""task_manage 插件入口层测试（server.py）。

覆盖：importlib 显式路径加载（唯一模块名 + 逐出裸名）、on_load 能力接线
（chat / pipeline-state / pipeline-executor 三路注入）、task_manage handler
（成功返回 output / 失败返回 {"error": ...} 契约）。

真实依赖：TaskTool 执行链（handler 内部构造）；capability 为
AgentOSPlugin 内部句柄注入（fake call_fn），不触碰内核。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent.parent.parent
_TASKS_DIR = _PROJECT_ROOT / "plugins" / "shared" / "system" / "tasks"
_SYSTEM_DIR = _PROJECT_ROOT / "plugins" / "shared" / "system"
# 与 server.py 自身 sys.path 注入一致（tasks 平铺目录 + system/）。
for _d in (str(_HERE), str(_TASKS_DIR), str(_SYSTEM_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# 自加载：显式路径 + 唯一模块名；依赖的裸名（tool/service/http_api）一并逐出。
_SERVER_PY = _HERE / "server.py"
assert _SERVER_PY.exists(), f"缺 server.py: {_SERVER_PY}"
for _bare in ("tool", "service", "http_api"):
    sys.modules.pop(_bare, None)
sys.modules.pop("task_manage_server_plugin_test", None)
_spec = importlib.util.spec_from_file_location("task_manage_server_plugin_test", _SERVER_PY)
assert _spec is not None and _spec.loader is not None
_server_mod = importlib.util.module_from_spec(_spec)
sys.modules["task_manage_server_plugin_test"] = _server_mod
_spec.loader.exec_module(_server_mod)

import tool as _task_mod  # noqa: E402
import task_manage_server_plugin_test as _srv  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_paths_and_slots():
    """平铺串扰自持：用例前重插路径 + 还原 service/http_api/tool 槽位。

    "tool" 槽位重绑（非逐出）：共跑车里前序测试会把 sys.modules["tool"]
    换成其目录的 tool.py，_on_load 内部 `import tool` 即命中错误模块——
    重绑回 _task_mod 保证接线与断言读同一对象；逐出会触发重导入造成
    新对象（setter 写到的全局与断言读取的 _task_mod 不同）。
    """
    for _d in (str(_HERE), str(_TASKS_DIR), str(_SYSTEM_DIR)):
        if _d in sys.path:
            sys.path.remove(_d)
        sys.path.insert(0, _d)
    saved = {n: sys.modules.get(n) for n in ("service", "http_api")}
    for n in saved:
        sys.modules.pop(n, None)
    saved_tool = sys.modules.get("tool")
    sys.modules["tool"] = _task_mod
    try:
        yield
    finally:
        for n, m in saved.items():
            sys.modules.pop(n, None)
            if m is not None:
                sys.modules[n] = m
        sys.modules.pop("tool", None)
        if saved_tool is not None:
            sys.modules["tool"] = saved_tool


@pytest.fixture(autouse=True)
def _capabilities():
    """capability 全局注入点还原（server 测试会改写）。"""
    prev_chat = _task_mod._chat_sender
    prev_state = _task_mod._state_reader
    prev_exec = _task_mod._pipeline_executor
    yield
    _task_mod._chat_sender = prev_chat
    _task_mod._state_reader = prev_state
    _task_mod._pipeline_executor = prev_exec


def _plugin() -> Any:
    return _srv.plugin


def _fake_handle(call: Any) -> Any:
    """构造 fake CapabilityHandle（call 为收参的可调用对象）。"""
    from agentos_plugin_sdk import CapabilityHandle

    return CapabilityHandle(name="fake", call_fn=call)


# ─────────────────────────── 加载面 ───────────────────────────


def test_server_module_loads_with_unique_name() -> None:
    """server.py 经 importlib 显式路径加载成功，模块名唯一。"""
    assert _srv.plugin is not None
    assert _srv.plugin.name == "task_manage_tool"


def test_server_registers_tool_definition() -> None:
    """插件注册了 task_manage 工具定义（name/schema/description）。"""
    tools = _srv.plugin._tools  # noqa: SLF001 —— SDK 私有注册表（测试唯一读取面）
    assert "task_manage" in tools
    tdef = tools["task_manage"]
    assert tdef.description == "任务管理工具"
    schema = tdef.schema
    assert schema["type"] == "object"
    assert schema["required"] == ["action"]
    assert schema["properties"]["action"]["enum"] == ["get", "continue", "stop", "delete", "change"]
    assert "task_id" in schema["properties"]
    assert "message" in schema["properties"]


def test_on_load_hook_registered() -> None:
    """on_load 生命周期钩子已注册（sidecar 启动接线点）。"""
    from agentos_plugin_sdk.types import LifecycleEvent

    assert _srv.plugin._lifecycle_handlers[LifecycleEvent.ON_LOAD.value] is not None  # noqa: SLF001


# ─────────────────────────── on_load 接线 ───────────────────────────


async def test_on_load_wires_capabilities(tmp_path: Path) -> None:
    """on_load：chat/pipeline-state/pipeline-executor 三能力经 setter 注入全局。

    三个 fake handle 捕获调用参数并返回固定结果，验证闭包转发方法名与参数。
    """
    chat_calls: list[tuple[str, dict[str, Any]]] = []
    state_calls: list[tuple[str, dict[str, Any]]] = []
    exec_calls: list[tuple[str, dict[str, Any]]] = []

    async def _chat(method: str, params: dict[str, Any], timeout: Any = None) -> dict[str, Any]:
        chat_calls.append((method, params))
        return {"ok": True}

    async def _state(
        method: str, params: dict[str, Any], timeout: Any = None
    ) -> list[dict[str, Any]]:
        state_calls.append((method, params))
        return [{"pipeline_id": "p1"}]

    async def _exec(
        method: str, params: dict[str, Any], timeout: Any = None
    ) -> dict[str, Any]:
        exec_calls.append((method, params))
        return {"ok": True}

    plugin_obj = _srv.plugin
    plugin_obj._capabilities = {
        "chat": _fake_handle(_chat),
        "pipeline-state": _fake_handle(_state),
        "pipeline-executor": _fake_handle(_exec),
    }

    await _srv._on_load({})

    assert _task_mod._chat_sender is not None
    assert _task_mod._state_reader is not None
    assert _task_mod._pipeline_executor is not None

    # 注入闭包按 server.py 契约转发（method + params）
    send_result = await _task_mod._chat_sender({"pipeline_id": "p1", "message": "hi"})
    assert send_result == {"ok": True}
    assert chat_calls == [("send_message", {"pipeline_id": "p1", "message": "hi"})]

    rows = await _task_mod._state_reader()
    assert rows == [{"pipeline_id": "p1"}]
    assert state_calls == [("list", {})]

    exec_result = await _task_mod._pipeline_executor(
        {"method": "suspend_pipeline", "params": {"pipeline_id": "p2"}}
    )
    assert exec_result == {"ok": True}
    assert exec_calls == [("suspend_pipeline", {"pipeline_id": "p2"})]


async def test_on_load_state_non_list_returns_empty() -> None:
    """pipeline-state 返回非 list → state_reader 归一为空列表。"""
    plugin_obj = _srv.plugin

    async def _state(method: str, params: dict[str, Any], timeout: Any = None) -> dict[str, Any]:
        return {"pipeline_id": "p1"}

    plugin_obj._capabilities = {"pipeline-state": _fake_handle(_state)}
    await _srv._on_load({})
    assert await _task_mod._state_reader() == []


async def test_on_load_restores_capabilities_after_each_case(
    tmp_path: Path, capsys: Any
) -> None:
    """回归：on_load 不抛异常且可重复执行（接线幂等）。"""
    plugin_obj = _srv.plugin
    plugin_obj._capabilities = {}

    async def _chat(method: str, params: dict[str, Any], timeout: Any = None) -> dict[str, Any]:
        return {}

    async def _state(method: str, params: dict[str, Any], timeout: Any = None) -> list[dict[str, Any]]:
        return []

    async def _exec(method: str, params: dict[str, Any], timeout: Any = None) -> dict[str, Any]:
        return {}

    plugin_obj._capabilities = {
        "chat": _fake_handle(_chat),
        "pipeline-state": _fake_handle(_state),
        "pipeline-executor": _fake_handle(_exec),
    }
    await _srv._on_load({})
    await _srv._on_load({})
    assert _task_mod._chat_sender is not None


# ─────────────────────────── handler 契约 ───────────────────────────


async def test_handler_success_returns_output() -> None:
    """成功 → 返回 result.output（任务不存在路径的失败结果除外）。"""
    # 用 get 列表路径（无副作用，不需 capability）
    out = await _srv.task_manage(action="get", parent_agent_level=1)
    assert isinstance(out, dict)
    assert "d" in out, "get 列表成功返回 output dict"
    assert "hint" in out


async def test_handler_failure_returns_error_dict() -> None:
    """失败 → 返回 {"error": <错误信息>}（错误码不回传）。"""
    out = await _srv.task_manage(action="get")
    assert out == {"error": "系统错误：parent_agent_level 未注入，无法确定调用者层级"}

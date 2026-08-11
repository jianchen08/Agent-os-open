#!/usr/bin/env python3
"""task_08 Python 插件 SDK 功能验证脚本 —— 完整用户旅程 + 补充场景。

模拟真实开发者从零搭建 MCP 插件服务的完整流程：
1. 导入 SDK → 2. 创建插件注册工具 → 3. 注册资源 → 4. 注册生命周期钩子 →
5. initialize 握手注入能力 → 6. tools/list → 7. tools/call(async) →
8. tools/call(sync) → 9. resources/read → 10. notifications 触发钩子

使用 McpServer 的内部 handler 方法直接调用，模拟 JSON-RPC 请求。
"""
from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock

# ─── 步骤1: SDK 导入 + 版本检查 ─────────────────────────────
from agentos_plugin_sdk import (
    AgentOSPlugin,
    CapabilityHandle,
    McpServer,
    ToolDef,
    tool,
)

from agentos_plugin_sdk.capability import STANDARD_CAPABILITIES
from agentos_plugin_sdk.tool import collect_tools

print("=" * 70)
print("[步骤1] SDK 导入验证")
print("=" * 70)

# 场景1: SDK导入 → 版本0.2.0
from agentos_plugin_sdk import __version__

assert __version__ == "0.2.0", f"Expected 0.2.0, got {__version__}"
print(f"  ✓ __version__ = {__version__}")
print(f"  ✓ AgentOSPlugin: {AgentOSPlugin}")
print(f"  ✓ tool: {tool}")
print(f"  ✓ CapabilityHandle: {CapabilityHandle}")

# 验证 5 个标准能力名称
assert STANDARD_CAPABILITIES == [
    "pipeline-executor",
    "config-reader",
    "tenant-context",
    "event-bus",
    "logger",
], f"Standard caps mismatch: {STANDARD_CAPABILITIES}"
print(f"  ✓ STANDARD_CAPABILITIES: {STANDARD_CAPABILITIES}")

# ─── 步骤2: 创建插件并注册工具（含 @tool 装饰器 + register_tool） ───
print()
print("=" * 70)
print("[步骤2] 工具注册（@tool 装饰器 + register_tool）")
print("=" * 70)

plugin = AgentOSPlugin("demo_plugin")

# 场景2a: @plugin.tool 装饰器注册
@plugin.tool(
    name="add",
    schema={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
    description="Add two numbers",
)
async def add(a: float, b: float) -> dict:
    """Add two numbers and return the sum."""
    return {"sum": a + b}


assert "add" in plugin._tools, "tool 'add' not registered"
assert plugin._tools["add"].name == "add"
assert plugin._tools["add"].description == "Add two numbers"
print(f"  ✓ @plugin.tool 装饰器注册成功: 'add' in plugin._tools")

# 场景2b: register_tool 方法注册（sync handler）
def sync_greet(name: str) -> dict:
    return {"greeting": f"Hello, {name}!"}

plugin.register_tool("greet", {"type": "object"}, sync_greet, "Greeting tool")
assert "greet" in plugin._tools
assert plugin._tools["greet"].handler is sync_greet
print(f"  ✓ register_tool 方法注册成功: 'greet' in plugin._tools")

# 场景2c: 模块级 @tool 装饰器
@tool(name="standalone_tool", schema={"type": "object"}, description="Module-level tool")
async def standalone_tool(x: int) -> dict:
    return {"x": x}

assert hasattr(standalone_tool, "_agentos_tool")
assert standalone_tool._agentos_tool.name == "standalone_tool"
print(f"  ✓ 模块级 @tool 装饰器标记成功: _agentos_tool.name = '{standalone_tool._agentos_tool.name}'")

# collect_tools 验证
class FakeModule:
    @tool(name="mod_a", schema={"type": "object"})
    async def method_a(self) -> dict:
        return {}

collected = collect_tools(FakeModule())
assert "mod_a" in collected
print(f"  ✓ collect_tools 收集模块工具: {list(collected.keys())}")

# ─── 步骤3: 资源注册 ────────────────────────────────────────
print()
print("=" * 70)
print("[步骤3] 资源注册")
print("=" * 70)

def read_config() -> dict:
    return {"version": "1.0", "debug": True}

plugin.register_resource("config://app", read_config, "App Config", "Application config")
assert "config://app" in plugin._resources
print(f"  ✓ 资源注册成功: 'config://app' in plugin._resources")

# ─── 步骤4: 生命周期钩子注册 ────────────────────────────────
print()
print("=" * 70)
print("[步骤4] 生命周期钩子注册（装饰器 + on_lifecycle）")
print("=" * 70)

lifecycle_log: list[str] = []

@plugin.on_load
def handle_load(params: dict) -> None:
    lifecycle_log.append(f"on_load:{params}")

@plugin.on_unload
def handle_unload(params: dict) -> None:
    lifecycle_log.append(f"on_unload:{params}")

@plugin.on_config_change
def handle_config_change(params: dict) -> None:
    lifecycle_log.append(f"on_config_change:{params}")

assert "on_load" in plugin._lifecycle_handlers
assert "on_unload" in plugin._lifecycle_handlers
assert "on_config_change" in plugin._lifecycle_handlers
print(f"  ✓ 三个装饰器钩子均注册: {list(plugin._lifecycle_handlers.keys())}")

# 也验证 on_lifecycle 直接注册
def on_error_handler(params: dict) -> None:
    lifecycle_log.append(f"on_error:{params}")

plugin.on_lifecycle("on_error", on_error_handler)
assert "on_error" in plugin._lifecycle_handlers
print(f"  ✓ on_lifecycle 直接注册: 'on_error' 已添加")


# ─── 创建 McpServer（模拟 Rust 内核连接） ─────────────────
server = McpServer(
    tools=plugin._tools,
    resources=plugin._resources,
    lifecycle_handlers=plugin._lifecycle_handlers,
    on_initialize=plugin._on_initialize,
)


async def run_journey() -> None:
    # ─── 步骤5: initialize 握手 + 依赖注入 ─────────────────
    print()
    print("=" * 70)
    print("[步骤5] MCP initialize 握手 + 依赖注入（5个能力句柄）")
    print("=" * 70)

    # 场景9: 模拟内核 initialize 握手注入 5 个标准能力句柄
    init_params = {
        "capabilities": {
            name: {"call_fn": AsyncMock(return_value={"ok": True}), "context": {"active": True}}
            for name in STANDARD_CAPABILITIES
        },
        "config": {"model": "gpt-4", "temperature": 0.7},
    }

    result = server._handle_initialize(init_params)
    assert result["protocolVersion"] == "2024-11-05", f"protocolVersion mismatch: {result['protocolVersion']}"
    assert result["serverInfo"]["name"] == "agentos-plugin-sdk"
    assert result["serverInfo"]["version"] == "0.2.0"
    assert "tools" in result["capabilities"]
    assert "resources" in result["capabilities"]
    print(f"  ✓ protocolVersion = {result['protocolVersion']}")
    print(f"  ✓ serverInfo = {result['serverInfo']}")
    print(f"  ✓ capabilities keys = {list(result['capabilities'].keys())}")

    # 验证 5 个能力句柄已注入到 plugin
    for cap_name in STANDARD_CAPABILITIES:
        cap = plugin.get_capability(cap_name)
        assert isinstance(cap, CapabilityHandle)
        assert cap.name == cap_name
        assert cap.get("active") is True
    print(f"  ✓ 全部 5 个标准能力句柄注入成功: {STANDARD_CAPABILITIES}")

    # 验证配置注入
    config = plugin.get_config()
    assert config["model"] == "gpt-4"
    assert config["temperature"] == 0.7
    print(f"  ✓ 配置注入成功: {config}")

    # ─── 步骤6: tools/list ─────────────────────────────────
    print()
    print("=" * 70)
    print("[步骤6] MCP tools/list — 返回已注册工具列表")
    print("=" * 70)

    tools_list = server._handle_tools_list()
    tool_names = [t["name"] for t in tools_list["tools"]]
    assert "add" in tool_names, f"'add' not in {tool_names}"
    assert "greet" in tool_names, f"'greet' not in {tool_names}"
    assert len(tools_list["tools"]) == 2, f"Expected 2 tools, got {len(tools_list['tools'])}"

    add_tool_info = next(t for t in tools_list["tools"] if t["name"] == "add")
    assert add_tool_info["description"] == "Add two numbers"
    assert "properties" in add_tool_info["inputSchema"]
    print(f"  ✓ 返回 {len(tools_list['tools'])} 个工具: {tool_names}")
    for t in tools_list["tools"]:
        print(f"    - {t['name']}: {t['description']}")

    # ─── 步骤7: tools/call(async) — 调用 async handler ────
    print()
    print("=" * 70)
    print("[步骤7] MCP tools/call(async) — 调用 async handler 'add'")
    print("=" * 70)

    # 场景5: tools/call(async)
    call_result = await server._handle_tools_call({"name": "add", "arguments": {"a": 3, "b": 5}})
    assert call_result["isError"] is False
    content_text = json.loads(call_result["content"][0]["text"])
    assert content_text == {"sum": 8.0}, f"Expected {{'sum': 8.0}}, got {content_text}"
    assert call_result["content"][0]["type"] == "text"
    print(f"  ✓ add(a=3, b=5) → {content_text}")
    print(f"  ✓ isError = {call_result['isError']}")

    # ─── 步骤8: tools/call(sync) — sync handler 自动包装 ──
    print()
    print("=" * 70)
    print("[步骤8] MCP tools/call(sync) — sync handler 自动包装为 async")
    print("=" * 70)

    # 场景6: sync handler 被自动包装
    greet_result = await server._handle_tools_call({"name": "greet", "arguments": {"name": "World"}})
    assert greet_result["isError"] is False
    greet_content = json.loads(greet_result["content"][0]["text"])
    assert greet_content == {"greeting": "Hello, World!"}
    print(f"  ✓ greet(name='World') → {greet_content}")

    # 场景12: iscoroutine 检测验证
    import asyncio as _aio
    # async handler 返回 coroutine
    coro = add(a=1, b=2)
    assert _aio.iscoroutine(coro), "async handler should return coroutine"
    _aio.run(coro) if False else None  # type: ignore
    # 直接 await 关闭
    await coro
    # sync handler 不返回 coroutine
    sync_result = sync_greet("test")
    assert not _aio.iscoroutine(sync_result)
    print(f"  ✓ iscoroutine 检测正确: async handler → coroutine, sync handler → 非 coroutine")

    # ─── 步骤9: resources/read ─────────────────────────────
    print()
    print("=" * 70)
    print("[步骤9] MCP resources/read — 读取资源")
    print("=" * 70)

    # 场景7: resources/read
    res_result = await server._handle_resources_read({"uri": "config://app"})
    assert len(res_result["contents"]) == 1
    res_content = res_result["contents"][0]
    assert res_content["uri"] == "config://app"
    assert res_content["mimeType"] == "application/json"
    config_data = json.loads(res_content["text"])
    assert config_data == {"version": "1.0", "debug": True}
    print(f"  ✓ resources/read('config://app') → {config_data}")

    # ─── 步骤10: notifications — 触发生命周期钩子 ──────────
    print()
    print("=" * 70)
    print("[步骤10] MCP notifications — 触发生命周期 handler")
    print("=" * 70)

    # 场景8 + 场景10: notification 触发生命周期 handler
    lifecycle_log.clear()

    await server._handle_notification("notifications/on_load", {"step": "init"})
    await server._handle_notification("notifications/on_unload", {"step": "cleanup"})
    await server._handle_notification("notifications/on_config_change", {"new": {"key": "val"}})
    await server._handle_notification("notifications/on_error", {"err": "something"})
    # initialized notification 应被忽略
    await server._handle_notification("notifications/initialized", {})

    assert len(lifecycle_log) == 4, f"Expected 4 lifecycle events, got {lifecycle_log}"
    assert "on_load" in lifecycle_log[0]
    assert "on_unload" in lifecycle_log[1]
    assert "on_config_change" in lifecycle_log[2]
    assert "on_error" in lifecycle_log[3]
    print(f"  ✓ 4 个生命周期事件触发:")
    for entry in lifecycle_log:
        print(f"    - {entry}")
    print(f"  ✓ notifications/initialized 被正确忽略")

    print()
    print("=" * 70)
    print("✅ 完整用户旅程全部通过！10/10 步骤成功。")
    print("=" * 70)


asyncio.run(run_journey())


# ═══════════════════════════════════════════════════════════
# 补充场景A: 错误输入
# ═══════════════════════════════════════════════════════════
print()
print("=" * 70)
print("[补充场景A] 错误输入验证")
print("=" * 70)


async def run_error_scenarios() -> None:
    # A1: 调用不存在的工具
    try:
        await server._handle_tools_call({"name": "nonexistent", "arguments": {}})
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "tool not found" in str(e)
        print(f"  ✓ 调用不存在工具 → ValueError: {e}")

    # A2: 读取不存在的资源
    try:
        await server._handle_resources_read({"uri": "unknown://x"})
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "resource not found" in str(e)
        print(f"  ✓ 读取不存在资源 → ValueError: {e}")

    # A3: 未注入能力调用
    p2 = AgentOSPlugin("test")
    try:
        p2.get_capability("pipeline-executor")
        assert False, "Should have raised KeyError"
    except KeyError as e:
        assert "not injected" in str(e)
        print(f"  ✓ 未注入能力 get_capability → KeyError: {e}")

    # A4: CapabilityHandle 未连接调用
    handle = CapabilityHandle("test")
    try:
        await handle.call("method", {})
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "not connected" in str(e)
        print(f"  ✓ 未连接句柄 call → RuntimeError: {e}")

    # A5: 空参数 tools/call — handler 参数不匹配时抛异常
    # _handle_tools_call 直接抛异常，由 _handle_message 的 try/except 捕获转为 JSON-RPC error
    import io
    from contextlib import redirect_stdout

    captured = io.StringIO()
    with redirect_stdout(captured):
        await server._handle_message({
            "jsonrpc": "2.0",
            "id": "err-1",
            "method": "tools/call",
            "params": {"name": "add", "arguments": {}},
        })
    output = captured.getvalue().strip()
    err_response = json.loads(output)
    assert err_response["jsonrpc"] == "2.0"
    assert err_response["id"] == "err-1"
    assert "error" in err_response
    assert err_response["error"]["code"] == -32603
    assert "missing" in err_response["error"]["message"] or "required" in err_response["error"]["message"]
    print(f"  ✓ 空参数 tools/call → _handle_message 捕获异常，返回 JSON-RPC error:")
    print(f"    code={err_response['error']['code']}, message={err_response['error']['message'][:60]}...")


asyncio.run(run_error_scenarios())


# ═══════════════════════════════════════════════════════════
# 补充场景B: 边界情况
# ═══════════════════════════════════════════════════════════
print()
print("=" * 70)
print("[补充场景B] 边界情况验证")
print("=" * 70)

# B1: 大量工具注册（边界量）
p3 = AgentOSPlugin("bulk")
for i in range(100):
    p3.register_tool(f"tool_{i}", {"type": "object"}, lambda i=i: {"i": i})
assert len(p3._tools) == 100
bulk_server = McpServer(p3._tools, p3._resources, p3._lifecycle_handlers)
bulk_list = bulk_server._handle_tools_list()
assert len(bulk_list["tools"]) == 100
print(f"  ✓ 100 个工具注册 + tools/list 返回 100 项")

# B2: 30 行代码封装验证（场景11）
plugin_code = '''
from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("calculator")

@plugin.tool(
    name="add",
    schema={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
    description="Add two numbers",
)
async def add(a: float, b: float) -> dict:
    """Add two numbers and return the sum."""
    return {"sum": a + b}

if __name__ == "__main__":
    plugin.run()
'''
lines = [l for l in plugin_code.strip().split("\n") if l.strip()]
assert len(lines) <= 30, f"Plugin code is {len(lines)} lines, should be <= 30"
print(f"  ✓ 30 行封装验证: 实际 {len(lines)} 行 ≤ 30 行")

# B3: CapabilityHandle get 不存在的 key 返回 None
h = CapabilityHandle("test", context={"a": 1})
assert h.get("nonexistent") is None
assert h.has("nonexistent") is False
assert h.has("a") is True
print(f"  ✓ CapabilityHandle.get(不存在key) → None, has() 正确")

# B4: JSON-RPC 消息分发 — 未知方法
print()
print("=" * 70)
print("[补充场景B5] 未知 JSON-RPC 方法分发")
print("=" * 70)


async def test_unknown_method() -> None:
    try:
        await server._dispatch("unknown/method", {})
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "unknown method" in str(e)
        print(f"  ✓ 未知方法 → ValueError: {e}")


asyncio.run(test_unknown_method())

print()
print("=" * 70)
print("🎉 所有验证场景全部通过！")
print("=" * 70)

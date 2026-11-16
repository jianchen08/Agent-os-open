# @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: python-test
"""MCP 暴露面 outputSchema 线格式合规：tools/list 不得因非 object 契约整组非法。

事故（logs/kernel.log.2026-10-16 起 183 次）：character_state 的
``character_state.history`` / ``character_state.render`` 在 manifest services 与
SDK 注册处声明 root ``type=array`` / ``type=string``；MCP 握手版本（2024-11-05..
2025-11-25）的 ``outputSchema`` 只允许根 ``type: "object"``。官方 runner 按协商
版本序列化 ``ListToolsResult`` 时 pydantic 报 ``literal_error``，整条 tools/list
以 -32603 应答——同宿主全部工具不可见，G2 观测连带失败。

契约（修复后）：非 object 的 output_schema 声明保留（tool_core 输出校验与
service 调用方都以它为准），但**不得**下发到 MCP ``outputSchema``；模型侧信息
仍由 description 的 ``Output contract:`` 摘要承载。
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest
from mcp_types import methods as mcp_methods

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.cohost import CohostServer

pytestmark = pytest.mark.unit

# 内核 MCP 客户端默认协商版本（kernel/crates/mcp/src/client.rs
# MCP_PROTOCOL_VERSION）；2024-11-05..2025-11-25 各握手版本的 outputSchema
# 线格式同为一个只允许根 type: "object" 的模型。
KERNEL_PROTOCOL_VERSION = "2024-11-05"

# 与 character_state 真实声明同形（manifest capabilities.services 与
# server.py 注册处各一份）。
ARRAY_OUTPUT = {"type": "array", "items": {"type": "object"}}
STRING_OUTPUT = {"type": "string"}
OBJECT_OUTPUT = {
    "type": "object",
    "required": ["card_id"],
    "properties": {"card_id": {"type": "string"}},
}
# 未写 type 的 schema：JSON Schema 合法，但同样不满足 MCP 握手版本对
# outputSchema 根 type 的强制要求（OutputSchema.type 必填）。
UNTYPED_OUTPUT = {"properties": {"card_id": {"type": "string"}}}


def _member(plugin_id: str, tools: dict[str, dict[str, Any]]) -> AgentOSPlugin:
    """构造声明指定 output_schema 的成员插件（handler 返回值不参与本组断言）。"""
    plugin = AgentOSPlugin(plugin_id)

    async def handler() -> dict[str, Any]:
        return {"ok": True}

    for name, output_schema in tools.items():
        plugin.register_tool(
            name,
            {"type": "object", "properties": {}},
            handler,
            f"{name} 描述",
            output_schema,
        )
    return plugin


def _aggregate_server() -> CohostServer:
    """合宿聚合面：成员工具名带 ``{plugin_id}.`` 前缀（与生产宿主同构）。"""
    return CohostServer(
        {
            "character_state": _member(
                "character_state",
                {
                    "character_state.history": ARRAY_OUTPUT,
                    "character_state.render": STRING_OUTPUT,
                    "character_state.get": OBJECT_OUTPUT,
                },
            ),
            "other": _member(
                "other",
                {"untyped_tool": UNTYPED_OUTPUT, "object_tool": OBJECT_OUTPUT},
            ),
        }
    )


async def _wire_tools() -> dict[str, dict[str, Any]]:
    """走官方 runner 同款序列化路径：handler 结果 → 按协商版本校验并转 wire。"""
    server = _aggregate_server()
    result = await server._server._on_list_tools(None, None)
    dumped = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    wire = mcp_methods.serialize_server_result("tools/list", KERNEL_PROTOCOL_VERSION, dumped)
    return {tool["name"]: tool for tool in wire["tools"]}


async def test_non_object_output_schema_keeps_tools_list_wire_valid() -> None:
    """修复前：serialize_server_result 抛 ValidationError（整组 tools/list 非法）。

    修复后：非 object 契约不下发 outputSchema，object 契约原样保留。
    """
    tools = await _wire_tools()

    for name in (
        "character_state.character_state.history",
        "character_state.character_state.render",
        "other.untyped_tool",
    ):
        assert "outputSchema" not in tools[name], f"{name}: 非 object 契约不得下发 outputSchema"

    # object 契约不得被误伤（暴露面只在根 type 非 object 时收口）
    assert tools["character_state.character_state.get"]["outputSchema"] == OBJECT_OUTPUT
    assert tools["other.object_tool"]["outputSchema"] == OBJECT_OUTPUT


async def test_non_object_contract_stays_model_visible_in_description() -> None:
    """不下发 outputSchema 不等于丢信息：description 摘要承载同一份输出契约。"""
    server = _aggregate_server()
    result = await server._server._on_list_tools(None, None)
    tools = {tool.name: tool for tool in result.tools}

    for name, schema in (
        ("character_state.character_state.history", ARRAY_OUTPUT),
        ("character_state.character_state.render", STRING_OUTPUT),
    ):
        contract = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        assert tools[name].description.endswith(f"Output contract: {contract}")


# ── 真实 stdio 端到端：镜像内核 McpClient（KernelHarness 复用官方协议对端）──
SIDECAR_SCRIPT = """
import asyncio

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.cohost import CohostServer

character_state = AgentOSPlugin("character_state")


@character_state.tool(
    name="character_state.history",
    schema={"type": "object", "properties": {"card_id": {"type": "string"}}, "required": ["card_id"]},
    description="查询最近 N 条变更历史",
    output_schema={"type": "array", "items": {"type": "object"}},
)
async def history(card_id: str) -> list[dict]:
    return [{"seq": 1, "card_id": card_id}]


@character_state.tool(
    name="character_state.render",
    schema={"type": "object", "properties": {"card_id": {"type": "string"}}, "required": ["card_id"]},
    description="渲染状态文本块",
    output_schema={"type": "string"},
)
async def render(card_id: str) -> str:
    return f"{card_id}: mood=b"


object_member = AgentOSPlugin("object_member")


@object_member.tool(
    name="object_tool",
    schema={"type": "object", "properties": {}},
    description="object 输出工具",
    output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
)
async def object_tool() -> dict:
    return {"ok": True}


server = CohostServer({"character_state": character_state, "object_member": object_member})

if __name__ == "__main__":
    asyncio.run(server.serve())
"""


@pytest.fixture()
def cohost_harness(tmp_path: Any) -> Any:
    """真实子进程 + stdio 拉起合宿服务端，按内核默认版本完成握手。"""
    from test_official_mcp_e2e import KernelHarness

    script = tmp_path / "cohost_output_schema_sidecar.py"
    script.write_text(SIDECAR_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603 - 探针脚本内容为测试内常量，非外部输入
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
    )
    harness = KernelHarness(proc)
    try:
        harness.request(
            "initialize",
            {
                "protocolVersion": KERNEL_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentos", "version": "0.2.0"},
                "config": {},
            },
        )
        harness.notify("notifications/initialized", None)
        yield harness
    finally:
        harness.stop()


def test_tools_list_over_stdio_succeeds_with_non_object_declarations(cohost_harness: Any) -> None:
    """修复前：runner 返回 -32603（Handler returned an invalid result）→ 本断言失败。"""
    result = cohost_harness.request("tools/list")
    tools = {tool["name"]: tool for tool in result["tools"]}

    assert "outputSchema" not in tools["character_state.character_state.history"]
    assert "outputSchema" not in tools["character_state.character_state.render"]
    assert tools["object_member.object_tool"]["outputSchema"] == {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    assert 'Output contract: {"type":"array"' in tools["character_state.character_state.history"]["description"]


def test_service_calls_keep_raw_return_shape(cohost_harness: Any) -> None:
    """修复只动 tools/list 暴露面：array/string service 调用返回值形状不变。"""
    history = cohost_harness.request(
        "tools/call",
        {"name": "character_state.character_state.history", "arguments": {"card_id": "c1"}},
    )
    assert json.loads(history["content"][0]["text"]) == [{"seq": 1, "card_id": "c1"}]

    render = cohost_harness.request(
        "tools/call",
        {"name": "character_state.character_state.render", "arguments": {"card_id": "c1"}},
    )
    assert json.loads(render["content"][0]["text"]) == "c1: mood=b"

# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""external MCP 工具纳入动态类的定义面测试（方案 §11 批 4 判据 1/4/5）。

锁的可观察契约：

- **定义真值 = 活注册表**：受管 external MCP 目录（``entry == "mcp:external"``）
  是**零声明**——``plugin.json`` 里没有 ``capabilities.tools``，工具名与
  ``input_schema`` 只能来自握手 ``tools/list`` 观测导入的 ``ToolDescriptor``。
  故 ``resource_search`` 必须先识别这类目录，再按 ``plugin_id`` 从活注册表取
  该目录的工具名集，schema 经内核既有 ``tool-surface.schemas`` 能力取（
  ``GET /api/v1/tools`` 不投喂 schema）。

- **fail-closed**：schema 通道不可用 → 工具**仍列出**（它确实活着），但逐条记
  ``tool_schemas_unavailable`` 错误项，不给盲调入口；schema 缺失/非 object →
  同 ``input_schema_missing`` / ``input_schema_not_object`` 口径拒收。

- **不回归**：声明式工具（有 ``capabilities.tools``）路径逐字节不变——同一
  次搜索里声明式与 external 两类并存，各自按自己的真值来源取定义。

外部依赖（内核 ``GET /api/v1/tools`` 与 ``tool-surface.schemas``）按门禁口径
打桩：桩的响应形态取自内核实现（``routes.rs`` 的 ``tools_handler`` 与
``capability_router.rs`` 的 ``handle_tool_surface_schemas``），不手写期望值。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SERVER_MOD = "search_external_mcp_test_server"

#: 零声明 external MCP 受管目录的 manifest 形态（``loader/managed_mcp.rs``
#: ``build_external_mcp_manifest`` 合成结果的真值字段）。
_EXTERNAL_MANIFEST: dict[str, Any] = {
    "id": "mcp_demo",
    "name": "MCP demo",
    "description": "External MCP (config/mcp.json)",
    "version": "1.0.0",
    "plugin_type": "tool",
    "language": "external",
    "host_type": "sidecar",
    "entry": "mcp:external",
    "capabilities": {},
    "mcp": {"transport": "stdio", "endpoint": {"command": "npx", "args": ["demo"]}},
    "permissions": {},
    "priority": 30,
}


def _load_tool_cls() -> Any:
    if _SERVER_MOD in sys.modules:
        del sys.modules[_SERVER_MOD]
    spec = importlib.util.spec_from_file_location(_SERVER_MOD, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SERVER_MOD] = module
    spec.loader.exec_module(module)
    return module._load_resource_search_tool()


def _run(tool: Any, **inputs: Any) -> dict[str, Any]:
    result = asyncio.run(tool.execute(inputs))
    assert result.error is None, f"execute 意外失败: {result.error}"
    return result.output


def _live(name: str, plugin_id: str, description: str = "") -> dict[str, Any]:
    """``GET /api/v1/tools`` 活条目（``routes.rs`` tools_handler 字段形态）。"""
    return {
        "name": name,
        "description": description,
        "plugin_id": plugin_id,
        "category": "search",
        "source": "builtin",
    }


def _install_surface(items: list[dict[str, Any]]) -> None:
    async def stub() -> dict[str, Any]:
        return {"items": items, "total": len(items)}

    sys.modules["resource_search_tool_impl"].set_tool_surface_caller(stub)


def _install_schemas(catalog: dict[str, dict[str, Any]]) -> list[list[str]]:
    """``tool-surface.schemas`` 桩：按 ``handle_tool_surface_schemas`` 响应形态应答。

    catalog: ``{tool_name: {description, input_schema, output_schema, render}}``
    返回每次调用记录（收到 requested tool_ids 列表）供交互断言。
    """
    calls: list[list[str]] = []

    async def stub(names: list[str]) -> dict[str, Any]:
        calls.append(list(names))
        schemas = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": meta.get("description", ""),
                    "parameters": meta["input_schema"],
                },
            }
            for name, meta in catalog.items()
            if name in names and isinstance(meta.get("input_schema"), dict)
        ]
        contracts = {
            name: {
                "schema": meta.get("output_schema"),
                "render": meta.get("render"),
                "isolation_capable": False,
            }
            for name, meta in catalog.items()
            if name in names
        }
        return {"schemas": schemas, "contracts": contracts}

    sys.modules["resource_search_tool_impl"].set_tool_schemas_caller(stub)
    return calls


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """沙箱：一个零声明 external MCP 目录 + 一个声明式工具插件（两类并存）。"""
    mcp_dir = tmp_path / "plugins" / "mcp_demo"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / ".mcp-managed").write_text("generated from config/mcp.json", encoding="utf-8")
    (mcp_dir / "plugin.json").write_text(
        json.dumps(_EXTERNAL_MANIFEST), encoding="utf-8"
    )

    decl_dir = tmp_path / "plugins" / "declared"
    decl_dir.mkdir(parents=True)
    (decl_dir / "plugin.json").write_text(json.dumps({
        "id": "declared_plugin",
        "plugin_type": "tool",
        "capabilities": {"tools": [{
            "name": "declared_tool",
            "description": "a declared tool",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }]},
    }), encoding="utf-8")

    tool = _load_tool_cls()()
    impl = sys.modules["resource_search_tool_impl"]
    monkeypatch.setattr(impl, "_PROJECT_ROOT", tmp_path)
    _install_surface([
        _live("mcp_fetch", "mcp_demo", "fetch a url via MCP"),
        _live("mcp_query", "mcp_demo", "query a store via MCP"),
        _live("declared_tool", "declared_plugin", "a declared tool"),
    ])
    yield tool, impl
    impl.set_tool_surface_caller(None)
    impl.set_tool_schemas_caller(None)


class TestExternalMcpDefinitions:
    """零声明 external MCP 目录的工具定义取自活注册表 + tool-surface.schemas。"""

    def test_definitions_taken_from_live_registry(self, sandbox: Any) -> None:
        """工具名来自活注册表（manifest 里无声明），schema 来自 schemas 能力。"""
        rs, _impl = sandbox
        calls = _install_schemas({
            "mcp_fetch": {
                "description": "fetch a url via MCP",
                "input_schema": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
                "output_schema": {"type": "object"},
                "render": {"card": "web"},
            },
            "mcp_query": {
                "description": "query a store via MCP",
                "input_schema": {"type": "object", "properties": {}},
            },
        })

        out = _run(rs, resource_type="tool", query="mcp_", mode="detailed")

        defs = {d["name"]: d for d in out["tool_defs"]}
        assert set(defs) == {"mcp_fetch", "mcp_query"}
        # 真实 schema 保真（性质断言：结构逐键相等，不是空占位）
        assert defs["mcp_fetch"]["input_schema"]["required"] == ["url"]
        assert defs["mcp_fetch"]["output_schema"] == {"type": "object"}
        assert defs["mcp_fetch"]["render"]["card"] == "web"
        # 路由目标 = 受管目录 manifest id（内核可解析），不是目录名以外的猜测
        assert defs["mcp_fetch"]["plugin_id"] == "mcp_demo"
        # 来源标记：external MCP 定义面标 source="mcp"——call_tool 据此不补
        # 服务端上下文（标准 MCP server 严格校验未声明参数）
        assert defs["mcp_fetch"]["source"] == "mcp"
        assert out["tool_defs_errors"] == []
        # 取数走既有 schemas 能力（一次批量调用，参数是本目录工具名集）
        assert len(calls) == 1
        assert set(calls[0]) == {"mcp_fetch", "mcp_query"}

    def test_declared_and_external_coexist_without_regression(self, sandbox: Any) -> None:
        """同一搜索里声明式与 external 两类并存，各按自己的真值来源取定义。"""
        rs, _impl = sandbox
        _install_schemas({
            "mcp_fetch": {
                "description": "fetch a url via MCP",
                "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
            },
            "mcp_query": {
                "description": "query a store via MCP",
                "input_schema": {"type": "object", "properties": {}},
            },
        })

        out = _run(rs, resource_type="tool", query="*", mode="detailed")

        defs = {d["name"]: d for d in out["tool_defs"]}
        assert {"mcp_fetch", "mcp_query", "declared_tool"} <= set(defs)
        assert defs["declared_tool"]["plugin_id"] == "declared_plugin"
        assert defs["declared_tool"]["source"] == "code", "声明式工具来源标记不变"
        assert defs["mcp_fetch"]["source"] == "mcp"
        assert defs["declared_tool"]["input_schema"]["properties"]["q"]["type"] == "string"

    def test_schema_channel_unavailable_still_lists_rows(self, sandbox: Any) -> None:
        """schemas 通道未注入：工具仍列出（活着），逐条 fail-closed 记原因。"""
        rs, _impl = sandbox
        # 不装 schemas 桩 = 通道未接线

        out = _run(rs, resource_type="tool", query="mcp_", mode="detailed")

        assert out["tool_c"] == 2, "工具确实活着，行必须列出"
        assert out["tool_defs"] == []
        reasons = {e["name"]: e["reason"] for e in out["tool_defs_errors"]}
        assert reasons == {
            "mcp_fetch": "tool_schemas_unavailable",
            "mcp_query": "tool_schemas_unavailable",
        }
        # 声明式工具不受影响（自己的真值来源是 manifest 声明）
        assert _run(rs, resource_type="tool", query="declared")["tool_defs"]

    def test_non_object_schema_rejected(self, sandbox: Any) -> None:
        """schemas 能力的 parameters 非 object → 同 ``input_schema_not_object`` 拒收。"""
        rs, _impl = sandbox
        _install_schemas({
            "mcp_fetch": {
                "description": "fetch a url via MCP",
                "input_schema": {"type": "array", "items": {"type": "string"}},
            },
            "mcp_query": {
                "description": "query a store via MCP",
                "input_schema": {"type": "object", "properties": {}},
            },
        })

        out = _run(rs, resource_type="tool", query="mcp_", mode="detailed")

        defs = {d["name"] for d in out["tool_defs"]}
        assert defs == {"mcp_query"}
        reasons = {e["name"]: e["reason"] for e in out["tool_defs_errors"]}
        assert reasons == {"mcp_fetch": "input_schema_not_object"}

    def test_missing_schema_rejected(self, sandbox: Any) -> None:
        """工具在活注册表但 schemas 能力没给 parameters → ``input_schema_missing``。"""
        rs, _impl = sandbox
        _install_schemas({
            "mcp_query": {
                "description": "query a store via MCP",
                "input_schema": {"type": "object", "properties": {}},
            },
        })

        out = _run(rs, resource_type="tool", query="mcp_fetch", mode="detailed")

        assert out["tool_defs"] == []
        assert out["tool_defs_errors"] == [
            {"name": "mcp_fetch", "reason": "input_schema_missing"}
        ]

    def test_manifest_id_missing_yields_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """受管目录缺 ``id`` → 无路由目标，零产出（不猜目录名当 plugin_id）。"""
        mcp_dir = tmp_path / "plugins" / "mcp_demo"
        mcp_dir.mkdir(parents=True)
        broken = dict(_EXTERNAL_MANIFEST)
        broken.pop("id")
        (mcp_dir / "plugin.json").write_text(json.dumps(broken), encoding="utf-8")

        tool = _load_tool_cls()()
        impl = sys.modules["resource_search_tool_impl"]
        monkeypatch.setattr(impl, "_PROJECT_ROOT", tmp_path)
        _install_surface([_live("mcp_fetch", "mcp_demo")])
        try:
            out = _run(tool, resource_type="tool", query="mcp_", mode="detailed")
            assert out == {}
        finally:
            impl.set_tool_surface_caller(None)
            impl.set_tool_schemas_caller(None)

    def test_no_live_tools_yields_nothing(self, sandbox: Any) -> None:
        """受管目录在活注册表里没有任何工具（宿主未起/导入失败）→ 零产出。"""
        rs, impl = sandbox
        _install_surface([_live("declared_tool", "declared_plugin")])

        out = _run(rs, resource_type="tool", query="mcp_", mode="detailed")

        assert out == {}

# @feature: FP-0.2.二 工具面声明与动态定义集 | @vision: V2 全能闭环 | @ci: python-coverage
"""per-agent 动态面工具否决（tool_deny）行为测试。

锁的可观察契约（ADR 2026-11-18-agent-dynamic-tool-deny-list）：

- **resource_search 搜不到**：``_tool_deny``（服务端注入、模型不可伪造）命中的
  工具名从 tool 表格行、``tool_defs``、``tool_defs_errors`` 全部缺席——认知面
  干净，模型不浪费轮次尝试被否决工具。
- **call_tool 调不动**：目标名命中 deny 即显式失败（``TOOL_DENIED``），即使其
  定义已被引入——防「先搜后否决」竞态与绕过，fail-closed。
- **无 deny 键行为不变**：缺席语义 = 无否决，既有面不受影响。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])
for _p in (_THIS_DIR, _SHARED_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import call_tool as ct  # noqa: E402
import tool as search_tool_mod  # noqa: E402

from agentos_plugin_sdk import ToolExecutionResult  # noqa: E402


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _def(name: str, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "description": f"工具 {name}",
        "input_schema": {"type": "object", "properties": {}},
        "plugin_id": "p1",
    }
    item.update(overrides)
    return item


def _success_payload(result: ToolExecutionResult) -> dict[str, Any]:
    assert result.success, f"预期成功: {result.error}"
    output = result.output
    return output if isinstance(output, dict) else {}


@pytest.fixture(autouse=True)
def _clear_channels():
    ct.set_state_reader(None)
    ct.set_tool_invoker(None)
    yield
    ct.set_state_reader(None)
    ct.set_tool_invoker(None)


class TestResourceSearchDenyFilter:
    """resource_search 工具面按 ``_tool_deny`` 过滤（注册表=外部依赖打桩）。"""

    @staticmethod
    def _stub_registry(monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_search_tools(*_a: Any, **_k: Any) -> tuple:
            names = ["alpha", "beta", "gamma"]
            descs = [f"desc-{n}" for n in names]
            schemas = [{"type": "object"} for _ in names]
            tool_defs = [_def(n) for n in names]
            def_errors = [{"name": "gamma", "reason": "缺 input_schema"}]
            return names, descs, schemas, tool_defs, def_errors

        monkeypatch.setattr(
            search_tool_mod.ResourceSearchTool, "_search_tools", fake_search_tools
        )

    def test_denied_names_absent_everywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """deny 命中：表格行、tool_defs、def_errors 三面全缺席。"""
        self._stub_registry(monkeypatch)
        plugin = search_tool_mod.ResourceSearchTool()
        res = _run(
            plugin.execute(
                {
                    "resource_type": "tool",
                    "query": "",
                    "mode": "detailed",
                    "_tool_deny": ["beta", "gamma"],
                }
            )
        )
        data = _success_payload(res)
        row_names = [row[0] for row in data.get("tool_d", [])]
        assert row_names == ["alpha"], f"deny 后应只剩 alpha，实际: {row_names}"
        assert [d["name"] for d in data.get("tool_defs", [])] == ["alpha"]
        assert data.get("tool_defs_errors") == [], "被否决工具的拒收条目也不得泄露存在性"
        assert data.get("tool_c") == 1
        assert data.get("tool_defs_count") == 1

    def test_no_deny_key_keeps_full_surface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无 _tool_deny 键 = 缺席语义 = 无否决，面不变（防回归）。"""
        self._stub_registry(monkeypatch)
        plugin = search_tool_mod.ResourceSearchTool()
        res = _run(plugin.execute({"resource_type": "tool", "query": "", "mode": "detailed"}))
        data = _success_payload(res)
        assert [d["name"] for d in data.get("tool_defs", [])] == ["alpha", "beta", "gamma"]
        assert data.get("tool_defs_errors") == [
            {"name": "gamma", "reason": "缺 input_schema"}
        ]

    def test_empty_deny_keeps_full_surface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式空 deny 列表 = 无否决（空列表与缺席同义，不收紧面）。"""
        self._stub_registry(monkeypatch)
        plugin = search_tool_mod.ResourceSearchTool()
        res = _run(
            plugin.execute(
                {"resource_type": "tool", "query": "", "mode": "detailed", "_tool_deny": []}
            )
        )
        data = _success_payload(res)
        assert data.get("tool_c") == 3


class TestCallToolDenyGate:
    """call_tool 门禁：deny 命中即拒，即使定义已引入。"""

    def _introduce(self) -> dict[str, Any]:
        return _def(
            "beta",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
        )

    def test_denied_target_rejected_even_if_introduced(self) -> None:
        """定义在场 + deny 命中 → TOOL_DENIED，不转发执行。"""
        forwarded: list[tuple[str, dict[str, Any]]] = []

        async def invoker(name: str, args: dict[str, Any], _plugin: str) -> dict[str, Any]:
            forwarded.append((name, dict(args)))
            return {"ok": True}

        ct.set_tool_invoker(invoker)
        res = _run(
            ct.CallTool().execute(
                {
                    "tool": "beta",
                    "arguments": {"x": "1"},
                    "_dynamic_tools": [self._introduce()],
                    "_tool_deny": ["beta"],
                }
            )
        )
        assert not res.success
        assert res.error_code == "TOOL_DENIED"
        assert forwarded == [], "deny 命中必须拦截在转发之前"

    def test_undenied_target_passes(self) -> None:
        """deny 未命中 → 正常转发（既有行为不受 deny 机制影响）。"""
        async def invoker(name: str, args: dict[str, Any], _plugin: str) -> dict[str, Any]:
            return {"ok": True, "echo": args.get("x")}

        ct.set_tool_invoker(invoker)
        res = _run(
            ct.CallTool().execute(
                {
                    "tool": "beta",
                    "arguments": {"x": "1"},
                    "_dynamic_tools": [self._introduce()],
                    "_tool_deny": ["alpha"],
                }
            )
        )
        assert res.success
        assert res.output.get("tool_name") == "beta"

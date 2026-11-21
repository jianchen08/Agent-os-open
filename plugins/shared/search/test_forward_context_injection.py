# @feature: FP-0.2.二 工具面声明与动态定义集 | @vision: V2 全能闭环 | @ci: python-coverage
"""call_tool 转发链的**纯透传**契约（2026-11-18 用户裁定）。

壳的职责 = 读动态定义（准入/否决）→ 解析 ``arguments`` → 转发 → 归一信封。
锁的可观察契约：

- **不注入**：环境上下文由管道注入级 ``tool_args_inject`` 前瞻写进嵌套
  ``arguments``；壳对入参顶层的同名键一律不转发（无第二份注入键表）。
- **不校验**：参数解析为对象后原样透传，参数合法性终审归目标工具自身
  handler/schema；壳不做 ``input_schema`` 校验（重造必漂移）。
- **线形解析**：``arguments`` 为 dict 直接透传；JSON 字符串解析为 dict；
  非对象/解析失败 → ``INVALID_ARGUMENTS``（不转发）。
- **黑名单门禁**：``tool_deny`` 否决集在转发前拒绝（本壳唯一校验面）；
  权限/危险命令黑名单由 security_check/level_guard 按目标名展开。
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


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _tool_def(
    name: str,
    props: dict[str, Any],
    required: list[str] | None = None,
    *,
    source: str = "code",
) -> dict[str, Any]:
    return {
        "name": name,
        "description": "目标工具",
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required or [],
            "additionalProperties": False,
        },
        "plugin_id": "p1",
        "source": source,
    }


_SEARCH_DEF = _tool_def("enhanced_search", {"query": {"type": "string"}}, required=["query"])
_MCP_DEF = _tool_def("mcp_fetch", {"url": {"type": "string"}}, required=["url"], source="mcp")
_FS_DEF = _tool_def(
    "FileSystem",
    {"mode": {"type": "string"}, "path": {"type": "string"}},
    required=["mode", "path"],
)


@pytest.fixture(autouse=True)
def _clear_channels():
    ct.set_state_reader(None)
    ct.set_tool_invoker(None)
    yield
    ct.set_state_reader(None)
    ct.set_tool_invoker(None)


def _capture_invoker(captured: dict[str, Any]) -> Any:
    async def invoker(name: str, args: dict[str, Any], _plugin: str) -> dict[str, Any]:
        captured.update(args)
        return {"ok": True}

    return invoker


class TestPurePassthrough:
    def test_wrapper_ignores_top_level_ambient_context(self) -> None:
        """壳不转发入参顶层的环境键——注入是注入方的职责，不是壳的。"""
        captured: dict[str, Any] = {}
        ct.set_tool_invoker(_capture_invoker(captured))
        res = _run(
            ct.CallTool().execute(
                {
                    "tool": "enhanced_search",
                    "arguments": {"query": "q"},
                    "_dynamic_tools": [_SEARCH_DEF],
                    "session_id": "s-1",
                    "workspace": "D:/ws/task-1",
                    "task_id": "t-1",
                }
            )
        )
        assert res.success
        assert captured == {"query": "q"}, "壳不得二次注入环境键"

    def test_arguments_forwarded_verbatim_without_schema_checks(self) -> None:
        """未声明键/缺必填/类型不符一律原样透传——壳不做 input_schema 校验。"""
        captured: dict[str, Any] = {}
        ct.set_tool_invoker(_capture_invoker(captured))
        args = {"query": 123, "bogus": 1, "_container_id": "ctr-1"}
        res = _run(
            ct.CallTool().execute(
                {"tool": "enhanced_search", "arguments": args, "_dynamic_tools": [_SEARCH_DEF]}
            )
        )
        assert res.success, f"透传不得被 schema 校验拦下: {res.error}"
        assert captured == args, "参数必须逐键原样到达目标"

    def test_missing_required_forwarded_target_decides(self) -> None:
        """缺必填不拦：目标自身 handler/schema 终审（壳不越权判定）。"""
        captured: dict[str, Any] = {}
        ct.set_tool_invoker(_capture_invoker(captured))
        res = _run(
            ct.CallTool().execute(
                {"tool": "FileSystem", "arguments": {"mode": "list"}, "_dynamic_tools": [_FS_DEF]}
            )
        )
        assert res.success
        assert captured == {"mode": "list"}

    def test_json_string_arguments_parsed_then_forwarded(self) -> None:
        """arguments 为 JSON 字符串 → 解析为对象后透传。"""
        captured: dict[str, Any] = {}
        ct.set_tool_invoker(_capture_invoker(captured))
        res = _run(
            ct.CallTool().execute(
                {
                    "tool": "FileSystem",
                    "arguments": '{"mode": "list", "path": "D:/ws"}',
                    "_dynamic_tools": [_FS_DEF],
                }
            )
        )
        assert res.success
        assert captured == {"mode": "list", "path": "D:/ws"}

    def test_non_object_arguments_rejected(self) -> None:
        """arguments 非对象且不可解析为对象 → INVALID_ARGUMENTS（不转发）。"""
        captured: dict[str, Any] = {}
        ct.set_tool_invoker(_capture_invoker(captured))
        for bad in ("not-json", "[1,2]", 42, None):
            res = _run(
                ct.CallTool().execute(
                    {"tool": "FileSystem", "arguments": bad, "_dynamic_tools": [_FS_DEF]}
                )
            )
            assert res.success is False
            assert res.error_code == "INVALID_ARGUMENTS", f"bad={bad!r}"
            assert captured == {}

    def test_mcp_target_forwards_model_args_only(self) -> None:
        """source=mcp：注入方跳过注入，壳转发参数 = 模型给的目标参数。"""
        captured: dict[str, Any] = {}
        ct.set_tool_invoker(_capture_invoker(captured))
        res = _run(
            ct.CallTool().execute(
                {
                    "tool": "mcp_fetch",
                    "arguments": {"url": "https://example.invalid"},
                    "_dynamic_tools": [_MCP_DEF],
                    "session_id": "s-1",
                    "workspace": "D:/ws",
                }
            )
        )
        assert res.success
        assert captured == {"url": "https://example.invalid"}

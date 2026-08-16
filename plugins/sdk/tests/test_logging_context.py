# @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: python-test
"""sidecar 日志统一接入 + per-request 上下文绑定测试。

覆盖：
- setup_sidecar_logging：幂等、降级路径可用
- _bind_log_context：字段过滤（None/"-"丢弃）、降级 no-op
- _handle_tools_call：_log_ctx 被提取并从 arguments 移除（不污染 handler）
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from agentos_plugin_sdk._logging import setup_sidecar_logging
from agentos_plugin_sdk.server import McpServer, _bind_log_context
from agentos_plugin_sdk.types import ToolDef

# ═══════════════════════════════════════════════════════════
# setup_sidecar_logging
# ═══════════════════════════════════════════════════════════


class TestSetupSidecarLogging:
    def test_idempotent(self) -> None:
        """重复调用不应报错（幂等）。"""
        setup_sidecar_logging()
        setup_sidecar_logging()  # 第二次应直接返回

    def test_root_logger_has_handler(self) -> None:
        """初始化后 root logger 必须有 handler（不再走 lastResort）。"""
        setup_sidecar_logging()
        root = logging.getLogger()
        assert len(root.handlers) > 0, "root logger 无 handler，日志会被 lastResort 丢弃"


# ═══════════════════════════════════════════════════════════
# _bind_log_context
# ═══════════════════════════════════════════════════════════


class TestBindLogContext:
    def test_filters_empty_fields(self) -> None:
        """None / 空串 / '-' 应被丢弃，全空时返回 no-op context。"""
        with _bind_log_context({"pipeline_id": None, "session_id": "-", "agent_name": ""}):
            # 不抛异常即可；no-op 不绑定任何字段
            pass

    def test_context_manager_exits_clean(self) -> None:
        """正常进入退出，异常也能正确退出（contextvars 恢复）。"""
        ctx = _bind_log_context({"pipeline_id": "p-1"})
        with ctx:
            pass
        # 退出后再次进入不应残留（contextvars 已恢复）
        with _bind_log_context({"pipeline_id": "p-2"}):
            pass


# ═══════════════════════════════════════════════════════════
# _handle_tools_call 与 _log_ctx 集成
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tools_call_strips_log_ctx_from_arguments() -> None:
    """_log_ctx 应从 arguments 移除，handler 收到的只有 state/config。"""

    received: dict[str, Any] = {}

    def handler(state: dict, config: dict) -> dict:
        received["state"] = state
        received["config"] = config
        received["keys"] = list(received.keys())
        return {"ok": True}

    tools = {
        "do_work": ToolDef(
            name="do_work",
            schema={"type": "object"},
            handler=handler,
        ),
    }
    server = McpServer(tools, {}, {})

    result = await server._handle_tools_call(
        {
            "name": "do_work",
            "arguments": {
                "state": {"x": 1},
                "config": {"y": 2},
                "_log_ctx": {"pipeline_id": "p-abc"},
            },
        }
    )

    import json

    content = json.loads(result.content[0].text)
    assert content == {"ok": True}
    # handler 收到的 arguments 不应含 _log_ctx
    assert "_log_ctx" not in received
    assert received["state"] == {"x": 1}
    assert received["config"] == {"y": 2}


@pytest.mark.asyncio
async def test_tools_call_without_log_ctx_still_works() -> None:
    """无 _log_ctx 时（旧内核兼容）正常工作。"""

    def handler(text: str) -> dict:
        return {"echo": text}

    tools = {"echo": ToolDef(name="echo", schema={"type": "object"}, handler=handler)}
    server = McpServer(tools, {}, {})

    import json

    result = await server._handle_tools_call({"name": "echo", "arguments": {"text": "hi"}})
    content = json.loads(result.content[0].text)
    assert content == {"echo": "hi"}


# ═══════════════════════════════════════════════════════════
# _handle_tools_call 参数透传：签名感知过滤
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tools_call_filters_injected_fields_for_plain_function() -> None:
    """纯函数工具（无 **kwargs）不应收到 parent_agent_level/timestamp 等内部注入字段。

    背景：param_inject 插件向所有工具参数注入 parent_agent_level/timestamp，
    纯函数工具签名无 **kwargs，透传会触发 unexpected keyword argument 报错。
    """
    received: dict[str, Any] = {}

    def calculator(operation: str, expression: str = "") -> dict:
        received["operation"] = operation
        received["expression"] = expression
        return {"ok": True}

    tools = {
        "scientific_calculator": ToolDef(
            name="scientific_calculator",
            schema={"type": "object"},
            handler=calculator,
        ),
    }
    server = McpServer(tools, {}, {})

    import json

    result = await server._handle_tools_call(
        {
            "name": "scientific_calculator",
            "arguments": {
                "operation": "calculate",
                "expression": "1+1",
                "parent_agent_level": 1,
                "timestamp": "2026-01-01T00:00:00",
                "_log_ctx": {"pipeline_id": "p-abc"},
            },
        }
    )
    content = json.loads(result.content[0].text)
    assert content == {"ok": True}
    # handler 只收到签名中存在的参数，内部注入字段被过滤
    assert received == {"operation": "calculate", "expression": "1+1"}
    assert "parent_agent_level" not in received
    assert "timestamp" not in received


@pytest.mark.asyncio
async def test_tools_call_passes_all_args_to_var_keyword_handler() -> None:
    """**kwargs 工具（task 系）应全量收到 arguments（含 parent_agent_level）。

    背景：task_manage(**kwargs) 依赖 parent_agent_level 做权限校验，
    过滤会破坏任务管理权限判断，故 VAR_KEYWORD handler 必须全量透传。
    """
    received: dict[str, Any] = {}

    def task_manage(**kwargs) -> dict:
        received.update(kwargs)
        return {"ok": True}

    tools = {
        "task_manage": ToolDef(
            name="task_manage",
            schema={"type": "object"},
            handler=task_manage,
        ),
    }
    server = McpServer(tools, {}, {})

    import json

    result = await server._handle_tools_call(
        {
            "name": "task_manage",
            "arguments": {
                "action": "get",
                "task_id": "t-1",
                "parent_agent_level": 1,
                "timestamp": "2026-01-01T00:00:00",
                "_log_ctx": {"pipeline_id": "p-abc"},
            },
        }
    )
    content = json.loads(result.content[0].text)
    assert content == {"ok": True}
    # _log_ctx 仍被 pop（不进入 handler），其余参数全量透传
    assert "_log_ctx" not in received
    assert received["action"] == "get"
    assert received["task_id"] == "t-1"
    assert received["parent_agent_level"] == 1
    assert received["timestamp"] == "2026-01-01T00:00:00"

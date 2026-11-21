# @feature: FP-0.2.一 插件协议 | @ci: python-coverage
"""env_face（执行环境自述与修复提示）+ 工具契约增强单测。

背景（用户裁定 2026-11-15）：工具失败返回必须自包含「在哪执行 + 这错意味
着什么 + 怎么修」——单点在 SDK 工具分发异常出口（server.py），全部 sidecar
工具受益；不注入提示词，执行面信息按需从失败返回获得。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentos_plugin_sdk import server as sdk_server
from agentos_plugin_sdk.env_face import (
    describe_execution_face,
    enrich_error_message,
    fix_hints_for_error,
)

# ═══════════════ describe_execution_face ═══════════════


def test_face_has_supply_and_sandbox_sections() -> None:
    face = describe_execution_face()
    assert "platform" in face
    assert face["sandbox"]["mnt_masked"] in (True, False)
    assert "llm_provider_config" in face["supply"]
    assert "model_keys" in face["supply"]
    assert "kernel_binaries" in face["supply"]


# ═══════════════ fix_hints_for_error ═══════════════


@pytest.mark.parametrize(
    "error_text,expected_frag",
    [
        ("provider ? 缺 api_base（模型 gpt-4）", "未在 provider 配置定义"),
        ("No such file or directory: /mnt/d/agentos-r297-main", "tmpfs 遮蔽"),
        ("无法执行 agentos-kernel.exe（Exec format error）", "平台错配"),
        ("HTTPConnection(host='127.0.0.1', port=9101): Max retries exceeded", "WSL NAT"),
        ("No module named 'agentos_plugin_sdk'", "Python 环境缺位"),
    ],
)
def test_fix_hints_match_known_symptoms(error_text: str, expected_frag: str) -> None:
    hints = fix_hints_for_error(error_text)
    assert hints, f"已知症状必须命中: {error_text}"
    assert expected_frag in hints[0]


def test_fix_hints_unknown_symptom_returns_empty() -> None:
    assert fix_hints_for_error("完全无关的错误文本 xyzzy") == []


# ═══════════════ enrich_error_message ═══════════════


def test_enrich_appends_targeted_detailed_diagnosis() -> None:
    msg = enrich_error_message("RuntimeError: provider ? 缺 api_base（模型 gpt-4）")
    assert msg.startswith("原错误: RuntimeError: provider ? 缺 api_base")  # 原文保留
    assert "DIAGNOSIS: 模型 gpt-4 未在 provider 配置定义" in msg
    assert "models 表可用模型" in msg  # 定向核对：真读配置列出可用模型
    assert "user_root/.env" in msg  # 密钥供给核对
    assert "修复（二选一）" in msg


def test_enrich_unknown_symptom_falls_back_to_generic() -> None:
    msg = enrich_error_message("ValueError: xyzzy")
    assert "原错误: ValueError: xyzzy" in msg
    assert "未命中已知症状表" in msg
    assert "熔断上报" in msg


# ═══════════════ 工具契约增强：分发异常出口单点 ═══════════════


class _FakeServer(sdk_server.McpServer):
    """绕过 __init__（不起 MCP/桥），仅装工具表供分发测试。"""


def _make_server_with_tool(handler: Any) -> _FakeServer:
    srv = _FakeServer.__new__(_FakeServer)
    srv._tools = {
        "boom": type(
            "TD",
            (),
            {"handler": handler, "schema": {"type": "object", "properties": {}}},
        )(),
    }
    srv._on_call_config = None
    srv._handler_locks = {}
    srv._handler_lock = lambda key: _NullCtx()  # type: ignore[method-assign]
    return srv


class _NullCtx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *a: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_tool_exception_result_carries_env_face_and_fix() -> None:
    """工具 handler 抛异常 → 返回 is_error 结果，含执行面档案与命中 FIX。"""

    def handler(state: Any = None) -> Any:
        raise RuntimeError("provider ? 缺 api_base（模型 gpt-4）")

    srv = _make_server_with_tool(handler)
    result = await srv._handle_tools_call({"name": "boom", "arguments": {}})
    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["success"] is False
    assert payload["error_code"] == "TOOL_EXECUTION_FAILED"
    # 错误原文保留 + 执行面档案 + 命中 FIX（缺 key 症状 → user_root/.env 提示）
    assert payload["error"].startswith("原错误: RuntimeError: provider ? 缺 api_base")
    assert "DIAGNOSIS: 模型 gpt-4 未在 provider 配置定义" in payload["error"]
    assert "修复（二选一）" in payload["error"]


@pytest.mark.asyncio
async def test_tool_success_result_unmodified() -> None:
    """成功路径零变化（增强只挂失败出口）。"""

    def handler(state: Any = None) -> Any:
        return {"success": True, "output": "ok"}

    srv = _make_server_with_tool(handler)
    result = await srv._handle_tools_call({"name": "boom", "arguments": {}})
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["success"] is True
    assert "DIAGNOSIS:" not in result.content[0].text

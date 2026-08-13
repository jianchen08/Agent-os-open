# @feature: trigger_setup schema 同步 | @ci: python-plugins-test
"""回归：trigger_setup 的 MCP schema 必须与后端 Tool 定义一致。

历史 bug：server.py 注册的是残桩 schema（仅 action/trigger_type/config），
导致 message/interval 等 LLM 必填参数在分发层被丢，execute() 误报
「缺少注入参数: pipeline_id」。修复后直接复用 get_tool_definition().input_schema。
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

pytestmark = pytest.mark.unit


def _load_plugin():
    # 多个工具插件都有 `tool.py`（task/tool.py、triggers_ext/tool.py 等）。
    # 其他测试文件（如 tools/task/test_task_manage.py）先被收集时会把其 tool 模块
    # 缓存在 sys.modules['tool']——不清除会让 server.py 的 `from tool import ...`
    # 命中错误缓存。这里先弹出旧缓存，再确保本目录优先，保证解析到本插件 tool。
    sys.modules.pop("tool", None)
    sys.path.insert(0, _HERE)
    import server  # noqa: PLC0415  由上方 sys.path 注入解析

    return server.plugin


def test_trigger_setup_schema_has_message_and_interval() -> None:
    plugin = _load_plugin()
    td = plugin._tools["trigger_setup"]
    props = td.schema["properties"]
    # LLM 必填的关键字段必须出现在 MCP 暴露的 schema 里
    assert "message" in props, "message 缺失——残桩 schema 未同步"
    assert "interval" in props
    assert "trigger_type" in props
    assert td.schema["required"] == ["trigger_type", "message"]


def test_trigger_setup_schema_excludes_injected_and_stub_fields() -> None:
    plugin = _load_plugin()
    td = plugin._tools["trigger_setup"]
    props = td.schema["properties"]
    # pipeline_id/execution_id 是 injected_params（系统注入），不应暴露给 LLM
    assert "pipeline_id" not in props
    assert "execution_id" not in props
    # 旧残桩的 config 字段必须消失
    assert "config" not in props


def test_trigger_setup_schema_matches_backend_definition() -> None:
    """MCP schema 应与后端 Tool.get_tool_definition().input_schema 完全一致。"""
    plugin = _load_plugin()
    # _load_plugin 已确保 sys.modules['tool'] 是本插件的 tool（弹出旧缓存）
    from tool import TriggerSetupTool  # noqa: PLC0415

    backend = TriggerSetupTool.get_tool_definition().input_schema
    exposed = plugin._tools["trigger_setup"].schema
    assert exposed == backend, "MCP schema 与后端 input_schema 不一致"

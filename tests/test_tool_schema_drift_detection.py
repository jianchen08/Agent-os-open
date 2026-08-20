# @feature: FP-0.2.二 工具面加载期漂移检测 | @ci: none-local
"""tool_schema 加载期漂移检测测试（2026-08-20 F5）。

锁一件事：agent tool_ids 引用了注册表不存在的工具时，组装期必须报警暴露
（配置错误/注册异常守门闸）——实测背景：task_manage 被 G2 启动闸净化后，
LLM 工具面 11→9 静默缩面，模型照提示词调用不存在的工具报
"plugin not found"，全程无人知晓漂移发生。

[来源: docs/working/管道执行bug修复方案_20260820.md F5]
"""

from __future__ import annotations

import asyncio
import logging

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "tool_schema")
import plugin as tool_schema_mod  # noqa: E402


def _ctx(state: dict):
    from pipeline.plugin import PluginContext

    return PluginContext(state=state, config={})


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_drift_warns_on_missing_tool_ids(caplog):
    """tool_ids 引用缺失工具（被净化/未启用/写错名）→ 组装期 warning 报警。"""
    state = {
        # 无 tool_registry 服务 → 走内核注入 schema 路径（sidecar 现实路径）。
        # 含一个不在 tool_ids 的工具（memory）使过滤真正发生（全命中时
        # 走"不覆盖"路径返回 {}，属内核注入保留语义）。
        "tool_schemas": [_schema("file_read"), _schema("task_submit"), _schema("memory")],
        "tool_ids": ["file_read", "task_manage", "task_submit"],
    }
    plugin = tool_schema_mod.ToolSchemaPlugin(config={})
    with caplog.at_level(logging.WARNING, logger=plugin.name):
        updates = asyncio.run(plugin.execute(_ctx(state))).state_updates

    assert "tool_schemas" in updates
    names = {(s.get("function") or {}).get("name") for s in updates["tool_schemas"]}
    assert names == {"file_read", "task_submit"}
    drift_logs = [r for r in caplog.records if "工具面漂移" in r.message]
    assert drift_logs, "引用缺失工具必须报警暴露，不许静默缩面"
    assert "task_manage" in drift_logs[0].getMessage()


def test_no_drift_no_warning(caplog):
    """tool_ids 全部在注册表 → 无漂移报警（防误报噪音）。"""
    state = {
        "tool_schemas": [_schema("file_read"), _schema("task_submit")],
        "tool_ids": ["file_read", "task_submit"],
    }
    plugin = tool_schema_mod.ToolSchemaPlugin(config={})
    with caplog.at_level(logging.WARNING, logger=plugin.name):
        asyncio.run(plugin.execute(_ctx(state)))
    assert not [r for r in caplog.records if "工具面漂移" in r.message]


def test_empty_tool_ids_warns_config_break(caplog):
    """K10 配套：agent/state 完全无 tool_ids → warning（配置加载断链信号）。

    内核侧 K10 已改为无 tool_ids 不再全量注入；sidecar 同点报警，
    双侧对齐暴露"agent 配置没接上"而非静默全量/空面。
    """
    state = {
        "tool_schemas": [_schema("file_read"), _schema("task_submit")],
        # 无 tool_ids 键（context_build 未装载/agent yaml 断链）
    }
    plugin = tool_schema_mod.ToolSchemaPlugin(config={})
    with caplog.at_level(logging.WARNING, logger=plugin.name):
        updates = asyncio.run(plugin.execute(_ctx(state))).state_updates
    assert updates == {}, "无 tool_ids 时不覆盖内核注入（行为保持）"
    warn_logs = [r for r in caplog.records if "未声明 tool_ids" in r.message]
    assert warn_logs, "agent 无 tool_ids 必须告警（检查配置加载断链）"
    assert "断链" in warn_logs[0].getMessage()

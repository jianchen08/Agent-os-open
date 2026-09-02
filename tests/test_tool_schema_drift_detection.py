# @feature: FP-0.2.二 工具面加载期漂移检测 | @ci: python-coverage
"""tool_schema 工具面漂移检测测试（2026-08-20 F5，capability 通道版）。

锁一件事：agent tool_ids 引用了注册表不存在的工具时，组装期必须报警暴露
（配置错误/注册异常守门闸）——实测背景：task_manage 被 G2 启动闸净化后，
LLM 工具面 11→9 静默缩面，模型照提示词调用不存在的工具报
"plugin not found"，全程无人知晓漂移发生。

工具面来源：tool_schema 插件经内核 tool-surface capability 按 state.tool_ids
过滤注册表（内核零 agent 配置知识）；本测试用 capability 替身回放过滤结果。

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


def _caller_with(schemas: list[dict]):
    """tool-surface capability 轻量替身：回放脚本化过滤结果。"""
    calls: list[tuple] = []

    async def caller(method: str, params: dict, timeout=None):
        calls.append((method, params))
        return {"schemas": schemas, "contracts": {}}

    caller.calls = calls  # type: ignore[attr-defined]
    return caller


def test_drift_warns_on_missing_tool_ids(caplog):
    """tool_ids 引用注册表缺失工具（被净化/未启用/写错名）→ 组装期 warning 报警。"""
    caller = _caller_with(
        # 内核过滤结果缺 task_manage（被 G2 净化的同款场景）
        [_schema("file_read"), _schema("task_submit")]
    )
    # caller 挂实例（2026-09-02 合宿撕裂修复后收敛自 server.py get_instance()，
    # 模块级 set_capability_caller 已随旧实现退役）——实例本地注入即自洽。
    plugin = tool_schema_mod.ToolSchemaPlugin(config={})
    plugin.set_capability_caller(caller)
    state = {"tool_ids": ["file_read", "task_manage", "task_submit"]}
    with caplog.at_level(logging.WARNING, logger=plugin.name):
        updates = asyncio.run(plugin.execute(_ctx(state))).state_updates

    # 转发契约：短方法名 + state.tool_ids 原样透传内核
    assert caller.calls == [("schemas", {"tool_ids": ["file_read", "task_manage", "task_submit"]})]
    assert "tool_schemas" in updates
    names = {(s.get("function") or {}).get("name") for s in updates["tool_schemas"]}
    assert names == {"file_read", "task_submit"}
    drift_logs = [r for r in caplog.records if "工具面漂移" in r.message]
    assert drift_logs, "引用缺失工具必须报警暴露，不许静默缩面"
    assert "task_manage" in drift_logs[0].getMessage()


def test_no_drift_no_warning(caplog):
    """tool_ids 全部在注册表 → 无漂移报警（防误报噪音）。"""
    caller = _caller_with([_schema("file_read"), _schema("task_submit")])
    plugin = tool_schema_mod.ToolSchemaPlugin(config={})
    plugin.set_capability_caller(caller)
    state = {"tool_ids": ["file_read", "task_submit"]}
    with caplog.at_level(logging.WARNING, logger=plugin.name):
        asyncio.run(plugin.execute(_ctx(state)))
    assert not [r for r in caplog.records if "工具面漂移" in r.message]


def test_empty_tool_ids_warns_config_break(caplog):
    """K10 配套：state 完全无 tool_ids → warning（配置加载断链信号）+ 空工具面。

    agent 配置唯一事实源在 context_build 插件（按 agent yaml 注入
    state.tool_ids）；缺键 = 断链，fail-closed 置空而非全量兜底。
    """
    state = {
        # 无 tool_ids 键（context_build 未装载/agent yaml 断链）
    }
    plugin = tool_schema_mod.ToolSchemaPlugin(config={})
    with caplog.at_level(logging.WARNING, logger=plugin.name):
        updates = asyncio.run(plugin.execute(_ctx(state))).state_updates
    assert updates["tool_schemas"] == [], "断链空面（K10 不兜底全量）"
    assert "tool_output_contracts" in updates
    warn_logs = [r for r in caplog.records if "tool_ids" in r.message]
    assert warn_logs, "agent 无 tool_ids 必须告警（检查配置加载断链）"

#!/usr/bin/env python3
"""Trigger Setup 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

logger = logging.getLogger(__name__)

# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
# 触发器领域代码（triggers/）为本工具自有子包，位于本工具目录下，由上方
# sys.path 注入解析。

from http_api import handle_http_dispatch  # noqa: E402
from tool import (  # noqa: E402
    TriggerSetupTool,
    build_service_config,
    set_capability_provider,
)
from triggers.manager import get_trigger_manager  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from agentos_plugin_sdk.capability import FrontendEmitter  # noqa: E402

plugin = AgentOSPlugin("trigger_setup_tool")

# MCP 暴露的 schema 必须与后端 Tool.get_tool_definition() 一致——
# input_schema 本身不含 injected_params（pipeline_id/execution_id），由 param_inject
# 从 pipeline state 注入，故直接复用 input_schema 作为 LLM 可见 schema。
_TD = TriggerSetupTool.get_tool_definition()
_TRIGGER_SETUP_SCHEMA = _TD.input_schema
_TRIGGER_SETUP_DESCRIPTION = _TD.description


def _make_trigger_injector() -> Any:
    """构造 0.2 sidecar 触发消息投递器。

    到期触发时 TriggerManager 经此把消息投给内核 ``chat.send_message`` capability，
    复用前端 WS 派发路径唤醒 agent。能力句柄懒解析（在协程内 get_capability），
    即便 on_load 时机早于 capability 声明完成，也能在真正触发时拿到（或抛错由 manager 记录）。

    ``state`` 是可选的 state overlay（触发来源坐标），经 send_message 并入
    本轮管道 state，供 notify_judge 等步骤识别触发轮次。
    """

    async def _inject(
        pipeline_id: str,
        message: str,
        user_id: str,
        state: dict[str, Any] | None = None,
    ) -> Any:
        handle = plugin.get_capability("chat")
        params: dict[str, Any] = {
            "pipeline_id": pipeline_id,
            "message": message,
            "user_id": user_id,
        }
        if isinstance(state, dict) and state:
            params["state"] = state
        return await handle.call("send_message", params)

    return _inject


def _make_domain_emitter() -> Any:
    """构造触发完成域事件发出口（event-bus.emit_domain）。

    触发完成后广播 ``trigger.fired``，供事项域订阅方做周期再武装与观测。
    """

    async def _emit(event: str, tags: dict[str, Any]) -> Any:
        handle = plugin.get_capability("event-bus")
        return await handle.call("emit_domain", {"event": event, "tags": tags})

    return _emit


def _make_state_provider() -> Any:
    """构造 state 聚合行提供者（GAP-2 CONDITION 求值上下文）。

    经内核 ``pipeline-state`` capability 读管道 state 聚合（与
    /api/v1/pipelines/state 同构：扁平点号键行）。能力句柄懒解析
    （协程内 get_capability），读取失败由 manager 记录并跳过本轮。
    """

    async def _provide() -> list[dict[str, Any]]:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
        return rows if isinstance(rows, list) else []

    return _provide


def _make_state_writer() -> Any:
    """构造触发器注册表持久化写面（P25：state 是权威持久层）。

    经内核 ``pipeline-state`` capability 的 update 方法把触发器配置写到目标
    管道 state（``trigger.registry.<trigger_id>`` 扁平键，manifest
    ``state.writes`` 声明放行）——pipeline_state 表跨 sidecar 重启/迁移/回收
    存活，管道清理时随 state 天然 GC。能力句柄懒解析（协程内 get_capability），
    与 injector / state provider 同款。
    """

    async def _write(pipeline_id: str, fields: dict[str, Any]) -> None:
        handle = plugin.get_capability("pipeline-state")
        await handle.call("update", {"pipeline_id": pipeline_id, "fields": fields})

    return _write


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """sidecar 启动（主事件循环内）：接通触发检查循环 + 注入器 + 双桥 + 审批闸。

    1. set_main_loop 注入运行循环 → 触发器到期可调度；
    2. 注入器经内核 chat capability 投递触发消息；
    3. state provider 注入 → CONDITION 触发器有了求值上下文（state 聚合轮询）；
    4. 域事件桥就绪标记 → manifest 声明 domain_event hook + 下方
       ``_on_domain_event`` 处理器注册后，内核终态事件可达 evaluate_event；
    5. capability provider 注入 → trigger_setup 写入 command 类动作时经
       human-interaction 审批闸（经 human-interaction capability 反向调用）；
    6. state 写面注入 + 全量重灌 → sidecar 重启/迁移/回收后从管道 state
       恢复触发器注册表（P25 根治 P21/P24 内存态丢失）。
    """
    mgr = get_trigger_manager()
    mgr.set_main_loop(asyncio.get_running_loop())
    mgr.set_injector(_make_trigger_injector())
    mgr.set_state_provider(_make_state_provider())
    mgr.set_state_writer(_make_state_writer())
    mgr.set_domain_emitter(_make_domain_emitter())
    # WS 腿（X24-F1/P-71）：异常交付（failed/skipped）经 frontend.emit 推前端
    # 通知帧；from_plugin 对缺 frontend capability 内建降级（返回 None，只走
    # 域总线），不可能外抛。
    mgr.set_frontend_emitter(FrontendEmitter.from_plugin(plugin))
    mgr.set_event_bridge_ready()
    set_capability_provider(plugin.get_capability)
    try:
        restored = await mgr.load_from_state()
        logger.info("[triggers_ext] state 触发器重灌完成: %d 个", restored)
    except Exception as exc:  # noqa: BLE001 - 重灌失败不阻断启动，本轮以空注册表运行
        logger.error("[triggers_ext] 触发器 state 重灌失败（本轮以空注册表运行）: %s", exc)
    mgr.start_check_loop()


@plugin.on_unload
async def _on_unload(_params: dict[str, Any]) -> None:
    """sidecar 卸载：停止后台检查线程 + 复位审批闸/域事件出口。"""
    set_capability_provider(None)
    get_trigger_manager().set_domain_emitter(None)
    get_trigger_manager().set_frontend_emitter(None)
    get_trigger_manager().stop_check_loop()


@plugin.on_domain_event
async def _on_domain_event(params: dict[str, Any]) -> None:
    """域事件入口（GAP-2 EVENT 接线）。

    内核在 run 终态（completed/failed/suspended）经 broadcast_domain_event
    推送域事件（state 带 ``task.*`` 字段时派生 ``task_completed`` /
    ``task_failed``）；此处转发给 TriggerManager.evaluate_event 匹配触发器。
    """
    event_name = params.get("event") or ""
    if not event_name:
        return
    await get_trigger_manager().handle_domain_event(event_name, params)


@plugin.tool(
    name="trigger_setup",
    schema=_TRIGGER_SETUP_SCHEMA,
    description=_TRIGGER_SETUP_DESCRIPTION,
)
async def trigger_setup(**kwargs: dict[str, Any]) -> dict[str, Any]:
    t = TriggerSetupTool()
    result = await t.execute(kwargs)
    # 失败信封显式 success=False（ADR 2026-09-28 收编契约）——本工具
    # output_schema required 含 success，裸 error 载荷会被包成伪成功再撞校验。
    if not result.success:
        return {"success": False, "error": result.error}
    return result.output if isinstance(result.output, dict) else {"success": True}


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "query": {"type": "object", "additionalProperties": {"type": "string"}},
        },
    },
    description="HTTP endpoint handler for /ext/trigger_setup_tool/** (trigger management REST)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """REST 分发到 triggers 域端点（含 /pipelines 选项）。"""
    return await handle_http_dispatch(path, method, raw_body, query or {}, headers or {})


# ══ capability 服务面（trigger-control）══════════════════════════════
# 供 mode_personal 等事项域插件程序化注册/注销/迁移触发器；不经 LLM 工具面。
# 与 trigger_setup 工具的分工：工具面面向 LLM（7 天上限/5 类触发器/command
# 审批），服务面面向插件（schedule/delay notify，per-item 长地平线）。


@plugin.tool(
    name="trigger.register",
    schema={
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string"},
            "pipeline_id": {"type": "string"},
            "trigger_type": {"type": "string", "enum": ["schedule", "delay"]},
            "scheduled_at": {"type": "string"},
            "delay_seconds": {"type": "number"},
            "message": {"type": "string"},
            "name": {"type": "string"},
            "metadata": {"type": "object"},
            "max_fires": {"type": "integer"},
        },
        "required": ["pipeline_id", "message"],
    },
    description="注册触发器（capability 服务面；schedule/delay notify）",
)
async def trigger_register(
    pipeline_id: str = "",
    message: str = "",
    trigger_type: str = "schedule",
    scheduled_at: str = "",
    delay_seconds: float | None = None,
    name: str = "",
    trigger_id: str = "",
    metadata: dict[str, Any] | None = None,
    max_fires: int = 1,
) -> dict[str, Any]:
    config, error = build_service_config(
        {
            "pipeline_id": pipeline_id,
            "message": message,
            "trigger_type": trigger_type,
            "scheduled_at": scheduled_at,
            "delay_seconds": delay_seconds,
            "name": name,
            "trigger_id": trigger_id,
            "metadata": metadata or {},
            "max_fires": max_fires,
        }
    )
    if config is None:
        return {"success": False, "error": error}
    get_trigger_manager().register(config)
    return {
        "success": True,
        "trigger_id": config.trigger_id,
        "trigger_type": config.trigger_type.value,
    }


@plugin.tool(
    name="trigger.unregister",
    schema={
        "type": "object",
        "properties": {"trigger_id": {"type": "string"}},
        "required": ["trigger_id"],
    },
    description="注销触发器（capability 服务面）",
)
async def trigger_unregister(trigger_id: str = "") -> dict[str, Any]:
    if not trigger_id:
        return {"success": False, "error": "trigger_id 必填"}
    ok = get_trigger_manager().unregister(trigger_id)
    return {"success": ok, "trigger_id": trigger_id}


@plugin.tool(
    name="trigger.list",
    schema={
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string"},
            "pipeline_id": {"type": "string"},
            "item_id": {"type": "string"},
        },
    },
    description="列出触发器（capability 服务面；可按 trigger_id/pipeline_id/item_id 过滤）",
)
async def trigger_list(
    trigger_id: str = "", pipeline_id: str = "", item_id: str = ""
) -> dict[str, Any]:
    items = []
    for trigger in get_trigger_manager().list_all():
        if trigger_id and trigger.trigger_id != trigger_id:
            continue
        if pipeline_id and trigger.pipeline_id != pipeline_id:
            continue
        if item_id and str((trigger.metadata or {}).get("item_id") or "") != item_id:
            continue
        items.append(trigger.to_state_dict())
    return {"success": True, "items": items}


@plugin.tool(
    name="trigger.retarget",
    schema={
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string"},
            "pipeline_id": {"type": "string"},
        },
        "required": ["trigger_id", "pipeline_id"],
    },
    description="把触发器迁移到新目标管道（会话活跃管道漂移跟随）",
)
async def trigger_retarget(trigger_id: str = "", pipeline_id: str = "") -> dict[str, Any]:
    if not trigger_id or not pipeline_id:
        return {"success": False, "error": "trigger_id/pipeline_id 必填"}
    ok = get_trigger_manager().retarget(trigger_id, pipeline_id)
    return {"success": ok, "trigger_id": trigger_id, "pipeline_id": pipeline_id}


if __name__ == "__main__":
    plugin.run()

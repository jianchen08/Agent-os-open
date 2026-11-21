#!/usr/bin/env python3
"""llm_provider_gateway —— LLM 网关 provider 插件（sidecar 入口，薄）。

形态裁定（用户 2026-11-21）：网关以 provider 插件承载——provides namespace
``llm_provider.gateway`` + 同名 service 背书（gateway.complete_stream/status/
start/stop），llm.yaml providers.cpa_gateway 挂 via=plugin 接入（ADR
2026-11-04 via=plugin 契约的首个正式实例）。网关二进制随插件分发
（bin/，配方脚本填充），插件自己拉起/管理网关进程（端口三态，见
gateway_manager）。本入口只做接线：on_load 种子修补 + 网关 ensure、
on_unload 收树、四个 MCP 工具（= manifest capabilities.services 声明名，
G2 声明=实现）。

已知边界（诚实声明）：complete_stream 为聚合响应契约（收流聚合后一次返回），
不向 event-bus 推逐字增量——provider 插件路径的前端逐字流式留待真需求出现
另立一刀（stream_ctx 语义已保留在入参面，扩展位现成）。
"""

from __future__ import annotations

import logging
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

plugin = AgentOSPlugin("llm_provider_gateway")

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from _root_api import user_root  # noqa: E402 — 必须在 bootstrap_plugin 之后（sys.path 注入）
from gateway_manager import (  # noqa: E402
    GatewayProcessManager,
    GatewayConfig,
    management_url,
    read_gateway_config,
    seed_server_config,
)
from gateway_wire import GatewayUpstreamError, to_failure_payload  # noqa: E402
import gateway_wire as _wire  # noqa: E402

logger = logging.getLogger("llm_provider_gateway")

_manager: GatewayProcessManager | None = None
_manager_key: tuple[Any, ...] | None = None


def _build_config() -> GatewayConfig:
    """注入命名空间 → 归一化配置（用户空间根来自 _root_api.user_root()）。"""
    return read_gateway_config(plugin.get_config(), user_root_dir=user_root())


def _manager_fingerprint(config: GatewayConfig) -> tuple[Any, ...]:
    return (
        config.base_url,
        str(config.binary_path),
        str(config.server_config_path),
        str(config.auth_dir),
        str(config.log_path),
        config.client_api_key,
        config.autostart,
        config.health_timeout_secs,
        config.startup_timeout_secs,
        config.poll_interval_secs,
        config.call_timeout_secs,
        config.restart_backoff_base_secs,
        config.restart_backoff_max_secs,
        config.max_consecutive_failures,
        config.stable_uptime_secs,
    )


async def _get_manager() -> GatewayProcessManager:
    """管理器单点（配置热更感知：指纹变更即换管，存活进程移交新管不树杀）。"""
    global _manager, _manager_key
    config = _build_config()
    key = _manager_fingerprint(config)
    if _manager is not None and _manager_key == key:
        return _manager
    old = _manager
    adopted = await old.detach() if old is not None else None
    _manager = GatewayProcessManager(config)
    if adopted is not None:
        _manager.adopt(adopted)
    _manager_key = key
    return _manager


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """server config 种子修补（幂等）+ 网关 ensure（端口三态）。

    加载永不因网关缺失失败：配置非法/拉起失败均显式降级为日志（fail-visible），
    gateway.status 服务可查现状。
    """
    del params
    try:
        config = _build_config()
    except ValueError as exc:
        logger.error("[llm_provider_gateway] 配置非法，网关托管停摆: %s", exc)
        return
    try:
        seed = seed_server_config(config)
        logger.info("[llm_provider_gateway] server config 种子结果: %s", seed)
    except OSError as exc:
        logger.error("[llm_provider_gateway] server config 种子失败（fail-visible）: %s", exc)
    if not config.autostart:
        logger.info("[llm_provider_gateway] autostart=false——跳过启动拉起")
        return
    try:
        manager = await _get_manager()
        result = await manager.ensure_running()
        logger.info("[llm_provider_gateway] 网关托管结果: %s", result)
    except (OSError, ValueError) as exc:
        logger.error("[llm_provider_gateway] 网关 ensure 失败（fail-visible）: %s", exc)


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """收树本插件持有的网关进程（外部网关不受扰）。

    经 _get_manager 委托（与 status/start 同一管理器单点）；配置读取失败时
    跳过收树（无管理器即无持有进程，fail-visible 记日志）。"""
    del params
    global _manager, _manager_key
    try:
        manager = await _get_manager()
    except (OSError, ValueError) as exc:
        logger.warning("[llm_provider_gateway] on_unload 管理器不可得，跳过收树: %s", exc)
        _manager = None
        _manager_key = None
        return
    try:
        result = await manager.stop()
        logger.info("[llm_provider_gateway] on_unload 收树结果: %s", result)
    except OSError as exc:
        logger.warning("[llm_provider_gateway] on_unload 收树失败: %s", exc)
    _manager = None
    _manager_key = None  # 换管指纹一并失效


# ── 服务面（manifest capabilities.services 声明即契约，G2 声明=实现） ────

_COMPLETE_STREAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "description": "llm.yaml 模型 id（路由键回传语义）"},
        "model_name": {"type": "string", "description": "网关侧模型名（model_name 缺省回落 model）"},
        "messages": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Chat messages 数组",
        },
        "tools": {
            "type": ["array", "null"],
            "items": {"type": "object"},
            "description": "工具 schema（可选）",
        },
        "stream": {"type": "boolean", "description": "上游 SSE 收流聚合（true）/ JSON（false）"},
        "params": {"type": "object", "description": "模型参数透传（temperature/max_tokens 等）"},
        "stream_ctx": {"type": "object", "description": "流事件信封（本插件聚合语义下暂不消费）"},
        "call_context": {"type": "object", "description": "调用上下文（透传保留）"},
        "api_key": {"type": "string", "description": "网关托管形态注入的 client key（覆盖插件配置）"},
        "api_base": {"type": "string", "description": "网关托管形态注入的 base（覆盖插件配置）"},
    },
}


def _llm_failure_unavailable(ensure: dict[str, Any]) -> dict[str, Any]:
    """网关不可达 → 结构化失败载荷（service_down，细节进 message）。"""
    return {
        "status": "error",
        "llm_failure": {
            "code": "service_down",
            "message": (
                f"网关不可用（action={ensure.get('action')}）：{ensure.get('detail')}"
                "——cpa_gateway 车道暂不可用，不影响其他车道"
            ),
        },
    }


@plugin.tool(
    name="gateway.complete_stream",
    schema=_COMPLETE_STREAM_SCHEMA,
    description="via=plugin 契约方：调捆绑网关 /v1/chat/completions 并返回聚合响应（传输在插件，池化/兜底/用量留网关）",
)
async def gateway_complete_stream(
    model: str = "",
    model_name: str = "",
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    stream: bool = False,
    params: dict[str, Any] | None = None,
    stream_ctx: dict[str, Any] | None = None,
    call_context: dict[str, Any] | None = None,
    api_key: str = "",
    api_base: str = "",
) -> dict[str, Any]:
    """provider 契约入口：网关 ensure（三态幂等）→ 上游调用 → 聚合/失败契约。"""
    del stream_ctx, call_context  # 聚合语义下保留扩展位（见模块注记已知边界）
    try:
        manager = await _get_manager()
    except ValueError as exc:
        return {"status": "error", "llm_failure": {"code": "service_down", "message": str(exc)[:200]}}
    ensure = await manager.ensure_running()
    if not ensure.get("running"):
        return _llm_failure_unavailable(ensure)
    config = manager.config
    try:
        return await _complete_via_gateway(
            base_url=api_base or config.base_url,
            model_name=model_name or model,
            messages=messages or [],
            tools=tools,
            stream=bool(stream),
            params=params or {},
            client_api_key=api_key or config.client_api_key,
            timeout_secs=config.call_timeout_secs,
        )
    except GatewayUpstreamError as exc:
        return to_failure_payload(exc)
    except Exception as exc:  # noqa: BLE001 —— 契约失败收口（未知异常兜底 service_down）
        logger.exception("gateway.complete_stream 上游调用异常")
        return to_failure_payload(exc)


# 测试接缝：monkeypatch 本模块属性即可替换上游调用面（不经内核）。
_complete_via_gateway = _wire.complete_via_gateway


@plugin.tool(
    name="gateway.status",
    schema={"type": "object", "properties": {}},
    description="网关状态（探活二态 + 持有信息 + 管理页 URL，密钥不参与）",
)
async def gateway_status() -> dict[str, Any]:
    manager = await _get_manager()
    result = await manager.status()
    result["management_url"] = management_url(manager.config.base_url)
    result["autostart"] = manager.config.autostart
    return result


@plugin.tool(
    name="gateway.start",
    schema={"type": "object", "properties": {}},
    description="拉起网关（端口三态幂等：复用/拉起/fail-visible）",
)
async def gateway_start() -> dict[str, Any]:
    manager = await _get_manager()
    result = await manager.ensure_running()
    result["management_url"] = management_url(manager.config.base_url)
    return result


@plugin.tool(
    name="gateway.stop",
    schema={"type": "object", "properties": {}},
    description="停止本插件持有的网关进程（树杀；外部网关不受扰）",
)
async def gateway_stop() -> dict[str, Any]:
    """经管理器单点委托停止（stop 自身分辨持有/非持有，not_owned 原样透出）。"""
    global _manager, _manager_key
    manager = await _get_manager()
    result = await manager.stop()
    _manager = None
    _manager_key = None
    return result


if __name__ == "__main__":
    plugin.run()

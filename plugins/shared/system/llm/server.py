#!/usr/bin/env python3
"""LLM Service MCP 服务端——纯接口适配层。

老代码从 0.1 src/llm/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

核心能力：
- llm.complete: 统一 LLM 调用（非流式），支持 messages + tools
- llm.health_check: 检查模型是否可用

channel_api 退役批次 1 起同时承载 thinking-mode 域与 config/llm 段 HTTP 面：
``http.handle`` 按 path 分发（协议与 agent_manager/monitoring 同款），
plugin.json ``http_endpoints`` 声明（/ext/llm_service/thinking-mode/** 与
/ext/llm_service/config/llm/**）；业务函数在 ``routes_thinking_mode.py``
与 ``routes_llm_config.py``（原 channel_api 同名路由自持迁移）。

[来源: docs/working/module_migration_plan.md §六 P2 迁移；
docs/working/channel_api插件拆迁方案_20260821.md 批次 1]
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from _config_models import ModelConfigLoaderShim, set_config

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("llm_service")

# 全局 Adapter 实例
_adapter: Any = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize LLM adapter on load."""
    global _adapter
    config = plugin.get_config()
    logger.info("LLM service loaded, config keys: %s", list(config.keys()) if config else "(empty)")

    # 注入配置到 _config_models shim（供 router_factory/adapter 的懒加载路径复用）
    set_config(config)

    # 延迟构建 adapter：需要 model_loader（由配置注入）
    # 如果配置链路未修复，adapter 保持 None，工具调用时再延迟初始化
    _adapter = None


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup on unload."""
    global _adapter
    _adapter = None


def _ensure_adapter() -> Any:
    """延迟初始化 adapter（首次调用时构建）。"""
    global _adapter
    if _adapter is not None:
        return _adapter

    from router_factory import build_adapter  # noqa: PLC0415

    # 构建 model_loader shim：从 plugin 配置中读取 LLM 配置
    config = plugin.get_config()
    model_loader = _ModelLoaderShim(config)
    _adapter = build_adapter(model_loader)
    logger.info("LLM adapter initialized: %s", type(_adapter).__name__)
    return _adapter


class _ModelLoaderShim(ModelConfigLoaderShim):
    """server.py 侧的 model_loader 句柄（供 ``_ensure_adapter`` 构建时传参）。

    复用 ``_config_models.ModelConfigLoaderShim`` 的 ``_load_llm_data`` 实现，
    确保三条取配置路径（本类 / ``router_factory`` / ``adapter._route_call``）
    行为一致：P1 起统一从 ``config["llm"]`` 取值（config_files 命名空间）。
    """


@plugin.tool(
    name="llm.complete",
    schema={
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "LiteLLM model identifier (e.g. 'zai/glm-4-plus')"},
            "messages": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Chat messages array",
            },
            "tools": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Optional tool schemas for function calling",
            },
            "temperature": {"type": "number", "default": 0.7},
            "max_tokens": {"type": "integer", "default": 4096},
        },
        "required": ["model", "messages"],
    },
    description="Send a completion request to the LLM (non-streaming)",
)
async def llm_complete(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Execute an LLM completion request.

    Uses the KeyPoolAdapter internally for multi-key pooling, rate limiting,
    and automatic fallback.

    Args:
        model: LiteLLM model identifier string.
        messages: Chat message list.
        tools: Optional tool schemas for function calling.
        temperature: Sampling temperature.
        max_tokens: Maximum tokens to generate.

    Returns:
        LLM response containing text, tool_calls, thinking_text, usage.
    """
    adapter = _ensure_adapter()
    kwargs: dict[str, Any] = {
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    response = await adapter.completion(
        model=model,
        messages=messages,
        tools=tools,
        stream=False,
        **kwargs,
    )

    # LLMResponse dataclass → dict
    result: dict[str, Any] = {
        "text": response.text,
        "tool_calls": response.tool_calls or [],
        "thinking_text": response.thinking_text,
        "usage": response.usage or {},
    }
    return result


@plugin.tool(
    name="llm.health_check",
    schema={
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Model identifier to check"},
        },
        "required": ["model"],
    },
    description="Check if a specific LLM model is healthy and available",
)
async def llm_health_check(model: str) -> dict[str, Any]:
    """Check model availability.

    Args:
        model: LiteLLM model identifier string.

    Returns:
        Dict with 'healthy' boolean and model name.
    """
    adapter = _ensure_adapter()
    try:
        healthy = await adapter.health_check(model)
        return {"healthy": healthy, "model": model}
    except Exception as exc:
        logger.warning("Health check failed for %s: %s", model, exc)
        return {"healthy": False, "model": model, "error": str(exc)}


# ══ http.handle 响应封装（内核 HttpHandleResponse 约定，与 workspace/monitoring 同款）══


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _error(message: str, status: int = 503) -> dict[str, Any]:
    return {"success": False, "error": message, "data": _json_response({"error": message}, status)}


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 或明文 JSON）为 dict。"""
    if not raw_body:
        return {}
    try:
        try:
            decoded = base64.b64decode(raw_body).decode("utf-8")
            if not decoded.lstrip().startswith(("{", "[")):
                decoded = raw_body
        except Exception:  # noqa: BLE001
            decoded = raw_body
        return json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc


# ══ 域分发（channel_api 退役批次 1：thinking-mode + config/llm 迁入）══

_THINKING_MODE_PREFIX = "/ext/llm_service/thinking-mode"
_CONFIG_LLM_PREFIX = "/ext/llm_service/config/llm"


def _api_error_response(exc: Exception) -> dict[str, Any]:
    """把域业务异常（含 status_code + message/detail）转 HTTP 响应（404/400/409/502）。"""
    status = int(getattr(exc, "status_code", 500) or 500)
    message = getattr(exc, "message", None) or getattr(exc, "detail", None) or str(exc)
    return _ok(_json_response({"detail": message}, status))


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/llm_service/** (thinking-mode + config/llm domains, channel_api 拆迁批次 1)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发：thinking-mode 域 6 端点 + config/llm 段 13 端点。

    路径语义与原 /ext/channel_api/thinking-mode/** 与 /ext/channel_api/
    config/llm/** 逐项对齐（前端消费同一响应形态）；auth 由 http_endpoints
    auth=user 声明（dispatcher 层），handler 不读 _user。业务异常
    （status_code 属性）转对应 HTTP 状态，错误 body 形态与 FastAPI 版一致
    （``{"detail": ...}``）。
    """
    try:
        # ── thinking-mode 域 ──
        if path.startswith(_THINKING_MODE_PREFIX):
            import routes_thinking_mode as rtm  # noqa: PLC0415

            sub = path[len(_THINKING_MODE_PREFIX):]
            if sub == "/healthz" and method == "GET":
                return _ok(_json_response(rtm.health()))
            if sub == "/models" and method == "GET":
                return _ok(_json_response(rtm.list_models()))
            if sub.startswith("/models/") and method == "GET":
                model_name = sub[len("/models/"):]
                return _ok(_json_response(rtm.get_model_info(model_name)))
            if sub.startswith("/check/") and method == "GET":
                model_name = sub[len("/check/"):]
                return _ok(_json_response(rtm.check_support(model_name)))
            if sub == "/switch" and method == "POST":
                return _ok(_json_response(rtm.switch_mode(_decode_body(raw_body))))
            if sub == "/recommendations" and method == "POST":
                recs_body = _decode_body(raw_body) or None
                return _ok(_json_response(rtm.recommendations(recs_body)))

            logger.warning("llm http.handle: no thinking-mode route for sub=%s method=%s", sub, method)
            return _ok(_json_response({"error": "not found", "path": path}, 404))

        # ── config/llm 段 ──
        if path.startswith(_CONFIG_LLM_PREFIX):
            import routes_llm_config as rlc  # noqa: PLC0415

            sub = path[len(_CONFIG_LLM_PREFIX):]  # "" / "/providers" / "/models/xxx" ...
            if sub == "" and method == "GET":
                return _ok(_json_response(rlc.get_llm_config()))
            if sub == "/providers" and method == "GET":
                return _ok(_json_response(rlc.get_providers()))
            if sub == "/providers" and method == "POST":
                return _ok(_json_response(rlc.add_provider(_decode_body(raw_body))))
            if sub == "/provider-types" and method == "GET":
                return _ok(_json_response(rlc.get_provider_types()))
            if sub.startswith("/providers/") and sub.endswith("/remote-models") and method == "GET":
                provider_id = sub[len("/providers/"):-len("/remote-models")]
                return _ok(_json_response(rlc.get_remote_models(provider_id)))
            if sub.startswith("/providers/") and method == "PUT":
                provider_id = sub[len("/providers/"):]
                return _ok(_json_response(rlc.update_provider(provider_id, _decode_body(raw_body))))
            if sub.startswith("/providers/") and method == "DELETE":
                provider_id = sub[len("/providers/"):]
                return _ok(_json_response(rlc.delete_provider(provider_id)))
            if sub == "/models" and method == "GET":
                return _ok(_json_response(rlc.get_models()))
            if sub == "/models" and method == "POST":
                return _ok(_json_response(rlc.add_model(_decode_body(raw_body))))
            if sub.startswith("/models/") and method == "PUT":
                model_id = sub[len("/models/"):]
                return _ok(_json_response(rlc.update_model(model_id, _decode_body(raw_body))))
            if sub.startswith("/models/") and method == "DELETE":
                model_id = sub[len("/models/"):]
                return _ok(_json_response(rlc.delete_model(model_id)))
            if sub == "/defaults" and method == "GET":
                return _ok(_json_response(rlc.get_defaults()))
            if sub == "/defaults" and method == "PUT":
                return _ok(_json_response(rlc.save_defaults(_decode_body(raw_body))))

            logger.warning("llm http.handle: no config/llm route for sub=%s method=%s", sub, method)
            return _ok(_json_response({"error": "not found", "path": path}, 404))

        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code"):
            return _api_error_response(exc)
        logger.error("llm http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


if __name__ == "__main__":
    plugin.run()

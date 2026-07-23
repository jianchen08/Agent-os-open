#!/usr/bin/env python3
"""LLM Service MCP 服务端——纯接口适配层。

老代码从 0.1 src/llm/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

核心能力：
- llm.complete: 统一 LLM 调用（非流式），支持 messages + tools
- llm.health_check: 检查模型是否可用

[来源: docs/working/module_migration_plan.md §六 P2 迁移]
"""
from __future__ import annotations

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


class _ConfigCenterShim:
    """临时兼容层：模拟 0.1 ConfigCenter 接口，数据来自 plugin.get_config()。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def get_section(self, section: str) -> dict[str, Any]:
        return self._config.get(section, {})


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


if __name__ == "__main__":
    plugin.run()

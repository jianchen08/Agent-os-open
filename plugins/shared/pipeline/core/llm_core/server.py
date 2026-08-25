#!/usr/bin/env python3
"""llm_core core pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/core/llm_core/plugin.py 原封不动复制到本目录，
本文件只做接口适配：通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
import os
import sys
from functools import lru_cache

# 设置 sys.path：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
# + system/llm（复用 _config_models 配置注入桥，与 system/llm/server.py 共享）
_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
# pipeline/core 目录：llm_provider_* 提供者适配插件（deepseek/minimax/keypool）
# 作为可导入包存在（task_kernel_cleanup_and_split 3a，注册表按模型名懒加载）。
_core_dir = os.path.join(_this_dir, "..")
sys.path.insert(1, _core_dir)
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)
_system_llm_dir = os.path.join(_shared_dir, "system", "llm")
sys.path.insert(0, _system_llm_dir)

# litellm 首次 import 时会同步 fetch GitHub 的 model cost map，在离线/受限网络
# 下 SSL 握手超时（30s）拖垮 MCP initialize 握手。改用本地 backup 跳过远程
# fetch（litellm 官方开关，get_model_cost_map 顶部判断此环境变量）。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

from plugin import LLMCore  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("llm_core_pipeline")


@lru_cache(maxsize=1)
def get_instance() -> LLMCore:
    """懒构建并缓存 LLMCore 单例（线程安全；替代模块级可变 `_instance` 全局）。

    构造前注入 _config_models 配置 shim（供 LLMCore._apply_model_from_state 的
    get_llm_core_config 解析 llm.yaml；与 system/llm/server.py 对齐）。
    """
    from _config_models import set_config  # noqa: PLC0415

    config = plugin.get_config()
    set_config(config)
    return LLMCore(config=config)


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize llm_core plugin."""
    get_instance()  # 预热：注入配置 shim + 构建 LLMCore（保持原 on_load 构造时机）


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup llm_core plugin."""
    get_instance.cache_clear()


@plugin.tool(
    name="llm_core.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline state dict"},
            "config": {"type": "object", "default": {}, "description": "Plugin config overrides"},
        },
        "required": ["state"],
    },
    description="Execute LLM Core pipeline plugin",
)
async def execute(state: dict, config: dict | None = None) -> dict:
    """Execute the llm_core pipeline plugin.

    Args:
        state: Pipeline state dictionary.
        config: Optional plugin config overrides.

    Returns:
        Execution result containing state updates and optional route signal.
    """
    from agentos_plugin_sdk.pipeline_types import PluginContext, create_initial_state  # noqa: PLC0415

    merged_state = create_initial_state(**state)
    ctx = PluginContext(state=merged_state, config=config or {})

    # 流式 chunk 桥接：注入 on_chunk 闭包，把 LLM 逐字输出经 event-bus.emit notify 推到内核。
    # 闭包是同步的（litellm 流式循环同步调用），内部用 create_task 把 async notify
    # 安排到当前事件循环（execute 正在 await，循环在跑，task 会执行）。
    # 逐字流式由此打通：sidecar 边生成边 notify → 内核 event-bus handler → session.emit_stream → 前端。
    _thread_id = merged_state.get("session_id", "")
    _pipeline_id = merged_state.get("pipeline_id", "")
    _message_id = merged_state.get("message_id", "")
    try:
        _event_bus = plugin.get_capability("event-bus")
        _loop = asyncio.get_event_loop()
    except Exception:
        _event_bus = None
        _loop = None

    if _event_bus is not None and _thread_id and _loop is not None:
        # thinking 状态机：跟踪是否已发 thinking_start（对照 0.1 bridge_events.py）
        _thinking_active = {"value": False}
        _seq = {"value": 0}

        # Queue + 独立消费者：对照 0.1 engine_streaming 的 chunk 队列模式。
        # litellm 流式循环密集同步调 on_chunk，若用 create_task 推送，
        # task 会被堆积到 LLM 循环结束才一起执行（事件循环没机会切换），
        # 导致所有 chunk 最后一次性到达（前端「一起渲染」非逐字）。
        # Queue.put_nowait 是 O(1) 不阻塞，消费者协程 await get() 异步取出推送，
        # 与 LLM 流式循环并发，实现真正的逐字实时推送。
        _chunk_queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def _consumer() -> None:
            """独立消费者：从队列取 (event, content) 推送到内核。"""
            while True:
                item = await _chunk_queue.get()
                if item is None:
                    break  # 哨兵，LLM 结束后发 None 终止消费者
                event, content = item
                payload: dict[str, Any] = {
                    "thread_id": _thread_id,
                    "pipeline_id": _pipeline_id,
                    "message_id": _message_id,
                    "sequence": _seq["value"],
                }
                if content:
                    payload["content"] = content
                _seq["value"] += 1
                try:
                    await _event_bus.notify("emit", {"event": event, "payload": payload})
                except Exception:
                    pass

        _consumer_task = _loop.create_task(_consumer())

        def _put(event: str, content: str = "") -> None:
            """同步入队（O(1) 不阻塞，litellm 流式循环安全调用）。"""
            _chunk_queue.put_nowait((event, content))

        def _on_chunk(chunk_data: dict) -> None:
            # chunk 契约：system/llm adapter 的 on_chunk 只传 {"type", "content"}。
            chunk_type = chunk_data.get("type", "text")
            content = chunk_data.get("content", "")

            if chunk_type == "thinking":
                if content:
                    if not _thinking_active["value"]:
                        _thinking_active["value"] = True
                        _put("thinking_start")
                    _put("thinking_chunk", content)
            elif chunk_type == "thinking_end":
                if _thinking_active["value"]:
                    _thinking_active["value"] = False
                    _put("thinking_end")
            elif chunk_type == "text":
                if _thinking_active["value"]:
                    _thinking_active["value"] = False
                    _put("thinking_end")
                if content:
                    _put("stream_chunk", content)

        merged_state["on_chunk"] = _on_chunk
        # 注入清理钩子：LLM 结束后发哨兵终止消费者，防止协程泄漏
        merged_state["_stream_consumer"] = _consumer_task
        merged_state["_stream_queue"] = _chunk_queue

    result = await get_instance().execute(ctx)

    # LLM 结束：发哨兵 None 终止消费者协程，等待其排空剩余 chunk（保证不丢末尾）
    if "_stream_queue" in merged_state:
        merged_state["_stream_queue"].put_nowait(None)
        try:
            await merged_state["_stream_consumer"]
        except Exception:
            pass

    # Core 插件（LLMCore）返回 state_updates dict（含 raw_result 等），
    # 需包成 {"state_updates": <dict>} 供内核反序列化为 PluginResult。
    if isinstance(result, dict):
        return {"state_updates": result}

    data: dict = {"state_updates": result.state_updates}
    route_sig = getattr(result, "route_signal", None)
    if route_sig:
        data["route_signal"] = {
            "route_type": route_sig.route_type,
            "target": route_sig.target,
            "reason": route_sig.reason,
        }
    if getattr(result, "skip_remaining", False):
        data["skip_remaining"] = True
    return data


if __name__ == "__main__":
    plugin.run()

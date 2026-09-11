"""记忆后端接线工具 — 共享裸名模块，供各 sidecar server.py 在 on_load 时注入依赖。

统一模式：
1. ``make_capability_caller(plugin)`` — 从内核注入的 tool-executor 能力句柄构造
   async caller ``(method, params) -> Any``。
2. ``build_memory_backend(plugin)`` — 用该 caller 构建 IMemoryBackend（唯一后端
   hindsight；config 指定 backend=kernel 时抛 ValueError），失败返回 None
   （sidecar 降级，不崩溃）。

桥接说明：memory_backend 的 CapabilityCaller 约定传入**完整** wire method
（如 "tool-executor.invoke" / "memory.create"），而 SDK CapabilityHandle.call
会拼接 ``f"{cap}.{method}"``。因此需剥掉已含的能力前缀，避免双命名空间
（"tool-executor.tool-executor.invoke"——内核 CapabilityRouter 只认
("tool-executor", "invoke")）。

各插件的 server.py on_load 用法：:

    from wiring import build_memory_backend, make_capability_caller
    from plugin import set_memory_backend, set_capability_caller

    @plugin.on_load
    async def _on_load(params):
        global _instance
        config = plugin.get_config()
        _instance = MyPlugin(config=config)
        backend = build_memory_backend(plugin)
        if backend:
            set_memory_backend(backend)
        caller = make_capability_caller(plugin)
        if caller:
            set_capability_caller(caller)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from agentos_plugin_sdk.capability import bind_capability_caller

logger = logging.getLogger(__name__)

# hindsight_memory 插件目录（memory_backend.py 所在处）加入 sys.path，
# 供 get_memory_backend 工厂导入。本模块寄居共享根，插件目录为其下
# system/hindsight_memory 子目录。
_HINDSIGHT_MEMORY_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "system", "hindsight_memory"),
)
if _HINDSIGHT_MEMORY_DIR not in sys.path:
    sys.path.insert(0, _HINDSIGHT_MEMORY_DIR)


def make_capability_caller(plugin: Any) -> Any | None:
    """从内核注入的 tool-executor 句柄构造 capability_caller。

    仅走 tool-executor：service-registry 面向已退役的内核记忆表后端，
    对 hindsight 调用会发错命名空间。

    Args:
        plugin: AgentOSPlugin 实例（含 get_capability）

    Returns:
        async caller `(method, params) -> Any`；能力未注入时返回 None
    """
    try:
        handle = plugin.get_capability("tool-executor")
    except KeyError:
        logger.warning("[wiring] 未注入 tool-executor 能力，capability_caller 不可用")
        return None
    return bind_capability_caller(handle, "tool-executor")


def build_memory_backend(plugin: Any) -> Any | None:
    """构建 IMemoryBackend；能力缺失/构建失败时返回 None（插件降级，不崩溃）。

    Args:
        plugin: AgentOSPlugin 实例

    Returns:
        IMemoryBackend 实例（HindsightBackend），失败返回 None
    """
    caller = make_capability_caller(plugin)
    if caller is None:
        return None
    try:
        from memory_backend import get_memory_backend  # noqa: PLC0415

        return get_memory_backend(
            config=plugin.get_config() or {},
            capability_caller=caller,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wiring] 记忆后端构建失败 | error=%s", exc)
        return None

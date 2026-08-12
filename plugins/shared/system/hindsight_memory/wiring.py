"""记忆后端接线工具 — 供各 sidecar server.py 在 on_load 时注入依赖。

统一模式：
1. ``make_capability_caller(plugin)`` — 从内核注入的能力句柄构造
   async caller ``(method, params) -> Any``（tool-executor 优先，service-registry 回落）。
2. ``build_memory_backend(plugin)`` — 用该 caller 构建 IMemoryBackend（默认 hindsight，
   配置 backend=kernel 时用内核 memory 表），失败返回 None（sidecar 降级，不崩溃）。

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

logger = logging.getLogger(__name__)

# hindsight_memory 插件目录（memory_backend.py 所在处）加入 sys.path，
# 供 get_memory_backend 工厂导入。
_HINDSIGHT_MEMORY_DIR = os.path.abspath(os.path.dirname(__file__))
if _HINDSIGHT_MEMORY_DIR not in sys.path:
    sys.path.insert(0, _HINDSIGHT_MEMORY_DIR)


def _bind_caller(handle: Any, cap_name: str) -> Any:
    """绑定能力句柄与命名空间，构造 async caller `(method, params) -> Any`。

    闭包通过函数参数绑定，规避 B023（循环变量绑定）。

    Args:
        handle: CapabilityHandle 实例（其 call 会拼接 ``f"{cap}.{method}"``）
        cap_name: 能力命名空间（如 "tool-executor"）

    Returns:
        async caller：剥掉 memory_backend 已含的能力前缀后转交 handle.call
    """
    prefix = f"{cap_name}."

    async def _call(method: str, params: dict[str, Any]) -> Any:
        stripped = method[len(prefix):] if method.startswith(prefix) else method
        return await handle.call(stripped, params)

    return _call


def make_capability_caller(plugin: Any) -> Any | None:
    """从内核注入的能力句柄构造 capability_caller。

    Args:
        plugin: AgentOSPlugin 实例（含 get_capability）

    Returns:
        async caller `(method, params) -> Any`；能力未注入时返回 None
    """
    for cap_name in ("tool-executor", "service-registry"):
        try:
            handle = plugin.get_capability(cap_name)
        except KeyError:
            continue
        return _bind_caller(handle, cap_name)
    logger.warning("[wiring] 未注入 tool-executor/service-registry 能力，capability_caller 不可用")
    return None


def build_memory_backend(plugin: Any) -> Any | None:
    """构建 IMemoryBackend；能力缺失/构建失败时返回 None（插件降级，不崩溃）。

    Args:
        plugin: AgentOSPlugin 实例

    Returns:
        IMemoryBackend 实例（HindsightBackend 或 KernelMemoryBackend），失败返回 None
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

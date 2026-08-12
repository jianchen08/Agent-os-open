#!/usr/bin/env python3
"""Memory 工具 MCP 服务端——接口适配层（0.2 重写版）。

- 后端：通过 memory_backend.get_memory_backend 工厂构建 IMemoryBackend
  （默认 hindsight，降级 kernel），capability_caller 取自内核注入的
  tool-executor / service-registry 能力句柄（与 context_window_guard 注入同款做法）。
- 能力未注入 / 后端构建失败时降级：MemoryTool 未注入后端返回
  {"error": "memory backend 未注入"}，sidecar 永不崩溃。
- 工具 schema 与 tool.py 的 get_tool_definition() 保持一致（8 个 action）。

[来源: docs/tasks Step 5d MemoryTool 重写为 IMemoryBackend]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 将 0.1 源码目录加入 sys.path（过渡期兼容；0.2 下 src/ 已删除，此段为空操作）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_SRC_ROOT = os.path.join(_PROJECT_ROOT, 'src')
if os.path.isdir(_SRC_ROOT):
    sys.path.insert(0, _SRC_ROOT)

# hindsight_memory 插件目录（memory_backend.py 所在处）加入 sys.path，
# 供 get_memory_backend 工厂导入。
_HINDSIGHT_MEMORY_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'system', 'hindsight_memory')
)
if _HINDSIGHT_MEMORY_DIR not in sys.path:
    sys.path.insert(0, _HINDSIGHT_MEMORY_DIR)

from tool import MemoryTool  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("memory_tool")

# 工具 schema：与 MemoryTool.get_tool_definition() 的 input_schema 保持一致
_MEMORY_SCHEMA = MemoryTool.get_tool_definition().input_schema
_MEMORY_DESCRIPTION = MemoryTool.get_tool_definition().description

# ── 记忆后端（懒构建 + 缓存）──────────────────────────────
_memory_backend: Any | None = None
_memory_backend_attempted = False


def _make_capability_caller() -> Any | None:
    """从内核注入的能力句柄构造 capability_caller（async fn `(method, params)`）。

    优先 tool-executor（hindsight 后端），回落 service-registry（kernel 后端）；
    均未注入时返回 None。

    桥接说明：memory_backend 的 CapabilityCaller 约定传入**完整** wire method
    （如 "tool-executor.invoke" / "memory.create"），而 SDK CapabilityHandle.call
    会拼接 ``f"{cap}.{method}"``。因此需剥掉已含的能力前缀，避免双命名空间
    （"tool-executor.tool-executor.invoke"——内核 CapabilityRouter 只认
    ("tool-executor", "invoke")）。
    """
    for cap_name in ("tool-executor", "service-registry"):
        try:
            handle = plugin.get_capability(cap_name)
        except KeyError:
            continue
        return _bind_caller(handle, cap_name)
    return None


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


def _build_memory_backend() -> Any | None:
    """构建 IMemoryBackend；能力缺失/构建失败时返回 None（工具降级，不崩溃）。"""
    caller = _make_capability_caller()
    if caller is None:
        logger.warning(
            "[memory] 未注入 tool-executor/service-registry 能力，记忆后端不可用"
        )
        return None
    try:
        from memory_backend import get_memory_backend  # noqa: PLC0415

        return get_memory_backend(
            config=plugin.get_config() or {},
            capability_caller=caller,
        )
    except Exception as e:
        logger.warning("[memory] 记忆后端构建失败 | error=%s", e)
        return None


def _get_memory_backend() -> Any | None:
    """懒构建并缓存记忆后端（幂等，多次调用只构建一次）。"""
    global _memory_backend, _memory_backend_attempted
    if not _memory_backend_attempted:
        _memory_backend_attempted = True
        _memory_backend = _build_memory_backend()
    return _memory_backend


@plugin.tool(
    name="memory",
    schema=_MEMORY_SCHEMA,
    description=_MEMORY_DESCRIPTION,
)
async def memory(**kwargs):
    t = MemoryTool(memory_backend=_get_memory_backend())
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}


if __name__ == "__main__":
    plugin.run()

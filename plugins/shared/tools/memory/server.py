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

# 加载本目录 tool.py 的 MemoryTool：显式路径 + 唯一模块名（不用裸
# `from tool import ...`——同一进程里其它插件目录的 tool.py 会抢先占用
# 平铺模块名 `tool`（如 task/tool.py），导致 ImportError/拿到错误实现）。
import importlib.util as _ilu  # noqa: E402

_TOOL_MOD_NAME = "memory_tool_impl"
if _TOOL_MOD_NAME in sys.modules:
    _tool_mod = sys.modules[_TOOL_MOD_NAME]
else:
    _tool_spec = _ilu.spec_from_file_location(
        _TOOL_MOD_NAME, os.path.join(os.path.dirname(__file__), 'tool.py')
    )
    assert _tool_spec is not None
    assert _tool_spec.loader is not None
    _tool_mod = _ilu.module_from_spec(_tool_spec)
    sys.modules[_TOOL_MOD_NAME] = _tool_mod
    _tool_spec.loader.exec_module(_tool_mod)
MemoryTool = _tool_mod.MemoryTool

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("memory_tool")

# 工具 schema：与 MemoryTool.get_tool_definition() 的 input_schema 保持一致
_MEMORY_SCHEMA = MemoryTool.get_tool_definition().input_schema
_MEMORY_DESCRIPTION = MemoryTool.get_tool_definition().description

# ── 记忆后端（懒构建 + 缓存）──────────────────────────────
_memory_backend: Any | None = None


def _make_capability_caller() -> Any | None:
    """从内核注入的能力句柄构造 capability_caller（async fn `(method, params)`）。

    同时收集 tool-executor + service-registry 句柄，按 method 前缀路由：
    - "tool-executor.*"（hindsight 后端的 retain/recall）→ tool-executor handle
    - 无前缀域方法（kernel 后端的 memory.create/search）→ service-registry handle

    此前"优先 tool-executor 单句柄"的形态会把 kernel 后端的 memory.create
    错拼成 tool-executor.memory.create（内核 method not implemented，2026-08-19
    e2e 实测）。至少一个句柄可用即返回 caller；均未注入返回 None。
    """
    handles: dict[str, Any] = {}
    for cap_name in ("tool-executor", "service-registry"):
        try:
            handles[cap_name] = plugin.get_capability(cap_name)
        except KeyError:
            continue

    if "service-registry" in handles:
        sr = handles["service-registry"]
        te = handles.get("tool-executor")

        async def _call(method: str, params: dict[str, Any]) -> Any:
            if method.startswith("tool-executor."):
                if te is None:
                    raise RuntimeError("tool-executor 能力未注入")
                return await te.call(method[len("tool-executor."):], params)
            return await sr.call(method, params)

        return _call
    if "tool-executor" in handles:
        return _bind_caller(handles["tool-executor"], "tool-executor")
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
    """懒构建并缓存记忆后端（成功后幂等；失败不锁死，下次调用重试）。

    capability 句柄在 initialize 握手注入，早于首条工具调用；但空能力
    sidecar（boot 期无 router spawn）等异常形态下首次构建会失败——失败
    不缓存 None，下一次调用重试，避免一次竞态把后端永久判死。
    """
    global _memory_backend
    if _memory_backend is None:
        _memory_backend = _build_memory_backend()
    return _memory_backend


def _owner_from_inputs(inputs: dict[str, Any]) -> str | None:
    """从内核注入参数提取可信会话身份（bash/tool.py::_owner_from_inputs 同款）。

    优先级：_owner > session_id > thread_id > workspace > project_root，
    全部缺失返回 None。刻意**不含** user_id——那是客户端可伪造的参数，
    作为身份即开门给 IDOR。
    """
    for key in ("_owner", "session_id", "thread_id", "workspace", "project_root"):
        value = inputs.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


@plugin.tool(
    name="memory",
    schema=_MEMORY_SCHEMA,
    description=_MEMORY_DESCRIPTION,
)
async def memory(**kwargs):
    # IDOR 防护接线（B6）：从内核注入参数解析可信身份后注入工具；
    # 无注入时 MemoryTool 对敏感 action（store/import/update/delete）明确拒绝。
    t = MemoryTool(memory_backend=_get_memory_backend())
    t.set_trusted_user_id(_owner_from_inputs(kwargs))
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}


if __name__ == "__main__":
    plugin.run()

"""管道步骤/钩子的 SDK 侧分发共享逻辑。

内核把具名步骤调用（``config["_step_method"]``）与管道钩子
（``config["_pipe_hook"]``）都经同一 execute 通道送达（提案 §3.4/§3.6，
与 ``tool_call_json`` 同构的「约定字段表达特殊调用」先例，第二次应用）。
本模块承载钩子注册表的共享迭代逻辑：``AgentOSPlugin``（公开 API 层）与
``McpServer``（wire 分发层）共同引用，避免整段复制。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def dispatch_pipe_hook_registry(
    pipe_hooks: dict[str, list[Callable[..., Any]]],
    event: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """顺序 await 某事件的全部钩子 handler，收集非空返回。

    结构化否决指令（如 ``{"decision": "terminate", "reason": ...}``）与
    普通观察结果同列返回，多决策并存由内核裁决；无注册者返回空列表。

    Args:
        pipe_hooks: 事件名 → handler 列表的注册表。
        event: 事件名。
        payload: 事件负载 dict。

    Returns:
        各 handler 的非空返回列表（保持注册顺序）。
    """
    results: list[dict[str, Any]] = []
    for handler in pipe_hooks.get(event, []):
        result = handler(payload)
        if asyncio.iscoroutine(result):
            result = await result
        if result is not None:
            results.append(result)
    return results

"""三级并发控制器 — provider/model/agent 信号量。

精简原则：
- 保留三级信号量（provider/model/agent 确实需要不同粒度的限流）
- 去掉单例模式 → 普通实例，由管道持有
- 去掉线程锁 → 纯 asyncio
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any


class ConcurrencyController:
    """三级并发控制 — provider/model/agent 信号量。

    三级并发控制的用途：
    - provider: 限制同一 LLM Provider 的并发请求数（如 OpenAI 3 并发）
    - model: 限制同一模型的并发请求数（如 GPT-4 5 并发）
    - agent: 限制同一 Agent 管道的并发数（如主 Agent 10 并发）

    Attributes:
        _semaphores: 级别到信号量的映射
    """

    def __init__(self, config: dict[str, int] | None = None) -> None:
        """初始化并发控制器。

        Args:
            config: 各级别的最大并发数，未指定时使用默认值
                    默认: provider_max=3, model_max=5, agent_max=10
        """
        config = config or {}
        self._semaphores: dict[str, asyncio.Semaphore] = {
            "provider": asyncio.Semaphore(config.get("provider_max", 3)),
            "model": asyncio.Semaphore(config.get("model_max", 5)),
            "agent": asyncio.Semaphore(config.get("agent_max", 10)),
        }

    @asynccontextmanager
    async def acquire(self, level: str = "agent"):
        """获取指定级别的并发许可。

        使用 async with 语法自动获取和释放信号量。

        Args:
            level: 并发级别，可选 "provider"/"model"/"agent"

        Yields:
            None

        Raises:
            ValueError: 级别不存在时抛出
        """
        sem = self._semaphores.get(level)
        if sem is None:
            raise ValueError(f"Unknown concurrency level: {level}")
        async with sem:
            yield

    @property
    def available(self) -> dict[str, int]:
        """各级别当前可用许可数。

        Returns:
            级别到可用许可数的映射
        """
        return {level: sem._value for level, sem in self._semaphores.items()}

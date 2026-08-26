"""多 key 轮询负载均衡提供者策略插件（llm_provider_keypool）。

导出 [`KeyPoolAdapter`](keypool.KeyPoolAdapter)——按 API key 做并发控制 +
RPM 限流 + 配额追踪的提供者策略。需要多 key 时挂载；无 KeyPool 的 provider
回退 litellm.Router 默认行为。

依赖 llm_core（基类 `_BaseLiteLLMAdapter` 与流式基础设施），导入本包要求
llm_core 目录在 sys.path 上（llm_core/server.py 已设置）。导出经 PEP 562
惰性解析：包导入本身不触发 keypool.py 顶层裸名 import（测试收集期 sys.path
无 llm_core 目录时也可安全导入本包）。
"""

from __future__ import annotations

from typing import Any

__all__ = ["KeyPoolAdapter"]


def __getattr__(name: str) -> Any:
    if name == "KeyPoolAdapter":
        from .keypool import KeyPoolAdapter

        return KeyPoolAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

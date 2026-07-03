"""工具缓存 Input 插件。

负责在管道循环的输入阶段检查工具调用是否命中内存缓存，
命中时直接返回缓存结果并跳过后续插件执行。

使用基于 (tool_name + sorted_args_json) 的 MD5 哈希作为缓存 key，
    支持 TTL 过期和最大缓存条目限制。
    淘汰策略为 LRU（基于最近访问时间）。

State 命名空间：
    - cache_hit : 是否命中缓存
    - tool_results : 缓存命中时的结果（跳过工具执行）
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class ToolCache(IInputPlugin):
    """工具缓存 Input 插件。

    基于 (tool_name + sorted_args_json) 的 MD5 哈希作为缓存 key，
    使用内存缓存存储工具执行结果。命中缓存时直接返回结果，
    跳过后续所有插件和工具执行。
    淘汰策略为 LRU（基于最近访问时间），每次缓存命中时更新访问时间。

    配置项：
    - enabled: 是否启用缓存（默认 True）
    - default_ttl: 默认缓存过期时间，单位秒（默认 300）
    - max_size: 最大缓存条目数（默认 100）

    优先级：35（校验级，在 schema 验证之后）
    错误策略：SKIP（缓存异常不阻塞管道）

    Attributes:
        _config: 插件配置字典
        _cache: 内存缓存字典，key 为 MD5 哈希，value 为 (result, expire_time) 元组
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化工具缓存插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用缓存（默认 True）
                - default_ttl: 默认缓存过期时间，单位秒（默认 300）
                - max_size: 最大缓存条目数（默认 100）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._default_ttl = self._config.get("default_ttl", 300)
        self._max_size = self._config.get("max_size", 100)
        self._cache: dict[str, tuple[Any, float, float]] = {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_cache"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 35)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行缓存查询。

        对每个工具调用生成缓存 key，查找内存缓存。
        全部命中时设置 cache_hit=True 并跳过后续执行；
        未命中时不设置任何状态，正常执行工具。

        Args:
            ctx: 插件执行上下文

        Returns:
            缓存命中时包含结果和跳过标记的插件执行结果
        """
        if not self._enabled:
            return PluginResult()

        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return PluginResult()

        now = time.time()
        cached_results: list[Any] = []

        for tc in tool_calls:
            cache_key = self._make_cache_key(tc)
            entry = self._cache.get(cache_key)
            if entry is not None:
                result, expire_time, _last_access = entry
                if now < expire_time:
                    # LRU: 命中时更新访问时间
                    self._cache[cache_key] = (result, expire_time, now)
                    cached_results.append(result)
                    logger.debug(
                        "[%s] Cache hit | key=%s",
                        self.name,
                        cache_key[:12],
                    )
                    continue
                del self._cache[cache_key]
                logger.debug(
                    "[%s] Cache expired | key=%s",
                    self.name,
                    cache_key[:12],
                )

            return PluginResult()

        return PluginResult(
            state_updates={
                "cache_hit": True,
                StateKeys.TOOL_RESULTS: cached_results,
            },
            skip_remaining=True,
        )

    def put(self, tool_call: dict[str, Any], result: Any) -> None:
        """将工具执行结果写入缓存。

        外部调用方在工具执行完成后调用此方法将结果缓存。
        当缓存条目数超过 max_size 时，清理所有已过期的条目。

        Args:
            tool_call: 工具调用描述，包含 name 和 args
            result: 工具执行结果
        """
        if not self._enabled:
            return

        cache_key = self._make_cache_key(tool_call)
        expire_time = time.time() + self._default_ttl
        self._cache[cache_key] = (result, expire_time, time.time())

        if len(self._cache) > self._max_size:
            self._evict_expired()

    def _make_cache_key(self, tool_call: dict[str, Any]) -> str:
        """根据工具名称和参数生成缓存 key。

        使用 (tool_name + sorted_args_json) 的 MD5 哈希作为 key，
        确保相同参数的工具调用命中同一缓存条目。

        Args:
            tool_call: 工具调用描述，包含 name 和 args

        Returns:
            MD5 哈希字符串
        """
        tool_name = tool_call.get("name", "")
        args = tool_call.get("args", {})
        raw = f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _evict_expired(self) -> None:
        """清理过期的缓存条目。

        如果清理后仍超过 max_size，按 LRU 策略移除最久未访问的条目。
        """
        now = time.time()
        expired_keys = [k for k, (_, exp, _) in self._cache.items() if now >= exp]
        for k in expired_keys:
            del self._cache[k]

        if len(self._cache) > self._max_size:
            # LRU: 按 last_access_time 排序，移除最久未访问的条目
            sorted_items = sorted(
                self._cache.items(),
                key=lambda item: item[1][2],
            )
            to_remove = len(self._cache) - self._max_size
            for k, _ in sorted_items[:to_remove]:
                del self._cache[k]

"""工具缓存 Input 插件。

负责在管道循环的输入阶段检查工具调用是否命中内存缓存，
命中时直接返回缓存结果并跳过后续插件执行。

缓存存储在**模块级单例字典**中（进程内全局），与 tool_cache_writer
（output 阶段插件）共享同一份缓存。这样 input 读缓存 / output 写缓存
才能命中同一条目。

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

# 全局缓存载体：挂在 pipeline 包上（两端插件都 import pipeline，sys.path 一致，
# 保证是同一份字典）。不用模块级单例是因为 tool_cache_writer 通过 importlib
# 动态加载本模块时会得到独立的模块副本，模块级字典无法共享。
import pipeline as _pipeline_pkg  # noqa: E402

_GLOBAL_CACHE_ATTR = "_tool_result_cache"
if not hasattr(_pipeline_pkg, _GLOBAL_CACHE_ATTR):
    setattr(_pipeline_pkg, _GLOBAL_CACHE_ATTR, {})
# _GLOBAL_CACHE 是 pipeline 包上那个字典的直接引用
_GLOBAL_CACHE: dict[str, tuple[Any, float, float]] = getattr(_pipeline_pkg, _GLOBAL_CACHE_ATTR)

# 默认不缓存的有副作用工具（写操作 / 外部副作用，结果不可复用）
_DEFAULT_EXCLUDE_TOOLS: set[str] = {
    "bash_execute",
    "file_write",
    "file_delete",
    "file_move",
    "file_copy",
    "create_directory",
    "web_operate",
    "task_submit",
    "task_manage",
    "state_update",
}


def get_global_cache() -> dict[str, tuple[Any, float, float]]:
    """返回模块级单例缓存字典。

    供 tool_cache_writer（output 阶段）共享同一份缓存。
    """
    return _GLOBAL_CACHE


def evict_expired(cache: dict[str, tuple[Any, float, float]], max_size: int) -> None:
    """清理过期的缓存条目。

    如果清理后仍超过 max_size，按 LRU 策略移除最久未访问的条目。

    Args:
        cache: 缓存字典（就地修改）
        max_size: 最大条目数
    """
    now = time.time()
    expired_keys = [k for k, (_, exp, _) in cache.items() if now >= exp]
    for k in expired_keys:
        del cache[k]

    if len(cache) > max_size:
        # LRU: 按 last_access_time 排序，移除最久未访问的条目
        sorted_items = sorted(
            cache.items(),
            key=lambda item: item[1][2],
        )
        to_remove = len(cache) - max_size
        for k, _ in sorted_items[:to_remove]:
            del cache[k]


def make_cache_key(tool_call: dict[str, Any]) -> str:
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


class ToolCache(IInputPlugin):
    """工具缓存 Input 插件。

    基于 (tool_name + sorted_args_json) 的 MD5 哈希作为缓存 key，
    使用模块级单例内存缓存（与 tool_cache_writer 共享）。
    命中缓存时直接返回结果，跳过后续所有插件和工具执行。
    淘汰策略为 LRU（基于最近访问时间），每次缓存命中时更新访问时间。

    配置项：
    - enabled: 是否启用缓存（默认 True）
    - default_ttl: 默认缓存过期时间，单位秒（默认 300）
    - max_size: 最大缓存条目数（默认 100）
    - exclude_tools: 不缓存的工具名列表（默认含 bash_execute 等有副作用的工具）

    优先级：35（校验级，在 schema 验证之后）
    错误策略：SKIP（缓存异常不阻塞管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化工具缓存插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用缓存（默认 True）
                - default_ttl: 默认缓存过期时间，单位秒（默认 300）
                - max_size: 最大缓存条目数（默认 100）
                - exclude_tools: 不缓存的工具名列表
                  （默认含 bash_execute/file_write 等有副作用的工具）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._default_ttl = self._config.get("default_ttl", 300)
        self._max_size = self._config.get("max_size", 100)
        # exclude_tools 合并默认值（用户配置追加，不覆盖默认的有副作用工具）
        self._exclude_tools: set[str] = set(_DEFAULT_EXCLUDE_TOOLS)
        self._exclude_tools.update(self._config.get("exclude_tools", []))

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
        exclude_tools 中的工具（有副作用）跳过缓存查询。

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
            # 有副作用的工具不查缓存（结果不可复用）
            if self._is_excluded(tc.get("name", "")):
                return PluginResult()

            cache_key = make_cache_key(tc)
            entry = _GLOBAL_CACHE.get(cache_key)
            if entry is not None:
                result, expire_time, _last_access = entry
                if now < expire_time:
                    # LRU: 命中时更新访问时间
                    _GLOBAL_CACHE[cache_key] = (result, expire_time, now)
                    cached_results.append(result)
                    logger.debug(
                        "[%s] Cache hit | key=%s",
                        self.name,
                        cache_key[:12],
                    )
                    continue
                del _GLOBAL_CACHE[cache_key]
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

        外部调用方（如 tool_cache_writer output 插件）在工具执行完成后
        调用此方法将结果缓存。有副作用的工具（exclude_tools）不写缓存。
        当缓存条目数超过 max_size 时，清理所有已过期的条目。

        Args:
            tool_call: 工具调用描述，包含 name 和 args
            result: 工具执行结果
        """
        if not self._enabled:
            return
        if self._is_excluded(tool_call.get("name", "")):
            return

        cache_key = make_cache_key(tool_call)
        expire_time = time.time() + self._default_ttl
        _GLOBAL_CACHE[cache_key] = (result, expire_time, time.time())

        if len(_GLOBAL_CACHE) > self._max_size:
            evict_expired(_GLOBAL_CACHE, self._max_size)

    def _is_excluded(self, tool_name: str) -> bool:
        """判断工具是否在排除列表（不缓存）。

        Args:
            tool_name: 工具名

        Returns:
            True 表示该工具不查/不写缓存
        """
        return tool_name in self._exclude_tools

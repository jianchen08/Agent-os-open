"""工具缓存 Input 插件。

负责在管道循环的输入阶段检查工具调用是否命中内存缓存，
命中时直接返回缓存结果并跳过后续插件执行。

缓存实现与单例字典在 agentos_plugin_sdk.tool_result_cache（SDK 单一
真值源），与 tool_cache_writer（output 阶段插件）共享同一份缓存，
input 读缓存 / output 写缓存才能命中同一条目。

使用基于 (namespace + tool_name + sorted_args_json) 的 MD5 哈希作为
缓存 key（namespace 由 pipeline state 身份键 pipeline_id/user_id/
session_id 组装，跨会话同参调用不互命中；无身份键时退化为旧键语义），
    支持 TTL 过期和最大缓存条目限制。
    淘汰策略为 LRU（基于最近访问时间）。

State 命名空间：
    - cache_hit : 是否命中缓存
    - tool_results : 缓存命中时的结果（跳过工具执行）
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import StateKeys

from agentos_plugin_sdk.tool_result_cache import (
    ToolResultCache,
    make_cache_key,
    namespace_from_state,
)

logger = logging.getLogger(__name__)


class ToolCache(IInputPlugin):
    """工具缓存 Input 插件。

    基于 (tool_name + sorted_args_json) 的 MD5 哈希作为缓存 key，
    使用 SDK tool_result_cache 的进程内共享单例缓存（与 tool_cache_writer
    共享）。命中缓存时直接返回结果，跳过后续所有插件和工具执行。
    淘汰策略为 LRU（基于最近访问时间），每次缓存命中时更新访问时间。

    配置项：
    - enabled: 是否启用缓存（默认 True）
    - default_ttl: 默认缓存过期时间，单位秒（默认 300）
    - max_size: 最大缓存条目数（默认 100）
    - exclude_tools: 不缓存的工具名列表（默认含 bash_execute 等有副作用的工具）

    优先级：35（校验级，在 schema 验证之后）
    缓存异常不阻塞管道。

    Attributes:
        _config: 插件配置字典
    """

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
        self._cache = ToolResultCache(self._config)

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
        if not self._cache.enabled:
            return PluginResult()

        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return PluginResult()

        cached_results: list[Any] = []
        # 身份隔离维度：合宿进程服务多会话，同参调用必须按 pipeline/user/
        # session 隔离（memory 族等工具返回用户私有数据），跨身份不命中。
        # 无任何身份键时为 None（旧键语义，向后兼容）。
        namespace = namespace_from_state(ctx.state)

        for tc in tool_calls:
            # 有副作用的工具不查缓存（结果不可复用）
            if self._cache.is_excluded(tc.get("name", "")):
                return PluginResult()

            cache_key = make_cache_key(tc, namespace=namespace)
            hit, result = self._cache.get(cache_key)
            if hit:
                logger.debug(
                    "[%s] Cache hit | key=%s",
                    self.name,
                    cache_key[:12],
                )
                cached_results.append(result)
                continue

            return PluginResult()

        return PluginResult(
            state_updates={
                "cache_hit": True,
                StateKeys.TOOL_RESULTS: cached_results,
                # 命中即视为工具已消费：清空 raw_tool_calls（对齐 tool_core
                # 执行后清空），否则 post 链路由见非空调用会再派 tool_execute，
                # 与 tool_cache 命中形成死循环
                StateKeys.RAW_TOOL_CALLS: [],
                # 补 messages 配对：llm_core 已 append assistant(tool_calls)
                # 消息，这里追加 role=tool 结果消息（对齐 tool_core
                # messages::rebuild 的产物形状）——否则 LLM 下一轮看不到工具
                # 结果会重发同一调用，再次命中缓存，形成死循环
                "messages": self._build_tool_result_messages(ctx, tool_calls, cached_results),
            },
            skip_remaining=True,
        )

    def _build_tool_result_messages(
        self,
        ctx: PluginContext,
        tool_calls: list[dict[str, Any]],
        cached_results: list[Any],
    ) -> dict[str, Any]:
        """构造缓存命中的 tool 结果消息 ops（对齐 tool_core messages::rebuild 形状）。

        tool_core 正常路径在 execute 后重建 messages：assistant(tool_calls) 由
        llm_core 已 append，tool_core 追加 role=tool 配对消息（content 为结果
        序列化文本、tool_result envelope 为完整结果）。本方法对齐该形状，
        用引擎的 _ops 增量形态（set 无 seq = append）追加配对消息。

        Args:
            ctx: 插件执行上下文
            tool_calls: 本轮 raw_tool_calls（含 id）
            cached_results: 缓存命中的结果列表（与 tool_calls 按下标配对）

        Returns:
            messages ops 字典（{"_ops": [...]}）
        """
        ops: list[dict[str, Any]] = []
        for i, tc in enumerate(tool_calls):
            if i >= len(cached_results):
                break
            result = cached_results[i]
            call_id = tc.get("id") or f"call_{i}"
            # 内容序列化对齐 tool_core serialize_for_content（JSON 文本）
            content = result
            if not isinstance(result, str):
                try:
                    content = json.dumps(result, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    content = str(result)
            tool_msg: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": content,
                "tool_result": {
                    "call_id": call_id,
                    "tool_name": tc.get("name", ""),
                    "success": True,
                    "error": None,
                    "data": result if not isinstance(result, str) else {"content": result},
                    "metadata": None,
                    "duration_ms": 0.0,
                },
            }
            ops.append({"op": "set", "msg": tool_msg})
        return {"_ops": ops}

    def put(self, tool_call: dict[str, Any], result: Any) -> None:
        """将工具执行结果写入缓存（委托 SDK ToolResultCache.put）。

        Args:
            tool_call: 工具调用描述，包含 name 和 args
            result: 工具执行结果
        """
        self._cache.put(tool_call, result)

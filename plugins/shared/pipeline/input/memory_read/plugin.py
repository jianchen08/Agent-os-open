"""记忆读取 Input 插件。

包装 IRetriever -> IInputPlugin，在管道输入阶段
从记忆系统检索相关内容写入 state。

依赖注入：通过 ctx.get_service("retriever") 获取 IRetriever 实例，
构造函数只接受 config，由 build_plugin_registry 统一实例化。

inject_type 注入方式（3×3 矩阵的"注入"维度）：
    - FULL     : 全量注入（取最近 N 条记忆，不做 query 过滤）
    - RETRIEVAL: 检索注入（按 query 过滤，默认行为）
    - SUMMARY  : 摘要注入（检索后对结果做摘要）

State 命名空间：
    - memory.retrieved : 本插件写入的记忆检索结果。
      首轮执行检索后写入 state，后续轮次检测到已有值则跳过检索直接复用（缓存语义）。
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)

# 注入方式常量
INJECT_FULL = "full"
INJECT_RETRIEVAL = "retrieval"
INJECT_SUMMARY = "summary"
_VALID_INJECT_TYPES = (INJECT_FULL, INJECT_RETRIEVAL, INJECT_SUMMARY)


class MemoryReadPlugin(IInputPlugin):
    """记忆读取 Input 插件。

    从记忆系统检索与当前用户输入相关的内容，
    按 inject_type 决定注入形态，写入 state["memory.retrieved"]。

    通过 ctx.get_service("retriever") 获取 IRetriever 实例，
    无需在构造时注入。

    优先级：35（数据级，在 knowledge_inject 之后）
    错误策略：SKIP（检索失败不影响管道继续）

    Attributes:
        _config: 插件配置字典
        _inject_type: 注入方式（full/retrieval/summary）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化记忆读取插件。

        Args:
            config: 插件配置字典，支持以下键：
                - retrieval: 检索方式（默认 semantic）
                - top_k: 返回结果数量（默认 5）
                - memory_type: 记忆类型（默认 semantic）
                - inject_type: 注入方式（默认 retrieval）
        """
        self._config = config or {}
        self._top_k = self._config.get("top_k", 5)
        self._memory_type = self._config.get("memory_type", "semantic")
        self._inject_type = self._config.get("inject_type", INJECT_RETRIEVAL)
        if self._inject_type not in _VALID_INJECT_TYPES:
            logger.warning("[%s] 未知 inject_type=%s，回退为 retrieval", self.name, self._inject_type)
            self._inject_type = INJECT_RETRIEVAL
        self._enabled_by_agent: bool = True

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "memory_read"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 35

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """从记忆系统检索相关内容并按 inject_type 注入。

        通过 ctx.get_service("retriever") 获取检索器，
        无 retriever 服务时静默跳过。

        从 ctx.state["plugin_configs"] 读取 Agent 覆盖的配置，
        Agent 可禁用此插件。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含记忆检索结果的插件执行结果
        """
        self._apply_runtime_config(ctx)

        if not self._enabled_by_agent:
            return PluginResult(state_updates={"memory.retrieved": []})

        # 缓存检测：首轮检索后结果写入 state，后续轮次直接复用
        cached = ctx.state.get("memory.retrieved")
        if cached is not None:
            logger.debug("[%s] 复用首轮记忆检索缓存 | results=%d", self.name, len(cached))
            return PluginResult(state_updates={"memory.retrieved": cached})

        # 从服务注册表获取 retriever
        try:
            retriever = ctx.get_service("retriever")
        except KeyError:
            logger.debug("[%s] No retriever service, skipping", self.name)
            return PluginResult(state_updates={"memory.retrieved": []})

        try:
            context_data = await self._build_injection(ctx, retriever)
            logger.debug(
                "[%s] 记忆注入完成 | inject_type=%s | results=%d",
                self.name,
                self._inject_type,
                len(context_data) if isinstance(context_data, list) else 1,
            )
            return PluginResult(state_updates={"memory.retrieved": context_data})
        except Exception as e:
            logger.warning("[%s] 记忆检索失败: %s", self.name, e)
            return PluginResult(
                state_updates={"memory.retrieved": []},
                error=e,
            )

    async def _build_injection(self, ctx: PluginContext, retriever: Any) -> Any:
        """按 inject_type 构建注入内容。

        Args:
            ctx: 插件执行上下文
            retriever: IRetriever 实例

        Returns:
            注入内容：FULL/RETRIEVAL 返回 list[dict]，SUMMARY 返回 str
        """
        user_id = ctx.state.get("user_id")
        top_k = self._top_k
        memory_type = self._memory_type

        if self._inject_type == INJECT_FULL:
            # 全量注入：取最近 top_k 条，不做 query 过滤
            results = await self._retrieve_all(retriever, top_k, memory_type, user_id)
            return [r.to_dict() for r in results]

        if self._inject_type == INJECT_SUMMARY:
            return await self._build_summary(ctx, retriever, top_k, memory_type, user_id)

        # 默认 RETRIEVAL：按 query 检索
        query = ctx.state.get("user_message", "")
        if not query:
            return []
        results = await retriever.retrieve(
            query=query, user_id=user_id, top_k=top_k, memory_type=memory_type
        )
        return [r.to_dict() for r in results]

    async def _retrieve_all(
        self,
        retriever: Any,
        top_k: int,
        memory_type: str,
        user_id: str | None,
    ) -> list[Any]:
        """全量召回最近记忆（FULL 注入）。

        retriever 若支持 retrieve_all 则直接用，否则用空 query 取回并截断。

        Args:
            retriever: IRetriever 实例
            top_k: 返回数量
            memory_type: 记忆类型
            user_id: 用户 ID

        Returns:
            搜索结果列表
        """
        if hasattr(retriever, "retrieve_all"):
            return await retriever.retrieve_all(
                top_k=top_k, memory_type=memory_type, user_id=user_id
            )
        # 降级：空 query 不可行，用大 top_k 空串检索兜底
        return await retriever.retrieve(
            query=" ", user_id=user_id, top_k=top_k, memory_type=memory_type
        )

    async def _build_summary(
        self,
        ctx: PluginContext,
        retriever: Any,
        top_k: int,
        memory_type: str,
        user_id: str | None,
    ) -> str:
        """摘要注入（SUMMARY）。

        优先调用注入的 summarizer 服务（如 memory sidecar 暴露的 summarize 能力），
        不可用时降级为检索结果拼接。

        Args:
            ctx: 插件执行上下文
            retriever: IRetriever 实例
            top_k: 返回数量
            memory_type: 记忆类型
            user_id: 用户 ID

        Returns:
            摘要文本
        """
        # 优先使用注入的 summarizer 服务（签名: async summarize(memory_type, top_k, user_id) -> str）
        summarizer = None
        try:
            summarizer = ctx.get_service("memory_summarizer")
        except KeyError:
            pass

        if summarizer is not None and hasattr(summarizer, "summarize"):
            try:
                return await summarizer.summarize(
                    memory_type=memory_type, top_k=top_k, user_id=user_id
                )
            except Exception as e:
                logger.warning("[%s] summarizer 服务调用失败，降级拼接: %s", self.name, e)

        # 降级：检索后拼接（TODO: 接入 memory.summarize 工具后启用真实 LLM 摘要）
        query = ctx.state.get("user_message", "")
        if query:
            results = await retriever.retrieve(
                query=query, user_id=user_id, top_k=top_k, memory_type=memory_type
            )
        else:
            results = await self._retrieve_all(retriever, top_k, memory_type, user_id)

        parts = [getattr(r, "content", "") or str(r) for r in results]
        return "\n".join(p for p in parts if p)

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 ctx.state 读取 Agent 覆盖的运行时配置。

        Agent 可通过 plugins.disabled 禁用此插件，
        或通过 plugins.enabled.memory_read 覆盖参数。

        Args:
            ctx: 插件执行上下文
        """
        from pipeline.plugin import find_plugin_config  # noqa: PLC0415

        plugin_configs = ctx.state.get("plugin_configs", {})
        config = find_plugin_config("memory_read", plugin_configs)

        if not config.get("enabled", True):
            self._enabled_by_agent = False
            return

        self._enabled_by_agent = True
        if "top_k" in config:
            self._top_k = config["top_k"]
        if "memory_type" in config:
            self._memory_type = config["memory_type"]
        if "inject_type" in config:
            inject_type = config["inject_type"]
            if inject_type in _VALID_INJECT_TYPES:
                self._inject_type = inject_type
            else:
                logger.warning(
                    "[%s] 运行时 inject_type=%s 非法，保留 %s",
                    self.name,
                    inject_type,
                    self._inject_type,
                )

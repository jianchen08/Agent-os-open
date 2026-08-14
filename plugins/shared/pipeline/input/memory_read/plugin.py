"""记忆读取 Input 插件。

包装 IMemoryBackend -> IInputPlugin，在管道输入阶段
从记忆系统检索相关内容写入 state。

依赖注入：通过模块级 set_memory_backend() 注入 IMemoryBackend 实例
（由 server.py on_load 注入，测试直接赋值），不再依赖 0.2 中不存在的
ctx.get_service("retriever") 服务。

inject_type 注入方式（3×3 矩阵的"注入"维度）：
    - FULL     : 全量注入（空 query 取最近 N 条记忆，不做 query 过滤）
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

# ═══════════════════════════════════════════════════════════
# 模块级依赖注入（由 server.py 的 on_load 注入，测试直接赋值）
# ═══════════════════════════════════════════════════════════

# 长期记忆后端（IMemoryBackend，Hindsight/Kernel 统一形态）；None 时跳过记忆检索
_memory_backend: Any | None = None

# 能力调用器（wiring.make_capability_caller 注入：tool-executor/service-registry 优先）；
# SUMMARY 注入经它跨进程调 hindsight.summarize；None 时降级拼接
_capability_caller: Any | None = None


def set_memory_backend(backend: Any | None) -> None:
    """注入长期记忆后端（IMemoryBackend 实例或兼容 duck-type）。

    由 server.py on_load 调用，把 Step 3 构建的 Hindsight/Kernel 后端注入进来；
    测试环境直接传 FakeBackend/MagicMock。传 None 清空。

    Args:
        backend: 实现 add/search/delete/import_document 的后端实例
    """
    global _memory_backend
    _memory_backend = backend


def set_capability_caller(caller: Any | None) -> None:
    """注入能力调用器（async fn `(method, params) -> Any`）。

    由 server.py on_load 经 wiring.make_capability_caller 注入；
    SUMMARY 注入用 tool-executor.invoke 跨进程调 hindsight.summarize。
    传 None 清空（降级为检索拼接）。
    """
    global _capability_caller
    _capability_caller = caller


# 注入方式常量
INJECT_FULL = "full"
INJECT_RETRIEVAL = "retrieval"
INJECT_SUMMARY = "summary"
_VALID_INJECT_TYPES = (INJECT_FULL, INJECT_RETRIEVAL, INJECT_SUMMARY)


class MemoryReadPlugin(IInputPlugin):
    """记忆读取 Input 插件。

    从记忆系统检索与当前用户输入相关的内容，
    按 inject_type 决定注入形态，写入 state["memory.retrieved"]。

    通过模块级 set_memory_backend() 注入 IMemoryBackend 实例，
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

        通过模块级 _memory_backend 检索记忆（由 set_memory_backend 注入），
        无后端时静默跳过。

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

        # 从模块级注入的记忆后端获取检索能力（无后端时静默跳过）
        if _memory_backend is None:
            logger.debug("[%s] 无记忆后端（未注入 IMemoryBackend），跳过", self.name)
            return PluginResult(state_updates={"memory.retrieved": []})

        try:
            context_data = await self._build_injection(ctx, _memory_backend)
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

    async def _build_injection(self, ctx: PluginContext, backend: Any) -> Any:
        """按 inject_type 构建注入内容。

        Args:
            ctx: 插件执行上下文
            backend: IMemoryBackend 实例（search 返回统一形态 dict 列表）

        Returns:
            注入内容：FULL/RETRIEVAL 返回 list[dict]，SUMMARY 返回 str
        """
        user_id = ctx.state.get("user_id", "")
        top_k = self._top_k
        memory_type = self._memory_type

        if self._inject_type == INJECT_FULL:
            # 全量注入：空 query 取回最近 top_k 条，不做 query 过滤
            results = await self._retrieve_all(backend, top_k, memory_type, user_id)
            return self._normalize_results(results)

        if self._inject_type == INJECT_SUMMARY:
            return await self._build_summary(ctx, backend, top_k, memory_type, user_id)

        # 默认 RETRIEVAL：按 query 检索
        query = ctx.state.get("user_message", "")
        if not query:
            return []
        results = await backend.search(query=query, user_id=user_id, top_k=top_k, memory_type=memory_type)
        return self._normalize_results(results)

    async def _retrieve_all(
        self,
        backend: Any,
        top_k: int,
        memory_type: str,
        user_id: str,
    ) -> list[Any]:
        """全量召回最近记忆（FULL 注入）。

        IMemoryBackend 无 retrieve_all 能力，统一用空 query 检索
        （后端约定：空 query 表示全量取回）。

        Args:
            backend: IMemoryBackend 实例
            top_k: 返回数量
            memory_type: 记忆类型
            user_id: 用户 ID

        Returns:
            搜索结果列表（统一形态 dict 列表）
        """
        return await backend.search(query="", user_id=user_id, top_k=top_k, memory_type=memory_type)

    async def _build_summary(
        self,
        ctx: PluginContext,
        backend: Any,
        top_k: int,
        memory_type: str,
        user_id: str,
    ) -> str:
        """摘要注入（SUMMARY）。

        优先调用注入的 summarizer 服务（如 memory sidecar 暴露的 summarize 能力），
        不可用时降级为检索结果拼接。

        Args:
            ctx: 插件执行上下文
            backend: IMemoryBackend 实例
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
                return await summarizer.summarize(memory_type=memory_type, top_k=top_k, user_id=user_id)
            except Exception as e:
                logger.warning("[%s] summarizer 服务调用失败，降级拼接: %s", self.name, e)

        # 其次：经 capability_caller 跨进程调 hindsight.summarize（recall + reflect 真摘要）
        if _capability_caller is not None:
            try:
                query = ctx.state.get("user_message", "")
                params = {
                    "tool_name": "hindsight.summarize",
                    "args": {
                        "bank_id": user_id,
                        "query": query,
                        "top_k": top_k,
                        "memory_type": memory_type,
                    },
                }
                result = await _capability_caller("tool-executor.invoke", params)
                if isinstance(result, dict) and result.get("summary"):
                    return str(result["summary"])
                if isinstance(result, dict) and result.get("error"):
                    logger.warning(
                        "[%s] hindsight.summarize 不可用降级拼接: %s", self.name, result.get("error")
                    )
            except Exception as e:
                logger.warning("[%s] hindsight.summarize 调用失败，降级拼接: %s", self.name, e)

        # 兜底：检索后拼接（hindsight 不可用 / 无 caller 时的保险路径）
        query = ctx.state.get("user_message", "")
        if query:
            results = await backend.search(query=query, user_id=user_id, top_k=top_k, memory_type=memory_type)
        else:
            results = await self._retrieve_all(backend, top_k, memory_type, user_id)

        parts = [
            r.get("content", "") if isinstance(r, dict) else (getattr(r, "content", "") or str(r)) for r in results
        ]
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _normalize_results(results: list[Any]) -> list[dict[str, Any]]:
        """把后端检索结果归一化为统一形态 {id, content, score, memory_type, metadata}。

        IMemoryBackend.search 已返回该形态，直接透传；兼容老式对象（带 to_dict()）。
        每条追加 _context_form="recall" 语义标记（内部字段，压缩优化任务 1）：
        声明"这是从记忆库检索的内容"，供压缩链路差异化摘要；
        若后续被组装进 LLM 消息，发送前由 llm_core 清理。

        Args:
            results: 后端搜索结果列表

        Returns:
            统一形态的 dict 列表
        """
        normalized: list[dict[str, Any]] = []
        for r in results or []:
            if isinstance(r, dict):
                item = dict(r)
                item.setdefault("_context_form", "recall")
                normalized.append(item)
            elif hasattr(r, "to_dict"):
                d = r.to_dict()
                if isinstance(d, dict):
                    d.setdefault("_context_form", "recall")
                    normalized.append(d)
        return normalized

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

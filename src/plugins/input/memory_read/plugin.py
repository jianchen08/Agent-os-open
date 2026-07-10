"""记忆读取 Input 插件。

包装 IRetriever -> IInputPlugin，在管道输入阶段
从记忆系统检索相关内容写入 state。

依赖注入：通过 ctx.get_service("retriever") 获取 IRetriever 实例，
构造函数只接受 config，由 build_plugin_registry 统一实例化。

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


class MemoryReadPlugin(IInputPlugin):
    """记忆读取 Input 插件。

    从记忆系统检索与当前用户输入相关的内容，
    将检索结果写入 state["memory.retrieved"]。

    通过 ctx.get_service("retriever") 获取 IRetriever 实例，
    无需在构造时注入。

    优先级：35（数据级，在 knowledge_inject 之后）
    错误策略：SKIP（检索失败不影响管道继续）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化记忆读取插件。

        Args:
            config: 插件配置字典，支持以下键：
                - retrieval: 检索方式（默认 semantic）
                - top_k: 返回结果数量（默认 5）
                - memory_type: 记忆类型（默认 semantic）
        """
        self._config = config or {}
        self._top_k = self._config.get("top_k", 5)
        self._memory_type = self._config.get("memory_type", "semantic")
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
        """从记忆系统检索相关内容。

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

        query = ctx.state.get("user_message", "")
        user_id = ctx.state.get("user_id")
        top_k = self._top_k
        memory_type = self._memory_type

        if not query:
            return PluginResult(state_updates={"memory.retrieved": []})

        try:
            results = await retriever.retrieve(
                query=query,
                user_id=user_id,
                top_k=top_k,
                memory_type=memory_type,
            )

            # 格式化检索结果
            context_data = [r.to_dict() for r in results]

            logger.debug(
                "[%s] 记忆检索完成 | query_len=%d | results=%d",
                self.name,
                len(query),
                len(results),
            )

            return PluginResult(state_updates={"memory.retrieved": context_data})

        except Exception as e:
            logger.warning("[%s] 记忆检索失败: %s", self.name, e)
            return PluginResult(
                state_updates={"memory.retrieved": []},
                error=e,
            )

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

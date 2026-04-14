"""提示词构建 Input 插件 — 从旧代码 agents/prompt_builder.py 迁移。

负责在管道循环的输入阶段组装 SystemMessage，
按旧代码 layer_order 顺序拼接各层内容。

产出：
    - state["system_message"]: 一条 SystemMessage（不含历史消息和动态变量）
    - state["prompt.dynamic_vars"]: 动态变量文本（由 LLMCore 追加在历史消息之后）

构建顺序（与旧代码 context_window_config.yaml 的 layer_order 一致）：
    1. system_prompt      <- state["context.system_prompt"]
    2. tools_description  <- state["prompt.tool_descriptions"]（仅当开关开启时拼入）
    3. static_vars        <- agent_config 或 state 读取
    4. knowledge.context  <- 知识注入插件产出
    5. memory.retrieved   <- 记忆检索插件产出
    6. l3_memory          <- 关键词索引（从 ChunkService 读取）
    7. l2_memory          <- 三元组摘要（从 ChunkService 读取）
    8. l1_memory          <- 八段摘要（从 ChunkService 读取）
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class PromptBuildPlugin(IInputPlugin):
    """提示词构建 Input 插件。

    只产出一条 SystemMessage 写入 state["system_message"]，
    不包含历史消息和动态变量。历史消息和动态变量由 LLMCore._build_messages 负责组装。

    优先级：50（构建级，在 context_build 和 memory_read 之后）
    错误策略：ABORT（没有提示词 LLM 无法调用）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化提示词构建插件。

        Args:
            config: 插件配置字典，支持以下键：
                - include_tools_description_in_prompt: 是否将工具描述拼入 SystemMessage（默认 False）
                - include_static_vars: 是否包含静态变量（默认 True）
                - include_compressed_layers: 是否包含压缩层（默认 True）
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "prompt_build"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 50)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """构建 SystemMessage 并写入 state。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含 system_message 和 prompt.dynamic_vars 的插件执行结果
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行提示词构建逻辑。

        Returns:
            要写入 state 的字段字典，包含 system_message 和 prompt.dynamic_vars
        """
        updates: dict[str, Any] = {}

        # 按 layer_order 顺序组装系统消息内容
        system_content = await self._build_system_content(ctx)

        # 产出 SystemMessage
        updates["system_message"] = {"role": "system", "content": system_content}

        # 单独产出动态变量（由 LLMCore 追加在历史消息之后）
        dynamic_vars = self._build_dynamic_vars(ctx)
        if dynamic_vars:
            updates["prompt.dynamic_vars"] = dynamic_vars

        logger.debug(
            "[%s] SystemMessage built | content_len=%d | dynamic_vars=%s",
            self.name, len(system_content), bool(dynamic_vars),
        )

        return updates

    async def _build_system_content(self, ctx: PluginContext) -> str:
        """按旧代码 layer_order 顺序组装系统消息内容。

        顺序：system_prompt -> tools_description -> static_vars ->
              l3_memory -> l2_memory -> l1_memory
        不含 recent_messages 和 dynamic_vars。

        Args:
            ctx: 插件执行上下文

        Returns:
            系统消息内容字符串
        """
        parts: list[str] = []

        # 1. system_prompt（兼容 context.system_prompt 和 system_prompt 两种键名）
        system_prompt = ctx.state.get("context.system_prompt", "") or ctx.state.get("system_prompt", "")
        if system_prompt:
            parts.append(system_prompt)

        # 2. tools_description（仅当开关开启时拼入，默认走 function calling）
        if self._config.get("include_tools_description_in_prompt", False):
            tool_desc = ctx.state.get("prompt.tool_descriptions", "")
            if tool_desc:
                parts.append(tool_desc)

        # 3. static_vars
        if self._config.get("include_static_vars", True):
            static_vars_text = await self._load_static_vars(ctx)
            if static_vars_text:
                parts.append(static_vars_text)

        # 4. knowledge.context（知识注入插件产出）
        knowledge_context = ctx.state.get("knowledge.context", "")
        if knowledge_context:
            parts.append(knowledge_context)

        # 5. memory.retrieved（记忆检索插件产出）
        memory_retrieved = ctx.state.get("memory.retrieved", "")
        if memory_retrieved:
            parts.append(memory_retrieved)

        # 6-8. 压缩层（l3 -> l2 -> l1）
        if self._config.get("include_compressed_layers", True):
            # L3: 关键词索引
            l3_text = await self._load_l3_keywords(ctx)
            if l3_text:
                parts.append(l3_text)

            # L2: 三元组摘要
            l2_text = await self._load_compressed_layer(ctx, "L2")
            if l2_text:
                parts.append(l2_text)

            # L1: 八段摘要
            l1_text = await self._load_compressed_layer(ctx, "L1")
            if l1_text:
                parts.append(l1_text)

        return "\n\n".join(parts)

    async def _load_static_vars(self, ctx: PluginContext) -> str:
        """从 agent_config 或 state 加载静态变量。

        支持 4 种类型：timestamp / session / file / content
        支持 3 种模式：exact(直接文本) / vector(向量检索) / hybrid
        支持 output_format: full / summary

        Args:
            ctx: 插件执行上下文

        Returns:
            格式化后的静态变量文本，或空字符串
        """
        # 从 state 读取 agent 配置中的 static_vars 定义
        static_vars_def = ctx.state.get("context.static_vars", [])
        if not static_vars_def:
            # 尝试从插件配置读取
            static_vars_def = self._config.get("static_vars", [])
        if not static_vars_def:
            return ""

        parts: list[str] = []
        session_id = ctx.state.get("context.session_id", "")

        for var_def in static_vars_def:
            if not isinstance(var_def, dict):
                continue

            # 检查 enabled 开关
            if not var_def.get("enabled", True):
                continue

            var_type = var_def.get("type", "")
            var_name = var_def.get("name", var_type)
            mode = var_def.get("mode", "exact")
            output_format = var_def.get("output_format", "full")

            content = ""

            if var_type == "timestamp":
                now = datetime.now(UTC)
                fmt = var_def.get("format", "%Y-%m-%d %H:%M:%S")
                content = now.strftime(fmt)

            elif var_type == "session":
                content = session_id

            elif var_type == "file":
                file_path = var_def.get("path", "")
                if file_path:
                    try:
                        from pathlib import Path
                        p = Path(file_path)
                        if p.exists():
                            content = p.read_text(encoding="utf-8")
                    except Exception as e:
                        logger.warning("[%s] 读取静态变量文件失败 | path=%s | error=%s", self.name, file_path, e)

            elif var_type == "content":
                content = var_def.get("value", "")

            if not content:
                continue

            # 向量检索模式：通过 retriever 服务检索相关内容
            if mode in ("vector", "hybrid") and content:
                try:
                    retriever = ctx.get_service("retriever")
                    results = await retriever.retrieve(query=content, top_k=var_def.get("top_k", 5))
                    if results:
                        retrieved_text = "\n".join(r.content for r in results if hasattr(r, "content"))
                        if mode == "hybrid":
                            content = f"{content}\n\n### 相关检索结果\n{retrieved_text}"
                        else:
                            content = retrieved_text
                except (KeyError, Exception) as e:
                    logger.debug("[%s] 静态变量向量检索跳过 | name=%s | error=%s", self.name, var_name, e)

            # 摘要模式标记
            if output_format == "summary":
                content = f"[摘要] {content}"

            if content:
                parts.append(f"### {var_name}\n{content}")

        if not parts:
            return ""

        return "## 静态变量\n" + "\n\n".join(parts)

    async def _load_compressed_layer(self, ctx: PluginContext, layer: str) -> str:
        """从 ChunkService 读取指定层的压缩块。

        Args:
            ctx: 插件执行上下文
            layer: 压缩层标识（"L1" / "L2"）

        Returns:
            格式化后的压缩层文本，或空字符串
        """
        try:
            chunk_service = ctx.get_service("chunk_service")
        except KeyError:
            logger.debug("[%s] No chunk_service, skipping %s layer", self.name, layer)
            return ""

        session_id = ctx.state.get("context.session_id", "")
        if not session_id:
            return ""

        try:
            chunks = await chunk_service.find_by_session(session_id, layer)
        except Exception as e:
            logger.warning("[%s] 读取 %s 压缩块失败 | error=%s", self.name, layer, e)
            return ""

        if not chunks:
            return ""

        layer_names = {"L1": "八段摘要", "L2": "三元组摘要"}
        layer_name = layer_names.get(layer, layer)

        chunk_texts = []
        for chunk in chunks:
            chunk_header = f"[{chunk.sequence_start}-{chunk.sequence_end}]"
            chunk_texts.append(f"{chunk_header} {chunk.content}")

        return f"## {layer_name}（{layer}）\n" + "\n".join(chunk_texts)

    async def _load_l3_keywords(self, ctx: PluginContext) -> str:
        """从 L1 和 L2 压缩块提取关键词索引。

        读取 L1 和 L2 压缩块的 keywords 字段，去重后格式化为关键词列表。

        Args:
            ctx: 插件执行上下文

        Returns:
            格式化后的关键词索引文本，或空字符串
        """
        try:
            chunk_service = ctx.get_service("chunk_service")
        except KeyError:
            logger.debug("[%s] No chunk_service, skipping L3 keywords", self.name)
            return ""

        session_id = ctx.state.get("context.session_id", "")
        if not session_id:
            return ""

        all_keywords: set[str] = set()

        for layer in ("L1", "L2"):
            try:
                chunks = await chunk_service.find_by_session(session_id, layer)
                for chunk in chunks:
                    if hasattr(chunk, "keywords") and chunk.keywords:
                        all_keywords.update(chunk.keywords)
            except Exception as e:
                logger.warning("[%s] 读取 %s 关键词失败 | error=%s", self.name, layer, e)

        if not all_keywords:
            return ""

        keywords_list = sorted(all_keywords)
        return "## 关键词索引（L3）\n" + ", ".join(keywords_list)

    def _build_dynamic_vars(self, ctx: PluginContext) -> str:
        """构建动态变量内容。

        包含日期、时间、Agent 名称、会话 ID 等动态信息。
        动态变量不拼入 SystemMessage，由 LLMCore 追加在历史消息之后。

        Args:
            ctx: 插件执行上下文

        Returns:
            动态变量文本，或空字符串
        """
        now = datetime.now(UTC)
        parts: list[str] = []

        parts.append(f"- 日期: {now.strftime('%Y-%m-%d')}")
        parts.append(f"- 时间: {now.strftime('%H:%M:%S')}")

        agent_name = ctx.state.get("context.agent_name", "")
        if agent_name:
            parts.append(f"- Agent: {agent_name}")

        session_id = ctx.state.get("context.session_id", "")
        if session_id:
            parts.append(f"- 会话: {session_id}")

        return "\n".join(parts)

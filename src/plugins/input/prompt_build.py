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
    6. l2_memory          <- 三元组摘要（从 ChunkService 读取，含关键词）
    7. l1_memory          <- 八段摘要（从 ChunkService 读取，含关键词）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)

# 语言指令映射 — 根据语言代码生成对应的思考和回复指令
LANGUAGE_INSTRUCTIONS: dict[str, str] = {
    "zh-CN": "请使用中文（简体）思考和回复，所有输出内容必须使用中文",
    "zh-TW": "請使用繁體中文思考和回覆，所有輸出內容必須使用繁體中文",
    "en": "Please think and respond in English, all output must be in English",
    "ja": "日本語で思考し日本語で回答してください、すべての出力は日本語で行ってください",
    "ko": "한국어로 생각하고 한국어로 답변하세요, 모든 출력은 한국어로 작성하세요",
    "fr": "Pensez et répondez en français, toutes les sorties doivent être en français",
    "de": "Denken und antworten Sie auf Deutsch, alle Ausgaben müssen auf Deutsch sein",
    "es": "Piense y responda en español, toda la salida debe estar en español",
}


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
              memory.retrieved -> l2_memory -> l1_memory
        不含 recent_messages 和 dynamic_vars。

        知识检索已统一到 static_vars 的 tags/retrieval 类型中，
        不再单独读取 knowledge.context。

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

        # 1.5 语言指令（会话级不变，注入到系统消息）
        lang = self._config.get("language", "")
        if lang:
            instruction = LANGUAGE_INSTRUCTIONS.get(lang)
            if not instruction:
                instruction = f"请使用{lang}思考和回复，所有输出内容必须使用{lang}"
            parts.append(f"# 语言设置\n{instruction}")

        # 2. tools_description（仅当开关开启时拼入，默认走 function calling）
        if self._config.get("include_tools_description_in_prompt", False):
            tool_desc = ctx.state.get("prompt.tool_descriptions", "")
            if tool_desc:
                parts.append(tool_desc)

        # 3. static_vars（含 rules/path/reference/tags 等类型，知识检索统一在此处理）
        if self._config.get("include_static_vars", True):
            static_vars_text = await self._load_static_vars(ctx)
            if static_vars_text:
                parts.append(static_vars_text)

        # 4. memory.retrieved（记忆检索插件产出）
        memory_retrieved = ctx.state.get("memory.retrieved", "")
        if memory_retrieved:
            parts.append(memory_retrieved)

        # 5-6. 压缩层（l2 -> l1，关键词随压缩块一起加载）
        if self._config.get("include_compressed_layers", True):
            # L2: 三元组摘要（含关键词）
            l2_text = await self._load_compressed_layer(ctx, "L2")
            if l2_text:
                parts.append(l2_text)

            # L1: 八段摘要（含关键词）
            l1_text = await self._load_compressed_layer(ctx, "L1")
            if l1_text:
                parts.append(l1_text)

        return "\n\n".join(parts)

    async def _load_static_vars(self, ctx: PluginContext) -> str:
        """从 state 中的 context.static_vars 加载静态变量。

        静态变量在构建时拼入系统提示词（system_message），不属于动态变量。
        支持的类型：rules / path / reference / content / timestamp / session / tags(retrieval)。

        支持 3 种模式：exact(直接文本) / vector(向量检索) / hybrid
        支持 output_format: full / summary
        支持 inject_type: full / summary / retrieval（用于 tags 类型的知识检索）

        Args:
            ctx: 插件执行上下文

        Returns:
            格式化后的静态变量文本，或空字符串
        """
        static_vars_def = ctx.state.get("context.static_vars", [])
        if not static_vars_def:
            static_vars_def = self._config.get("static_vars", [])
        if not static_vars_def:
            return ""

        parts: list[str] = []
        session_id = ctx.state.get("context.session_id", "")
        constraints = ctx.state.get("constraints", {})

        for var_def in static_vars_def:
            if not isinstance(var_def, dict):
                continue

            if not var_def.get("enabled", True):
                continue

            var_type = var_def.get("type", "")
            var_name = var_def.get("name", var_type)
            mode = var_def.get("mode", "exact")
            output_format = var_def.get("output_format", "full")

            content = ""

            if var_type == "rules":
                rules_parts = []
                for c in constraints.get("hard", []):
                    rules_parts.append(f"- [必须] {c}")
                for c in constraints.get("soft", []):
                    rules_parts.append(f"- [建议] {c}")
                content = "\n".join(rules_parts)

            elif var_type == "path":
                file_path = var_def.get("path", "")
                if file_path:
                    try:
                        from pathlib import Path
                        p = Path(file_path)
                        if p.exists():
                            content = await asyncio.to_thread(p.read_text, "utf-8")
                    except Exception as e:
                        logger.warning("[%s] 读取静态变量文件失败 | path=%s | error=%s", self.name, file_path, e)

            elif var_type in ("reference", "content", ""):
                # "": 兜底 — YAML 中省略 type 但提供了 content 的情况
                content = var_def.get("content", "") or var_def.get("value", "")
                if not content and var_def.get("tags"):
                    # 空 type + tags → 走检索
                    content = await self._retrieve_by_tags(ctx, var_def)

            elif var_type == "timestamp":
                now = datetime.now(UTC)
                fmt = var_def.get("format", "%Y-%m-%d %H:%M:%S")
                content = now.strftime(fmt)

            elif var_type == "session":
                content = session_id

            elif var_type == "retrieval":
                content = await self._retrieve_by_tags(ctx, var_def)

            if not content:
                continue

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

            if output_format == "summary":
                content = f"[摘要] {content}"

            if content:
                parts.append(f"### {var_name}\n{content}")

        if not parts:
            return ""

        return "## 静态变量\n" + "\n\n".join(parts)

    async def _retrieve_by_tags(self, ctx: PluginContext, var_def: dict[str, Any]) -> str:
        """通过 tags 从知识库检索内容。

        优先读取 state["knowledge.context"]（由 knowledge_inject 插件产出），
        避免与 knowledge_inject 重复调用 KnowledgeService。
        仅在 knowledge.context 为空时才自行检索。

        支持 inject_type: full(完整内容) / summary(摘要) / retrieval(检索)
        当 var_def 中有 tags 但无 type 字段时，自动触发此方法。

        Args:
            ctx: 插件执行上下文
            var_def: 变量定义字典，包含 tags/inject_type/top_k 等

        Returns:
            检索到的知识内容，或空字符串
        """
        tags = var_def.get("tags", [])
        if not tags:
            return ""

        inject_type = var_def.get("inject_type", "full")
        top_k = var_def.get("top_k", 5)

        # 优先读取 knowledge_inject 插件已产出的知识内容，避免重复检索
        cached_context = ctx.state.get("knowledge.context", "")
        if cached_context:
            if inject_type == "summary":
                snippet = cached_context[:200] + "..." if len(cached_context) > 200 else cached_context
                return f"- {snippet}"
            return cached_context

        # 降级：knowledge_inject 未产出时自行检索
        try:
            semantic_storage = ctx.get_service("semantic_storage")
        except KeyError:
            logger.debug("[%s] No semantic_storage service, skipping tags retrieval", self.name)
            return ""

        try:
            from memory.knowledge_service import KnowledgeService
            knowledge_service = KnowledgeService(semantic_storage=semantic_storage)
            user_id = ctx.state.get("user_id", "")
            result = await knowledge_service.list_semantic_memory(user_id)
            items = result.get("items", [])
        except Exception as e:
            logger.debug("[%s] 知识检索失败 | tags=%s | error=%s", self.name, tags, e)
            return ""

        if not items:
            return ""

        filtered = []
        for item in items:
            item_tags = item.get("tags", [])
            if not item_tags:
                item_content = item.get("content", "")
                if item_content:
                    filtered.append(item_content)
            elif any(t in item_tags for t in tags):
                filtered.append(item.get("content", ""))

        filtered = [c for c in filtered if c][:top_k]
        if not filtered:
            return ""

        if inject_type == "summary":
            parts = []
            for c in filtered:
                snippet = c[:200] + "..." if len(c) > 200 else c
                parts.append(f"- {snippet}")
            return "\n".join(parts)

        return "\n\n".join(f"---\n{c}" for c in filtered)

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
        all_keywords: set[str] = set()
        for chunk in chunks:
            chunk_header = f"[{chunk.sequence_start}-{chunk.sequence_end}]"
            chunk_texts.append(f"{chunk_header} {chunk.content}")
            if hasattr(chunk, "keywords") and chunk.keywords:
                all_keywords.update(chunk.keywords)

        result = f"## {layer_name}（{layer}）\n" + "\n".join(chunk_texts)
        if all_keywords:
            keywords_str = ", ".join(sorted(all_keywords))
            result += f"\n\n## 关键词索引（{layer}）\n{keywords_str}"
        return result

    def _build_dynamic_vars(self, ctx: PluginContext) -> str:
        """构建动态变量内容。

        动态变量由 LLMCore 追加到消息列表末尾（在历史消息之后），
        不拼入系统提示词。包含日期、时间、Agent 名称、会话 ID 等。

        优先从 state["context.dynamic_vars"] 读取 Agent YAML 配置的
        dynamic_vars.items，回退到硬编码的默认动态变量。

        Args:
            ctx: 插件执行上下文

        Returns:
            动态变量文本，或空字符串
        """
        now = datetime.now(UTC)
        parts: list[str] = []

        dynamic_vars_def = ctx.state.get("context.dynamic_vars", [])
        if dynamic_vars_def:
            session_id = ctx.state.get("context.session_id", "")
            agent_name = ctx.state.get("context.agent_name", "")

            for var_def in dynamic_vars_def:
                if not isinstance(var_def, dict):
                    continue
                if not var_def.get("enabled", True):
                    continue

                var_type = var_def.get("type", "")
                var_name = var_def.get("name", var_type)

                if var_type == "timestamp":
                    fmt = var_def.get("format", "%Y-%m-%d %H:%M:%S")
                    parts.append(f"- {var_name}: {now.strftime(fmt)}")
                elif var_type == "session":
                    parts.append(f"- {var_name}: {session_id}")
                elif var_type == "agent":
                    parts.append(f"- {var_name}: {agent_name}")
                elif var_type == "model":
                    model_info = ctx.state.get("llm_model", "")
                    parts.append(f"- {var_name}: {model_info}")
                elif var_type in ("reference", "content", "inline", ""):
                    content = var_def.get("content", "")
                    if content:
                        parts.append(f"- {var_name}: {content}")
        else:
            parts.append(f"- 日期: {now.strftime('%Y-%m-%d')}")
            parts.append(f"- 时间: {now.strftime('%H:%M:%S')}")

            agent_name = ctx.state.get("context.agent_name", "")
            if agent_name:
                parts.append(f"- Agent: {agent_name}")

            session_id = ctx.state.get("context.session_id", "")
            if session_id:
                parts.append(f"- 会话: {session_id}")

        return "\n".join(parts)

"""提示词构建 Input 插件 — 从旧代码 agents/prompt_builder.py 迁移。

负责在管道循环的输入阶段组装 SystemMessage，
按旧代码 layer_order 顺序拼接各层内容。

产出：
    - state["system_message"]: 一条 SystemMessage（不含历史消息和动态变量）
    - state["prompt.dynamic_vars"]: 动态变量消息 dict（LLMCore 直接追加在历史消息之后）

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
import re
from datetime import UTC, datetime
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)

# 占位符正则：匹配 {{xxx}} 或 {{xxx:yyy}} 格式
PLACEHOLDER_PATTERN = re.compile(r'\{\{(.+?)\}\}')


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

    @staticmethod
    def _parse_placeholder(content: str) -> tuple[str, dict[str, Any]]:
        """解析占位符内容，返回 (类型名, 参数字典)。

        支持的格式：
          - 无参数：{{rules}}、{{session}}、{{timestamp}}
          - 带参数：{{timestamp:%Y-%m-%d}}、{{path:文件路径}}、{{content:文本}}
          - 键值对：{{retrieval:tags=a,b|top_k=5}}、{{vector:path:x|top_k=3}} 等

        Args:
            content: 占位符内部文本（不含 {{ 和 }}）

        Returns:
            (类型名, 参数字典) 二元组
        """
        if content in ("rules", "session", "workspace", "project_root"):
            return content, {}
        if content == "timestamp":
            return "timestamp", {}

        type_name, _, args_str = content.partition(":")

        if type_name == "timestamp":
            return "timestamp", {"format": args_str} if args_str else {}
        elif type_name == "path":
            return "path", {"path": args_str}
        elif type_name == "content":
            return "content", {"content": args_str}
        else:
            params = {}
            for pair in args_str.split("|"):
                k, _, v = pair.partition("=")
                params[k.strip()] = v.strip()
            return type_name, params

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

        # 单独产出动态变量消息（由 LLMCore 直接追加在历史消息之后）
        dynamic_vars_msg = await self._build_dynamic_vars(ctx)
        if dynamic_vars_msg:
            updates["prompt.dynamic_vars"] = dynamic_vars_msg

        logger.debug(
            "[%s] SystemMessage built | content_len=%d | dynamic_vars=%s",
            self.name, len(system_content), bool(dynamic_vars_msg),
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

        # 占位符替换：在拼接前将 {{xxx}} 替换为实际内容
        has_placeholders = bool(system_prompt and "{{" in system_prompt)
        if has_placeholders:
            system_prompt = await self._resolve_placeholders(ctx, system_prompt)

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

        # 5. 压缩层：统一预算驱动分层加载
        # 顺序：关键词(最老) → L2 → L1(最新)
        if self._config.get("include_compressed_layers", True):
            comp_text = await self._load_compression_blocks(ctx)
            if comp_text:
                parts.append(comp_text)

        return "\n\n".join(parts)

    async def _resolve_single_var_content(
        self, ctx: PluginContext, var_def: dict, session_id: str, constraints: dict,
    ) -> str:
        """解析单个变量的内容，供 _load_static_vars 和占位符替换共用。

        支持 mode（exact/vector/hybrid）后处理和 output_format（full/summary）后处理。

        Args:
            ctx: 插件执行上下文
            var_def: 变量定义字典，包含 type/name/mode/output_format 等
            session_id: 当前会话 ID
            constraints: 约束条件字典（含 hard/soft 列表）

        Returns:
            解析后的内容字符串，或空字符串
        """
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
            content = var_def.get("content", "") or var_def.get("value", "")
            if not content and var_def.get("tags"):
                content = await self._retrieve_by_tags(ctx, var_def)

        elif var_type == "timestamp":
            now = datetime.now(UTC)
            fmt = var_def.get("format", "%Y-%m-%d %H:%M:%S")
            content = now.strftime(fmt)

        elif var_type == "session":
            content = session_id

        elif var_type == "retrieval":
            content = await self._retrieve_by_tags(ctx, var_def)

        elif var_type == "routed":
            content = await self._resolve_routed_var(ctx, var_def)

        if not content:
            return ""

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
                logger.debug("[%s] 占位符向量检索跳过 | name=%s | error=%s", self.name, var_name, e)

        if output_format == "summary":
            content = f"[摘要] {content}"

        return content

    async def _resolve_placeholder(self, ctx: PluginContext, placeholder_content: str) -> str:
        """解析单个 {{占位符}} 并返回替换内容。

        将占位符语法转换为兼容的 var_def 字典，复用 _resolve_single_var_content。

        Args:
            ctx: 插件执行上下文
            placeholder_content: 占位符内部文本（不含 {{ 和 }}）

        Returns:
            替换后的内容字符串，无法识别时返回空字符串
        """
        var_type, params = self._parse_placeholder(placeholder_content)

        if var_type == "rules":
            var_def = {"type": "rules", "name": "rules"}
        elif var_type == "workspace":
            ws = ctx.state.get("workspace", "")
            if not ws:
                ws = (ctx._services or {}).get("project_root", "")
            return str(ws) if ws else ""
        elif var_type == "project_root":
            pr = ctx.state.get("project_root", "")
            if not pr:
                pr = (ctx._services or {}).get("project_root", "")
            return str(pr) if pr else ""
        elif var_type == "path":
            var_def = {"type": "path", "name": "path", "path": params["path"]}
        elif var_type == "content":
            var_def = {"type": "content", "name": "content", "content": params["content"]}
        elif var_type == "timestamp":
            var_def = {"type": "timestamp", "name": "timestamp", "format": params.get("format", "%Y-%m-%d %H:%M:%S")}
        elif var_type == "session":
            var_def = {"type": "session", "name": "session"}
        elif var_type == "retrieval":
            var_def = {
                "type": "retrieval", "name": "retrieval",
                "tags": params.get("tags", "").split(","),
                "top_k": int(params.get("top_k", 5)),
                "inject_type": params.get("inject_type", "full"),
            }
        elif var_type == "vector":
            var_def = {
                "type": "path", "name": "vector",
                "path": params.get("path", ""),
                "mode": "vector",
                "top_k": int(params.get("top_k", 5)),
            }
        elif var_type == "hybrid":
            var_def = {
                "type": "retrieval", "name": "hybrid",
                "tags": params.get("tags", "").split(","),
                "top_k": int(params.get("top_k", 5)),
                "mode": "hybrid",
            }
        elif var_type == "routed":
            var_def = {"type": "routed", "name": "routed", "route_key": params.get("route_key", "")}
            routes = {k: v for k, v in params.items() if k != "route_key"}
            var_def["routes"] = routes
        else:
            return ""

        session_id = ctx.state.get("context.session_id", "")
        constraints = ctx.state.get("constraints", {})
        return await self._resolve_single_var_content(ctx, var_def, session_id, constraints)

    async def _resolve_placeholders(self, ctx: PluginContext, text: str) -> str:
        """替换文本中的所有 {{占位符}} 为实际内容。

        Args:
            ctx: 插件执行上下文
            text: 包含占位符的原始文本

        Returns:
            替换后的文本
        """
        matches = PLACEHOLDER_PATTERN.findall(text)
        if not matches:
            return text

        for match in matches:
            content = await self._resolve_placeholder(ctx, match)
            placeholder = "{{" + match + "}}"
            text = text.replace(placeholder, content)

        return text

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

            var_name = var_def.get("name", var_def.get("type", ""))

            content = await self._resolve_single_var_content(ctx, var_def, session_id, constraints)

            if content:
                parts.append(f"### {var_name}\n{content}")

        context_window = ctx.state.get("context_window", 0)
        if context_window:
            model_info = ctx.state.get("llm_model", "")
            model_line = f"模型: {model_info}\n" if model_info else ""
            parts.append(f"### 模型信息\n{model_line}上下文窗口: {context_window} tokens")

        if not parts:
            return ""

        return "## 静态变量\n" + "\n\n".join(parts)

    async def _retrieve_by_tags(self, ctx: PluginContext, var_def: dict[str, Any]) -> str:
        """通过 tags 从知识库检索内容。

        统一通过 MemoryService.retrieve() 进行知识检索，
        不再自行创建 KnowledgeService 或读取 knowledge.context 缓存。
        所有知识检索路径收敛到 MemoryService 这一个入口。

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

        try:
            memory_service = ctx.get_service("memory_service")
        except KeyError:
            return ""

        user_id = ctx.state.get("user_id", "")

        results = await memory_service.retrieve(
            user_id=user_id,
            filter={"tags": tags, "memory_type": "semantic"},
            inject_type=inject_type,
            retrieval_method="keyword",
            query=" ".join(tags),
            top_k=top_k,
        )

        if not results:
            return ""

        if inject_type == "summary":
            return "\n".join(
                f"- {r.content[:200]}..." if len(r.content) > 200 else f"- {r.content}"
                for r in results
            )

        return "\n\n".join(r.content for r in results)

    async def _resolve_routed_var(self, ctx: PluginContext, var_def: dict[str, Any]) -> str:
        """解析路由变量，根据 state 中的 route_key 值从 routes 表中选择注入内容。

        routes 值支持两种形式：
          - 字符串：直接作为内容使用
          - 字典：作为嵌套变量定义递归解析（支持 path/tags/content 等类型）

        Args:
            ctx: 插件执行上下文
            var_def: 变量定义字典，包含 route_key 和 routes

        Returns:
            路由匹配到的内容，或空字符串
        """
        route_key = var_def.get("route_key", "")
        routes = var_def.get("routes", {})

        if not route_key or not routes:
            return ""

        current_value = ctx.state.get(route_key, "")
        matched = routes.get(str(current_value), routes.get("_default", ""))

        if isinstance(matched, str):
            return matched

        if isinstance(matched, dict):
            nested_type = matched.get("type", "")
            if nested_type == "path":
                file_path = matched.get("path", "")
                if file_path:
                    try:
                        from pathlib import Path
                        p = Path(file_path)
                        if p.exists():
                            return await asyncio.to_thread(p.read_text, "utf-8")
                    except Exception as e:
                        logger.warning("[%s] 路由嵌套变量文件读取失败 | path=%s | error=%s", self.name, file_path, e)
            elif nested_type == "retrieval" or matched.get("tags"):
                return await self._retrieve_by_tags(ctx, matched)
            else:
                return matched.get("content", "")

        return ""

    async def _load_compression_blocks(
        self,
        ctx: PluginContext,
    ) -> str:
        """统一预算驱动分层加载压缩块。

        只加载 L1 chunks（它们包含 l2_content 和 keywords 字段）。
        从最新块开始按 L1 预算分配，L1 满了溢出到 L2，L2 满了溢出到关键词。

        预算计算：
        1. 先算系统提示词 + 最近消息已占 tokens
        2. 压缩块可用 = 触发线 - 已占
        3. L1/L2 预算 = min(绝对上限, 可用 × 比例)

        最终组装顺序（由老到新）：
        关键词 → L2 三元组 → L1 十段摘要

        Returns:
            格式化后的分层压缩文本，或空字符串
        """
        try:
            chunk_service = ctx.get_service("chunk_service")
        except KeyError:
            logger.debug("[%s] No chunk_service, skipping", self.name)
            return ""

        from pipeline.types import StateKeys

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return ""

        try:
            chunks = await chunk_service.find_by_pipeline(
                pipeline_run_id, "L1",
            )
        except Exception as e:
            logger.warning(
                "[%s] 读取压缩块失败 | error=%s", self.name, e,
            )
            return ""

        if not chunks:
            return ""

        # ── 预算计算（全部从 CompressionConfig 读取） ──
        from memory.context_compressor import CompressionConfig

        context_window = ctx.state.get("context_window", 128000)
        config = CompressionConfig.from_yaml_config(context_window)
        budgets = config.get_budgets()
        trigger_tokens = config.get_trigger_threshold()

        # 已占 tokens：系统提示词 + 最近消息
        sys_msg = ctx.state.get("system_message", {})
        sys_tokens = self._estimate_tokens_for_budget(
            sys_msg.get("content", "") if isinstance(sys_msg, dict) else str(sys_msg),
        )
        messages = ctx.state.get("messages", [])
        msg_tokens = sum(
            self._estimate_tokens_for_budget(
                m.get("content", "") if isinstance(m, dict) else str(m),
            )
            for m in messages
        )
        used_tokens = sys_tokens + msg_tokens

        # 压缩块可用空间 = 触发线 - 已占
        available = max(0, trigger_tokens - used_tokens)

        # 按配置比例分配 L1/L2 预算，不超过绝对上限
        comp_total_ratio = config.l1_ratio + config.l2_ratio
        l1_budget = min(budgets["L1"], int(available * config.l1_ratio / comp_total_ratio))
        l2_budget = min(budgets["L2"], available - l1_budget)

        logger.info(
            "[%s] 预算: window=%d trigger=%d 已用=%d(sys=%d+msg=%d) "
            "可用=%d → L1=%d L2=%d",
            self.name, context_window, trigger_tokens,
            used_tokens, sys_tokens, msg_tokens,
            available, l1_budget, l2_budget,
        )

        if available <= 0:
            logger.info("[%s] 无可用预算，跳过压缩块加载", self.name)
            return ""

        # ── 去重：按 sequence_end 降序，高水位线算法移除被完全覆盖的块 ──
        dedup_sorted = sorted(
            chunks, key=lambda c: c.sequence_end, reverse=True,
        )
        high_water = float("inf")
        deduped: list = []
        for chunk in dedup_sorted:
            if chunk.sequence_start >= high_water:
                continue
            deduped.append(chunk)
            high_water = chunk.sequence_start

        logger.info(
            "[%s] 压缩块去重: %d → %d 块",
            self.name, len(chunks), len(deduped),
        )

        # 按创建时间排序：最新的先分配预算（使用去重后的块）
        sorted_chunks = sorted(
            deduped, key=lambda c: c.created_at, reverse=True,
        )

        # 分配每个块到对应层
        l1_used = 0
        l2_used = 0
        kw_blocks = []
        l2_blocks = []
        l1_blocks = []

        for chunk in sorted_chunks:
            l1_content = chunk.content or ""
            l2_content = getattr(chunk, "l2_content", "") or ""
            keywords = getattr(chunk, "keywords", []) or []
            seq_range = f"[{chunk.sequence_start}-{chunk.sequence_end}]"

            l1_tokens = self._estimate_tokens_for_budget(l1_content) if l1_content else 0
            l2_tokens = self._estimate_tokens_for_budget(l2_content) if l2_content else 0
            kw_text = ", ".join(keywords) if keywords else ""
            self._estimate_tokens_for_budget(kw_text) if kw_text else 0

            # 尝试 L1
            if l1_budget > 0 and l1_used + l1_tokens <= l1_budget and l1_content:
                l1_blocks.append((seq_range, l1_content, keywords))
                l1_used += l1_tokens
                continue

            # L1 满了，尝试 L2
            if l2_budget > 0 and l2_used + l2_tokens <= l2_budget and l2_content:
                l2_blocks.append((seq_range, l2_content, keywords))
                l2_used += l2_tokens
                continue

            # L2 也满了，用关键词
            if keywords:
                kw_blocks.append((seq_range, keywords))

        # 反转：从老到新
        kw_blocks.reverse()
        l2_blocks.reverse()
        l1_blocks.reverse()

        parts = []

        # 关键词层（最老）
        if kw_blocks:
            kw_lines = []
            for seq_range, keywords in kw_blocks:
                kw_lines.append(f"{seq_range} 关键词: {', '.join(keywords)}")
            parts.append("## 历史关键词索引\n" + "\n".join(kw_lines))

        # L2 层
        if l2_blocks:
            l2_lines = []
            for seq_range, content, _kw in l2_blocks:
                l2_lines.append(f"{seq_range} {content}")
            parts.append("## 三元组摘要（L2）\n" + "\n".join(l2_lines))

        # L1 层（最新）
        if l1_blocks:
            l1_lines = []
            for seq_range, content, _kw in l1_blocks:
                l1_lines.append(f"{seq_range} {content}")
            parts.append("## 八段摘要（L1）\n" + "\n".join(l1_lines))

        if not parts:
            return ""

        logger.info(
            "[%s] 压缩块加载: L1=%d块/%dtokens, "
            "L2=%d块/%dtokens, keywords=%d块",
            self.name, len(l1_blocks), l1_used,
            len(l2_blocks), l2_used, len(kw_blocks),
        )

        return "\n\n".join(parts)

    @staticmethod
    def _estimate_tokens_for_budget(text: str) -> int:
        """估算文本 token 数（用于预算计算）。"""
        if not text:
            return 0
        return max(1, len(text) // 2)

    async def _build_dynamic_vars(self, ctx: PluginContext) -> dict[str, str] | None:
        """构建动态变量消息。

        产出完整的消息 dict（含 role/name/content），
        LLMCore 直接追加到消息列表末尾，无需二次包装。

        优先从 state["context.dynamic_vars"] 读取 Agent YAML 配置的
        dynamic_vars.items，回退到硬编码的默认动态变量。

        Args:
            ctx: 插件执行上下文

        Returns:
            动态变量消息 dict，或 None（无动态变量时）
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
                elif var_type == "routed":
                    content = await self._resolve_routed_var(ctx, var_def)
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

        if not parts:
            return None

        content = (
            "<dynamic_vars>\n"
            "以下为系统注入的背景信息和思考提示。\n"
            f"{chr(10).join(parts)}\n"
            "</dynamic_vars>"
        )
        return {
            "role": "user",
            "name": "dynamic_context",
            "content": content,
        }

"""提示词构建 Input 插件 — 从旧代码 agents/prompt_builder.py 迁移。

负责在管道循环的输入阶段组装 SystemMessage 及相关消息。

产出：
    - state["system_message"]: 一条 SystemMessage（不含历史消息和动态变量）
    - state["compression_messages"]: 压缩层独立消息列表（L1/L2/KEYWORDS）
    - state["prompt.dynamic_vars"]: 动态变量消息 dict（LLMCore 直接追加在历史消息之后）

构建顺序（_build_system_content）：
    1. system_prompt      <- state["context.system_prompt"]（占位符 {{xxx}} 在此替换）
    2. language           <- config.language（语言指令，可选）
    3. tools_description  <- state["prompt.tool_descriptions"]（仅当开关开启时拼入）
    4. static_vars        <- agent_config 或 state 读取（记忆/知识检索的唯一 opt-in 入口）

注意：memory.retrieved / knowledge.context 不再无条件拼入 system_message —— 这两个 state
仅供其他插件（如 error_check）使用；记忆/知识要进提示词，必须由 static_vars 显式声明
retrieval/tags（走 _retrieve_by_tags）。压缩层（L1/L2）作为 compression_messages
独立消息输出，不合并到 system_message。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy
from src.config.settings import get_settings

logger = logging.getLogger(__name__)

# 占位符正则：匹配 {{xxx}} 或 {{xxx:yyy}} 格式
PLACEHOLDER_PATTERN = re.compile(r"\{\{(.+?)\}\}")


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
        if type_name == "path":
            # {{path:文件或目录路径}} 或 {{path:目录路径|extensions=.md,.yaml}}
            # （文件→注入单文件，目录→注入目录下所有顶层文件，两者共用同一套解析）
            params: dict[str, Any] = {}
            if "|" in args_str:
                path_part, _, ext_part = args_str.partition("|")
                params["path"] = path_part
                for pair in ext_part.split("|"):
                    k, _, v = pair.partition("=")
                    if k.strip() == "extensions" and v.strip():
                        params["extensions"] = [e.strip() for e in v.split(",") if e.strip()]
            else:
                params["path"] = args_str
            return "path", params
        if type_name == "content":
            return "content", {"content": args_str}
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
            要写入 state 的字段字典，含 system_message、compression_messages、dynamic_vars
        """
        from datetime import datetime as _dt  # noqa: PLC0415

        _t0 = _dt.now()

        updates: dict[str, Any] = {}

        # 按 layer_order 顺序组装系统消息内容（不含压缩块）
        _s = _dt.now()
        logger.debug("[%s] step=build_system_content BEGIN", self.name)
        system_content = await self._build_system_content(ctx)
        logger.debug(
            "[%s] step=build_system_content END | elapsed=%.3fs len=%d",
            self.name,
            (_dt.now() - _s).total_seconds(),
            len(system_content),
        )

        # 产出 SystemMessage（纯 prompt，永不变化）
        updates["system_message"] = {"role": "system", "content": system_content}

        # 加载压缩块和状态快照为独立消息
        if self._config.get("include_compressed_layers", True):
            _s = _dt.now()
            logger.debug("[%s] step=load_compression_messages BEGIN", self.name)
            try:
                compression_msgs = await asyncio.wait_for(
                    self._load_compression_messages(ctx),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "[%s] load_compression_messages 超时(60s)！压缩块加载卡死，用空列表继续",
                    self.name,
                )
                compression_msgs = []
            logger.debug(
                "[%s] step=load_compression_messages END | elapsed=%.3fs count=%d",
                self.name,
                (_dt.now() - _s).total_seconds(),
                len(compression_msgs),
            )
            updates["compression_messages"] = compression_msgs

        # 单独产出动态变量消息（由 LLMCore 直接追加在历史消息之后）
        _s = _dt.now()
        logger.debug("[%s] step=build_dynamic_vars BEGIN", self.name)
        try:
            dynamic_vars_msg = await asyncio.wait_for(
                self._build_dynamic_vars(ctx),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[%s] build_dynamic_vars 超时(30s)！动态变量构建卡死，用空继续",
                self.name,
            )
            dynamic_vars_msg = ""
        logger.debug(
            "[%s] step=build_dynamic_vars END | elapsed=%.3fs empty=%s",
            self.name,
            (_dt.now() - _s).total_seconds(),
            not dynamic_vars_msg,
        )
        if dynamic_vars_msg:
            updates["prompt.dynamic_vars"] = dynamic_vars_msg

        logger.debug(
            "[%s] _do_work 全部完成 | total_elapsed=%.3fs",
            self.name,
            (_dt.now() - _t0).total_seconds(),
        )

        logger.debug(
            "[%s] SystemMessage built | content_len=%d | compression_msgs=%d | dynamic_vars=%s",
            self.name,
            len(system_content),
            len(updates.get("compression_messages", [])),
            bool(dynamic_vars_msg),
        )

        return updates

    async def _build_system_content(self, ctx: PluginContext) -> str:
        """按 layer_order 顺序组装系统消息内容。

        顺序：system_prompt -> language -> tools_description -> static_vars
        不含 recent_messages 和 dynamic_vars。
        压缩层（L2/L1/KEYWORDS）通过 compression_messages 独立消息输出。

        记忆/知识不再自动拼入：memory.retrieved / knowledge.context 不在此追加，
        注入提示词只能由 static_vars 声明 retrieval/tags opt-in（_retrieve_by_tags）。

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

        # 3. static_vars（含 rules/path/reference/tags 等，记忆/知识检索的唯一 opt-in 入口）
        if self._config.get("include_static_vars", True):
            static_vars_text = await self._load_static_vars(ctx)
            if static_vars_text:
                parts.append(static_vars_text)

        # 记忆/知识不再无条件追加到 system_message：memory.retrieved / knowledge.context
        # 仅作为 state 供其他插件（如 error_check）使用；要进提示词必须由 static_vars
        # 声明 retrieval/tags 显式 opt-in（走 _retrieve_by_tags）。

        return "\n\n".join(parts)

    @staticmethod
    def _current_now(tz: Any) -> datetime:
        """当前时间（目标时区）。独立为方法便于测试注入。"""
        return datetime.now(tz)

    def _now_in_configured_tz(self) -> tuple[datetime, str]:
        """按 settings.timezone 返回 (当前时间, 时区标注后缀)。

        返回的 datetime 已转换到目标时区；后缀形如 "(UTC+8, Asia/Shanghai)"。
        时区名无效时降级到 UTC 并打 warning。

        Returns:
            (now, suffix)：now 为目标时区的 aware datetime；suffix 为时区标注字符串。
        """
        tz_name = get_settings().timezone
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.warning(
                "[%s] APP_TIMEZONE=%r 无效，时间注入回退到 UTC",
                self.name,
                tz_name,
            )
            tz = UTC
            tz_name = "UTC"
        now = self._current_now(tz)
        offset = now.strftime("%z")  # +0800 / -0530 / +0000
        sign, hh, mm = offset[0], offset[1:3], offset[3:5]
        # 整小时省略分钟，显示 UTC+8；半时区显示 UTC+5:30
        offset_fmt = f"UTC{sign}{int(hh)}" + (f":{int(mm)}" if mm != "00" else "")
        return now, f"({offset_fmt}, {tz_name})"

    async def _resolve_single_var_content(  # noqa: PLR0912,PLR0915
        self,
        ctx: PluginContext,
        var_def: dict,
        session_id: str,
        constraints: dict,
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

        if var_type == "placeholder":
            placeholder_text = var_def.get("name", "")
            if placeholder_text and "{{" in placeholder_text:
                matches = PLACEHOLDER_PATTERN.findall(placeholder_text)
                result_parts = []
                for match in matches:
                    resolved = await self._resolve_placeholder(ctx, match)
                    if resolved:
                        result_parts.append(resolved)
                content = "\n".join(result_parts)

        if var_type == "rules":
            rules_parts = []
            for c in constraints.get("hard", []):
                rules_parts.append(f"- [必须] {c}")
            for c in constraints.get("soft", []):
                rules_parts.append(f"- [建议] {c}")
            content = "\n".join(rules_parts)

        elif var_type == "path":
            # path 类型：文件注入 → 注入项目文件（base=project_root）
            # 绝对路径直接使用；相对路径基于 project_root 解析
            # 若无 project_root 则跳过（防止误注入容器自身文件）
            file_path = var_def.get("path", "")
            target = self._resolve_target_path(ctx, file_path)
            if target is not None and target.is_file():
                try:
                    text = await asyncio.to_thread(target.read_text, "utf-8")
                    tag_name = target.stem
                    content = f"<{tag_name}>\n{text}\n</{tag_name}>"
                except Exception as e:
                    logger.warning(
                        "[%s] 读取静态变量文件失败 | path=%s | error=%s",
                        self.name,
                        file_path,
                        e,
                    )
            elif target is not None and target.is_dir():
                # 目录 → 遍历读取（base=project_root）
                dir_content = await self._read_dir_entries(target, var_def.get("extensions"))
                if dir_content:
                    content = f'<files dir="{file_path}">\n{dir_content}\n</files>'
            else:
                logger.debug(
                    "[%s] path 类型变量跳过 | name=%s | path=%s | project_root=%s",
                    self.name,
                    var_name,
                    file_path,
                    bool(ctx.state.get("project_root") or (ctx._services or {}).get("project_root")),
                )

        elif var_type in ("reference", "content", ""):
            content = var_def.get("content", "") or var_def.get("value", "")
            if not content and var_def.get("tags"):
                content = await self._retrieve_by_tags(ctx, var_def)

        elif var_type == "timestamp":
            now, suffix = self._now_in_configured_tz()
            fmt = var_def.get("format", "%Y-%m-%d %H:%M:%S")
            content = f"{now.strftime(fmt)} {suffix}"

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
                    content = f"{content}\n\n### 相关检索结果\n{retrieved_text}" if mode == "hybrid" else retrieved_text
            except (KeyError, Exception) as e:
                logger.debug("[%s] 占位符向量检索跳过 | name=%s | error=%s", self.name, var_name, e)

        if output_format == "summary":
            content = f"[摘要] {content}"

        return content

    async def _read_dir_entries(
        self,
        target: Path,
        extensions: list[str] | None = None,
    ) -> str:
        """读取目录下所有顶层文件内容（接收已解析的 Path 对象）。

        非递归读取顶层文件，按文件名排序，extensions 过滤，每文件 10MB 上限。

        Args:
            target: 已解析的目录 Path 对象。
            extensions: 文件扩展名白名单，如 [".md", ".yaml"]。

        Returns:
            拼接后的文件内容字符串。
        """
        if not target.is_dir():
            return ""
        ext_set = {e.lower() for e in extensions} if extensions else None
        max_size = 10 * 1024 * 1024
        parts: list[str] = []
        try:
            entries = sorted(target.iterdir(), key=lambda p: p.name)
        except OSError as e:
            logger.warning("[%s] 目录遍历失败 | path=%s | error=%s", self.name, target, e)
            return ""
        for entry in entries:
            if not entry.is_file():
                continue
            if ext_set and entry.suffix.lower() not in ext_set:
                continue
            try:
                if entry.stat().st_size > max_size:
                    logger.debug("[%s] 跳过超大文件 | file=%s", self.name, entry.name)
                    continue
                text = await asyncio.to_thread(entry.read_text, "utf-8")
            except (OSError, UnicodeDecodeError, ValueError) as e:
                logger.debug("[%s] 文件读取失败 | file=%s | error=%s", self.name, entry.name, e)
                continue
            parts.append(f"--- {entry.name} ---\n{text}")
        return "\n\n".join(parts)

    def _resolve_target_path(self, ctx: PluginContext, rel_path: str) -> Path | None:
        """把相对路径解析为最终目标 Path。

        互斥选择逻辑（不是先后也不是回退）：
            - 文件：用 project_root 解析（项目文件注入）
            - 文件夹：用 workspace 解析（state["workspace"] = ws_meta.path）
            - 找不到就跳过

        Args:
            ctx: 插件执行上下文
            rel_path: 相对或绝对路径

        Returns:
            解析后的 Path，无法解析则返回 None
        """
        if not rel_path or not rel_path.strip():
            return None
        p = Path(rel_path)
        if p.is_absolute():
            return p

        project_root = ctx.state.get("project_root", "")
        if not project_root:
            project_root = (ctx._services or {}).get("project_root", "")

        # 文件夹 base：state["workspace"]（engine.run(workspace=ws_meta.path) 注入）
        ws_path = ctx.state.get("workspace", "")

        # 文件：用 project_root 解析
        if project_root:
            target = Path(project_root) / rel_path
            if target.is_file():
                return target

        # 文件夹：用 ws_meta.path / workspace 解析（互斥，无回退到 project_root）
        if ws_path:
            target = Path(ws_path) / rel_path
            if target.is_dir():
                return target

        return None

    async def _resolve_placeholder(self, ctx: PluginContext, placeholder_content: str) -> str:  # noqa: PLR0912
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
                "type": "retrieval",
                "name": "retrieval",
                "tags": params.get("tags", "").split(","),
                "top_k": int(params.get("top_k", 5)),
                "inject_type": params.get("inject_type", "full"),
            }
        elif var_type == "vector":
            var_def = {
                "type": "path",
                "name": "vector",
                "path": params.get("path", ""),
                "mode": "vector",
                "top_k": int(params.get("top_k", 5)),
            }
        elif var_type == "hybrid":
            var_def = {
                "type": "retrieval",
                "name": "hybrid",
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

        for idx, match in enumerate(matches):
            # 逐步日志 + 超时保护：卡死时定位到具体哪个占位符，并 fail 而非永久挂起
            # （历史多次出现 prompt_build 协程永久挂起拖垮整个进程的僵尸引擎问题）
            from datetime import datetime as _ph_t  # noqa: PLC0415

            _ph_s = _ph_t.now()
            logger.debug(
                "[%s] resolve_placeholder BEGIN | idx=%d/%d | %s",
                self.name,
                idx + 1,
                len(matches),
                match[:80] + ("..." if len(match) > 80 else ""),
            )
            try:
                content = await asyncio.wait_for(
                    self._resolve_placeholder(ctx, match),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "[%s] resolve_placeholder 超时(30s)！占位符解析卡死，"
                    "跳过此占位符避免永久挂起 | idx=%d/%d | placeholder=%s",
                    self.name,
                    idx + 1,
                    len(matches),
                    match[:120],
                )
                content = ""
            logger.debug(
                "[%s] resolve_placeholder END | idx=%d | elapsed=%.3fs | len=%d",
                self.name,
                idx + 1,
                (_ph_t.now() - _ph_s).total_seconds(),
                len(content) if content else 0,
            )
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

        from datetime import datetime as _rt  # noqa: PLC0415

        parts: list[str] = []
        session_id = ctx.state.get("context.session_id", "")
        constraints = ctx.state.get("constraints", {})

        for item in static_vars_def:
            # 字符串形式：占位符语法，如 "{{rules}}" 或 "{{path:config/rules/xxx.md}}"
            if isinstance(item, str):
                content = await self._resolve_placeholders(ctx, item)
                if content:
                    parts.append(content)
                continue

            # dict 形式：旧版配置语法（向后兼容）
            if not isinstance(item, dict):
                continue
            var_def = item
            if not var_def.get("enabled", True):
                continue

            var_name = var_def.get("name", var_def.get("type", ""))

            # 逐变量加边界日志：卡死时定位到具体哪个静态变量（path/tags/retrieval）
            _sv_t = _rt.now()
            logger.debug(
                "[%s] static_var BEGIN | name=%s type=%s",
                self.name,
                var_name,
                var_def.get("type", ""),
            )
            content = await self._resolve_single_var_content(ctx, var_def, session_id, constraints)
            logger.debug(
                "[%s] static_var END | name=%s | elapsed=%.3fs len=%d",
                self.name,
                var_name,
                (_rt.now() - _sv_t).total_seconds(),
                len(content) if content else 0,
            )

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

        # retrieve 边界日志（定位 prompt_build 卡点）。
        from datetime import datetime as _rt  # noqa: PLC0415

        _rt_s = _rt.now()
        logger.debug(
            "[%s] memory_service.retrieve BEGIN | tags=%s top_k=%d method=%s",
            self.name,
            tags,
            top_k,
            "keyword",
        )
        results = await memory_service.retrieve(
            user_id=user_id,
            filter={"tags": tags, "memory_type": "semantic"},
            inject_type=inject_type,
            retrieval_method="keyword",
            query=" ".join(tags),
            top_k=top_k,
        )
        logger.debug(
            "[%s] memory_service.retrieve END | elapsed=%.3fs results=%d",
            self.name,
            (_rt.now() - _rt_s).total_seconds(),
            len(results) if results else 0,
        )

        if not results:
            return ""

        if inject_type == "summary":
            return "\n".join(f"- {r.content[:200]}..." if len(r.content) > 200 else f"- {r.content}" for r in results)

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
                        from pathlib import Path  # noqa: PLC0415

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

    async def _load_compression_messages(  # noqa: PLR0912,PLR0915
        self,
        ctx: PluginContext,
    ) -> list[dict[str, Any]]:
        """加载压缩块和状态快照为独立消息列表。

        每个块一条消息（XML 包裹），组装顺序：L2(老→新) → L1(老→新) → state_snapshot。
        预算不足时从 L1 → L2 降级，L2 也不够则丢弃（state_snapshot.keywords 兜底）。

        Returns:
            独立消息列表
        """
        messages: list[dict[str, Any]] = []

        try:
            chunk_service = ctx.get_service("chunk_service")
        except KeyError:
            return messages

        from pipeline.types import StateKeys  # noqa: PLC0415

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return messages

        try:
            chunks = await chunk_service.find_by_pipeline(
                pipeline_run_id,
                "L1",
            )
        except Exception as e:
            logger.warning("[%s] 读取压缩块失败 | error=%s", self.name, e)
            return messages

        if not chunks:
            # 没有压缩块，只加载状态快照
            state_msgs = await self._load_state_snapshot_message(ctx, pipeline_run_id, chunk_service)
            messages.extend(state_msgs)
            return messages

        # ── 预算计算 ──
        from memory.context_compressor import CompressionConfig  # noqa: PLC0415

        context_window = ctx.state.get("context_window", 128000)
        config = CompressionConfig.from_yaml_config(context_window)
        budgets = config.get_budgets()
        trigger_tokens = config.get_trigger_threshold()

        sys_msg = ctx.state.get("system_message", {})
        sys_tokens = self._estimate_tokens_for_budget(
            sys_msg.get("content", "") if isinstance(sys_msg, dict) else str(sys_msg),
        )
        msgs = ctx.state.get("messages", [])
        msg_tokens = sum(
            self._estimate_tokens_for_budget(
                m.get("content", "") if isinstance(m, dict) else str(m),
            )
            for m in msgs
        )
        used_tokens = sys_tokens + msg_tokens
        available = max(0, trigger_tokens - used_tokens)
        comp_total_ratio = config.l1_ratio + config.l2_ratio
        l1_budget = min(budgets["L1"], int(available * config.l1_ratio / comp_total_ratio))
        l2_budget = min(budgets["L2"], available - l1_budget)

        logger.debug(
            "[%s] 预算: window=%d trigger=%d 已用=%d(sys=%d+msg=%d) 可用=%d → L1=%d L2=%d",
            self.name,
            context_window,
            trigger_tokens,
            used_tokens,
            sys_tokens,
            msg_tokens,
            available,
            l1_budget,
            l2_budget,
        )

        if available <= 0:
            logger.info("[%s] 无可用预算，跳过压缩块加载", self.name)
            return messages

        # ── 去重 ──
        dedup_sorted = sorted(chunks, key=lambda c: c.sequence_end, reverse=True)
        high_water = float("inf")
        deduped: list = []
        for chunk in dedup_sorted:
            if chunk.sequence_start >= high_water:
                continue
            deduped.append(chunk)
            high_water = chunk.sequence_start

        # ── 预算分配：新→老，L1→L2→keywords 兜底 ──
        # 用 sequence_end 排序：自增整数，语义绝对可靠，不受跨进程/容器时钟漂移影响
        sorted_chunks = sorted(deduped, key=lambda c: c.sequence_end, reverse=True)
        l1_used = 0
        l2_used = 0
        l1_blocks: list = []
        l2_blocks: list = []
        dropped_keywords: list[tuple[str, list[str]]] = []  # (seq, keywords) 被丢弃块的兜底

        for chunk in sorted_chunks:
            l1_content = chunk.content or ""
            l2_content = getattr(chunk, "l2_content", "") or ""
            keywords = getattr(chunk, "keywords", []) or []
            seq = f"{chunk.sequence_start}-{chunk.sequence_end}"

            l1_tokens = self._estimate_tokens_for_budget(l1_content) if l1_content else 0
            l2_tokens = self._estimate_tokens_for_budget(l2_content) if l2_content else 0

            if l1_budget > 0 and l1_used + l1_tokens <= l1_budget and l1_content:
                l1_blocks.append((seq, l1_content, keywords))
                l1_used += l1_tokens
            elif l2_budget > 0 and l2_used + l2_tokens <= l2_budget and l2_content:
                l2_blocks.append((seq, l2_content, keywords))
                l2_used += l2_tokens
            elif keywords:
                # keywords 兜底档：L1/L2 都放不下时，至少保留关键词索引供检索
                dropped_keywords.append((seq, keywords))
            # else: 完全无内容且无 keywords，真正丢弃

        # ── 组装消息：L2(老→新) → L1(老→新) ──
        l2_blocks.reverse()
        l1_blocks.reverse()
        dropped_keywords.reverse()  # 老→新，与上面一致

        for seq, content, _kw in l2_blocks:
            messages.append(
                {
                    "role": "system",
                    "name": "compressed",
                    "content": f'<compressed seq="{seq}" level="L2">\n## 三元组摘要\n{content}\n</compressed>',
                }
            )

        for seq, content, _kw in l1_blocks:
            messages.append(
                {
                    "role": "system",
                    "name": "compressed",
                    "content": f'<compressed seq="{seq}" level="L1">\n## 过程摘要\n{content}\n</compressed>',
                }
            )

        # keywords 兜底块：被丢弃的 L1/L2 块至少保留关键词索引
        if dropped_keywords:
            index_lines = []
            for seq, kws in dropped_keywords:
                top_kws = ", ".join(kws[:5])  # 每块最多 5 个关键词，控制体积
                index_lines.append(f"[seq {seq}] {top_kws}")
            messages.append(
                {
                    "role": "system",
                    "name": "compressed",
                    "content": '<compressed level="KEYWORDS">\n'
                    "## 已降级块的关键词索引（L1/L2 预算不足，仅保留关键词）\n" + "\n".join(index_lines) + "\n"
                    "</compressed>",
                }
            )

        # ── 状态快照（含 keywords 合并）──
        state_msgs = await self._load_state_snapshot_message(ctx, pipeline_run_id, chunk_service)
        messages.extend(state_msgs)

        logger.debug(
            "[%s] 压缩消息: L1=%d块 L2=%d块 keywords兜底=%d块 state_snapshot=%s",
            self.name,
            len(l1_blocks),
            len(l2_blocks),
            len(dropped_keywords),
            "有" if state_msgs else "无",
        )
        return messages

    async def _load_state_snapshot_message(
        self,
        ctx: PluginContext,
        pipeline_run_id: str,
        chunk_service,
    ) -> list[dict[str, Any]]:
        """加载状态快照为一条独立消息。"""
        try:
            snapshots = await chunk_service.find_by_pipeline(
                pipeline_run_id,
                "STATE_SNAPSHOT",
            )
            if snapshots:
                return [
                    {
                        "role": "system",
                        "name": "state_snapshot",
                        "content": (f"<current_state>\n{snapshots[0].content}\n</current_state>"),
                    }
                ]
        except Exception:
            pass
        return []

    @staticmethod
    def _estimate_tokens_for_budget(text: str) -> int:
        """估算文本 token 数（用于预算计算）。"""
        if not text:
            return 0
        return max(1, len(text) // 2)

    async def _build_dynamic_vars(self, ctx: PluginContext) -> dict[str, str] | None:  # noqa: PLR0912,PLR0915
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
        now, suffix = self._now_in_configured_tz()
        parts: list[str] = []

        dynamic_vars_def = ctx.state.get("context.dynamic_vars", [])
        if dynamic_vars_def:
            session_id = ctx.state.get("context.session_id", "")
            agent_name = ctx.state.get("context.agent_name", "")

            for item in dynamic_vars_def:
                # 字符串形式：占位符语法，如 "{{timestamp}}" 或 "{{session}}"
                if isinstance(item, str):
                    content = await self._resolve_placeholders(ctx, item)
                    if content:
                        parts.append(content)
                    continue

                # dict 形式：旧版配置语法（向后兼容）
                if not isinstance(item, dict):
                    continue
                var_def = item
                if not var_def.get("enabled", True):
                    continue

                var_type = var_def.get("type", "")
                var_name = var_def.get("name", var_type)

                if var_type == "placeholder":
                    placeholder_text = var_def.get("name", "")
                    if placeholder_text:
                        content = await self._resolve_placeholders(ctx, placeholder_text)
                        if content:
                            parts.append(content)
                    continue

                if var_type == "timestamp":
                    fmt = var_def.get("format", "%Y-%m-%d %H:%M:%S")
                    parts.append(f"- {var_name}: {now.strftime(fmt)} {suffix}")
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
            parts.append(f"- 时间: {now.strftime('%H:%M:%S')} {suffix}")

            agent_name = ctx.state.get("context.agent_name", "")
            if agent_name:
                parts.append(f"- Agent: {agent_name}")

            session_id = ctx.state.get("context.session_id", "")
            if session_id:
                parts.append(f"- 会话: {session_id}")

        if not parts:
            return None

        content = f"<dynamic_vars>\n以下为系统注入的背景信息和思考提示。\n{chr(10).join(parts)}\n</dynamic_vars>"
        return {
            "role": "system",
            "name": "dynamic_context",
            "content": content,
        }

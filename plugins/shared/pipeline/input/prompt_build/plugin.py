"""提示词构建 Input 插件。

负责在管道循环的输入阶段组装 SystemMessage 及相关消息。

产出：
    - state["system_message"]: 一条 SystemMessage（不含历史消息和动态变量）
    - state["compression_messages"]: 压缩层独立消息列表（L1/L2）
    - state["prompt.dynamic_vars"]: 动态变量消息 dict（LLMCore 直接追加在历史消息之后）

构建顺序（_build_system_content）：
    1. system_prompt      <- state["context.system_prompt"]（占位符 {{xxx}} 在此替换）
    2. language           <- config.language（语言指令，可选）
    3. tools_description  <- state["prompt.tool_descriptions"]（仅当开关开启时拼入）
    4. static_vars        <- agent_config 或 state 读取（记忆/知识检索的唯一 opt-in 入口）

注意：memory.retrieved / knowledge.context 不无条件拼入 system_message —— 这两个 state
仅供其他插件使用；记忆/知识要进提示词，必须由 static_vars 显式声明
retrieval/tags（走 _retrieve_by_tags）。压缩层（L1/L2）作为 compression_messages
独立消息输出，不合并到 system_message。

基础设施接线（与兄弟插件共享同一形态）：
- 压缩块/状态快照存储走模块级 ``_memory_backend: IMemoryBackend``，
  server.py on_load 注入 set_memory_backend()。L1/L2/STATE_SNAPSHOT 块以
  memory_type="chunk" 落库，metadata.tags 含 pipeline:{id} / L1|L2 / seq:{start}-{end}。
- 压缩预算配置复用 context_window_guard 内联的 CompressionConfig
  （单一实现：读 config/system/context_window_config.yaml，失败回退默认）。
- {{retrieval:...}} 占位符的向量检索走 _memory_backend.search。
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult

from agentos_plugin_sdk.settings import get_settings

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 模块级依赖注入（由 server.py 的 on_load 注入，测试直接赋值）
# ═══════════════════════════════════════════════════════════

# 长期记忆后端（IMemoryBackend，Hindsight/Kernel 统一形态）；None 时压缩块无法加载
_memory_backend: Any | None = None


def set_memory_backend(backend: Any | None) -> None:
    """注入长期记忆后端（IMemoryBackend 实例或兼容 duck-type）。

    由 server.py on_load 调用，把 Step 3 构建的 Hindsight/Kernel 后端注入进来；
    测试环境直接传 FakeBackend/MagicMock。传 None 清空。

    Args:
        backend: 实现 add/search/delete/import_document 的后端实例
    """
    global _memory_backend
    _memory_backend = backend


# 占位符正则：匹配 {{xxx}} 或 {{xxx:yyy}} 格式
PLACEHOLDER_PATTERN = re.compile(r"\{\{(.+?)\}\}")

# 压缩块加载 / 动态变量构建的看门狗超时。到点 fail-visible：注入带
# "[上下文降级]" 前缀的显式标记继续（LLM 与用户可感知本轮丢失了压缩
# 历史/动态快照），禁止静默以空数据继续——静默空列表与"无压缩历史"
# 同值，丢失不可感知。
COMPRESSION_LOAD_TIMEOUT_S = 60.0
DYNAMIC_VARS_BUILD_TIMEOUT_S = 30.0

DEGRADE_MARKER_PREFIX = "[上下文降级]"


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


# ═══════════════════════════════════════════════════════════
# 压缩预算配置（单一实现：复用 context_window_guard 的 CompressionConfig）
# ═══════════════════════════════════════════════════════════


def _compression_config(context_window: int) -> Any:
    """构建压缩预算配置（context_window_guard 为宿主的单一实现）。

    字段、yaml 键与回退语义均以 guard 内联的 CompressionConfig 为准
    （读 config/system/context_window_config.yaml，读取失败回退代码默认），
    本插件不再维护本地副本。

    Args:
        context_window: 当前模型上下文窗口大小

    Returns:
        带 get_budgets()/get_trigger_threshold() 的配置对象
    """
    from context_window_guard.plugin import CompressionConfig  # noqa: PLC0415

    return CompressionConfig.from_yaml_config(context_window)


class PromptBuildPlugin(IInputPlugin):
    """提示词构建 Input 插件。

    只产出一条 SystemMessage 写入 state["system_message"]，
    不包含历史消息和动态变量。历史消息和动态变量由 LLMCore._build_messages 负责组装。

    优先级：50（构建级，在 context_build 和 memory_read 之后）
    没有提示词 LLM 无法调用。

    Attributes:
        _config: 插件配置字典
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化提示词构建插件。

        Args:
            config: 插件配置字典，支持以下键：
                - include_tools_description_in_prompt: 是否将工具描述拼入 SystemMessage（默认 False）
                - include_static_vars: 是否包含静态变量（默认 True）
                - include_compressed_layers: 是否包含压缩层（默认 True）
                - placeholder_max_depth: 占位符递归解析最大深度（默认 5）
                  用于支持 {{path:partial.md}} 中嵌套 {{timestamp}} 这种组合。
                  0 表示关闭递归（单趟扁平替换，行为与旧版一致）。
        """
        self._config = config or {}
        self._placeholder_max_depth: int = int(self._config.get("placeholder_max_depth", 5))

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
                    timeout=COMPRESSION_LOAD_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                # fail-visible：注入降级标记继续，缺失可感知（静默空列表
                # 与"无压缩历史"同值，LLM 无法察觉本轮丢了全部压缩块）。
                logger.error(
                    "[%s] load_compression_messages 超时(%.0fs)！注入上下文降级标记继续",
                    self.name,
                    COMPRESSION_LOAD_TIMEOUT_S,
                )
                compression_msgs = [{
                    "role": "system",
                    "content": (
                        f"{DEGRADE_MARKER_PREFIX} 压缩历史加载超时"
                        f"（{COMPRESSION_LOAD_TIMEOUT_S:.0f}s），"
                        "本轮缺失全部压缩历史（L2 摘要/L1/关键词层）。"
                    ),
                }]
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
                timeout=DYNAMIC_VARS_BUILD_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            # fail-visible：动态变量丢失以标记消息呈现，不静默置空。
            logger.error(
                "[%s] build_dynamic_vars 超时(%.0fs)！注入上下文降级标记继续",
                self.name,
                DYNAMIC_VARS_BUILD_TIMEOUT_S,
            )
            dynamic_vars_msg = {
                "role": "user",
                "content": (
                    f"{DEGRADE_MARKER_PREFIX} 动态变量构建超时"
                    f"（{DYNAMIC_VARS_BUILD_TIMEOUT_S:.0f}s），"
                    "本轮缺失动态状态快照。"
                ),
            }
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
        压缩层（L2/L1）通过 compression_messages 独立消息输出。

        记忆/知识不再自动拼入：memory.retrieved / knowledge.context 不在此追加，
        注入提示词只能由 static_vars 声明 retrieval/tags opt-in（_retrieve_by_tags）。

        Args:
            ctx: 插件执行上下文

        Returns:
            系统消息内容字符串
        """
        parts: list[str] = []

        # 1. system_prompt：规范键 context.system_prompt（context_build 无条件写入，
        #    已合并 state 注入与插件配置两来源）。
        system_prompt = ctx.state.get("context.system_prompt", "")

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
        # 仅作为 state 供其他插件使用；要进提示词必须由 static_vars
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
                # 配置声明的注入路径不存在 = 注入落空，非空 path 须 warning 留痕
                # （与本插件"未识别占位符""memory_service 缺失致知识注入落空"同口径）
                log_missing = logger.warning if file_path.strip() else logger.debug
                log_missing(
                    "[%s] path 类型变量解析失败（文件/目录不存在），知识注入落空"
                    " | name=%s | path=%s | project_root=%s",
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
                if _memory_backend is None:
                    raise KeyError("no memory backend injected")
                user_id = ctx.state.get("user_id", "")
                results = await _memory_backend.search(
                    query=content,
                    user_id=user_id,
                    top_k=var_def.get("top_k", 5),
                    memory_type="semantic",
                )
                if results:
                    retrieved_text = "\n".join(r.get("content", "") for r in results if isinstance(r, dict))
                    content = f"{content}\n\n### 相关检索结果\n{retrieved_text}" if mode == "hybrid" else retrieved_text
            except Exception as e:
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
            # 未识别占位符（拼错/格式错）不能静默消失——配置作者需要留痕定位
            logger.warning(
                "[%s] 未识别的占位符，已替换为空串（检查拼写/格式）| placeholder={{%s}}",
                self.name,
                placeholder_content,
            )
            return ""

        session_id = ctx.state.get("context.session_id", "")
        constraints = ctx.state.get("constraints", {})
        return await self._resolve_single_var_content(ctx, var_def, session_id, constraints)

    async def _resolve_placeholders(self, ctx: PluginContext, text: str) -> str:
        """替换文本中的所有 {{占位符}} 为实际内容。

        支持有限深度的递归解析：若某占位符的解析结果里再次含 {{xxx}}，
        会在下一趟继续解析，直到文本收敛（无占位符或本趟无变化）或达到
        最大深度。典型用例：{{path:partial.md}} 读出的文件内容里含
        {{timestamp}} 等组合占位符。

        Args:
            ctx: 插件执行上下文
            text: 包含占位符的原始文本

        Returns:
            替换后的文本
        """
        max_depth = self._placeholder_max_depth
        # max_depth=0 退化为单趟扁平替换
        effective_depth = 1 if max_depth <= 0 else max_depth

        from datetime import datetime as _depth_t  # noqa: PLC0415

        for depth in range(effective_depth):
            matches = PLACEHOLDER_PATTERN.findall(text)
            if not matches:
                break  # 无占位符，已收敛

            _depth_s = _depth_t.now()
            logger.debug(
                "[%s] resolve_placeholders depth=%d/%d | matches=%d",
                self.name,
                depth + 1,
                effective_depth,
                len(matches),
            )

            text_before = text  # 收敛检测：本趟若无变化则提前结束

            for idx, match in enumerate(matches):
                # 逐步日志 + 超时保护：卡死时定位到具体哪个占位符，并 fail 而非永久挂起
                # （prompt_build 协程永久挂起会拖垮整个进程）
                _ph_s = _depth_t.now()
                logger.debug(
                    "[%s] resolve_placeholder BEGIN | depth=%d idx=%d/%d | %s",
                    self.name,
                    depth + 1,
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
                        "跳过此占位符避免永久挂起 | depth=%d idx=%d/%d | placeholder=%s",
                        self.name,
                        depth + 1,
                        idx + 1,
                        len(matches),
                        match[:120],
                    )
                    content = ""
                logger.debug(
                    "[%s] resolve_placeholder END | depth=%d idx=%d | elapsed=%.3fs | len=%d",
                    self.name,
                    depth + 1,
                    idx + 1,
                    (_depth_t.now() - _ph_s).total_seconds(),
                    len(content) if content else 0,
                )
                placeholder = "{{" + match + "}}"
                text = text.replace(placeholder, content)

            logger.debug(
                "[%s] resolve_placeholders depth=%d done | elapsed=%.3fs",
                self.name,
                depth + 1,
                (_depth_t.now() - _depth_s).total_seconds(),
            )

            if text == text_before:
                # 本趟无变化：剩余占位符都无法解析（如未知的 {{unknown}} 被替换为空），
                # 再循环也是同样结果，提前结束避免空转。
                break

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

            # dict 形式：配置语法（向后兼容）
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
            # static_vars 已声明知识注入 opt-in（配置明确要），服务缺失时
            # 配置意图无声落空——warning 留痕
            logger.warning(
                "[%s] memory_service 未注册，知识注入（retrieval）配置落空 | tags=%s",
                self.name,
                var_def.get("tags"),
            )
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

        匹配语义（2026-08-15 增强，向后兼容）：
          - 精确匹配：routes 键与 state 值规范化后全等（布尔 true/false
            通吃——yaml 键写 ``true`` 可匹配 Python True；数字 str 化）；
          - fnmatch 通配：精确未命中时遍历 routes 键做通配匹配
            （``deepseek-*`` / ``large|medium`` 等），`_default` 不参与通配；
          - `_default` 兜底：前两者均未命中时使用。

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

        current_value = self._norm_state_val(ctx.state.get(route_key))
        # 键规范化：yaml 布尔键（true/false）与 Python 值同构
        norm_routes = {self._norm_state_val(k): v for k, v in routes.items()}

        matched = norm_routes.get(current_value)
        if matched is None:
            for key, val in norm_routes.items():
                if key == "_default":
                    continue
                if fnmatch.fnmatchcase(current_value, key):
                    matched = val
                    break
        if matched is None:
            matched = norm_routes.get("_default", "")

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

    @staticmethod
    def _norm_state_val(value: Any) -> str:
        """state/routes 值规范化：布尔 → true/false，None → 空串，其余 str 化。

        与 model_prompt_adapter._norm_val 同语义（两插件各自内联，避免
        跨插件共享模块耦合）；fnmatchcase 大小写敏感，str(True)="True"
        匹配 yaml 的 ``true`` 键会失败，故统一小写布尔。
        """
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    async def _load_compression_messages(  # noqa: PLR0912,PLR0915
        self,
        ctx: PluginContext,
    ) -> list[dict[str, Any]]:
        """加载压缩块和状态快照为独立消息列表。

        每个块一条消息（XML 包裹），组装顺序：L2(老→新) → L1(老→新) → state_snapshot。
        预算不足时从 L1 → L2 降级，L2 也不够则丢弃。

        Returns:
            独立消息列表
        """
        messages: list[dict[str, Any]] = []

        from pipeline.types import StateKeys  # noqa: PLC0415

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id or _memory_backend is None:
            return messages

        try:
            results = await _memory_backend.search(
                query="",
                user_id=ctx.state.get("user_id", "") or pipeline_run_id,
                top_k=100,
                memory_type="chunk",
            )
        except Exception as e:
            logger.warning("[%s] 读取压缩块失败 | error=%s", self.name, e)
            return messages

        # 过滤出本管道的 L1/L2 压缩块（metadata.tags 含 pipeline:{id} 标签）
        chunks = self._filter_pipeline_chunks(results, pipeline_run_id)
        if not chunks:
            # 没有压缩块，只加载状态快照
            state_msgs = await self._load_state_snapshot_message(ctx, pipeline_run_id)
            messages.extend(state_msgs)
            return messages

        # ── 预算计算 ──
        context_window = ctx.state.get("context_window", 128000)
        config = _compression_config(context_window)
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
        high_water = float("inf")
        deduped: list = []
        for chunk in chunks:  # _filter_pipeline_chunks 已按 seq_end 降序
            if chunk["seq_start"] >= high_water:
                continue
            deduped.append(chunk)
            high_water = chunk["seq_start"]

        # ── 预算分配：新→老，L1→L2 ──
        # 用 sequence_end 排序：自增整数，语义绝对可靠，不受跨进程/容器时钟漂移影响
        sorted_chunks = sorted(deduped, key=lambda c: c["seq_end"], reverse=True)
        l1_used = 0
        l2_used = 0
        l1_blocks: list = []
        l2_blocks: list = []

        for chunk in sorted_chunks:
            l1_content = chunk["l1_content"] or ""
            l2_content = chunk["l2_content"] or ""
            seq = chunk["seq"]

            l1_tokens = self._estimate_tokens_for_budget(l1_content) if l1_content else 0
            l2_tokens = self._estimate_tokens_for_budget(l2_content) if l2_content else 0

            if l1_budget > 0 and l1_used + l1_tokens <= l1_budget and l1_content:
                l1_blocks.append((seq, l1_content))
                l1_used += l1_tokens
            elif l2_budget > 0 and l2_used + l2_tokens <= l2_budget and l2_content:
                l2_blocks.append((seq, l2_content))
                l2_used += l2_tokens
            # else: L1/L2 都放不下或内容为空，丢弃该块

        # ── 组装消息：L2(老→新) → L1(老→新) ──
        l2_blocks.reverse()
        l1_blocks.reverse()

        for seq, content in l2_blocks:
            messages.append(
                {
                    "role": "system",
                    "name": "compressed",
                    "content": f'<compressed seq="{seq}" level="L2">\n## 三元组摘要\n{content}\n</compressed>',
                    # 语义标记（内部字段）：记忆库检索内容；llm_core 发送前清理
                    "_context_form": "recall",
                }
            )

        for seq, content in l1_blocks:
            messages.append(
                {
                    "role": "system",
                    "name": "compressed",
                    "content": f'<compressed seq="{seq}" level="L1">\n## 过程摘要\n{content}\n</compressed>',
                    # 语义标记（内部字段）：记忆库检索内容；llm_core 发送前清理
                    "_context_form": "recall",
                }
            )

        # ── 状态快照 ──
        state_msgs = await self._load_state_snapshot_message(ctx, pipeline_run_id)
        messages.extend(state_msgs)

        logger.debug(
            "[%s] 压缩消息: L1=%d块 L2=%d块 state_snapshot=%s",
            self.name,
            len(l1_blocks),
            len(l2_blocks),
            "有" if state_msgs else "无",
        )
        return messages

    @staticmethod
    def _filter_pipeline_chunks(
        results: list[Any],
        pipeline_run_id: str,
    ) -> list[dict[str, Any]]:
        """从 backend 检索结果中过滤出本管道的压缩块并合并 L1/L2。

        压缩块以 memory_type="chunk" 落库（见 context_window_guard 的
        CompressionService.save_compression_result），metadata.tags 含
        pipeline:{id} / L1|L2 / seq:{start}-{end}。L1 与 L2 是两条独立记录，
        按 seq 范围合并为 {seq, seq_start, seq_end, l1_content, l2_content}。

        Args:
            results: backend.search 返回的统一形态列表
            pipeline_run_id: 管道运行 ID

        Returns:
            合并后的压缩块列表（按 seq_end 降序）；无匹配块返回 []
        """
        tag_prefix = f"pipeline:{pipeline_run_id}"
        merged: dict[tuple[int, int], dict[str, Any]] = {}
        for item in results or []:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            tags = meta.get("tags") if isinstance(meta, dict) else []
            if not isinstance(tags, list):
                continue
            tags = [t for t in tags if isinstance(t, str)]
            if tag_prefix not in tags:
                continue
            if "L1" not in tags and "L2" not in tags:
                continue
            seq_start, seq_end = PromptBuildPlugin._parse_seq_from_tags(tags)
            if seq_end <= 0:
                continue
            key = (seq_start, seq_end)
            entry = merged.setdefault(
                key,
                {
                    "seq": f"{seq_start}-{seq_end}",
                    "seq_start": seq_start,
                    "seq_end": seq_end,
                    "l1_content": "",
                    "l2_content": "",
                },
            )
            content = item.get("content", "")
            if "L1" in tags:
                entry["l1_content"] = content or entry["l1_content"]
            else:
                entry["l2_content"] = content or entry["l2_content"]
        return sorted(merged.values(), key=lambda c: c["seq_end"], reverse=True)

    @staticmethod
    def _parse_seq_from_tags(tags: list[Any]) -> tuple[int, int]:
        """从 tags 中解析 ``seq:start-end`` 标签。

        与 context_window_guard 的解析契约一致（落库时由
        CompressionService.save_compression_result 写入 ``seq:{start}-{end}``）。

        Args:
            tags: 落库时打的标签列表

        Returns:
            (sequence_start, sequence_end)，解析不到时返回 (0, 0)
        """
        seq_start = 0
        seq_end = 0
        for t in tags:
            if not isinstance(t, str):
                continue
            if t.startswith("seq:"):
                # 形如 "seq:5-12"
                rest = t[4:]
                if "-" in rest:
                    parts = rest.split("-", 1)
                    try:
                        seq_start = int(parts[0])
                        seq_end = int(parts[1])
                    except ValueError:
                        pass
                else:
                    try:
                        seq_start = int(rest)
                        seq_end = seq_start
                    except ValueError:
                        pass
        return seq_start, seq_end

    async def _load_state_snapshot_message(
        self,
        ctx: PluginContext,
        pipeline_run_id: str,
    ) -> list[dict[str, Any]]:
        """加载状态快照为一条独立消息。

        STATE_SNAPSHOT 块以 memory_type="chunk" 落库，metadata.tags 含
        STATE_SNAPSHOT / pipeline:{id}；与压缩块同源（模块级 _memory_backend）。
        返回最新一条匹配快照（backend 结果按相关性排序，取首个）。

        Args:
            ctx: 插件执行上下文
            pipeline_run_id: 管道运行 ID

        Returns:
            状态快照消息列表（最多一条）
        """
        if not pipeline_run_id or _memory_backend is None:
            return []
        tag_prefix = f"pipeline:{pipeline_run_id}"
        try:
            results = await _memory_backend.search(
                query="",
                user_id=ctx.state.get("user_id", "") or pipeline_run_id,
                top_k=100,
                memory_type="chunk",
            )
            for item in results or []:
                if not isinstance(item, dict):
                    continue
                meta = item.get("metadata") or {}
                tags = meta.get("tags") if isinstance(meta, dict) else []
                if not isinstance(tags, list):
                    continue
                tags = [t for t in tags if isinstance(t, str)]
                if "STATE_SNAPSHOT" in tags and tag_prefix in tags:
                    content = item.get("content", "")
                    if content:
                        return [
                            {
                                "role": "system",
                                "name": "state_snapshot",
                                "content": f"<current_state>\n{content}\n</current_state>",
                                # 语义标记（内部字段）：状态快照；llm_core 发送前清理
                                "_context_form": "snapshot",
                            }
                        ]
        except Exception as e:
            # 压缩恢复后对话静默丢失 <current_state> 快照必须可见
            logger.warning(
                "[%s] 状态快照检索失败，本轮缺少 <current_state> | error=%s",
                self.name,
                e,
            )
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

        优先级（2026-08-20 裁定"零兜底"）：agent 配置
        （state["context.dynamic_vars"]，context_build 装载）> 插件配置默认
        （config 的 dynamic_vars，全局变量声明口子）> 不注入（无硬编码兜底）。

        Args:
            ctx: 插件执行上下文

        Returns:
            动态变量消息 dict，或 None（无动态变量时）
        """
        now, suffix = self._now_in_configured_tz()
        parts: list[str] = []

        # 优先级：agent 配置（context.dynamic_vars，
        # context_build 装载）> 插件配置默认（config 的 dynamic_vars，全局变量
        # 声明口子）。两者皆无 → 不注入任何动态变量（返回 None）——硬编码
        # 兜底块（日期/时间/Agent/会话）已删：配置没声明的变量一律不注入，
        # 配置是单一真值源。
        dynamic_vars_def = ctx.state.get("context.dynamic_vars") or self._config.get(
            "dynamic_vars", []
        )
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

                # dict 形式：配置语法（向后兼容）
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
        # 零兜底：未声明（agent 配置与插件默认皆无）→ 无动态变量。
        # 需要环境事实由配置声明（类型系统已支持 timestamp/session/agent/
        # model/placeholder）；身份信息属 system prompt（persona）职责，
        # dynamic_vars 不注入第二个真值源。
        if not parts:
            return None

        content = f"<dynamic_vars>\n以下为系统注入的背景信息和思考提示。\n{chr(10).join(parts)}\n</dynamic_vars>"
        # role 用 user 而非 system：实测（DeepSeek/opencode_go）末尾的 role=system 且每轮变化的
        # 消息会破坏 prompt cache（命中率从 ~97% 崩到 ~5%），role=user 则正常（~99%）。
        # 用 <dynamic_vars> XML 包裹 + 明确"系统注入"措辞，模型仍能识别为背景信息。
        return {
            "role": "user",
            "content": content,
        }

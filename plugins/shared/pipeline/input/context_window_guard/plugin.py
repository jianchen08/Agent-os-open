"""上下文窗口守卫 Input 插件（Step 4 重建版）。

在每次 LLM 调用前检查上下文大小，超阈值时执行预算驱动的分层压缩。

本插件 Step 4 重建要点（相对 0.1 的变化）：
- 压缩算法（CompressionConfig + ContextCompressor + COMPRESS_PROMPT）从
  memory/context_compressor.py 内联到本文件，消除对 0.2 中不存在的
  `memory` 模块的导入依赖（修复老版 _get_memory_service 第 629 行的 import
  在 try/except 之外的 bug，以及 _resolve_trigger_ratio 对
  memory.context_compressor 的硬导入）。
- LLM 调用优先用进程内 `_llm_client`（LLMClient，由 server.py 在 on_load 时
  从内核注入的 models config 构造）；`_llm_client` 为 None 时回退到经
  `_capability_caller` 调 memory.compress 工具的旧路径。测试环境直接注入
  llm_call_fn。
- 存储后端由 ctx.get_service("chunk_service") 改为模块级
  `_memory_backend: IMemoryBackend`（Hindsight/capability），通过
  `set_memory_backend()` 注入。L1/L2/STATE_SNAPSHOT 块以
  memory_type="chunk" 写入，memory_items 以 memory_type="semantic" 写入。

State 命名空间:
    - messages : 压缩后替换的消息列表
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)

# LLM 调用函数类型：接收 prompt 字符串，返回响应字符串
LLMCallFn = Callable[[str], Awaitable[str]]
# 能力调用函数类型：(method, params) -> Any（用于调 memory.compress 等工具）
CapabilityCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]

# 压缩摘要注入提示（与 0.1 保持一致）
_COMPRESSION_NOTICE = (
    "[系统提示] 由于对话历史过长，较早的上下文已被记忆系统分层压缩。"
    "压缩摘要包含在上方消息中，请基于压缩摘要和当前剩余上下文继续完成任务。"
)

# ═══════════════════════════════════════════════════════════
# 模块级依赖注入（由 server.py 的 on_load 注入，测试直接赋值）
# ═══════════════════════════════════════════════════════════

# 长期记忆后端（Hindsight/capability）；None 时压缩块无法落库，相关流程早退
_memory_backend: Any | None = None
# 能力调用句柄；server.py 注入后用于构建压缩 LLM 调用函数（memory.compress 回退路径）
_capability_caller: CapabilityCaller | None = None
# 进程内 LLM 客户端（压缩首选路径）；None 时回退到 _capability_caller
_llm_client: Any | None = None


def set_memory_backend(backend: Any | None) -> None:
    """注入长期记忆后端（IMemoryBackend 实例或兼容 duck-type）。

    由 server.py on_load 调用，把 Step 3 构建的 Hindsight/Kernel 后端注入进来；
    测试环境直接传 FakeBackend/MagicMock。

    Args:
        backend: 实现 add/search/delete/import_document 的后端实例；传 None 清空
    """
    global _memory_backend
    _memory_backend = backend


def set_capability_caller(caller: CapabilityCaller | None) -> None:
    """注入能力调用句柄（async fn `(method, params) -> Any`）。

    server.py 在 on_load 时调用：
    ``set_capability_caller(lambda m, p: plugin.get_capability("tool-executor").call(m, p))``
    压缩执行时据此构建 memory.compress 的 LLM 调用函数。

    Args:
        caller: 能力调用 async 函数；传 None 清空
    """
    global _capability_caller
    _capability_caller = caller


def set_llm_client(client: Any | None) -> None:
    """注入进程内 LLM 客户端（压缩首选路径）。

    server.py 在 on_load 时从内核注入的 models config 构造 LLMClient 并注入；
    测试环境可直接传 MagicMock。传 None 则回退到 _capability_caller 旧路径。

    Args:
        client: 暴露 ``chat_available`` 属性与 ``chat_completion(prompt, max_tokens)``
                方法的客户端实例；传 None 清空
    """
    global _llm_client
    _llm_client = client


def _make_minimal_ctx(
    state: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
    pipeline_id: str = "",
) -> PluginContext:
    """构造测试用最小 PluginContext（不直接被生产代码调用）。

    Args:
        state: state 字典初值（与 pipeline_id 合并）
        config: 配置字典
        services: 服务注册表
        pipeline_id: 写入 state[StateKeys.PIPELINE_ID]

    Returns:
        PluginContext 实例
    """
    s: dict[str, Any] = dict(state or {})
    if pipeline_id:
        s.setdefault(StateKeys.PIPELINE_ID, pipeline_id)
    return PluginContext(state=s, config=config or {}, _services=services or {})


# ═══════════════════════════════════════════════════════════
# 压缩配置（从 0.1 memory/context_compressor.py 移植）
# ═══════════════════════════════════════════════════════════

# 层级名称映射（向后兼容，DSL/CSL/KIL 等历史命名归一到 L1/L2）
LAYER_NAME_MAP = {
    "DSL": "L1",
    "CSL": "L2",
    "KIL": "L2",
}


def normalize_layer_name(layer: str) -> str:
    """标准化层级名称（DSL→L1, CSL→L2, KIL→L2，其余大写化）。

    Args:
        layer: 层级名称

    Returns:
        标准化后的层级名称
    """
    return LAYER_NAME_MAP.get(layer.upper(), layer.upper())


@dataclass
class CompressionConfig:
    """压缩配置。

    所有比例基于 context_window 计算实际 token 数。

    Attributes:
        context_window: 模型上下文窗口大小
        compress_trigger_ratio: 压缩触发比例（默认 0.55，见 config/system/context_window_config.yaml）
        l1_ratio: L1 预算比例
        l2_ratio: L2 预算比例
        recent_ratio: 最近原文预算比例
        retrieval_ratio: 检索召回预算比例
        max_turn_ratio: 单轮次最大比例
    """

    context_window: int = 128000
    compress_trigger_ratio: float = 0.55  # 见 config/system/context_window_config.yaml
    l1_ratio: float = 0.1
    l2_ratio: float = 0.05
    recent_ratio: float = 0.18
    retrieval_ratio: float = 0.05
    max_turn_ratio: float = 0.5

    @classmethod
    def from_yaml_config(cls, context_window: int) -> CompressionConfig:
        """从 config/system/context_window_config.yaml 加载预算配置。

        通过 ConfigCenter 读取（统一缓存 + 热重载），路径由 ConfigCenter 解析。
        读取失败时回退到代码默认（与 0.1 行为一致）。

        Args:
            context_window: 当前模型上下文窗口大小

        Returns:
            填充好预算比例的 CompressionConfig
        """
        try:
            from config.config_center import get_config_center  # noqa: PLC0415

            yaml_data = get_config_center().get("system/context_window_config.yaml") or {}
            budgets = yaml_data.get("budgets", {})
            return cls(
                context_window=context_window,
                compress_trigger_ratio=yaml_data.get(
                    "compress_trigger_ratio", 0.55
                ),  # 见 config/system/context_window_config.yaml
                l1_ratio=budgets.get("l1", 0.1),
                l2_ratio=budgets.get("l2", 0.05),
                recent_ratio=budgets.get("recent", 0.18),
                retrieval_ratio=budgets.get("retrieval", 0.05),
            )
        except Exception:
            return cls(context_window=context_window)

    def get_budgets(self) -> dict[str, int]:
        """计算各部分实际 token 预算。

        Returns:
            各层 token 预算字典 {recent, L1, L2, retrieval, max_turn}
        """
        recent_budget = int(self.context_window * self.recent_ratio)
        return {
            "recent": recent_budget,
            "L1": int(self.context_window * self.l1_ratio),
            "L2": int(self.context_window * self.l2_ratio),
            "retrieval": int(self.context_window * self.retrieval_ratio),
            "max_turn": int(recent_budget * self.max_turn_ratio),
        }

    def get_trigger_threshold(self) -> int:
        """获取触发压缩的 token 阈值。

        Returns:
            触发阈值 = context_window * compress_trigger_ratio
        """
        return int(self.context_window * self.compress_trigger_ratio)


# ═══════════════════════════════════════════════════════════
# 压缩器（从 0.1 memory/context_compressor.py 移植）
# ═══════════════════════════════════════════════════════════


class ContextCompressor:
    """上下文压缩器。

    负责把长对话历史压成结构化摘要。支持分层递进：
    L0(原文) → L1(过程摘要) → L2(三元组)。

    设计原则（与 0.1 一致）：
    - 纯函数设计，无状态管理
    - 输入输出都是字符串/字典
    - 不直接操作数据库（落库由 CompressionService 负责）
    - LLM 调用通过注入的 llm_call_fn 实现而非硬依赖

    Attributes:
        config: 压缩配置
        budgets: 各层 token 预算
        _llm_call_fn: LLM 调用函数（async (prompt) -> response_text）
    """

    # 一次性压缩模板：L1 + L2 + keywords + state_snapshot + memory_items
    # 从 0.1 memory/context_compressor.py 原样移植（精心调校的 prompt，勿改）
    COMPRESS_PROMPT = """## 任务
将以下对话历史压缩为五部分：l1 / l2 / keywords / state_snapshot / memory_items。

l1/l2/keywords 描述同一批对话，详略不同。
**L1 必须比 L2 详细得多**：L1 含具体步骤、决策、产出，
L2 是 L1 的紧凑概括（每个字段一两句话）。降级才有意义。

## 输入

### 当前全局状态（已累积，请在此基础上合并更新）
{state_snapshot}

### 最近的步骤（了解上下文即可，不重复描述）
{recent_process_blocks}

### 需要压缩的对话
{messages}

---

## l1 — 过程摘要（预算充足时使用）

只描述本批新增，不重复已有状态。

### session_title
本段对话的核心主题，一句话。
提取方法：从对话中找最核心的一件事。

### workflow
执行步骤及结果，省略重试和微调，只写关键步骤。
提取方法：从 assistant 消息和 tool 调用结果中提取。

### errors_and_corrections
本段遇到的错误和修复方案。无则填 null。
提取方法：从"报错"、"bug"、"修复"等关键词中提取。

### decisions
重要决策结论和理由。无则填 null。
提取方法：从"决定"、"用XX方案"、"最终选"等描述中提取。

### key_results
本段产出成果，含文件路径和数据。无则填 null。
提取方法：从最终结果消息中提取。

---

## l2 — 三元组（L1 的紧凑概括版）

比 L1 简略得多。每个字段一两句话，不展开细节。

### intent
用户目标和验收标准。
提取方法：从用户消息中提取最终目的。

### process
关键步骤，一句话。

### results
用 | 分隔"本轮产出 | 剩余待办"。

---

## keywords — 本批新增关键词

提取本批特有的关键词，数量不固定，有则提取，无则空数组。
用于检索本压缩块。允许与前面块的关键词重复（方便检索），

---

## state_snapshot — 合并更新

在传入的当前状态基础上更新，不是从零写。
没变化的字段保持原值，不要改写为"无变化"或编造。

### current_state
当前整体进度。
提取方法：从最后几条消息判断"进展到哪了"。

### task_specification
用户要完成的具体任务。本批细化则更新。
提取方法：从用户最新消息中提取。

### pending
待办列表。做完删除，新增追加，搁置标注"(搁置)"。
提取方法：用户说"还要/接下来" + 助手说"接下来要..."的事。

### key_entities
累积实体列表。路径/URL/函数名/类名/配置项，原样保留，合并去重。
提取方法：从对话中提取所有被明确引用的实体名。

### domain_knowledge
累积的重要事实/规则/约束。新发现则更新，无变化则保持。
提取方法：从助手消息中提取关键结论和发现。

### user_feedback
用户明确的纠偏指令（"不对"、"不要XX"、"应该是XX"等）。
无新反馈则保持原值。
提取方法：从用户纠正性话语中提取。

### attention_hints
接手者须知，帮下一个 LLM 快速进入状态。
提取方法：提炼本批最关键的变化点和注意事项。

---

## memory_items — 长期记忆（不进上下文）

有值得跨会话保存的才填，无则 null。

### user_profile_updates
用户偏好/习惯更新。

### project_knowledge_updates
项目技术决策/架构约定。

### experience_updates
踩过的坑/验证过的方案。

---

## 输出格式
严格输出 JSON，不要任何其他内容。

{{
  "l1": {{
    "session_title": "...",
    "workflow": "...",
    "errors_and_corrections": "无则null",
    "decisions": "无则null",
    "key_results": "无则null"
  }},
  "l2": {{
    "intent": "...",
    "process": "...",
    "results": "..."
  }},
  "keywords": ["词1", "词2"],
  "state_snapshot": {{
    "current_state": "...",
    "task_specification": "...",
    "pending": "...",
    "key_entities": "...",
    "domain_knowledge": "...",
    "user_feedback": "...",
    "attention_hints": "..."
  }},
  "memory_items": {{
    "user_profile_updates": "无则null",
    "project_knowledge_updates": "无则null",
    "experience_updates": "无则null"
  }}
}}
"""

    def __init__(
        self,
        llm_call_fn: LLMCallFn | None = None,
        config: CompressionConfig | None = None,
    ) -> None:
        """初始化上下文压缩器。

        Args:
            llm_call_fn: LLM 调用函数（async (prompt) -> response_text），可选
            config: 压缩配置；不传则用默认 CompressionConfig
        """
        self._llm_call_fn = llm_call_fn
        self.config = config or CompressionConfig()
        self.budgets = self.config.get_budgets()

    def set_llm_call_fn(self, llm_call_fn: LLMCallFn) -> None:
        """延迟注入 LLM 调用函数。

        Args:
            llm_call_fn: 异步 LLM 调用函数
        """
        self._llm_call_fn = llm_call_fn

    async def compress_all(
        self,
        messages: list[dict[str, Any]],
        state_snapshot: str = "",
        recent_process_blocks: str = "",
    ) -> dict[str, Any] | None:
        """一次性完成 L1 + L2 + keywords + state_snapshot + memory_items 压缩。

        Args:
            messages: 对话消息列表
            state_snapshot: 当前累积的状态快照（JSON 字符串）
            recent_process_blocks: 最近的过程块样本（采样后的文本）

        Returns:
            压缩结果字典；空消息返回空结果字典（含空键）；
            LLM 空响应或 JSON 解析失败返回 None（fail-closed）；
            LLM 调用异常抛 RuntimeError。

        Raises:
            RuntimeError: LLM 调用过程异常时
        """
        # 空消息：返回空结果字典（不调 LLM），与 0.1 行为一致
        if not messages:
            return {
                "l1": "",
                "l2": "",
                "keywords": [],
                "state_snapshot": {},
                "memory_items": {},
            }

        messages_text = self._format_messages(messages)

        prompt = self.COMPRESS_PROMPT.format(
            messages=messages_text,
            state_snapshot=state_snapshot or "（无已有状态，这是首次压缩）",
            recent_process_blocks=recent_process_blocks or "（无最近步骤）",
        )

        try:
            response = await self._call_llm(prompt)
            if not response or not response.strip():
                logger.warning("[ContextCompressor] LLM 返回空响应，跳过压缩")
                return None

            raw_json = self._extract_json(response)
            if not raw_json or not raw_json.strip():
                logger.warning("[ContextCompressor] JSON 提取结果为空，跳过压缩")
                return None

            import json  # noqa: PLC0415

            try:
                parsed = json.loads(raw_json)
            except json.JSONDecodeError as je:
                logger.warning(
                    "[ContextCompressor] JSON 解析失败: %s | raw_json 前 200 字符: %s",
                    je,
                    raw_json[:200],
                )
                return None

            l1_data = parsed.get("l1", {})
            l1_str = json.dumps(l1_data, ensure_ascii=False, indent=2) if l1_data else ""

            l2_data = parsed.get("l2", {})
            l2_str = json.dumps(l2_data, ensure_ascii=False, indent=2) if l2_data else ""

            raw_keywords = parsed.get("keywords", [])
            keywords = [kw.strip() for kw in raw_keywords if isinstance(kw, str) and kw.strip()][
                :10
            ]

            state_snapshot_data = parsed.get("state_snapshot", {})
            memory_items_data = parsed.get("memory_items", {})

            l1_max = self.budgets.get("L1", 1000)
            l2_max = self.budgets.get("L2", 500)
            l1_str = self._truncate_to_budget(l1_str, l1_max)
            l2_str = self._truncate_to_budget(l2_str, l2_max)

            logger.info(
                "[ContextCompressor] 一次性压缩完成 | L1≈%d字符 L2≈%d字符 "
                "keywords=%d state_snapshot=%d字段 memory_items=%d",
                len(l1_str),
                len(l2_str),
                len(keywords),
                sum(1 for v in state_snapshot_data.values() if v)
                if isinstance(state_snapshot_data, dict)
                else 0,
                sum(1 for v in memory_items_data.values() if v and v != "null")
                if isinstance(memory_items_data, dict)
                else 0,
            )

            return {
                "l1": l1_str,
                "l2": l2_str,
                "keywords": keywords,
                "state_snapshot": state_snapshot_data,
                "memory_items": memory_items_data,
            }

        except Exception as e:
            logger.error("[ContextCompressor] 一次性压缩失败 | error=%s", e)
            raise RuntimeError(f"压缩失败: {e}") from e

    def _truncate_to_budget(self, text: str, max_tokens: int) -> str:
        """截断文本到预算内，保持 JSON 结构完整。

        Args:
            text: 文本
            max_tokens: 最大 token 数

        Returns:
            截断后的文本（JSON 合法）
        """
        estimated = self._estimate_tokens(text)
        if estimated <= max_tokens:
            return text

        import json  # noqa: PLC0415

        max_chars = int(max_tokens * 1.5)
        truncated = text[:max_chars]

        # 不是 JSON 则直接返回字符截断
        try:
            json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return truncated

        # 是 JSON：找一个安全的截断点（最后一个完整 key-value 后的逗号）
        last_comma = truncated.rfind(",\n")
        if last_comma > 0:  # noqa: SIM108
            safe = truncated[:last_comma] + "\n}"
        else:
            # 找不到安全逗号 → 兜底返回空对象保证 JSON 合法
            safe = "{}"

        try:
            parsed = json.loads(safe)
            # 空对象 {} 合法但 falsy，必须用 isinstance 判定
            if isinstance(parsed, dict):
                return safe
        except (json.JSONDecodeError, ValueError):
            pass

        return "{}"

    def _extract_json(self, text: str) -> str:
        """从 LLM 响应中提取 JSON 并格式化。

        处理 LLM 可能包裹代码块、reasoning 模型 <think> 块或添加额外文本的情况。

        Args:
            text: LLM 原始响应

        Returns:
            格式化后的 JSON 字符串；解析失败返回 strip 后的原文
        """
        import json  # noqa: PLC0415
        import re  # noqa: PLC0415

        if not text:
            return text

        # 剥离 reasoning 模型的 <think>...</think> 块（MiniMax-M3/DeepSeek-R1 等输出格式）。
        # think 块内可能含 { 或 ``` 干扰 JSON 提取，必须先剥离。
        stripped = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        # 优先从 markdown 代码块提取
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", stripped, re.DOTALL)
        if json_match:
            json_str = json_match.group(1).strip()
        else:
            # 退化：匹配第一个 { 到最后一个 } 的完整对象
            json_match = re.search(r"\{.*\}", stripped, re.DOTALL)
            json_str = json_match.group(0) if json_match else stripped

        try:
            parsed = json.loads(json_str)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            logger.warning("[ContextCompressor] JSON 解析失败，返回原文")
            return stripped

    def _format_messages(self, messages: list[dict[str, Any]]) -> str:
        """格式化消息为带角色头的文本。

        Args:
            messages: 消息列表

        Returns:
            格式化后的文本（每条以 【角色 N】\\n 内容 形式）
        """
        lines: list[str] = []

        for i, msg in enumerate(messages, 1):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if not content:
                continue

            if role == "user":
                lines.append(f"【用户 {i}】\n{content}")
            elif role == "assistant":
                lines.append(f"【助手 {i}】\n{content}")
            elif role == "system":
                lines.append(f"【系统 {i}】\n{content}")
            elif role == "tool":
                tool_name = msg.get("name", "unknown_tool")
                # 完整保留工具结果：压缩需看到完整内容才能产出高质量摘要。
                # 预算控制由调用方按批次 token 总量切分，不在单条消息上砍。
                lines.append(f"【工具 {i}: {tool_name}】\n{content}")
            else:
                lines.append(f"【{role.upper()} {i}】\n{content}")

            lines.append("")

        return "\n".join(lines)

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 生成摘要。

        Args:
            prompt: 提示词

        Returns:
            LLM 响应文本

        Raises:
            RuntimeError: 无 LLM 调用函数或调用失败时
        """
        if not self._llm_call_fn:
            raise RuntimeError("未提供 LLM 调用函数，无法执行压缩")

        return await self._llm_call_fn(prompt)

    def _estimate_tokens(self, text: str | list[dict[str, Any]]) -> int:
        """估算 token 数（简化版：字符数 // 2）。

        Args:
            text: 文本或消息列表

        Returns:
            估算的 token 数
        """
        if isinstance(text, list):
            total = 0
            for msg in text:
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                total += self._estimate_tokens(content)
            return total

        if not text:
            return 0

        return max(1, len(text) // 2)


# ═══════════════════════════════════════════════════════════
# 压缩服务：协调 compress_all + 落库到 IMemoryBackend
# ═══════════════════════════════════════════════════════════


class CompressionService:
    """压缩服务：在 ContextCompressor 之上叠加预算切分、多轮、落库。

    0.1 MemoryContextService 的精简版：去掉 router_factory / llm_core 适配，
    LLM 调用统一走注入的 llm_call_fn（由模块级 _capability_caller 构建）；
    存储统一走注入的 IMemoryBackend（模块级 _memory_backend）。

    落库映射（与 0.1 chunk_service + memory_service 对齐）：
    - L1 / L2 / STATE_SNAPSHOT → backend.add(memory_type="chunk", ...)
    - memory_items → backend.add(memory_type="semantic", ...)
    """

    _MAX_COMPRESS_ROUNDS = 2

    def __init__(
        self,
        backend: Any | None = None,
        llm_call_fn: LLMCallFn | None = None,
        config: CompressionConfig | None = None,
    ) -> None:
        """初始化压缩服务。

        Args:
            backend: IMemoryBackend 实例（或 duck-type）；None 时压缩结果不入库
            llm_call_fn: LLM 调用函数；None 时压缩无法执行（compress_messages 早退）
            config: 压缩配置；None 用默认 CompressionConfig
        """
        self._backend = backend
        self._llm_call_fn: LLMCallFn | None = llm_call_fn
        self._compressor = ContextCompressor(config=config or CompressionConfig())
        if llm_call_fn:
            self._compressor.set_llm_call_fn(llm_call_fn)
        # 运行时上下文（setup 时填充）
        self._pipeline_id = ""
        self._session_id = ""
        self._user_id = ""
        self._config: dict[str, Any] = {"context_window": 128000}

    def set_llm_call_fn(self, llm_call_fn: LLMCallFn) -> None:
        """延迟注入 LLM 调用函数。

        Args:
            llm_call_fn: 异步 LLM 调用函数
        """
        self._llm_call_fn = llm_call_fn
        self._compressor.set_llm_call_fn(llm_call_fn)

    def setup(
        self,
        *,
        pipeline_id: str = "",
        session_id: str = "",
        user_id: str = "",
        context_window: int = 0,
    ) -> None:
        """注入运行时上下文（pipeline/session/user/window）。

        Args:
            pipeline_id: 管道运行 ID（隔离压缩块的 key）
            session_id: 会话 ID
            user_id: 用户/租户 ID（落库 bank_id）
            context_window: 当前模型上下文窗口（覆盖 config）
        """
        if pipeline_id:
            self._pipeline_id = pipeline_id
        if session_id:
            self._session_id = session_id
        if user_id:
            self._user_id = user_id
        if context_window:
            self._config["context_window"] = context_window

    async def compress_messages(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        trigger_ratio: float = 0.55,
        state_snapshot: str = "",
        recent_process_blocks: str = "",
        llm_call_fn: LLMCallFn | None = None,
    ) -> list[dict[str, Any]] | None:
        """预算驱动的完整压缩流程。

        多轮压缩（最多 _MAX_COMPRESS_ROUNDS 轮）：每轮切 recent 预算 →
        压旧消息 → 落库 → 检查总 tokens；仍超阈值则再压一轮。

        Args:
            messages: 完整消息列表
            context_window: 主模型上下文窗口（预算切分用）
            trigger_ratio: 触发压缩比例
            state_snapshot: 当前累积状态快照（JSON）
            recent_process_blocks: 最近过程块采样文本
            llm_call_fn: 本次调用覆盖的 LLM 调用函数（async (prompt) -> str），
                可选；注入后即作为本服务的 LLM 通道（并同步到内部 compressor）。
                不传则沿用构造/上次注入的函数

        Returns:
            压缩后的消息列表；无需压缩/失败/无 LLM 函数返回 None
        """
        if llm_call_fn is not None:
            self.set_llm_call_fn(llm_call_fn)
        try:
            return await self._compress_messages_impl(
                messages,
                context_window,
                trigger_ratio,
                state_snapshot,
                recent_process_blocks,
            )
        except Exception as exc:
            logger.error(
                "[CompressionService] compress_messages 顶层异常: %s",
                exc,
                exc_info=True,
            )
            return None

    async def _compress_messages_impl(  # noqa: PLR0911
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        trigger_ratio: float,
        state_snapshot: str,
        recent_process_blocks: str,
    ) -> list[dict[str, Any]] | None:
        """compress_messages 的实际实现。"""
        # 自动从 backend 加载背景（state_snapshot + process blocks）
        if not state_snapshot and self._backend and self._pipeline_id:
            bg = await self._load_background()
            state_snapshot = bg["state_snapshot"]
            if not recent_process_blocks:
                recent_process_blocks = bg["process_blocks"]

        if not self._llm_call_fn:
            logger.warning("[CompressionService] 跳过压缩：未提供 LLM 调用函数")
            return None

        config = CompressionConfig.from_yaml_config(context_window)
        budgets = config.get_budgets()
        trigger_tokens = int(context_window * trigger_ratio)

        current_messages = messages
        compressed: list[dict[str, Any]] | None = None

        for round_idx in range(self._MAX_COMPRESS_ROUNDS):
            compressed = await self._do_compress_round(
                current_messages,
                context_window,
                budgets,
                state_snapshot,
                recent_process_blocks,
            )
            if compressed is None:
                break

            total_tokens = sum(self._estimate_msg_tokens(m) for m in compressed)
            logger.info(
                "[CompressionService] 第 %d 轮压缩: %d -> %d 条, %d tokens (触发线 %d)",
                round_idx + 1,
                len(current_messages),
                len(compressed),
                total_tokens,
                trigger_tokens,
            )

            if total_tokens < trigger_tokens:
                return compressed

            current_messages = compressed

        return compressed

    async def _do_compress_round(  # noqa: PLR0911
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        budgets: dict[str, int],
        state_snapshot: str,
        recent_process_blocks: str,
    ) -> list[dict[str, Any]] | None:
        """执行一轮预算驱动的压缩。

        三路分离：pure_system / 旧压缩块（## 历史对话压缩摘要 / _COMPRESSION_NOTICE）/
        其他消息；只压最后一个压缩块之后的新消息；组装为
        pure_system + recent。

        Args:
            messages: 当前消息列表
            context_window: 主模型窗口（recent 预算切分）
            budgets: 各层 token 预算
            state_snapshot: 状态快照
            recent_process_blocks: 过程块采样

        Returns:
            pure_system + recent 组成的消息列表；无需压缩返回 None
        """
        pure_system_msgs: list[dict[str, Any]] = []
        old_blocks: list[dict[str, Any]] = []
        other_msgs: list[dict[str, Any]] = []

        for m in messages:
            role = m.get("role", "")
            content = str(m.get("content", ""))
            if role != "system":
                other_msgs.append(m)
            elif content.startswith("## 历史对话压缩摘要") or content == _COMPRESSION_NOTICE:
                old_blocks.append(m)
            else:
                pure_system_msgs.append(m)

        if not other_msgs:
            return None

        # 按 token 预算从尾部向前算切分点
        recent_budget = budgets["recent"]
        split_idx = self._find_split_by_budget(other_msgs, recent_budget)
        if split_idx <= 0:
            total_est = sum(self._estimate_msg_tokens(m) for m in other_msgs)
            logger.warning(
                "[CompressionService] split_idx=%d, 所有消息都在 recent 预算内: "
                "total_estimated=%d tokens, recent_budget=%d, msg_count=%d",
                split_idx,
                total_est,
                recent_budget,
                len(other_msgs),
            )
            return None

        # 保证工具调用配对完整
        old_msgs, recent_msgs = self._split_preserving_tool_pairs(other_msgs, split_idx)

        if not old_msgs:
            return None

        # 分批压缩（按压缩模型窗口的 0.5 切片，防止单批超压缩模型上下文）
        old_tokens = sum(self._estimate_msg_tokens(m) for m in old_msgs)
        batch_ratio = 0.5
        batch_budget = int(context_window * batch_ratio)
        num_batches = max(1, -(-old_tokens // batch_budget))  # 向上取整

        any_success = False

        for batch_idx in range(num_batches):
            start = batch_idx * len(old_msgs) // num_batches
            end = (batch_idx + 1) * len(old_msgs) // num_batches
            batch = old_msgs[start:end]
            if not batch:
                continue

            comp_result = await self._build_compression_content(
                batch,
                state_snapshot,
                recent_process_blocks,
            )
            if not comp_result:
                logger.warning("[CompressionService] 第 %d 批压缩失败", batch_idx + 1)
                continue

            try:
                await self.save_compression_result(
                    old_msgs=batch,
                    comp_result=comp_result,
                    pipeline_id=self._pipeline_id,
                    session_id=self._session_id,
                    context_window=context_window,
                )
            except Exception as exc:
                logger.warning("[CompressionService] 保存压缩块失败: %s", exc)

            any_success = True

        if not any_success:
            return None

        return pure_system_msgs + recent_msgs

    async def _build_compression_content(
        self,
        old_msgs: list[dict[str, Any]],
        state_snapshot: str,
        recent_process_blocks: str,
    ) -> dict[str, Any] | None:
        """调 compress_all 压一批旧消息，返回 5 部分结果字典。

        Args:
            old_msgs: 待压缩的旧消息批次
            state_snapshot: 状态快照
            recent_process_blocks: 过程块采样

        Returns:
            {l1, l2, keywords, state_snapshot, memory_items} 或 None
        """
        if not self._llm_call_fn:
            return None

        self._compressor.set_llm_call_fn(self._llm_call_fn)

        try:
            result = await self._compressor.compress_all(
                old_msgs,
                state_snapshot=state_snapshot,
                recent_process_blocks=recent_process_blocks,
            )
        except Exception as exc:
            logger.warning("[CompressionService] 压缩失败: %s", exc)
            return None

        # compress_all 失败返回 None（LLM 空响应/JSON 解析失败），跳过保存
        if result is None:
            return None

        if not result.get("l1"):
            return None

        return result

    async def save_compression_result(  # noqa: PLR0912, PLR0915
        self,
        old_msgs: list[dict[str, Any]],
        comp_result: dict[str, Any],
        pipeline_id: str,
        session_id: str,
        context_window: int,
    ) -> None:
        """把压缩结果落到 IMemoryBackend。

        映射（与 0.1 chunk_service + memory_service 对齐）：
        - L1 块：backend.add(memory_type="chunk", content=l1, tags=["L1", ...])
        - L2 块：backend.add(memory_type="chunk", content=l2, tags=["L2", ...])
        - STATE_SNAPSHOT：backend.add(memory_type="chunk", content=ss_json,
          tags=["STATE_SNAPSHOT"])
        - memory_items：backend.add(memory_type="semantic", content=value,
          tags=[规范化字段名])

        Args:
            old_msgs: 本批被压缩的旧消息（用于算 sequence 范围）
            comp_result: compress_all 产出的 5 部分字典
            pipeline_id: 管道运行 ID
            session_id: 会话 ID
            context_window: 当前模型上下文窗口
        """
        if not self._backend:
            return

        import json  # noqa: PLC0415

        l1_content = comp_result.get("l1", "")
        l2_content = comp_result.get("l2", "")
        keywords = comp_result.get("keywords", [])
        state_snapshot = comp_result.get("state_snapshot", {})
        memory_items = comp_result.get("memory_items", {})

        # sequence 范围（用于 _trim_covered_messages / _estimate_assembled_tokens）
        sequences = [
            m["_record_sequence"]
            for m in old_msgs
            if "_record_sequence" in m and isinstance(m["_record_sequence"], int)
        ]
        sequence_start = min(sequences) if sequences else 1
        sequence_end = max(sequences) if sequences else (sequence_start + len(old_msgs) - 1)

        user_id = self._user_id or session_id or pipeline_id or "default"

        # L1 块
        if l1_content:
            await self._backend.add(
                user_id=user_id,
                content=l1_content,
                memory_type="chunk",
                tags=[
                    "L1",
                    f"pipeline:{pipeline_id}",
                    f"seq:{sequence_start}-{sequence_end}",
                    f"ctx:{context_window}",
                ],
                source="compression",
            )

        # L2 块
        if l2_content:
            await self._backend.add(
                user_id=user_id,
                content=l2_content,
                memory_type="chunk",
                tags=[
                    "L2",
                    f"pipeline:{pipeline_id}",
                    f"seq:{sequence_start}-{sequence_end}",
                ],
                source="compression",
            )

        # STATE_SNAPSHOT（覆盖语义由调用方按 tag 删除旧值，这里只追加最新）
        if state_snapshot:
            ss_content = json.dumps(state_snapshot, ensure_ascii=False, indent=2)
            await self._backend.add(
                user_id=user_id,
                content=ss_content,
                memory_type="chunk",
                tags=[
                    "STATE_SNAPSHOT",
                    f"pipeline:{pipeline_id}",
                    f"seq_end:{sequence_end}",
                ],
                source="compression",
            )

        # memory_items → semantic
        if memory_items and isinstance(memory_items, dict):
            tag_map = {
                "user_profile_updates": "user_profile",
                "project_knowledge_updates": "project_knowledge",
                "experience_updates": "experience",
            }
            for key, value in memory_items.items():
                if value and value != "null":
                    await self._backend.add(
                        user_id=user_id,
                        content=str(value),
                        memory_type="semantic",
                        tags=[tag_map.get(key, key)],
                        source="compression",
                    )

    async def _load_background(self) -> dict[str, str]:
        """从 backend 加载压缩背景（state_snapshot + 过程块采样）。

        Returns:
            {"state_snapshot": str, "process_blocks": str}；加载失败返回空串
        """
        state_snapshot = ""
        process_blocks = ""

        if not self._backend or not self._pipeline_id:
            return {"state_snapshot": state_snapshot, "process_blocks": process_blocks}

        try:
            results = await self._backend.search(
                query=f"pipeline:{self._pipeline_id}",
                user_id=self._user_id or self._pipeline_id,
                top_k=20,
                memory_type="chunk",
            )
        except Exception as e:
            logger.warning("[CompressionService] 加载压缩背景失败: %s", e)
            return {"state_snapshot": "", "process_blocks": ""}

        l1_chunks = []
        snapshot = ""
        for item in results:
            meta = item.get("metadata", {}) if isinstance(item, dict) else {}
            tags = meta.get("tags") if isinstance(meta, dict) else None
            if not tags:
                continue
            if "L1" in tags:
                l1_chunks.append(item)
            elif "STATE_SNAPSHOT" in tags:
                content = item.get("content", "") if isinstance(item, dict) else ""
                if content and not snapshot:
                    snapshot = content

        state_snapshot = snapshot

        if l1_chunks:
            # 按 sequence 排序后采样首/中/尾
            def _seq_start(item: dict[str, Any]) -> int:
                tags = (item.get("metadata", {}) or {}).get("tags", [])
                for t in tags:
                    if isinstance(t, str) and t.startswith("seq:"):
                        try:
                            return int(t.split(":")[1].split("-")[0])
                        except (IndexError, ValueError):
                            pass
                return 0

            sorted_chunks = sorted(l1_chunks, key=_seq_start)
            if len(sorted_chunks) <= 3:
                samples = sorted_chunks
            else:
                mid_idx = len(sorted_chunks) // 2
                samples = [sorted_chunks[0], sorted_chunks[mid_idx], sorted_chunks[-1]]
            process_blocks = "\n\n---\n\n".join(
                (c.get("content", "") if isinstance(c, dict) else "") for c in samples
            )

        return {"state_snapshot": state_snapshot, "process_blocks": process_blocks}

    @staticmethod
    def _find_split_by_budget(
        messages: list[dict[str, Any]],
        token_budget: int,
    ) -> int:
        """从尾部向前累加 token，找到预算内的切分点。

        返回 split_idx：
          messages[:split_idx] → 待压缩
          messages[split_idx:] → 保留（在预算内）
        """
        accumulated = 0
        for i in range(len(messages) - 1, -1, -1):
            msg_tokens = CompressionService._estimate_msg_tokens(messages[i])
            if accumulated + msg_tokens > token_budget:
                return i + 1
            accumulated += msg_tokens
        return 0

    @staticmethod
    def _estimate_msg_tokens(msg: dict[str, Any]) -> int:
        """估算单条消息的 token 数（简化版：字符数 // 2）。"""
        content = str(msg.get("content", ""))
        tokens = max(1, len(content) // 2) if content else 0
        for tc in msg.get("tool_calls", []):
            args = tc.get("function", {}).get("arguments", "")
            if args:
                tokens += max(1, len(args) // 2)
        return tokens

    @staticmethod
    def _split_preserving_tool_pairs(
        messages: list[dict[str, Any]],
        split_idx: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按 split_idx 分割消息列表，保证 tool call/result 配对完整。"""
        old_msgs = list(messages[:split_idx])
        recent_msgs = list(messages[split_idx:])

        recent_tool_ids: set[str] = set()
        for msg in recent_msgs:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    recent_tool_ids.add(tc_id)

        if not recent_tool_ids:
            return old_msgs, recent_msgs

        move_count = 0
        for i in range(len(old_msgs) - 1, -1, -1):
            msg = old_msgs[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                call_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
                if call_ids & recent_tool_ids:
                    move_count = len(old_msgs) - i
                    break

        if move_count > 0:
            migrated = old_msgs[-move_count:]
            old_msgs = old_msgs[:-move_count]
            recent_msgs = migrated + recent_msgs

        return old_msgs, recent_msgs


# ═══════════════════════════════════════════════════════════
# ContextWindowGuardPlugin（保持 execute / name / priority / __init__ 签名不变）
# ═══════════════════════════════════════════════════════════


class ContextWindowGuardPlugin(IInputPlugin):
    """上下文窗口守卫 Input 插件。

    检查 messages 的估算 token 数，超阈值时执行预算驱动的分层压缩。
    压缩算法（CompressionConfig + ContextCompressor）内联在本文件，
    存储走模块级 _memory_backend（Hindsight/capability），LLM 调用走
    模块级 _capability_caller（memory.compress 工具）。

    优先级：5（在 prompt_build 的 10 之前执行）
    错误策略：SKIP（压缩失败不阻塞管线）

    Attributes:
        _config: 插件配置字典
        _trigger_ratio: 触发压缩的阈值比例（默认 0.55）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化上下文窗口守卫插件。

        配置优先级（高→低）：
          ① Agent YAML plugins.enabled.context_window_guard.trigger_ratio
             （由 plugin_resolver 合并进 config，或由 _apply_runtime_config 从 ctx.state 读）
          ② Pipeline YAML plugins.context_window_guard.config.trigger_ratio
             （即本 __init__ 收到的 config 参数）
          ③ System YAML config/system/context_window_config.yaml 的 compress_trigger_ratio
          ④ 代码硬编码默认 0.55

        Args:
            config: 插件配置字典（来自 pipeline yaml），支持以下键：
                - enabled: 是否启用（默认 True）
                - trigger_ratio: 触发压缩的阈值比例（不配则继承 system yaml）
                - compression_model: 压缩专用模型 ID（如 minimax-m3），
                  为空时回退到 llm.yaml 的 defaults.compression，再为空则用主模型
        """
        self._config = config or {}
        self._trigger_ratio = self._resolve_trigger_ratio(self._config.get("trigger_ratio"))
        self._compression_model: str | None = self._resolve_compression_model(
            self._config.get("compression_model"),
        )
        # 实例级追踪：插件可能被重复实例化，state 不一定跨迭代持久化
        # 用实例变量做主存储，ctx.state 做辅助（重启恢复场景）
        self._tracked_msg_count: int = 0

    @staticmethod
    def _resolve_trigger_ratio(explicit: float | None) -> float:
        """解析 trigger_ratio：pipeline 显式值 → system yaml → 代码默认。

        三层覆盖链路中 ②→③ 的衔接：当 pipeline yaml 没配 trigger_ratio 时，
        从 system 的 context_window_config.yaml 继承 compress_trigger_ratio。
        Step 4 修复：改用本文件内联的 CompressionConfig，不再导入
        memory.context_compressor（0.2 中不存在）。

        Args:
            explicit: pipeline yaml 显式配置的 trigger_ratio（可能为 None）

        Returns:
            最终生效的 trigger_ratio
        """
        # ② Pipeline 显式配置优先
        if explicit is not None:
            return explicit

        # ③ System YAML fallback（用本文件内联的 CompressionConfig）
        try:
            sys_config = CompressionConfig.from_yaml_config(context_window=128000)
            return sys_config.compress_trigger_ratio
        except Exception:
            pass

        # ④ 代码默认（见 config/system/context_window_config.yaml）
        return 0.55

    @staticmethod
    def _resolve_compression_model(explicit: str | None) -> str | None:
        """解析压缩模型：插件配置优先，回退到 llm.yaml defaults.compression。

        Args:
            explicit: 插件配置中显式指定的 compression_model（可能为空）

        Returns:
            最终使用的模型 ID；若都为空则返回 None（运行时用主模型）
        """
        if explicit:
            return explicit
        try:
            from config.models import get_model_config_loader  # noqa: PLC0415

            loader = get_model_config_loader()
            defaults = loader._load_llm_data().get("defaults", {})
            default_id = defaults.get("compression", "")
            if default_id:
                return default_id
        except Exception:
            pass
        return None

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "context_window_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级，在 prompt_build 之前执行。"""
        return self._config.get("priority", 5)

    # ------------------------------------------------------------------
    # Agent 级运行时配置覆盖
    # ------------------------------------------------------------------

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 Agent 配置覆盖运行时参数。

        三层覆盖链路（高优先级覆盖低优先级）：
          ① Agent YAML (plugins.enabled.context_window_guard.{key})
          ② Pipeline YAML (plugins.context_window_guard.config.{key})
          ③ 代码默认值

        Agent 覆盖通过两条路径生效：
        - 路径 A：plugin_resolver.apply_agent_plugin_configs() 已用合并后的
          config 重新构造本插件实例（_config 已含 agent override），构造时
          _trigger_ratio 已正确。此方法处理路径 B。
        - 路径 B：ctx.state 中可能携带 agent 注入的运行时覆盖（与 stop_check
          等插件从 ctx.state 读 max_iterations 同一机制）。

        本方法读 ctx.state 里的 context_guard.trigger_ratio（如有）覆盖 _trigger_ratio。

        Args:
            ctx: 插件执行上下文
        """
        state_ratio = ctx.state.get("context_guard.trigger_ratio")
        if state_ratio is not None:
            self._trigger_ratio = state_ratio

    # ------------------------------------------------------------------
    # Token 估算（统一算法：len//2）
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_msg_tokens(msg: dict[str, Any]) -> int:
        """估算单条消息的 token 数（简化版：字符数 // 2）。"""
        content = str(msg.get("content", ""))
        tokens = max(1, len(content) // 2) if content else 0
        for tc in msg.get("tool_calls", []):
            args = tc.get("function", {}).get("arguments", "")
            if args:
                tokens += max(1, len(args) // 2)
        return tokens

    async def _estimate_effective_tokens(
        self,
        messages: list[dict[str, Any]],
        ctx: PluginContext,
    ) -> int:
        """估算有效上下文大小。

        三级估算策略：
        1. prev_input + delta：用上一轮 LLM 真实 input_tokens + 新增消息增量
        2. 压缩块拼接估算：L1 块 tokens + recent 消息 tokens（重启/llm_usage 丢失时）
        3. 全量字符估算：最后手段
        """
        llm_usage = ctx.state.get("llm_usage", {})
        prev_input = llm_usage.get("input_tokens", 0)

        # llm_usage 可能为空（空响应/截断），从历史累计回退
        if prev_input == 0:
            track_usage = ctx.state.get("track.llm_usage", {})
            prev_input = track_usage.get("input_tokens", 0)
            if prev_input > 0:
                logger.debug(
                    "[%s] 估算: llm_usage 为空，从 track 回退: prev_input=%d",
                    self.name,
                    prev_input,
                )

        # 策略 1：prev_input + delta（仅同进程连续迭代有效）
        tracked = max(self._tracked_msg_count, ctx.state.get("_tracked_msg_count", 0))
        current_non_sys = sum(1 for m in messages if m.get("role") != "system")
        restart_signature = tracked == 0 and current_non_sys > 50
        logger.debug(
            "[%s] 估算分叉: prev_input=%d, tracked=%d, current_non_sys=%d, restart_signature=%s, msg_total=%d",
            self.name,
            prev_input,
            tracked,
            current_non_sys,
            restart_signature,
            len(messages),
        )
        if prev_input > 0 and not restart_signature:
            if current_non_sys <= tracked:
                logger.debug(
                    "[%s] 估算(无增量): %d tokens (prev_input=%d, tracked=%d, current=%d)",
                    self.name,
                    prev_input,
                    prev_input,
                    tracked,
                    current_non_sys,
                )
                return prev_input

            non_sys_msgs = [m for m in messages if m.get("role") != "system"]
            delta_msgs = non_sys_msgs[tracked:]
            delta_tokens = sum(self._estimate_msg_tokens(m) for m in delta_msgs)

            effective = prev_input + delta_tokens
            logger.debug(
                "[%s] 估算(增量): %d tokens (prev_input=%d + delta=%d, tracked=%d, current=%d, delta_count=%d)",
                self.name,
                effective,
                prev_input,
                delta_tokens,
                tracked,
                current_non_sys,
                len(delta_msgs),
            )
            return effective

        # 策略 2：压缩块拼接估算
        assembled = await self._estimate_assembled_tokens(ctx, messages)
        logger.debug(
            "[%s] 估算(策略2/压缩块拼接): assembled=%d, msg_count=%d",
            self.name,
            assembled,
            len(messages),
        )
        if assembled >= 0:
            return assembled

        # 策略 3：全量字符估算（最后手段）
        estimated = sum(self._estimate_msg_tokens(m) for m in messages)
        logger.warning(  # 落到策略3说明前两个都失败了，值得告警
            "[%s] 估算(策略3/全量字符 兜底): estimated=%d, msg_count=%d, prev_input=%d, tracked=%d",
            self.name,
            estimated,
            len(messages),
            prev_input,
            tracked,
        )
        return estimated

    async def _estimate_assembled_tokens(
        self,
        ctx: PluginContext,
        messages: list[dict[str, Any]],
    ) -> int:
        """用已有的压缩块 + recent 消息估算实际发送给 LLM 的 token 数。

        模拟 prompt_build 的拼接逻辑：
        system 消息 + L1 压缩块 + STATE_SNAPSHOT + recent 消息

        Step 4 修复：压缩块来源由 ctx.get_service("chunk_service") 改为
        模块级 _memory_backend（Hindsight/capability），无 backend 时早退返回 -1。

        Returns:
            估算 token 数，无法估算时返回 -1
        """
        pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_id or not _memory_backend:
            return -1

        # 从 backend 检索该 pipeline 的 chunk 类记忆
        user_id = ctx.state.get("user_id", "") or pipeline_id
        try:
            results = await _memory_backend.search(
                query=f"pipeline:{pipeline_id}",
                user_id=user_id,
                top_k=20,
                memory_type="chunk",
            )
        except Exception:
            return -1

        l1_chunks, snapshot_chunks = self._filter_chunks(results)
        if not l1_chunks:
            return -1

        # L1 压缩块 token 估算
        l1_tokens = sum(max(1, len(c["content"]) // 2) for c in l1_chunks)

        # STATE_SNAPSHOT token 估算
        snapshot_tokens = 0
        if snapshot_chunks:
            snapshot_tokens = max(1, len(snapshot_chunks[0]["content"]) // 2)

        # system 消息 + recent 消息（非压缩块的）
        system_tokens = sum(
            self._estimate_msg_tokens(m) for m in messages if m.get("role") == "system"
        )

        # recent 消息：全局 _record_sequence > max_end 的部分
        max_end = max((c["sequence_end"] for c in l1_chunks if c["sequence_end"]), default=0)
        recent_tokens = 0
        for m in messages:
            if m.get("role") == "system":
                continue
            seq = m.get("_record_sequence")
            if isinstance(seq, int) and seq > max_end:
                recent_tokens += self._estimate_msg_tokens(m)

        total = l1_tokens + snapshot_tokens + system_tokens + recent_tokens
        logger.debug(
            "[%s] 压缩块拼接估算: l1=%d (blocks=%d), snapshot=%d, system=%d, recent=%d (after=%d), total=%d",
            self.name,
            l1_tokens,
            len(l1_chunks),
            snapshot_tokens,
            system_tokens,
            recent_tokens,
            max_end,
            total,
        )
        return total

    @staticmethod
    def _filter_chunks(results: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从 backend 检索结果中分出 L1 块和 STATE_SNAPSHOT 块。

        每个返回块统一为 {content, sequence_start, sequence_end, ...} 形态。
        sequence 信息从 metadata.tags 中的 ``seq:start-end`` 标签解析。

        Args:
            results: backend.search 返回的统一形态列表

        Returns:
            (l1_chunks, snapshot_chunks)
        """
        l1_chunks: list[dict[str, Any]] = []
        snapshot_chunks: list[dict[str, Any]] = []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            tags = meta.get("tags") if isinstance(meta, dict) else []
            if not tags:
                continue
            seq_start, seq_end = ContextWindowGuardPlugin._parse_seq_from_tags(tags)
            entry = {
                "content": item.get("content", ""),
                "sequence_start": seq_start,
                "sequence_end": seq_end,
            }
            if "L1" in tags:
                l1_chunks.append(entry)
            elif "STATE_SNAPSHOT" in tags:
                snapshot_chunks.append(entry)
        return l1_chunks, snapshot_chunks

    @staticmethod
    def _parse_seq_from_tags(tags: list[Any]) -> tuple[int, int]:
        """从 tags 中解析 ``seq:start-end`` / ``seq_end:N`` 标签。

        Args:
            tags: backend 写入时打的标签列表

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
            elif t.startswith("seq_end:"):
                try:
                    seq_end = int(t.split(":", 1)[1])
                except (IndexError, ValueError):
                    pass
        return seq_start, seq_end

    _warned_no_context_window = False

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def execute(self, ctx: PluginContext) -> PluginResult:  # noqa: PLR0911
        """检查上下文大小并在超阈值时触发记忆系统压缩。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含压缩后 messages 的插件执行结果
        """
        # Agent 级覆盖：从 ctx.state 读 agent YAML 中 plugins.enabled.context_window_guard 的配置
        self._apply_runtime_config(ctx)

        context_window = ctx.state.get("context_window")
        if not context_window:
            if not self._warned_no_context_window:
                self._warned_no_context_window = True
                logger.error(
                    "[%s] context_window 未设置，上下文守卫无法工作！"
                    " 请检查模型配置（llm.yaml）是否包含 context_window，"
                    "以及 core_plugins 是否正确合并了模型配置。",
                    self.name,
                )
            return PluginResult()

        messages = ctx.state.get("messages", [])
        if not messages:
            return PluginResult()

        # 获取压缩服务（基于模块级 backend + capability 注入；无依赖返回 None 早退）
        service = self._get_memory_service(ctx)
        if not service:
            return PluginResult()

        # 注入运行时上下文到 service
        self._setup_service(ctx, service, context_window)

        # 窗口变更检测（无 backend 时 clean_if_window_changed 直接返回 None）
        cleaned = await self.clean_if_window_changed(messages, context_window, ctx)
        if cleaned is not None:
            messages = cleaned

        # 重启场景裁剪：从存储全量恢复后，已被压缩块覆盖的旧消息需先剔除。
        trimmed = False
        if len(messages) > self._tracked_msg_count + 50:
            new_messages = await self._trim_covered_messages(ctx, messages)
            trimmed = new_messages is not messages
            if trimmed:
                messages = new_messages

        # 阈值检查
        estimated_tokens = await self._estimate_effective_tokens(messages, ctx)
        trigger_tokens = int(context_window * self._trigger_ratio)
        logger.debug(
            "[%s] 阈值检查: estimated=%d, trigger=%d, context_window=%d, ratio=%.2f, msg_count=%d, service=%s",
            self.name,
            estimated_tokens,
            trigger_tokens,
            context_window,
            self._trigger_ratio,
            len(messages),
            type(service).__name__,
        )
        if estimated_tokens < trigger_tokens:
            # 不压缩，仅更新追踪计数；messages 已在上方裁剪过
            current_non_sys = sum(1 for m in messages if m.get("role") != "system")
            self._tracked_msg_count = current_non_sys
            updates: dict[str, Any] = {"_tracked_msg_count": current_non_sys}
            if trimmed or cleaned is not None:
                updates["messages"] = messages
            return PluginResult(state_updates=updates)

        logger.info(
            "[%s] 上下文接近窗口限制: estimated_tokens=%d, trigger_tokens=%d, "
            "context_window=%d, trigger_ratio=%.2f, msg_count=%d",
            self.name,
            estimated_tokens,
            trigger_tokens,
            context_window,
            self._trigger_ratio,
            len(messages),
        )

        # 前端压缩进度通知
        _on_chunk = ctx.state.get("on_chunk")
        if _on_chunk:
            with contextlib.suppress(Exception):
                _on_chunk(
                    {
                        "type": "compression_start",
                        "pipeline_id": ctx.state.get("pipeline_id", ""),
                    }
                )

        # 调用压缩
        logger.info("[%s] 开始调用 compress_messages ...", self.name)
        try:
            compressed = await service.compress_messages(
                messages=messages,
                context_window=context_window,
                trigger_ratio=self._trigger_ratio,
            )
        except Exception as exc:
            logger.error(
                "[%s] compress_messages 异常: %s | service=%s",
                self.name,
                exc,
                type(service).__name__,
                exc_info=True,
            )
            # 压缩异常 → 终止管线
            ctx.state[StateKeys.ENDED] = True
            return PluginResult(
                state_updates={StateKeys.ENDED: True, "input_route_target": "end"},
                skip_remaining=True,
            )

        if compressed and len(compressed) < len(messages):
            logger.info(
                "[%s] 压缩完成: %d -> %d 条消息",
                self.name,
                len(messages),
                len(compressed),
            )
            # 压缩只搬运消息不格式化，会原样保留历史段里的 raw 格式 tool_calls，
            # 写回 state 前强制标准化为 OpenAI API 格式，否则上游报"工具类型不能为空"。
            self._standardize_tool_calls(compressed)
            post_compress_count = sum(1 for m in compressed if m.get("role") != "system")
            self._tracked_msg_count = post_compress_count
            ctx.state["_tracked_msg_count"] = post_compress_count
            return PluginResult(
                state_updates={
                    "messages": compressed,
                    "_tracked_msg_count": post_compress_count,
                }
            )

        # 压缩返回 None（失败）或未减少消息数 → 终止管线
        logger.error(
            "[%s] 上下文压缩失败: estimated=%d 超过 trigger=%d 但压缩未能减少消息 (compressed=%s, original=%d)",
            self.name,
            estimated_tokens,
            trigger_tokens,
            f"{len(compressed)}条" if compressed else "None",
            len(messages),
        )
        ctx.state[StateKeys.ENDED] = True
        return PluginResult(
            state_updates={StateKeys.ENDED: True, "input_route_target": "end"},
            skip_remaining=True,
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _standardize_tool_calls(self, messages: list[dict[str, Any]]) -> None:
        """压缩写回前把 tool_calls 标准化为 OpenAI API 格式。

        委托给 normalizer 的公共入口 standardize_tool_calls_in_messages
        （纯函数全量修复，同步配对的 tool result）。延迟 import 避免
        input 插件模块加载期耦合 core 插件模块。

        异常策略：只容忍 messages 数据形态引发的运行期错误（不阻塞写回）；
        ImportError 等编程/配置错误必须上抛——历史上用空泛 ``except Exception``
        吞掉了断裂 import（``plugins.core`` 不存在），使标准化静默失效三轮审查未觉。
        """
        try:
            # 经 pipeline 命名空间包解析（plugins/shared 在 sys.path）。
            # 原 ``plugins.core.llm_core._message_normalizer`` 路径不存在。
            from pipeline.core.llm_core._message_normalizer import (  # noqa: PLC0415
                standardize_tool_calls_in_messages,
            )

            standardize_tool_calls_in_messages(messages)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            logger.warning(
                "[%s] tool_calls 标准化失败（不阻塞写回）: %s",
                self.name,
                exc,
            )

    async def _trim_covered_messages(  # noqa: PLR0911
        self,
        ctx: PluginContext,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """裁剪被已有压缩块覆盖的旧消息（重启场景）。

        Step 4 修复：压缩块来源由 ctx.get_service("chunk_service") 改为
        模块级 _memory_backend，无 backend 时原样返回。

        裁剪逻辑：逐条按全局 _record_sequence 过滤，保留 system 消息 + 非系统消息
        中 _record_sequence > max_end 的部分。max_end 取自压缩块的 sequence_end。

        Args:
            ctx: 插件执行上下文
            messages: 当前消息列表

        Returns:
            裁剪后的消息列表（如果没有压缩块则原样返回）
        """
        pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_id or not _memory_backend:
            return messages

        user_id = ctx.state.get("user_id", "") or pipeline_id
        try:
            results = await _memory_backend.search(
                query=f"pipeline:{pipeline_id}",
                user_id=user_id,
                top_k=20,
                memory_type="chunk",
            )
        except Exception:
            return messages

        l1_chunks, _ = self._filter_chunks(results)
        if not l1_chunks:
            return messages

        max_end = max((c["sequence_end"] for c in l1_chunks if c["sequence_end"]), default=0)
        if max_end <= 0:
            return messages

        # 裁剪：逐条按全局 _record_sequence 过滤，保留序号 > max_end 的非 system 消息。
        trimmed: list[dict[str, Any]] = []
        trimmed_non_sys = 0
        orig_non_sys = 0
        dropped_seqs: list[int] = []
        for m in messages:
            if m.get("role") == "system":
                trimmed.append(m)
                continue
            orig_non_sys += 1
            seq = m.get("_record_sequence")
            if not isinstance(seq, int) or seq > max_end:
                trimmed.append(m)
                trimmed_non_sys += 1
            else:
                dropped_seqs.append(seq)

        # 防护：裁剪后非 system 消息不足原 10% 说明边界异常，保留原消息避免把上下文裁空。
        if orig_non_sys > 0 and trimmed_non_sys < orig_non_sys * 0.1:
            logger.error(
                "[%s] 裁剪后非 system 消息仅剩 %d/%d（<10%%），疑似 max_end=%d 与消息"
                " sequence 范围错位，放弃裁剪保留原消息",
                self.name,
                trimmed_non_sys,
                orig_non_sys,
                max_end,
            )
            return messages

        if dropped_seqs:
            logger.info(
                "[%s] 裁剪被压缩块覆盖的旧消息: %d -> %d (max_end=%d, 裁掉非system seq=%s)",
                self.name,
                len(messages),
                len(trimmed),
                max_end,
                f"min={min(dropped_seqs)},max={max(dropped_seqs)},count={len(dropped_seqs)}",
            )
            return trimmed

        return messages

    async def clean_if_window_changed(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        ctx: PluginContext,
    ) -> list[dict[str, Any]] | None:
        """检测 context_window 是否变化，变化时清理旧压缩摘要。

        Step 4 修复：压缩块来源由 chunk_service 改为 _memory_backend，
        无 backend 时直接返回 None。

        Args:
            messages: 当前消息列表
            context_window: 当前模型上下文窗口
            ctx: 插件执行上下文

        Returns:
            清理后的消息列表；无需清理返回 None
        """
        pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_id or not _memory_backend:
            return None

        user_id = ctx.state.get("user_id", "") or pipeline_id
        try:
            results = await _memory_backend.search(
                query=f"pipeline:{pipeline_id}",
                user_id=user_id,
                top_k=20,
                memory_type="chunk",
            )
        except Exception:
            return None

        l1_chunks, _ = self._filter_chunks(results)
        if not l1_chunks:
            return None

        # 检查最新块的 ctx 标签（window 变更检测）
        latest = max(
            (c for c in l1_chunks if c.get("sequence_end")),
            key=lambda c: c["sequence_end"],
            default=None,
        )
        if latest is None:
            return None

        # 从 backend 结果里重新找含 ctx 标签的那条
        latest_window = self._find_chunk_window(results, latest["sequence_end"])
        if not latest_window or latest_window == context_window:
            return None

        cleaned = [
            m
            for m in messages
            if not (
                m.get("role") == "system"
                and (
                    str(m.get("content", "")).startswith("## 历史对话压缩摘要")
                    or str(m.get("content", "")) == _COMPRESSION_NOTICE
                )
            )
        ]

        if len(cleaned) == len(messages):
            return None

        logger.info(
            "[%s] context_window 变更: %d → %d, 清理 %d 条旧压缩摘要",
            self.name,
            latest_window,
            context_window,
            len(messages) - len(cleaned),
        )
        return cleaned

    @staticmethod
    def _find_chunk_window(results: list[Any], target_seq_end: int) -> int:
        """从 backend 检索结果中找含 ctx: 标签且 seq_end 匹配的窗口值。

        Args:
            results: backend.search 返回结果
            target_seq_end: 目标 sequence_end

        Returns:
            窗口值；找不到返回 0
        """
        for item in results or []:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            tags = meta.get("tags") if isinstance(meta, dict) else []
            if not tags:
                continue
            seq_start, seq_end = ContextWindowGuardPlugin._parse_seq_from_tags(tags)
            if seq_end != target_seq_end:
                continue
            for t in tags:
                if isinstance(t, str) and t.startswith("ctx:"):
                    try:
                        return int(t.split(":", 1)[1])
                    except (IndexError, ValueError):
                        pass
        return 0

    @staticmethod
    def _get_memory_service(ctx: PluginContext):  # noqa: C901
        """获取 CompressionService 实例（基于模块级注入的 backend + capability）。

        Step 4 修复：
        - 老版 line 629 的 ``from memory.memory_context_service import`` 在
          try/except 之外，0.2 中 memory 模块不存在直接 ImportError。
        - 现改为：用模块级 _memory_backend / _capability_caller 构造本地
          CompressionService；无 backend/capability 时返回 None（早退）。

        Args:
            ctx: 插件执行上下文（用于读取 context_window）

        Returns:
            CompressionService 实例；依赖不全返回 None
        """
        # 优先用 ctx 已注入的 context_service（保留老接口兼容，便于其他路径注入）
        try:
            svc = ctx.get_service("context_service")
            if svc is not None:
                return svc
        except (KeyError, AttributeError):
            pass

        # 无 backend 或无 capability → 无法构建可用的 CompressionService
        if _memory_backend is None and _capability_caller is None:
            return None

        context_window = ctx.state.get("context_window", 128000)
        config = CompressionConfig.from_yaml_config(context_window)

        # 构建 LLM 调用函数：经 capability_caller 调 memory.compress 工具
        llm_call_fn = None
        if _capability_caller is not None:
            llm_call_fn = _build_compress_llm_call_fn(_capability_caller)

        try:
            return CompressionService(
                backend=_memory_backend,
                llm_call_fn=llm_call_fn,
                config=config,
            )
        except Exception as exc:
            logger.warning(
                "[ContextWindowGuardPlugin] 构造 CompressionService 失败: %s",
                exc,
            )
            return None

    def _setup_service(self, ctx: PluginContext, service, context_window: int) -> None:
        """将运行时上下文注入到 service（pipeline/session/user/window）。

        Step 4 修复：不再注入 chunk_service/memory_service/llm_core（0.2 已去），
        改用 CompressionService.setup 的轻量接口。
        """
        pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        session_id = ctx.state.get("context.session_id", "")
        user_id = ctx.state.get("user_id", "")

        try:
            service.setup(
                pipeline_id=pipeline_id,
                session_id=session_id,
                user_id=user_id,
                context_window=context_window,
            )
            logger.debug(
                "[%s] setup 完成: pipeline_id=%s, compression_model=%s",
                self.name,
                pipeline_id[:8] if pipeline_id else "无",
                self._compression_model,
            )
        except Exception as exc:
            logger.error("[%s] setup 异常: %s", self.name, exc, exc_info=True)


def _build_compress_llm_call_fn(caller: CapabilityCaller) -> LLMCallFn:
    """构建压缩用的 LLM 调用函数。

    优先路径：进程内 `_llm_client`（LLMClient），直接调 chat_completion，
    避免一次跨进程 tool-executor hop。
    回退路径：经 capability_caller 调 memory.compress 工具（入参 {prompt, max_tokens}，
    出参 {summary, degraded}）。

    Args:
        caller: 能力调用 async 函数 (method, params) -> Any（_llm_client 为 None 时启用）

    Returns:
        async (prompt) -> response_text 的 LLM 调用函数；降级时返回空串
    """
    import asyncio  # noqa: PLC0415

    async def _call(prompt: str) -> str:
        # ── 首选：进程内 LLMClient ──
        if _llm_client is not None:
            if not getattr(_llm_client, "chat_available", False):
                logger.info("[compress_llm_call] LLMClient chat 不可用，降级返回空串")
                return ""
            try:
                summary = await asyncio.to_thread(_llm_client.chat_completion, prompt, 8000)
                return summary.strip() if summary else ""
            except Exception as e:
                logger.warning("[compress_llm_call] LLMClient chat_completion 失败: %s", e)
                return ""

        # ── 回退：capability_caller → memory.compress 工具 ──
        params = {
            "tool_name": "memory.compress",
            "args": {"prompt": prompt, "max_tokens": 8000},
        }
        try:
            result = await caller("tool-executor.invoke", params)
        except Exception as e:
            logger.warning("[compress_llm_call] memory.compress 调用失败: %s", e)
            return ""
        if isinstance(result, dict):
            if result.get("degraded"):
                logger.info(
                    "[compress_llm_call] memory.compress 降级: %s",
                    result.get("error", ""),
                )
                return ""
            return str(result.get("summary", "") or "")
        # 兼容直接返回字符串的形态
        return str(result) if result else ""

    return _call


# ── 兼容老测试/外部调用：保留 create_compress_llm_call_fn 公共入口 ──
def create_compress_llm_call_fn(caller: CapabilityCaller) -> LLMCallFn:
    """公共入口：从 capability_caller 构建压缩 LLM 调用函数。

    供 server.py / 测试直接调用（模块内别名，避免直接引用下划线函数）。

    Args:
        caller: 能力调用 async 函数 (method, params) -> Any

    Returns:
        async (prompt) -> response_text
    """
    return _build_compress_llm_call_fn(caller)


# asyncio 仅在 _build_compress_llm_call_fn 内引用以表明导入意图；保留以兼容静态检查
_ = asyncio if False else None  # type: ignore[func-returns-value]

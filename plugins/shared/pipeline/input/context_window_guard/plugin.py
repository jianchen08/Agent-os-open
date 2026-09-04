"""上下文窗口守卫 Input 插件。

在每次 LLM 调用前检查上下文大小，超阈值时执行预算驱动的分层压缩。

职责与接线（现状契约）：
- 压缩算法（CompressionConfig + ContextCompressor）内联于本文件；
  压缩 LLM 经 ``_capability_caller`` 调 memory.compress 工具，测试环境
  直接注入 llm_call_fn。
- 存储后端为模块级注入的 ``_memory_backend: IMemoryBackend``
  （server.py on_load 注入）；L1/L2/STATE_SNAPSHOT 块以 memory_type="chunk"
  写入，memory_items 以 memory_type="semantic" 写入。

压缩优化机制：
- 任务 1 语义标记：消息内部字段 _context_form 声明语义形态
  （instructions/notice/recall/relay/snapshot，见 CONTEXT_FORM_* 词汇表），
  压缩时渲染为 [form] 标签前缀供压缩 LLM 差异化摘要；llm_core 发送前清理。
- 任务 2 fork 压缩（对标 DSH summarizer）：压缩输入从 COMPRESS_PROMPT 拼字符串
  改为 fork 执行时消息队列（[system] + compression_messages + 待压缩消息原样）
  + 末尾追加 COMPACTION_INSTRUCTION（user 角色）。同模型时复用前缀 cache，
  任何模型时压缩 LLM 读到完整连贯消息流。产物结构不变（五段 JSON）。

压缩块消息化（ADR 2026-08-28-compression-block-pointer-indirection）：
- 压缩 = 对 message 序列的一次原地编辑：被压批次头部原位变为压缩块消息
  （过程块 + 快照块，普通 system 消息形态），中段槽位删除留 gap，后段不动；
- 每条块消息自带 metadata.compression_ref 引用元数据（指向记忆库落库锚点，
  供回溯/展开/审计；LLM 输入只消费块消息自身的摘要内容）；块消息随消息
  序列经引擎 messages ops 与普通消息同机制持久化（message_slots/blobs 账本），
  读路径零额外操作；
- 存储放置默认记忆库（tags/pipeline:{id} 落库契约保留）；写库失败 → 块内容
  降级为仅内联摘要、引用留空并 warning，流程不阻塞（fail-open）。

State 命名空间:
    - messages : 压缩后替换的消息列表（含原位插入的压缩块消息）
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)

# LLM 调用函数类型：接收 fork 消息列表（或兼容旧路径的 prompt 字符串），返回响应字符串
LLMCallFn = Callable[[str | list[dict[str, Any]]], Awaitable[str]]
# 能力调用函数类型：(method, params, timeout) -> Any（用于调 memory.compress 等工具）。
# 压缩 LLM 调用耗时可达 llm.yaml call_timeout，第三参 timeout 必须显式传大值——
# SDK 默认 CAPABILITY_CALL_TIMEOUT_S=30s 面向短调用，不传会先于压缩完成被掐断。
CapabilityCaller = Callable[[str, dict[str, Any], float | None], Awaitable[Any]]


def _now_iso() -> str:
    """当前时刻 ISO8601（UTC，带时区）——压缩块引用元数据的落块时间。"""
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.now(UTC).isoformat()

# 压缩摘要注入提示
_COMPRESSION_NOTICE = (
    "[系统提示] 由于对话历史过长，较早的上下文已被记忆系统分层压缩。"
    "压缩摘要包含在上方消息中，请基于压缩摘要和当前剩余上下文继续完成任务。"
)

# ═══════════════════════════════════════════════════════════
# 语义标记词汇表（对齐 DSH ContextForm，docs/tasks/task_compression_optimization.md 任务 1）
# ═══════════════════════════════════════════════════════════
# 消息的内部字段 _context_form 声明"这条内容是什么"（语义形态），与 role（谁说的）
# 正交。产出方（prompt_build 压缩块 / memory_read 检索条目等）打标，消费者：
# 1. 压缩 fork——与执行路径（llm_core._build_messages）同集剥离，内容原样，
#    不渲染标签（2026-09-03 用户裁定：渲染会在首个标记消息处分叉 token
#    前缀，击穿与执行请求的 prefix cache 对齐）；
# 2. capability 回退序列化（_format_messages）——渲染为 [form] 可见前缀；
# 3. 最终 LLM——llm_core._build_messages 发送前清理该字段（同 seq/tool_result），
#    不进 API 载荷，不影响 prompt cache。
CONTEXT_FORM_INSTRUCTIONS = "instructions"  # 文件规则/项目约定（须遵守的指令性内容）
CONTEXT_FORM_NOTICE = "notice"  # 一次性通知（发生了什么，无需展开）
CONTEXT_FORM_RECALL = "recall"  # 从记忆库检索的内容
CONTEXT_FORM_RELAY = "relay"  # 其他 agent 传来的消息
CONTEXT_FORM_SNAPSHOT = "snapshot"  # 状态快照（后一份覆盖前一份）

CONTEXT_FORM_LABELS = {
    CONTEXT_FORM_INSTRUCTIONS: "[instructions]",
    CONTEXT_FORM_NOTICE: "[notice]",
    CONTEXT_FORM_RECALL: "[recall]",
    CONTEXT_FORM_RELAY: "[relay]",
    CONTEXT_FORM_SNAPSHOT: "[snapshot]",
}

# ═══════════════════════════════════════════════════════════
# 模块级依赖注入（由 server.py 的 on_load 注入，测试直接赋值）
# ═══════════════════════════════════════════════════════════

# 长期记忆后端（Hindsight/capability）；None 时压缩块无法落库，相关流程早退
_memory_backend: Any | None = None
# 能力调用句柄；server.py 注入后用于构建压缩 LLM 调用函数（memory.compress 回退路径）
_capability_caller: CapabilityCaller | None = None


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
    """注入能力调用句柄（async fn `(method, params, timeout) -> Any`）。

    server.py 在 on_load 时调用（wiring.make_capability_caller 构造，timeout
    透传 SDK CapabilityHandle.call）：
    ``set_capability_caller(lambda m, p, t=None: plugin.get_capability("tool-executor").call(m, p, t))``
    压缩执行时据此构建 memory.compress 的 LLM 调用函数。

    Args:
        caller: 能力调用 async 函数；传 None 清空
    """
    global _capability_caller
    _capability_caller = caller


# 前端一次性事件通道（frontend.emit，内核内置能力）——压缩彻底失败时向
# 前端推送 compression_failed 通知。独立于 _capability_caller（那是
# tool-executor 形态，调用协议不同）。
FrontendEmitFn = Callable[[str, dict[str, Any], str], Awaitable[None]]
_frontend_emit: FrontendEmitFn | None = None


def set_frontend_emit(fn: FrontendEmitFn | None) -> None:
    """注入前端事件发射函数（async fn `(event, payload, thread_id)`）。

    server.py 在 on_load 时从 ``plugin.get_capability("frontend")`` 构造注入；
    传 None 清空（未注入时压缩失败只留日志，管线行为不变）。
    """
    global _frontend_emit
    _frontend_emit = fn


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
# 压缩预算配置（注入命名空间 context_window 的单一实现；内联形态真值即
# manifest fields.default，引用/内联两形态统一经 build_injected_config 送达）
# ═══════════════════════════════════════════════════════════

@dataclass
class CompressionConfig:
    """压缩配置。

    所有比例基于 context_window 计算实际 token 数。

    Attributes:
        context_window: 模型上下文窗口大小
        compress_trigger_ratio: 压缩触发比例（代码默认 0.55；真值经注入命名空间 compress_trigger_ratio 送达）
        l1_ratio: L1 过程块预算比例
        l2_ratio: L2 过程块预算比例
        recent_ratio: 最近原文预算比例

    预算只保留三类（2026-09-02 裁定）：最近原文 recent / 过程块 L1 / L2；
    其余维度（retrieval/max_turn 等）不管控。
    """

    context_window: int = 128000
    compress_trigger_ratio: float = 0.55  # 代码默认兜底；真值经注入命名空间送达
    l1_ratio: float = 0.1
    l2_ratio: float = 0.05
    recent_ratio: float = 0.18

    @classmethod
    def from_yaml_config(
        cls,
        context_window: int,
        injected: dict[str, Any] | None = None,
    ) -> CompressionConfig:
        """加载压缩预算配置。

        取值优先级：
        ① 注入命名空间 dict（config["context_window"]，manifest
           config_files.fields.default 内联展开，ADR 2026-09-02
           context-window-config-inline-manifest——值与 manifest 同构：
           compress_trigger_ratio / budgets / compression.*）
        ② ConfigCenter 兼容路径（0.1 历史装配缝注入场景）
        ③ 代码默认

        Args:
            context_window: 当前模型上下文窗口大小
            injected: 注入的窗口配置 dict；空/None 时走 ② 兼容路径

        Returns:
            填充好预算比例的 CompressionConfig
        """
        yaml_data = injected if isinstance(injected, dict) and injected else None
        if yaml_data is None:
            try:
                from config.config_center import get_config_center  # noqa: PLC0415

                yaml_data = get_config_center().get("system/context_window_config.yaml") or {}
            except Exception as e:
                logger.warning(
                    "[context_window_guard] 压缩预算配置读取失败，回退代码默认 | path=system/context_window_config.yaml | error=%s",
                    e,
                )
                return cls(context_window=context_window)
        budgets = yaml_data.get("budgets", {})
        return cls(
            context_window=context_window,
            compress_trigger_ratio=yaml_data.get(
                "compress_trigger_ratio", 0.55
            ),
            l1_ratio=budgets.get("l1", 0.1),
            l2_ratio=budgets.get("l2", 0.05),
            recent_ratio=budgets.get("recent", 0.18),
        )

    def get_budgets(self) -> dict[str, int]:
        """计算各部分实际 token 预算。

        Returns:
            各层 token 预算字典 {recent, L1, L2}（三类，2026-09-02 裁定）
        """
        return {
            "recent": int(self.context_window * self.recent_ratio),
            "L1": int(self.context_window * self.l1_ratio),
            "L2": int(self.context_window * self.l2_ratio),
        }

    def get_trigger_threshold(self) -> int:
        """获取触发压缩的 token 阈值。

        Returns:
            触发阈值 = context_window * compress_trigger_ratio
        """
        return int(self.context_window * self.compress_trigger_ratio)


# ═══════════════════════════════════════════════════════════
# 分层压缩器
# ═══════════════════════════════════════════════════════════


class ContextCompressor:
    """上下文压缩器。

    负责把长对话历史压成结构化摘要。支持分层递进：
    L0(原文) → L1(过程摘要) → L2(三元组)。

    fork 消息队列压缩（0.2 压缩优化任务 2，对标 DSH summarizer）：
    压缩输入不再拼字符串模板，而是 fork 执行时消息队列（system +
    compression_messages + 待压缩消息原样）+ 末尾追加 COMPACTION_INSTRUCTION。
    同模型时复用 provider 前缀 cache；任何模型时压缩 LLM 读到完整连贯消息流。
    带 _context_form 的消息渲染 [form] 语义标签（任务 1 叠加生效）。

    设计原则：
    - 纯函数设计，无状态管理
    - 输入输出都是字符串/字典
    - 不直接操作数据库（落库由 CompressionService 负责）
    - LLM 调用通过注入的 llm_call_fn 实现而非硬依赖

    Attributes:
        config: 压缩配置
        budgets: 各层 token 预算
        _llm_call_fn: LLM 调用函数（async (messages) -> response_text）
    """

    # fork 压缩指令（对标 DSH summarizer 的 COMPACTION_INSTRUCTION）：
    # 作为 fork 消息队列的"最后一条 user 消息"追加，不是独立的 summarizer
    # system prompt。复现执行时的 system + 压缩块 + 消息前缀，让压缩调用成为
    # 上一次执行请求的真前缀（同模型时可复用 provider 的 warm prefix cache），
    # 末尾指令是唯一新增输入。
    # 产出结构不变：仍要求 L1/L2/keywords/state_snapshot/memory_items 五段 JSON
    # （五段字段名是解析器按段提取的解析契约，精心调校，勿改）。
    # 旧 {messages}/{state_snapshot}/{recent_process_blocks} 占位符已删——
    # 这些内容已在 fork 消息流里（压缩块/快照在 compression_messages 段，
    # 过程在 messages 段），模板不再重复拼。
    COMPACTION_INSTRUCTION = """## 任务
你现在作为压缩引擎工作：把上方（本条指令之前）的消息流压缩为五部分：l1 / l2 / keywords / state_snapshot / memory_items。

上方的消息流即压缩输入，包括：系统提示、<compressed> 压缩摘要块（此前压缩的产物，只描述已覆盖的旧消息）、<current_state> 状态快照、以及其后的对话消息。不要复述压缩摘要块已覆盖的内容，只在 state_snapshot 中合并更新。

l1/l2/keywords 描述同一批对话，详略不同。
**L1 必须比 L2 详细得多**：L1 含具体步骤、决策、产出，
L2 是 L1 的紧凑概括（每个字段一两句话）。降级才有意义。

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

{
  "l1": {
    "session_title": "...",
    "workflow": "...",
    "errors_and_corrections": "无则null",
    "decisions": "无则null",
    "key_results": "无则null"
  },
  "l2": {
    "intent": "...",
    "process": "...",
    "results": "..."
  },
  "keywords": ["词1", "词2"],
  "state_snapshot": {
    "current_state": "...",
    "task_specification": "...",
    "pending": "...",
    "key_entities": "...",
    "domain_knowledge": "...",
    "user_feedback": "...",
    "attention_hints": "..."
  },
  "memory_items": {
    "user_profile_updates": "无则null",
    "project_knowledge_updates": "无则null",
    "experience_updates": "无则null"
  }
}
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
        system_message: dict[str, Any] | str | None = None,
        prior_blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """fork 消息队列压缩：L1 + L2 + keywords + state_snapshot + memory_items。

        对标 DSH summarizer：不拼 COMPRESS_PROMPT 字符串，改为 fork 执行时的
        消息队列并在末尾追加压缩指令——

        fork = [执行时 system] + prior_blocks（既有压缩块消息，原样）
             + 待压缩消息（原样 role/content，不压成 【用户 N】）
             + [user: COMPACTION_INSTRUCTION]

        压缩块消息排在 message 序列里（ADR 2026-08-28 压缩块消息化），由调用方
        从序列中拣出传入——压缩 LLM 据此"只合并更新，不复述已覆盖内容"。
        同模型时前缀与执行请求逐字节一致（cache 复用）；任何模型时压缩 LLM
        读到完整连贯消息流（理解质量）。内部字段（seq/tool_result/
        _context_form）剥离后进载荷，内容原样不渲染标签（与执行路径
        llm_core._build_messages 同一清理集，前缀 cache 对齐）。

        Args:
            messages: 待压缩消息列表
            system_message: 执行时系统消息（dict 含 role/content，或纯字符串）；
                None 时 fork 不带 system 前缀
            prior_blocks: 序列中既有的压缩块消息（metadata 含 compression_ref）；
                None/空时跳过

        Returns:
            压缩结果字典；空消息返回空结果字典（含空键）；
            LLM 空响应或 JSON 解析失败返回 None（fail-closed）；
            LLM 调用异常抛 RuntimeError。

        Raises:
            RuntimeError: LLM 调用过程异常时
        """
        # 空消息：返回空结果字典（不调 LLM）
        if not messages:
            return {
                "l1": "",
                "l2": "",
                "keywords": [],
                "state_snapshot": {},
                "memory_items": {},
            }

        fork_messages = self._build_fork_messages(messages, system_message, prior_blocks)

        try:
            response = await self._call_llm(fork_messages)
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

    @classmethod
    def _format_messages(cls, messages: list[dict[str, Any]]) -> str:
        """格式化消息为带角色头的文本（capability 回退路径 / 兜底序列化用）。

        带 _context_form 内部字段的消息在角色头后渲染语义标签前缀
        （如 ``[instructions]``），让压缩 LLM 区分消息重要性；
        无标记消息行为与旧版一致。

        Args:
            messages: 消息列表

        Returns:
            格式化后的文本（每条以 【角色 N】\\n [form] 内容 形式）
        """
        lines: list[str] = []

        for i, msg in enumerate(messages, 1):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if not content:
                continue

            form = msg.get("_context_form")
            label = CONTEXT_FORM_LABELS.get(form, "") if isinstance(form, str) else ""
            if label:
                content = f"{label} {content}"

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

    # 发送给 LLM 前必须剥离的内部字段（fork 复制消息时清理，不改动原消息；
    # metadata=内核 client_message_id 等持久化对账数据，与 llm_core 出站
    # 黑名单 _OUTBOUND_INTERNAL_FIELDS 对齐——严格 provider 拒收未知字段）
    _INTERNAL_MSG_FIELDS = ("seq", "tool_result", "_context_form", "metadata")

    @classmethod
    def _render_fork_message(cls, msg: dict[str, Any]) -> dict[str, Any]:
        """把消息转为可发压缩 LLM 的副本：剥离内部字段，内容原样。

        - 内部字段（seq/tool_result/_context_form）不进 LLM 载荷——清理集与
          执行路径 llm_core._build_messages 完全一致，fork 内容与执行请求
          逐字节相同。2026-09-03 用户裁定：不再渲染 [form] 标签前缀——渲染
          会在首个标记消息处改变 token 前缀，击穿「fork=执行请求真前缀」的
          prefix cache 对齐；块消息靠内容自带的 <compressed>/
          <current_state> 结构标记区分。
        - 其余字段（role/content/name/tool_calls 等）原样保留——fork 前缀
          与执行请求保持一致是 cache 复用的前提。

        Args:
            msg: 原始消息（不会被改动）

        Returns:
            可发送的消息副本
        """
        return {k: v for k, v in msg.items() if k not in cls._INTERNAL_MSG_FIELDS}

    def _build_fork_messages(
        self,
        messages: list[dict[str, Any]],
        system_message: dict[str, Any] | str | None = None,
        prior_blocks: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """构造 fork 消息队列（对齐执行时装配顺序 + 末尾压缩指令）。

        顺序：[执行时 system] + prior_blocks（既有压缩块消息，原样）+
        待压缩消息 + [user 指令]。
        system 与块前缀复现执行请求前缀（同模型 cache 复用）；末尾指令是
        唯一新增输入（DSH summarizer 方案）。

        Args:
            messages: 待压缩消息（孤儿 tool result 摘除后进 fork，不
                _format_messages 压扁；state 原列表不被修改）
            system_message: 执行时系统消息（dict 或 str；None 跳过）
            prior_blocks: 序列中既有的压缩块消息（None/空跳过）

        Returns:
            fork 消息列表
        """
        fork: list[dict[str, Any]] = []

        if isinstance(system_message, dict):
            if system_message.get("content"):
                fork.append(self._render_fork_message(system_message))
        elif isinstance(system_message, str) and system_message.strip():
            fork.append({"role": "system", "content": system_message})

        for pb in prior_blocks or []:
            if isinstance(pb, dict) and pb.get("content"):
                fork.append(self._render_fork_message(pb))

        # 孤儿 tool result 摘除：state 历史遗留的孤儿（cancelled/failed run
        # 留下，或更早压缩块吞掉 assistant 后残留）进 fork 载荷会被 MiniMax
        # 严格校验拒 400（tool result's tool id not found 2013）。判定语义与
        # 执行路径 llm_core normalizer Phase A 一致。state 原消息不动——
        # 执行请求的清理由 llm_core normalizer 负责，此处只管压缩请求。
        expecting_ids: set[str] = set()
        dropped_orphans = 0
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "assistant":
                if m.get("tool_calls"):
                    expecting_ids = {
                        tc.get("id") for tc in m["tool_calls"] if tc.get("id")
                    }
                else:
                    expecting_ids = set()
                fork.append(self._render_fork_message(m))
            elif role == "tool":
                tc_id = m.get("tool_call_id")
                if tc_id and tc_id in expecting_ids:
                    expecting_ids.discard(tc_id)
                    fork.append(self._render_fork_message(m))
                else:
                    dropped_orphans += 1
            else:
                if role in ("user", "system"):
                    expecting_ids = set()
                fork.append(self._render_fork_message(m))
        if dropped_orphans:
            logger.warning(
                "[ContextCompressor] fork 载荷摘除 %d 条孤儿 tool result（无配对 assistant tool_calls）",
                dropped_orphans,
            )

        fork.append({"role": "user", "content": self.COMPACTION_INSTRUCTION})
        return fork

    async def _call_llm(self, messages: list[dict[str, Any]]) -> str:
        """调用 LLM 生成摘要（fork 消息队列）。

        Args:
            messages: fork 消息列表（system + messages + 末尾指令）

        Returns:
            LLM 响应文本

        Raises:
            RuntimeError: 无 LLM 调用函数或调用失败时
        """
        if not self._llm_call_fn:
            raise RuntimeError("未提供 LLM 调用函数，无法执行压缩")

        return await self._llm_call_fn(messages)

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

    面向管道的最小记忆服务：只做本插件的块写入/查询。
    LLM 调用统一走注入的 llm_call_fn（由模块级 _capability_caller 构建）；
    存储统一走注入的 IMemoryBackend（模块级 _memory_backend）。

    落库映射：
    - L1 / L2 / STATE_SNAPSHOT → backend.add(memory_type="chunk", ...)
    - memory_items → backend.add(memory_type="semantic", ...)
    """

    _MAX_COMPRESS_ROUNDS = 2

    def __init__(
        self,
        backend: Any | None = None,
        llm_call_fn: LLMCallFn | None = None,
        config: CompressionConfig | None = None,
        injected: dict[str, Any] | None = None,
    ) -> None:
        """初始化压缩服务。

        Args:
            backend: IMemoryBackend 实例（或 duck-type）；None 时压缩块不入库
                （块消息仍以内联摘要进入序列，引用留空）
            llm_call_fn: LLM 调用函数；None 时压缩无法执行（compress_messages 早退）
            config: 压缩配置；None 用默认 CompressionConfig
            injected: 注入的窗口配置 dict（manifest fields.default 内联）；
                _compress_messages_impl 的预算读取用它（单一值源）
        """
        self._backend = backend
        self._llm_call_fn: LLMCallFn | None = llm_call_fn
        self._injected: dict[str, Any] | None = injected
        self._compressor = ContextCompressor(config=config or CompressionConfig())
        if llm_call_fn:
            self._compressor.set_llm_call_fn(llm_call_fn)
        # 运行时上下文（setup 时填充）
        self._pipeline_id = ""
        self._session_id = ""
        self._user_id = ""
        self._config: dict[str, Any] = {"context_window": 128000}
        # 最近一次 compress_messages 累计的被删消息 seq 列表（供插件 emit set(seq,null) ops）
        self._last_deleted_seqs: list[int] = []
        # 最近一次 compress_messages 产出的压缩块消息（自带 seq；供插件 emit
        # set(seq, 块消息) ops——块消息原位占用被压批次的头部槽位）
        self._last_block_msgs: list[dict[str, Any]] = []

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
        llm_call_fn: LLMCallFn | None = None,
        system_message: dict[str, Any] | str | None = None,
    ) -> list[dict[str, Any]] | None:
        """预算驱动的完整压缩流程。

        多轮压缩（最多 _MAX_COMPRESS_ROUNDS 轮）：每轮切 recent 预算 →
        压旧消息 → 落库 → 检查总 tokens；仍超阈值则再压一轮。

        Args:
            messages: 完整消息列表（含既有压缩块消息——fork 时作为消息流
                一部分原样呈现给压缩 LLM）
            context_window: 主模型上下文窗口（预算切分用）
            trigger_ratio: 触发压缩比例
            llm_call_fn: 本次调用覆盖的 LLM 调用函数（async (messages) -> str），
                可选；注入后即作为本服务的 LLM 通道（并同步到内部 compressor）。
                不传则沿用构造/上次注入的函数
            system_message: 执行时系统消息（fork 前缀用，cache 复现）；
                由插件从 ctx.state["system_message"] 传入

        Returns:
            压缩后的消息列表（含原位插入的压缩块消息）；
            无需压缩/失败/无 LLM 函数返回 None
        """
        if llm_call_fn is not None:
            self.set_llm_call_fn(llm_call_fn)
        try:
            return await self._compress_messages_impl(
                messages,
                context_window,
                trigger_ratio,
                system_message,
            )
        except Exception as exc:
            logger.error(
                "[CompressionService] compress_messages 顶层异常: %s",
                exc,
                exc_info=True,
            )
            return None

    async def _compress_messages_impl(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        trigger_ratio: float,
        system_message: dict[str, Any] | str | None = None,
    ) -> list[dict[str, Any]] | None:
        """compress_messages 的实际实现。

        多轮压缩中累计的"被删消息 seq 列表"与"压缩块消息"写入
        self._last_deleted_seqs / self._last_block_msgs，供外层（插件 execute）
        据此生成 set(seq, null) / set(seq, 块) ops。返回值仍为压缩后消息列表
        （或 None），保持 compress_messages 向后兼容。
        """
        # 重置上一轮累计（每轮调用独立；注解见 __init__）
        self._last_deleted_seqs = []
        self._last_block_msgs = []

        if not self._llm_call_fn:
            logger.warning("[CompressionService] 跳过压缩：未提供 LLM 调用函数")
            return None

        config = CompressionConfig.from_yaml_config(context_window, injected=self._injected)
        budgets = config.get_budgets()
        trigger_tokens = int(context_window * trigger_ratio)

        current_messages = messages
        compressed: list[dict[str, Any]] | None = None

        for round_idx in range(self._MAX_COMPRESS_ROUNDS):
            round_result = await self._do_compress_round(
                current_messages,
                context_window,
                budgets,
                system_message,
            )
            if round_result is None:
                break
            compressed, round_deleted = round_result
            self._last_deleted_seqs.extend(round_deleted)

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
        system_message: dict[str, Any] | str | None = None,
    ) -> tuple[list[dict[str, Any]], list[int]] | None:
        """执行一轮预算驱动的压缩。

        三路分离：pure_system（含既有压缩块消息，原样保留）/ 旧压缩块
        （## 历史对话压缩摘要 / _COMPRESSION_NOTICE 遗留格式）/ 其他消息；
        只压最后一个压缩块之后的新消息。

        被压批次原位编辑（ADR 2026-08-28 压缩块消息化）：批次头部槽位变为
        过程块消息，次槽变为快照块消息（批次仅 1 条时快照引用并入过程块），
        其余被压槽位删除留 gap，recent 段不动。

        Args:
            messages: 当前消息列表
            context_window: 主模型窗口（recent 预算切分）
            budgets: 各层 token 预算
            system_message: 执行时系统消息（fork 前缀）

        Returns:
            (压缩后消息列表含块消息, 留 gap 的被删 seq 列表)；无需压缩或
            全部批次压缩失败返回 None。
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

        # 序列中既有的压缩块消息（pure_system 一部分）：进 fork 前缀，
        # 压缩 LLM 据此"只合并更新，不复述已覆盖内容"（COMPACTION_INSTRUCTION 契约）
        prior_blocks = [
            m
            for m in pure_system_msgs
            if isinstance(m.get("metadata"), dict) and "compression_ref" in m["metadata"]
        ]

        # 分批压缩（按压缩模型窗口的 0.5 切片，防止单批超压缩模型上下文）
        old_tokens = sum(self._estimate_msg_tokens(m) for m in old_msgs)
        batch_ratio = 0.5
        batch_budget = int(context_window * batch_ratio)
        num_batches = max(1, -(-old_tokens // batch_budget))  # 向上取整

        any_compressed = False
        block_msgs: list[dict[str, Any]] = []
        compressed_slot_seqs: list[int] = []

        for batch_idx in range(num_batches):
            start = batch_idx * len(old_msgs) // num_batches
            end = (batch_idx + 1) * len(old_msgs) // num_batches
            batch = old_msgs[start:end]
            if not batch:
                continue

            comp_result = await self._build_compression_content(
                batch,
                system_message,
                prior_blocks,
            )
            if not comp_result:
                logger.warning("[CompressionService] 第 %d 批压缩失败", batch_idx + 1)
                continue

            # 落记忆库（默认存储放置）；写失败由 save 层逐工件降级
            # （引用留空 + warning），块消息以内联摘要照常产出（fail-open）
            refs = await self.save_compression_result(
                old_msgs=batch,
                comp_result=comp_result,
                pipeline_id=self._pipeline_id,
                session_id=self._session_id,
                context_window=context_window,
            )

            batch_blocks = self._build_batch_blocks(batch, comp_result, refs)
            block_msgs.extend(batch_blocks)
            compressed_slot_seqs.extend(
                m["seq"] for m in batch if isinstance(m.get("seq"), int)
            )
            any_compressed = True

        if not any_compressed:
            return None

        # 留 gap 的被删 seq = 被压批次槽位 - 块消息占用的槽位；遗留 old_blocks
        # （旧格式摘要 system 消息）一并删除。
        occupied = {
            m["seq"] for m in block_msgs if isinstance(m.get("seq"), int)
        }
        deleted_seqs: list[int] = [
            seq for seq in compressed_slot_seqs if seq not in occupied
        ]
        deleted_seqs.extend(
            m["seq"] for m in old_blocks if isinstance(m.get("seq"), int)
        )
        self._last_block_msgs.extend(block_msgs)

        assembled = sorted(
            pure_system_msgs + recent_msgs + block_msgs,
            key=lambda m: m["seq"] if isinstance(m.get("seq"), int) else float("inf"),
        )
        return assembled, deleted_seqs

    def _build_batch_blocks(
        self,
        batch: list[dict[str, Any]],
        comp_result: dict[str, Any],
        refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把一批压缩产物构建为原位插入的块消息（过程块 + 快照块）。

        块消息是普通 system 消息：LLM 输入只消费其内联摘要内容；引用元数据
        （metadata.compression_ref）指向记忆库落库锚点，供回溯/展开/审计。

        Args:
            batch: 本批被压缩的原消息（自带 seq，升序）
            comp_result: compress_all 产出的 5 部分字典
            refs: save_compression_result 返回的落库引用清单

        Returns:
            块消息列表（seq 已按原位槽位赋值）；空批次返回 []
        """
        seqs = [m["seq"] for m in batch if isinstance(m.get("seq"), int)]
        if not seqs:
            return []
        seq_start = min(seqs)
        seq_end = max(seqs)

        import json  # noqa: PLC0415

        process_refs = [r for r in refs if r["level"] in ("L1", "L2")]
        snapshot_refs = [r for r in refs if r["level"] == "state_snapshot"]

        blocks: list[dict[str, Any]] = [
            {
                "role": "system",
                "name": "compressed",
                "seq": seq_start,
                "content": (
                    f'<compressed seq="{seq_start}-{seq_end}" level="L1">\n'
                    f"## 过程摘要\n{comp_result.get('l1', '')}\n</compressed>"
                ),
                # 语义标记（内部字段）：记忆库摘要内容；llm_core 发送前清理
                "_context_form": CONTEXT_FORM_RECALL,
                "metadata": {
                    "compression_ref": {
                        "kind": "process",
                        "seq_range": [seq_start, seq_end],
                        "stored_at": _now_iso(),
                        "memory_ids": process_refs,
                    }
                },
            }
        ]

        state_snapshot = comp_result.get("state_snapshot")
        if state_snapshot:
            snapshot_block = {
                "role": "system",
                "name": "state_snapshot",
                "seq": seq_start,
                "content": (
                    "<current_state>\n"
                    + json.dumps(state_snapshot, ensure_ascii=False, indent=2)
                    + "\n</current_state>"
                ),
                "_context_form": CONTEXT_FORM_SNAPSHOT,
                "metadata": {
                    "compression_ref": {
                        "kind": "state_snapshot",
                        "seq_range": [seq_start, seq_end],
                        "stored_at": _now_iso(),
                        "memory_ids": snapshot_refs,
                    }
                },
            }
            if len(seqs) >= 2:
                # 批次次槽留给快照块（原位，不占新槽）；单条批次无空闲槽，
                # 快照引用并入过程块（落库锚点不丢）
                snapshot_block["seq"] = seq_start + 1
                blocks.append(snapshot_block)
            else:
                blocks[0]["metadata"]["compression_ref"]["memory_ids"] = (
                    process_refs + snapshot_refs
                )
        return blocks

    async def _build_compression_content(
        self,
        old_msgs: list[dict[str, Any]],
        system_message: dict[str, Any] | str | None = None,
        prior_blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """调 compress_all 压一批旧消息，返回 5 部分结果字典。

        Args:
            old_msgs: 待压缩的旧消息批次
            system_message: 执行时系统消息（fork 前缀）
            prior_blocks: 序列中既有的压缩块消息（fork 前缀，压缩 LLM 据此
                只合并更新不复述）

        Returns:
            {l1, l2, keywords, state_snapshot, memory_items} 或 None
        """
        if not self._llm_call_fn:
            return None

        self._compressor.set_llm_call_fn(self._llm_call_fn)

        try:
            result = await self._compressor.compress_all(
                old_msgs,
                system_message=system_message,
                prior_blocks=prior_blocks,
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

    async def save_compression_result(
        self,
        old_msgs: list[dict[str, Any]],
        comp_result: dict[str, Any],
        pipeline_id: str,
        session_id: str,
        context_window: int,
    ) -> list[dict[str, str]]:
        """把压缩结果落到 IMemoryBackend，返回成功工件的引用清单。

        映射（tags/pipeline:{id} 落库契约保留不动）：
        - L1 块：backend.add(memory_type="chunk", content=l1, tags=["L1", ...])
        - L2 块：backend.add(memory_type="chunk", content=l2, tags=["L2", ...])
        - STATE_SNAPSHOT：backend.add(memory_type="chunk", content=ss_json,
          tags=["STATE_SNAPSHOT"])
        - memory_items：backend.add(memory_type="semantic", content=value,
          tags=[规范化字段名])

        存储放置默认记忆库（可检索）；写库失败逐工件降级——该工件引用留空
        并 warning，块消息以内联摘要照常产出，流程不阻塞（fail-open，
        ADR 2026-08-28-compression-block-pointer-indirection）。

        Args:
            old_msgs: 本批被压缩的旧消息（用于算 sequence 范围）
            comp_result: compress_all 产出的 5 部分字典
            pipeline_id: 管道运行 ID
            session_id: 会话 ID
            context_window: 当前模型上下文窗口

        Returns:
            成功落库工件的引用清单 [{"level": "L1"|"L2"|"state_snapshot",
            "id": <memory id>}, ...]（按落库顺序）；写失败的工件不出现在
            清单中。
        """
        refs: list[dict[str, str]] = []
        backend = self._backend
        if backend is None:
            return refs

        import json  # noqa: PLC0415

        l1_content = comp_result.get("l1", "")
        l2_content = comp_result.get("l2", "")
        state_snapshot = comp_result.get("state_snapshot", {})
        memory_items = comp_result.get("memory_items", {})

        # sequence 范围（用于 _trim_covered_messages / _estimate_assembled_tokens）。
        # 零兼容：统一用引擎分配的消息自带 seq 字段（_record_sequence 已退役）。
        sequences = [
            m["seq"] for m in old_msgs if isinstance(m.get("seq"), int)
        ]
        sequence_start = min(sequences) if sequences else 1
        sequence_end = max(sequences) if sequences else (sequence_start + len(old_msgs) - 1)

        user_id = self._user_id or session_id or pipeline_id or "default"

        async def _add_ref(level: str, content: str, memory_type: str, tags: list[str]) -> None:
            try:
                memory_id = await backend.add(
                    user_id=user_id,
                    content=content,
                    memory_type=memory_type,
                    tags=tags,
                    source="compression",
                )
            except Exception as exc:
                logger.warning(
                    "[CompressionService] %s 落库失败（引用留空，块内联摘要不受影响）: %s",
                    level,
                    exc,
                )
                return
            refs.append({"level": level, "id": memory_id})

        # L1 块
        if l1_content:
            await _add_ref(
                "L1",
                l1_content,
                "chunk",
                [
                    "L1",
                    f"pipeline:{pipeline_id}",
                    f"seq:{sequence_start}-{sequence_end}",
                    f"ctx:{context_window}",
                ],
            )

        # L2 块
        if l2_content:
            await _add_ref(
                "L2",
                l2_content,
                "chunk",
                [
                    "L2",
                    f"pipeline:{pipeline_id}",
                    f"seq:{sequence_start}-{sequence_end}",
                ],
            )

        # STATE_SNAPSHOT（最新快照追加落库，引用随快照块进入序列）
        if state_snapshot:
            ss_content = json.dumps(state_snapshot, ensure_ascii=False, indent=2)
            await _add_ref(
                "state_snapshot",
                ss_content,
                "chunk",
                [
                    "STATE_SNAPSHOT",
                    f"pipeline:{pipeline_id}",
                    f"seq_end:{sequence_end}",
                ],
            )

        # memory_items → semantic（长期记忆，不属于块引用面）
        if memory_items and isinstance(memory_items, dict):
            tag_map = {
                "user_profile_updates": "user_profile",
                "project_knowledge_updates": "project_knowledge",
                "experience_updates": "experience",
            }
            for key, value in memory_items.items():
                if value and value != "null":
                    try:
                        await backend.add(
                            user_id=user_id,
                            content=str(value),
                            memory_type="semantic",
                            tags=[tag_map.get(key, key)],
                            source="compression",
                        )
                    except Exception as exc:
                        logger.warning(
                            "[CompressionService] memory_items.%s 落库失败（跳过）: %s",
                            key,
                            exc,
                        )

        return refs

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
    压缩失败不阻塞管线。

    Attributes:
        _config: 插件配置字典
        _trigger_ratio: 触发压缩的阈值比例（默认 0.55）
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化上下文窗口守卫插件。

        配置优先级（高→低）：
          ① Agent YAML plugins.enabled.context_window_guard.trigger_ratio
             （由 plugin_resolver 合并进 config，或由 _apply_runtime_config 从 ctx.state 读）
          ② Pipeline YAML plugins.context_window_guard.config.trigger_ratio
             （即本 __init__ 收到的 config 参数）
          ③ manifest 内联注入 context_window.compress_trigger_ratio
             （config_files.fields.default 经 build_injected_config 注入，
             ADR 2026-09-02-context-window-config-inline-manifest）
          ④ 代码硬编码默认 0.55

        Args:
            config: 插件配置字典（来自 pipeline yaml / 注入命名空间合并），支持：
                - enabled: 是否启用（默认 True）
                - trigger_ratio: 触发压缩的阈值比例（不配则继承注入值）
                - context_window: 注入命名空间 dict（compress_trigger_ratio/
                  budgets/compression.*，manifest fields.default 内联展开）
        """
        self._config = config or {}
        window_cfg = self._config.get("context_window")
        self._window_cfg = (
            window_cfg if isinstance(window_cfg, dict) else {}
        )
        self._trigger_ratio = self._resolve_trigger_ratio(
            self._config.get("trigger_ratio"), self._window_cfg
        )
        # 压缩失败已透传前端的去重标记：连续失败只推一次 compression_failed
        # 事件（同一故障周期不刷屏），压缩成功后复位。
        self._compress_fail_notified = False
        # 实例级追踪：插件可能被重复实例化，state 不一定跨迭代持久化
        # 用实例变量做主存储，ctx.state 做辅助（重启恢复场景）
        self._tracked_msg_count: int = 0

    @staticmethod
    def _resolve_trigger_ratio(
        explicit: float | None, window_cfg: dict[str, Any] | None = None
    ) -> float:
        """解析 trigger_ratio：pipeline 显式值 → manifest 注入值 → 代码默认。

        ②→③ 的衔接：pipeline yaml 没配 trigger_ratio 时，从注入命名空间
        context_window.compress_trigger_ratio 继承；注入缺失时
        from_yaml_config 自带回退（ConfigCenter 兼容 → 代码默认 0.55），
        故此处无需再包异常防御。

        Args:
            explicit: pipeline yaml 显式配置的 trigger_ratio（可能为 None）
            window_cfg: 注入的 context_window 命名空间 dict（可能为 None）

        Returns:
            最终生效的 trigger_ratio
        """
        # ② Pipeline 显式配置优先
        if explicit is not None:
            return explicit

        # ③ manifest 内联注入优先于 ConfigCenter 兼容路径
        if window_cfg:
            injected_ratio = window_cfg.get("compress_trigger_ratio")
            if isinstance(injected_ratio, (int, float)):
                return injected_ratio

        # ④ 兼容路径 + 代码默认（内部已含读取失败回退）
        return CompressionConfig.from_yaml_config(context_window=128000).compress_trigger_ratio

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
        """从每调用注入与 Agent 配置覆盖运行时参数。

        覆盖链（高→低）：
          ① ctx.state["context_guard.trigger_ratio"]（agent YAML 运行时覆盖，
             与 stop_check 等插件从 ctx.state 读 max_iterations 同一机制）
          ② pipeline/agent 显式 trigger_ratio（构造时 _config 已带，
             _resolve_trigger_ratio 内保持其高于注入值）
          ③ ctx.config["context_window"] 注入命名空间——内核 sidecar 管道每
             调用合入 build_injected_config（内联形态真值即 manifest
             fields.default）；每调用重解析，保存即热生效，不依赖实例重建
          ④ 构造时的 _config/_window_cfg（initialize 握手注入；合宿宿主按
             首触发成员的 manifest 注入后同份扇给全员，本插件可能拿到空值）

        Args:
            ctx: 插件执行上下文
        """
        config = ctx.config if isinstance(ctx.config, dict) else {}
        window_ns = config.get("context_window")
        if isinstance(window_ns, dict) and window_ns:
            self._window_cfg = window_ns
            self._trigger_ratio = self._resolve_trigger_ratio(
                self._config.get("trigger_ratio"), self._window_cfg
            )
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

    @staticmethod
    def _deleted_seq_list(
        before: list[dict[str, Any]],
        after: list[dict[str, Any]],
    ) -> list[int]:
        """计算 before 相对 after 多出的 seq（即被删除的槽位地址）。

        clean/trim/compression 的纯删除路径都用 seq 差集确定被删消息。
        """
        after_seqs = {
            m.get("seq") for m in after if isinstance(m.get("seq"), int)
        }
        deleted: list[int] = []
        seen: set[int] = set()
        for m in before:
            seq = m.get("seq")
            if isinstance(seq, int) and seq not in after_seqs and seq not in seen:
                deleted.append(seq)
                seen.add(seq)
        return deleted

    @staticmethod
    def _deleted_seq_ops(
        before: list[dict[str, Any]],
        after: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """构造被删消息的 set(seq, null) ops 列表。"""
        return [
            {"op": "set", "seq": seq, "msg": None}
            for seq in ContextWindowGuardPlugin._deleted_seq_list(before, after)
        ]

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

        # 从 backend 检索该 pipeline 的 chunk 类记忆（tags 服务端精确过滤
        # 同 prompt_build 检索口径；query 非空为 hindsight recall 硬性要求）
        user_id = ctx.state.get("user_id", "") or pipeline_id
        try:
            results = await _memory_backend.search(
                query=f"pipeline:{pipeline_id}",
                user_id=user_id,
                top_k=20,
                memory_type="chunk",
                tags=[f"pipeline:{pipeline_id}"],
                tags_match="any",
            )
        except Exception as e:
            logger.warning(
                "[context_window_guard] 记忆后端检索失败（按无 L1 预算处理）| pipeline_id=%s | error=%s",
                pipeline_id,
                e,
            )
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

        # recent 消息：seq > max_end 的部分（_record_sequence 已退役，统一用 seq）
        max_end = max((c["sequence_end"] for c in l1_chunks if c["sequence_end"]), default=0)
        recent_tokens = 0
        for m in messages:
            if m.get("role") == "system":
                continue
            seq = m.get("seq")
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

        # 压缩总开关：manifest 内联 compression.enabled=false 时整个守卫不工作
        # （不触发压缩也不做压缩块清理——关闭语义即上下文守卫停摆）
        if not self._window_cfg.get("compression", {}).get("enabled", True):
            return PluginResult()

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
        service = self._get_memory_service(ctx, self._window_cfg)
        if not service:
            return PluginResult()

        # 注入运行时上下文到 service
        self._setup_service(ctx, service, context_window)

        # 窗口变更检测（无 backend 时 clean_if_window_changed 直接返回 None）
        # delete_ops 收集 clean/trim 两条纯删除路径的 set(seq, null) ops
        delete_ops: list[dict[str, Any]] = []
        cleaned = await self.clean_if_window_changed(messages, context_window, ctx)
        if cleaned is not None:
            delete_ops.extend(self._deleted_seq_ops(messages, cleaned))
            messages = cleaned

        # 重启场景裁剪：从存储全量恢复后，已被压缩块覆盖的旧消息需先剔除。
        trimmed = False
        if len(messages) > self._tracked_msg_count + 50:
            new_messages = await self._trim_covered_messages(ctx, messages)
            trimmed = new_messages is not messages
            if trimmed:
                delete_ops.extend(self._deleted_seq_ops(messages, new_messages))
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
            # clean/trim 产生的删除以增量 ops 上报（无删除则不写 messages key）
            if delete_ops:
                updates["messages"] = {"_ops": delete_ops}
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
            # fork 上下文：执行时 system + 消息流（含既有压缩块消息——它们
            # 排在序列里，随 messages 原样进入 fork）。压缩调用复现执行请求
            # 前缀（同模型 cache 复用 + 理解质量）。
            compressed = await service.compress_messages(
                messages=messages,
                context_window=context_window,
                trigger_ratio=self._trigger_ratio,
                system_message=ctx.state.get("system_message"),
            )
        except Exception as exc:
            # 压缩异常 → 降级继续（压缩失败不阻塞管线；上下文超窗由真实
            # LLM 调用的报错暴露，不以零回复终止会话）
            logger.error(
                "[%s] compress_messages 异常，降级继续管线: %s | service=%s",
                self.name,
                exc,
                type(service).__name__,
                exc_info=True,
            )
            await self._notify_compress_failure(ctx)
            compressed = None

        # 成功判据 = 估算 token 收缩（压缩块消息原位替换被压消息后，条数可能
        # 不减——如单条超大消息被替换为一条块消息——token 收缩才是压缩的
        # 可观察不变量）。
        original_tokens = sum(self._estimate_msg_tokens(m) for m in messages)
        compressed_tokens = sum(self._estimate_msg_tokens(m) for m in compressed or [])
        if compressed and compressed_tokens < original_tokens:
            logger.info(
                "[%s] 压缩完成: %d -> %d 条消息, ~%d -> ~%d tokens",
                self.name,
                len(messages),
                len(compressed),
                original_tokens,
                compressed_tokens,
            )
            self._compress_fail_notified = False
            # 压缩只搬运消息不格式化，会原样保留历史段里的 raw 格式 tool_calls，
            # 写回 state 前强制标准化为 OpenAI API 格式，否则上游报"工具类型不能为空"。
            # standardize 返回被改写的消息下标，外层据此 emit set(seq, 新内容) ops。
            changed_indices = self._standardize_tool_calls(compressed)
            post_compress_count = sum(1 for m in compressed if m.get("role") != "system")
            self._tracked_msg_count = post_compress_count
            ctx.state["_tracked_msg_count"] = post_compress_count

            # 构造增量 ops（零兼容：不再回写全量数组）
            ops: list[dict[str, Any]] = list(delete_ops)
            # 压缩块消息：原位占用被压批次头部槽位 set(seq, 块消息)
            block_msgs = getattr(service, "_last_block_msgs", None)
            if not isinstance(block_msgs, list):
                block_msgs = []
            block_seqs = {
                m["seq"] for m in block_msgs if isinstance(m, dict) and isinstance(m.get("seq"), int)
            }
            for m in block_msgs:
                if isinstance(m, dict) and isinstance(m.get("seq"), int):
                    ops.append({"op": "set", "seq": m["seq"], "msg": m})
            # 压缩删除：未被块占用的被压槽位 + 遗留 old_blocks 按其 seq set(seq, null)
            deleted_seqs = getattr(service, "_last_deleted_seqs", None)
            if not isinstance(deleted_seqs, list):
                # 回退：按 before/after 的 seq 差集计算（多轮压缩同样成立）
                deleted_seqs = self._deleted_seq_list(messages, compressed)
            for seq in deleted_seqs:
                if isinstance(seq, int) and seq in block_seqs:
                    continue
                ops.append({"op": "set", "seq": seq, "msg": None})
            # 幸存但被 standardize 改写的消息 set(seq, 新内容)
            for idx in changed_indices:
                m = compressed[idx]
                seq = m.get("seq")
                if isinstance(seq, int):
                    ops.append({"op": "set", "seq": seq, "msg": m})

            return PluginResult(
                state_updates={
                    "messages": {"_ops": ops},
                    "_tracked_msg_count": post_compress_count,
                }
            )

        # 压缩无产出（无料可压/失败/未收缩）→ 降级继续管线，不阻塞回复。
        # 类契约"压缩失败不阻塞管线"：clean/trim 的删除 ops 照常上报，
        # 压缩本身跳过；上下文超窗由真实 LLM 调用的报错暴露。
        logger.warning(
            "[%s] 压缩未产出有效收缩，跳过压缩继续管线: estimated=%d 超过 trigger=%d "
            "(compressed=%s, original=%d, tokens ~%d -> ~%d)",
            self.name,
            estimated_tokens,
            trigger_tokens,
            f"{len(compressed)}条" if compressed else "None",
            len(messages),
            original_tokens,
            compressed_tokens,
        )
        current_non_sys = sum(1 for m in messages if m.get("role") != "system")
        self._tracked_msg_count = current_non_sys
        degrade_updates: dict[str, Any] = {"_tracked_msg_count": current_non_sys}
        if delete_ops:
            degrade_updates["messages"] = {"_ops": delete_ops}
        await self._notify_compress_failure(ctx)
        return PluginResult(state_updates=degrade_updates)

    async def _notify_compress_failure(self, ctx: PluginContext) -> None:
        """压缩彻底失败时向前端推送一次 compression_failed 事件。

        「重试到上限透传」语义：走到这里的失败已是本轮压缩的最终失败
        （压缩失败不阻塞管线，fail-open 降级继续）。连续失败只在首个失败
        轮推送一次（同一故障周期不刷屏），压缩成功后复位。通道未注入或
        发射失败均只留日志，绝不反噬管线降级路径。
        """
        if self._compress_fail_notified:
            return
        self._compress_fail_notified = True
        emit = _frontend_emit
        if emit is None:
            logger.debug(
                "[%s] frontend.emit 未注入，压缩失败不推前端（仅日志）",
                self.name,
            )
            return
        thread_id = str(
            ctx.state.get("session_id") or ctx.state.get("thread_id") or ""
        )
        payload = {
            "thread_id": thread_id,
            "pipeline_id": str(ctx.state.get(StateKeys.PIPELINE_ID) or ""),
            "message": (
                "上下文压缩失败（重试已耗尽），会话继续但上下文将持续膨胀，"
                "建议关注会话状态或新建会话。"
            ),
        }
        try:
            await emit("compression_failed", payload, thread_id)
        except Exception as exc:  # noqa: BLE001 —— 通知是增强能力，失败不阻断降级路径
            logger.warning(
                "[%s] compression_failed 事件推送失败（忽略）: %s",
                self.name,
                exc,
            )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _standardize_tool_calls(self, messages: list[dict[str, Any]]) -> list[int]:
        """压缩写回前把 tool_calls 标准化为 OpenAI API 格式。

        委托给 normalizer 的公共入口 standardize_tool_calls_in_messages
        （纯函数全量修复，同步配对的 tool result）。延迟 import 避免
        input 插件模块加载期耦合 core 插件模块。

        异常策略：只容忍 messages 数据形态引发的运行期错误（不阻塞写回）；
        ImportError 等编程/配置错误必须上抛——历史上用空泛 ``except Exception``
        吞掉了断裂 import（``plugins.core`` 不存在），使标准化静默失效三轮审查未觉。

        Returns:
            被改写的消息下标列表（用于对这些槽位 emit 增量 set(seq, 新内容) op）；
            标准化失败时返回空列表（不阻塞写回）。
        """
        try:
            # 经 pipeline 命名空间包解析（plugins/shared 在 sys.path）。
            # 原 ``plugins.core.llm_core._message_normalizer`` 路径不存在。
            from pipeline.core.llm_core._message_normalizer import (  # noqa: PLC0415
                standardize_tool_calls_in_messages,
            )

            return standardize_tool_calls_in_messages(messages)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            logger.warning(
                "[%s] tool_calls 标准化失败（不阻塞写回）: %s",
                self.name,
                exc,
            )
            return []

    async def _trim_covered_messages(  # noqa: PLR0911
        self,
        ctx: PluginContext,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """裁剪被已有压缩块覆盖的旧消息（重启场景）。

        Step 4 修复：压缩块来源由 ctx.get_service("chunk_service") 改为
        模块级 _memory_backend，无 backend 时原样返回。

        裁剪逻辑：逐条按消息自带 seq 过滤，保留 system 消息 + 非系统消息
        中 seq > max_end 的部分。max_end 取自压缩块的 sequence_end。
        （零兼容：_record_sequence 已退役，统一用引擎分配的 seq 字段。）

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
        except Exception as e:
            logger.warning(
                "[context_window_guard] 记忆后端检索失败（跳过压缩消息注入）| pipeline_id=%s | error=%s",
                pipeline_id,
                e,
            )
            return messages

        l1_chunks, _ = self._filter_chunks(results)
        if not l1_chunks:
            return messages

        max_end = max((c["sequence_end"] for c in l1_chunks if c["sequence_end"]), default=0)
        if max_end <= 0:
            return messages

        # 裁剪：逐条按 seq 过滤，保留序号 > max_end 的非 system 消息。
        trimmed: list[dict[str, Any]] = []
        trimmed_non_sys = 0
        orig_non_sys = 0
        dropped_seqs: list[int] = []
        for m in messages:
            if m.get("role") == "system":
                trimmed.append(m)
                continue
            orig_non_sys += 1
            seq = m.get("seq")
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
        except Exception as e:
            logger.warning(
                "[context_window_guard] 记忆后端检索失败（跳过 window 变更检测）| pipeline_id=%s | error=%s",
                pipeline_id,
                e,
            )
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
    def _get_memory_service(
        ctx: PluginContext,
        window_cfg: dict[str, Any] | None = None,  # noqa: C901
    ):
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
        config = CompressionConfig.from_yaml_config(context_window, injected=window_cfg)

        # 构建 LLM 调用函数：经 capability_caller 调 llm.complete_stream
        # （llm_service 服务轴）。压缩模型唯一真值 = manifest 内联
        # compression.model；未配置即停用压缩（服务不构建，与依赖不全早退
        # 同构），禁止把空模型名发往 llm_service。采样参数不在此配置：
        # 缺省由 llm.complete_stream 按 llm.yaml 模型条目服务侧回填；
        # compression 配置显式携带的键（enabled/model 之外）作为调用方
        # 覆盖透传（2026-09-03 用户裁定）。
        llm_call_fn = None
        if _capability_caller is not None:
            compress_cfg = window_cfg.get("compression", {}) if window_cfg else {}
            compress_model = _resolve_compress_model(ctx, injected=compress_cfg)
            if compress_model:
                extra_params = {
                    key: value
                    for key, value in compress_cfg.items()
                    if key not in ("enabled", "model") and value is not None
                }
                llm_call_fn = _build_compress_llm_call_fn(
                    _capability_caller,
                    model_id=compress_model,
                    extra_params=extra_params,
                )
            else:
                logger.warning(
                    "[ContextWindowGuardPlugin] compression.model 未配置，压缩停用"
                )
                return None

        try:
            return CompressionService(
                backend=_memory_backend,
                llm_call_fn=llm_call_fn,
                config=config,
                injected=window_cfg,
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
                "[%s] setup 完成: pipeline_id=%s",
                self.name,
                pipeline_id[:8] if pipeline_id else "无",
            )
        except Exception as exc:
            logger.error("[%s] setup 异常: %s", self.name, exc, exc_info=True)


def _resolve_compress_model(
    ctx: PluginContext, injected: dict[str, Any] | None = None
) -> str:
    """压缩用模型 id：唯一真值 = manifest 内联字段 ``compression.model``。

    本 manifest 的 fields.default 即配置本体（单一真值裁定 2026-09-02 同款
    形态），不经 state/llm.yaml 解析——guard 所在 sidecar 未注入 llm.yaml
    配置 shim，任何经 loader 的兜底链在这里都是死路。返回空串表示未配置，
    调用方据此停用压缩（见 _get_memory_service）。

    Args:
        ctx: 插件执行上下文（模型解析不再消费 state，保留形参对齐调用形态）
        injected: 注入的 compression 配置 dict（manifest fields.default 内联）

    Returns:
        model_id；未配置返回空串
    """
    if injected and isinstance(injected.get("model"), str):
        return injected["model"].strip()
    return ""


def _compress_capability_timeout(model_id: str) -> float:
    """压缩 LLM 反向调用的等待上限：压缩模型 call_timeout + 余量。

    SDK 默认 CAPABILITY_CALL_TIMEOUT_S=30s 会先于压缩完成掐断请求（压缩是
    LLM 级耗时，上界即 llm.yaml call_timeout）。+60s 余量让 llm_service 的
    结构化错误信封先于 SDK 超时返回（错误是值，不丢语义）。

    _config_models 不可达（llm.yaml 未注入本 sidecar）或模型无配置时，
    用 360s 口径（loader 内部默认 300s + 60s 余量）。
    """
    try:
        from _config_models import get_model_config_loader  # noqa: PLC0415
    except ImportError:
        return 360.0
    loader = get_model_config_loader()
    conf = loader.get_llm_core_config(model_id) if model_id else None
    call_timeout = float(conf["call_timeout"]) if conf else 300.0
    return call_timeout + 60.0


def _build_compress_llm_call_fn(
    caller: CapabilityCaller,
    model_id: str = "",
    extra_params: dict[str, Any] | None = None,
) -> LLMCallFn:
    """构建压缩用的 LLM 调用函数。

    经 capability_caller 调 llm.complete_stream（llm_service 服务轴，与
    llm_core 同款调用形态）：tool-executor.invoke 携带 plugin_id 显式点名
    llm_service，返回 {success, data, error} 信封；data 是聚合响应 dict
    （text/tool_calls/thinking_text/usage/finish_reason/partial），压缩取
    data.text 为摘要。

    model 参数 = manifest 内联 compression.model（_resolve_compress_model
    解析），由调用方保证非空——空模型名发往 llm_service 会被 litellm
    以 BadRequest 拒绝（You passed in model=）。

    采样参数（temperature/max_tokens/thinking 等）不在本函数硬编码：
    缺省由 llm.complete_stream 按 llm.yaml 模型条目 default_params 服务侧
    回填（单一真值在模型配置）；extra_params = compression 配置显式携带
    的参数（enabled/model 之外的键），作为调用方显式参数覆盖服务侧默认
    （2026-09-03 用户裁定：manifest 只给模型名也行，给了完整参数就覆盖）。

    Args:
        caller: 能力调用 async 函数 (method, params, timeout) -> Any
        model_id: 压缩用模型 id（manifest compression.model）
        extra_params: 调用方显式覆盖参数（原样透传进工具 args）

    Returns:
        async (messages) -> response_text 的 LLM 调用函数；
        llm.complete_stream 调用失败时抛 RuntimeError 并携带原因
        （禁止把调用错误伪装成空响应——上游会把空串诊断为"LLM 空响应"）
    """
    timeout = _compress_capability_timeout(model_id)

    async def _call(payload: str | list[dict[str, Any]]) -> str:
        # llm.complete_stream 收消息数组；字符串形态包成单条 user 消息
        if isinstance(payload, list):
            messages = payload
        else:
            messages = [{"role": "user", "content": payload}]
        params = {
            "tool_name": "llm.complete_stream",
            "plugin_id": "llm_service",
            "args": {"model": model_id, "messages": messages, **(extra_params or {})},
        }
        try:
            result = await caller("tool-executor.invoke", params, timeout)
        except Exception as e:
            raise RuntimeError(f"[compress_llm_call] llm.complete_stream 调用失败: {e}") from e
        if not isinstance(result, dict):
            raise RuntimeError(
                f"[compress_llm_call] llm.complete_stream 信封形状异常: {type(result).__name__}"
            )
        # tool-executor.invoke 返回 {success, data, error} 信封（对齐 llm_core）：
        # success=false（服务未注册/执行失败）是错误不是空响应，fail-closed 抛出。
        if not result.get("success"):
            raise RuntimeError(
                f"[compress_llm_call] llm.complete_stream 工具执行失败: {result.get('error') or result}"
            )
        data = result.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(
                f"[compress_llm_call] llm.complete_stream 返回形状异常: {type(data).__name__}"
            )
        # 流中断/取消：半截内容不可作压缩摘要（压缩须全文理解），显式放弃
        if data.get("partial") is not None:
            raise RuntimeError(
                "[compress_llm_call] llm.complete_stream 流中断（partial），放弃压缩"
            )
        return str(data.get("text") or "")

    return _call

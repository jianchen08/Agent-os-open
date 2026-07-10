"""上下文压缩器。

从旧代码 src/memory/compressor/core.py 搬迁。
移除 LLMClient/langchain 硬依赖和 cachetools 依赖，
token 计数使用简化估算，LLM 调用通过注入的可调用对象实现。

暴露接口：
- CompressionConfig: 压缩配置
- ContextCompressor: 上下文压缩器
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 层级名称映射（向后兼容）
LAYER_NAME_MAP = {
    "DSL": "L1",
    "CSL": "L2",
    "KIL": "L2",
}


def normalize_layer_name(layer: str) -> str:
    """标准化层级名称。

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
        compress_trigger_ratio: 压缩触发比例
        l1_ratio: L1 预算比例
        l2_ratio: L2 预算比例
        recent_ratio: 最近原文预算比例
        retrieval_ratio: 检索召回预算比例
        max_turn_ratio: 单轮次最大比例
    """

    context_window: int = 128000
    compress_trigger_ratio: float = 0.55  # 见 config.defaults.COMPRESS_TRIGGER_RATIO
    l1_ratio: float = 0.08
    l2_ratio: float = 0.03
    recent_ratio: float = 0.10
    retrieval_ratio: float = 0.03
    max_turn_ratio: float = 0.5

    @classmethod
    def from_yaml_config(cls, context_window: int) -> CompressionConfig:
        """从 context_window_config.yaml 加载预算配置。

        通过 ConfigCenter 读取（统一缓存 + 热重载），路径由 ConfigCenter 解析，
        不再硬编码 Path(__file__).parent...
        """
        try:
            from config.config_center import get_config_center  # noqa: PLC0415

            yaml_data = get_config_center().get("system/context_window_config.yaml") or {}
            budgets = yaml_data.get("budgets", {})
            return cls(
                context_window=context_window,
                compress_trigger_ratio=yaml_data.get(
                    "compress_trigger_ratio", 0.55
                ),  # 见 config.defaults.COMPRESS_TRIGGER_RATIO
                l1_ratio=budgets.get("l1", 0.08),
                l2_ratio=budgets.get("l2", 0.03),
                recent_ratio=budgets.get("recent", 0.10),
                retrieval_ratio=budgets.get("retrieval", 0.03),
            )
        except Exception:
            return cls(context_window=context_window)

    def get_budgets(self) -> dict[str, int]:
        """计算各部分实际 token 预算。

        Returns:
            各层 token 预算字典
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
            触发阈值
        """
        return int(self.context_window * self.compress_trigger_ratio)


class ContextCompressor:
    """上下文压缩器。

    负责将长对话历史压缩成结构化摘要。
    支持分层递进压缩：L0(原文) → L1(十模块) → L2(三元组)。

    设计原则：
    - 纯函数设计，无状态管理
    - 输入输出都是字符串
    - 不操作数据库

    LLM 调用通过注入的 llm_call_fn 实现而非硬依赖。

    Attributes:
        config: 压缩配置
        budgets: 各层 token 预算
        _llm_call_fn: LLM 调用函数
    """

    # 一次性压缩模板：L1 + L2 + keywords + state_snapshot + memory_items
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
        llm_call_fn: Callable[[str], Awaitable[str]] | None = None,
        config: CompressionConfig | None = None,
    ) -> None:
        """初始化上下文压缩器。

        Args:
            llm_call_fn: LLM 调用函数，接收 prompt 返回 response 文本
            config: 压缩配置
        """
        self._llm_call_fn = llm_call_fn
        self.config = config or CompressionConfig()
        self.budgets = self.config.get_budgets()

    def set_llm_call_fn(self, llm_call_fn: Callable[[str], Awaitable[str]]) -> None:
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
            压缩结果字典；LLM 空响应或 JSON 解析失败时返回 None（fail-closed），
            调用方应检查 None 以避免空内容被当成功保存。

        Raises:
            RuntimeError: LLM 调用过程异常时
        """
        if not messages:
            return {"l1": "", "l2": "", "keywords": [], "state_snapshot": {}, "memory_items": {}}

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
            keywords = [kw.strip() for kw in raw_keywords if isinstance(kw, str) and kw.strip()][:10]

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
                sum(1 for v in state_snapshot_data.values() if v) if isinstance(state_snapshot_data, dict) else 0,
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
            截断后的文本
        """
        estimated = self._estimate_tokens(text)
        if estimated <= max_tokens:
            return text

        import json  # noqa: PLC0415

        max_chars = int(max_tokens * 1.5)
        truncated = text[:max_chars]

        # 如果不是 JSON，直接返回字符截断
        try:
            json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return truncated

        # 是 JSON：找一个安全的截断点（最后一个完整 key-value 后的逗号）
        # 注意 last_comma 是在 truncated 里找的索引，必须用它切 truncated
        last_comma = truncated.rfind(",\n")
        if last_comma > 0:  # noqa: SIM108
            safe = truncated[:last_comma] + "\n}"
        else:
            # 找不到安全的逗号（可能是单字段对象或截断点太靠前）
            # 兜底：返回空对象，保证 JSON 合法
            safe = "{}"

        try:
            parsed = json.loads(safe)
            # 空对象 {} 合法但 falsy，必须用 isinstance 判定而非真值
            if isinstance(parsed, dict):
                return safe
        except (json.JSONDecodeError, ValueError):
            pass

        return "{}"

    def _extract_json(self, text: str) -> str:
        """从 LLM 响应中提取 JSON 并格式化。

        处理 LLM 可能包裹代码块或添加额外文本的情况。

        Args:
            text: LLM 原始响应

        Returns:
            格式化后的 JSON 字符串
        """
        import json  # noqa: PLC0415
        import re  # noqa: PLC0415

        if not text:
            return text

        # 尝试从 markdown 代码块中提取
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1).strip()
        else:
            # 尝试直接匹配 { ... }
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            json_str = json_match.group(0) if json_match else text

        try:
            parsed = json.loads(json_str)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            logger.warning("[ContextCompressor] JSON 解析失败，返回原文")
            return text.strip()

    def _format_messages(self, messages: list[dict[str, Any]]) -> str:
        """格式化消息为文本。

        Args:
            messages: 消息列表

        Returns:
            格式化后的文本
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
                # 完整保留工具结果：压缩需要看到完整内容才能产出高质量摘要。
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
        """估算 token 数（简化版）。

        1 个中文字 ≈ 1.5 token，1 个英文词 ≈ 1 token。

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

        # 简化估算：字符数 / 2
        return max(1, len(text) // 2)

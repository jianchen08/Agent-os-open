"""
推理提取器

从消息中提取推理内容
"""

import logging

logger = logging.getLogger(__name__)


class ReasoningExtractor:
    """推理提取器"""

    def extract(self, messages: list) -> str | None:
        """
        从消息中提取推理内容

        Args:
            messages: 对话消息列表（支持字典或 LangChain 消息对象）

        Returns:
            推理文本，如果没有则返回 None
        """
        if not messages:
            return None

        last_message = messages[-1]

        # 兼容字典和 LangChain 消息对象
        if isinstance(last_message, dict):
            if last_message.get("role") != "assistant":
                return None
            content = last_message.get("content", "")
        else:
            # LangChain 消息对象
            from langchain_core.messages import AIMessage

            if not isinstance(last_message, AIMessage):
                return None
            content = last_message.content if hasattr(last_message, "content") else ""

        # 查找推理部分
        if "🤔 意图分析" not in content:
            return None

        # 提取从"🤔 意图分析"到"现在执行"之间的内容
        start_idx = content.find("🤔 意图分析")
        end_markers = ["现在执行", "开始执行", "执行工具", "调用工具"]

        end_idx = len(content)
        for marker in end_markers:
            idx = content.find(marker, start_idx)
            if idx != -1:
                end_idx = min(end_idx, idx)

        reasoning_text = content[start_idx:end_idx].strip()

        if reasoning_text:
            logger.debug(
                f"[ReasoningExtractor] 提取到推理内容，长度: {len(reasoning_text)}"
            )

        return reasoning_text if reasoning_text else None

    def extract_summary(self, reasoning_text: str, max_length: int = 100) -> str:
        """
        提取推理摘要

        Args:
            reasoning_text: 完整推理文本
            max_length: 最大长度

        Returns:
            推理摘要
        """
        if not reasoning_text:
            return "已完成推理"

        lines = reasoning_text.split("\n")

        # 提取关键行
        key_lines = []
        for line in lines:
            if any(marker in line for marker in ["真实意图:", "高风险:", "操作类型:"]):
                key_lines.append(line.strip())

        summary = " | ".join(key_lines[:3])

        if len(summary) > max_length:
            summary = summary[: max_length - 3] + "..."

        return summary or "已完成推理"

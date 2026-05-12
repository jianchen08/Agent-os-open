"""
结构化压缩模块

包含 StructuredCompressor 类，生成更结构化的摘要，便于程序化处理
"""

from datetime import datetime
from typing import Any

from src.llm.base import LLMClient

from .config import CompressionConfig
from .core import ContextCompressor


class StructuredCompressor:
    """
    结构化压缩器

    生成更结构化的摘要，便于程序化处理
    """

    async def compress_structured(
        self,
        messages: list[dict[str, Any]],
        llm_client: LLMClient,
        config: CompressionConfig | None = None,
    ) -> dict[str, Any]:
        """
        压缩为结构化数据

        Args:
            messages: 消息列表
            llm_client: LLM 客户端
            config: 压缩配置

        Returns:
            结构化摘要
        """
        compressor = ContextCompressor(llm_client, config)
        summary_text = await compressor.compress_to_l1(messages)

        # 解析八段结构化摘要
        return self._parse_eight_sections(summary_text)

    def _parse_eight_sections(self, summary: str) -> dict[str, Any]:
        """
        解析八段结构化摘要

        Args:
            summary: 摘要文本

        Returns:
            结构化数据
        """
        result = {
            "core_events": "",  # 1. 核心事件线索
            "intent_evolution": "",  # 2. 用户意图演进
            "key_decisions": [],  # 3. 关键决策节点
            "knowledge_refs": [],  # 4. 知识引用记录
            "execution_results": [],  # 5. 执行结果摘要
            "problems_solutions": [],  # 6. 问题与解决方案
            "important_context": [],  # 7. 重要上下文
            "pending_items": [],  # 8. 待续事项
        }

        current_section = None
        section_map = {
            "1. 核心事件线索": "core_events",
            "2. 用户意图演进": "intent_evolution",
            "3. 关键决策节点": "key_decisions",
            "4. 知识引用记录": "knowledge_refs",
            "5. 执行结果摘要": "execution_results",
            "6. 问题与解决方案": "problems_solutions",
            "7. 重要上下文": "important_context",
            "8. 待续事项": "pending_items",
        }

        lines = summary.split("\n")

        for line in lines:
            line = line.strip()

            if not line:
                continue

            # 识别章节
            for section_title, section_key in section_map.items():
                if section_title in line:
                    current_section = section_key
                    break
            else:
                # 不是章节标题，处理内容
                if current_section:
                    if line.startswith("- "):
                        content = line[2:].strip()
                        if isinstance(result[current_section], list):
                            result[current_section].append(content)
                        else:
                            if result[current_section]:
                                result[current_section] += " " + content
                            else:
                                result[current_section] = content
                    elif not line.startswith("#"):
                        # 普通文本
                        if isinstance(result[current_section], str):
                            if result[current_section]:
                                result[current_section] += " " + line
                            else:
                                result[current_section] = line

        # 添加元数据
        result["metadata"] = {
            "generated_at": datetime.now().isoformat(),
            "format": "eight_sections",
            "total_items": sum(
                len(v) if isinstance(v, list) else (1 if v else 0)
                for k, v in result.items()
                if k != "metadata"
            ),
        }

        return result

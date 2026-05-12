"""
推理验证器

验证推理内容的完整性和质量
"""

import logging

logger = logging.getLogger(__name__)


class ReasoningValidator:
    """推理验证器"""

    def validate(self, reasoning_text: str | None) -> tuple[bool, dict]:
        """
        验证推理完整性

        Args:
            reasoning_text: 推理文本

        Returns:
            (是否有效, 质量信息字典)
        """
        if not reasoning_text:
            return False, {
                "completeness_score": 0.0,
                "missing_sections": ["intent", "impact", "strategy"],
            }

        # 检查必需部分
        required_sections = {
            "intent": ["意图分析", "真实意图"],
            "impact": ["影响范围分析", "风险评估"],
            "strategy": ["执行计划", "操作类型"],
        }

        missing = []
        found_count = 0

        for section, markers in required_sections.items():
            if any(marker in reasoning_text for marker in markers):
                found_count += 1
            else:
                missing.append(section)

        completeness_score = found_count / len(required_sections)

        is_valid = len(missing) == 0

        quality = {
            "completeness_score": completeness_score,
            "missing_sections": missing,
        }

        if is_valid:
            logger.info(
                f"[ReasoningValidator] 推理验证通过 | score={completeness_score:.2f}"
            )
        else:
            logger.warning(
                f"[ReasoningValidator] 推理不完整 | "
                f"score={completeness_score:.2f} | missing={missing}"
            )

        return is_valid, quality

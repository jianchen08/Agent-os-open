"""
推理拦截器

检查工具执行前是否需要推理，以及推理是否完整
"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class ReasoningInterceptor:
    """推理拦截器"""

    def __init__(self, config_path: str = "config/tools/reasoning_rules.yaml"):
        """
        初始化拦截器

        Args:
            config_path: 配置文件路径
        """
        self.config = self._load_config(config_path)
        self.high_risk_tools = set(self.config.get("high_risk_tools", []))
        self.max_retries = self.config.get("reasoning_rules", {}).get("max_retries", 2)
        self.auto_pass = self.config.get("reasoning_rules", {}).get(
            "auto_pass_after_retries", True
        )

    def _load_config(self, config_path: str) -> dict:
        """加载配置文件"""
        path = Path(config_path)
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"加载推理配置失败: {e}")
                return {}
        return {}

    def is_high_risk(self, tool_name: str) -> bool:
        """
        检查是否高风险工具

        Args:
            tool_name: 工具名称

        Returns:
            是否高风险
        """
        return tool_name in self.high_risk_tools

    def check(
        self, tool_name: str, messages: list[dict], retry_count: int = 0
    ) -> tuple[bool, str | None]:
        """
        检查是否需要推理

        Args:
            tool_name: 工具名称
            messages: 对话消息列表
            retry_count: 当前重试次数

        Returns:
            (是否允许执行, 推理提示)
        """
        # 1. 不是高风险工具，直接通过
        if not self.is_high_risk(tool_name):
            logger.debug(
                f"[ReasoningInterceptor] 非高风险工具，直接通过 | tool={tool_name}"
            )
            return True, None

        # 2. 超过重试次数，降级处理
        if retry_count >= self.max_retries:
            if self.auto_pass:
                logger.warning(
                    f"[ReasoningInterceptor] 推理重试超限，自动通过 | "
                    f"tool={tool_name} | retry={retry_count}"
                )
                return True, None
            else:
                logger.error(
                    f"[ReasoningInterceptor] 推理重试超限，拒绝执行 | "
                    f"tool={tool_name} | retry={retry_count}"
                )
                return False, "推理重试次数超限"

        # 3. 检查最近消息是否包含推理
        if messages:
            last_message = messages[-1]
            if last_message.get("role") == "assistant":
                content = last_message.get("content", "")
                if self._has_reasoning_markers(content):
                    logger.info(
                        f"[ReasoningInterceptor] 检测到推理内容，通过 | tool={tool_name}"
                    )
                    return True, None

        # 4. 没有推理，返回提示
        logger.info(
            f"[ReasoningInterceptor] 缺少推理，要求补充 | "
            f"tool={tool_name} | retry={retry_count}"
        )
        from .templates import generate_reasoning_prompt

        prompt = generate_reasoning_prompt(tool_name)
        return False, prompt

    def _has_reasoning_markers(self, text: str) -> bool:
        """
        检查文本中是否包含推理标记

        Args:
            text: 文本内容

        Returns:
            是否包含推理标记
        """
        required_markers = ["🤔 意图分析", "🔍 影响范围分析", "📝 执行计划"]
        return all(marker in text for marker in required_markers)

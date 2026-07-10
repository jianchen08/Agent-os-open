"""
推理相关异常
"""

from typing import Any

from core.exceptions.tool import ToolException


class ReasoningRequiredError(ToolException):
    """需要推理异常

    当检测到高风险操作但缺少推理时抛出此异常
    """

    def __init__(
        self,
        tool_name: str,
        tool_call_id: str,
        reasoning_prompt: str,
        retry_count: int = 0,
        details: dict[str, Any] | None = None,
    ):
        """初始化异常

        Args:
            tool_name: 工具名称
            tool_call_id: 工具调用 ID
            reasoning_prompt: 推理提示词
            retry_count: 重试次数
            details: 额外的错误详情（可选）
        """
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.reasoning_prompt = reasoning_prompt
        self.retry_count = retry_count
        error_details = details or {}
        error_details.update(
            {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "reasoning_prompt": reasoning_prompt,
                "retry_count": retry_count,
            }
        )
        super().__init__(
            f"工具 {tool_name} 需要推理（重试次数: {retry_count}）",
            code="REASONING_REQUIRED",
            details=error_details,
        )

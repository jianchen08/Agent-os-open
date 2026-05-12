"""
工具消息格式化器

从 execute_tools_node 中提取的消息格式化逻辑
负责构建 LLM 版本和纯净版本的消息
"""

import json
from typing import Any

from src.core.tokenizer import get_token_counter


class ToolMessageFormatter:
    """
    工具消息格式化器

    负责格式化工具执行结果，生成两种版本的消息：
    1. LLM 版本：包含提示词包装，适合给 LLM 理解
    2. 纯净版本：只包含原始结果，适合给前端/数据库
    """

    # 内容长度限制（不限制，设为 100000 tokens）
    MAX_TOKENS = 100000
    MAX_OUTPUT_LENGTH = 100000

    @classmethod
    def format_success_message(
        cls,
        tool_name: str,
        output: Any,
        record_id: str | None = None,
    ) -> tuple[str, str]:
        """
        格式化工具执行成功的消息

        Args:
            tool_name: 工具名称
            output: 工具输出结果
            record_id: 执行记录ID（可选）

        Returns:
            元组 (llm_content, pure_result)
            - llm_content: 给 LLM 看的版本（包含提示词包装）
            - pure_result: 纯净版本（只包含原始结果）
        """
        token_counter = get_token_counter()

        # 格式化输出内容
        if isinstance(output, dict):
            # 将字典格式化为可读文本
            output_text = json.dumps(output, ensure_ascii=False, indent=2)
            # 限制长度，避免消息过长
            if token_counter.count_tokens(output_text) > cls.MAX_TOKENS:
                output_text = output_text[: cls.MAX_TOKENS] + "..."

            # 添加明确的完成提示
            completion_hint = cls._get_completion_hint(tool_name, output)

            # 给 LLM 的版本：包含提示词包装
            llm_content = (
                f"工具 {tool_name} 执行成功\n"
                f"返回结果:\n{output_text}{completion_hint}"
            )
            # 纯净版本：只包含原始结果
            pure_result = output_text

        elif output:
            output_str = str(output)
            if len(output_str) > cls.MAX_TOKENS:
                output_str = output_str[: cls.MAX_TOKENS] + "..."
            llm_content = (
                f"工具 {tool_name} 执行成功\n"
                f"返回结果: {output_str}\n\n✅ 操作已完成。"
            )
            pure_result = output_str
        else:
            llm_content = (
                f"工具 {tool_name} 执行成功\n✅ 操作已完成，无需再次调用。"
            )
            pure_result = "操作已完成"

        return llm_content, pure_result

    @classmethod
    def format_error_message(
        cls,
        tool_name: str,
        error: str | None,
    ) -> tuple[str, str]:
        """
        格式化工具执行失败的消息

        Args:
            tool_name: 工具名称
            error: 错误信息

        Returns:
            元组 (llm_content, pure_result)
            - llm_content: 给 LLM 看的版本
            - pure_result: 纯净版本
        """
        llm_content = f"工具 {tool_name} 执行失败:\n{error}"
        pure_result = f"执行失败: {error}"
        return llm_content, pure_result

    @classmethod
    def format_exception_message(
        cls,
        tool_name: str,
        exception: Exception,
    ) -> tuple[str, str]:
        """
        格式化工具执行异常的消息

        Args:
            tool_name: 工具名称
            exception: 异常对象

        Returns:
            元组 (llm_content, pure_result)
        """
        error_msg = str(exception)
        llm_content = f"工具 {tool_name} 执行异常: {error_msg}"
        pure_result = f"执行异常: {error_msg}"
        return llm_content, pure_result

    @classmethod
    def _get_completion_hint(cls, tool_name: str, output: dict[str, Any]) -> str:
        """
        获取完成提示

        根据工具名称和输出内容，生成适当的完成提示

        Args:
            tool_name: 工具名称
            output: 工具输出

        Returns:
            完成提示字符串
        """
        # 检查是否是任务提交类工具
        if tool_name in ["task_submit", "task_manage"]:
            if output.get("task_submitted") or output.get("task_id"):
                return "\n\n✅ 任务已成功提交，无需再次调用此工具。"

        # 检查是否有明确的成功标志
        if output.get("success") is True or output.get("completed"):
            return "\n\n✅ 操作已完成，无需再次调用。"

        return ""

    @classmethod
    def truncate_output(cls, output: Any) -> str:
        """
        截断输出内容

        将输出内容截断到合适的长度

        Args:
            output: 输出内容

        Returns:
            截断后的字符串
        """
        token_counter = get_token_counter()

        if isinstance(output, dict):
            output_text = json.dumps(output, ensure_ascii=False, indent=2)
        else:
            output_text = str(output)

        if token_counter.count_tokens(output_text) > cls.MAX_OUTPUT_LENGTH:
            return output_text[: cls.MAX_OUTPUT_LENGTH] + "..."

        return output_text

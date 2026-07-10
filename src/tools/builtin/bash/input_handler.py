"""
输入处理器

暴露接口：
- check_sensitive(self, input_text: str) -> tuple[bool, str]：check_sensitive功能
- validate_input(self, input_text: str) -> tuple[bool, str | None]：validate_input功能
- format_input(self, input_text: str, add_newline: bool) -> str：format_input功能
- process(self, input_text: str, add_newline: bool) -> tuple[bool, str | None, str]：process功能
- InputHandler：InputHandler类
"""

from __future__ import annotations

from typing import ClassVar


class InputHandler:
    """
    输入处理器

    处理向运行中进程发送的输入，包括安全检查和敏感信息隐藏。
    """

    # 敏感关键词（输入会被隐藏）
    SENSITIVE_KEYWORDS: ClassVar[list[str]] = [
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "key",
        "api_key",
        "apikey",
        "credential",
        "auth",
        "private",
    ]

    # 禁止的输入字符
    FORBIDDEN_CHARS: ClassVar[list[str]] = [
        "\x00",  # 空字符
        "\x03",  # Ctrl+C
        "\x04",  # Ctrl+D (EOF)
        "\x1a",  # Ctrl+Z
    ]

    # 最大输入长度
    MAX_INPUT_LENGTH: ClassVar[int] = 4096

    def __init__(self):
        """初始化输入处理器"""

    def check_sensitive(self, input_text: str) -> tuple[bool, str]:
        """检查是否为敏感输入"""
        input_lower = input_text.lower()

        for keyword in self.SENSITIVE_KEYWORDS:
            if keyword in input_lower:
                # 返回掩码版本
                masked = "*" * len(input_text)
                return True, masked

        return False, input_text

    def validate_input(self, input_text: str) -> tuple[bool, str | None]:
        """验证输入有效性"""
        # 检查长度
        if len(input_text) > self.MAX_INPUT_LENGTH:
            return False, f"输入长度超过限制（最大{self.MAX_INPUT_LENGTH}字符）"

        # 检查禁止字符
        for char in self.FORBIDDEN_CHARS:
            if char in input_text:
                return False, "输入包含禁止字符"

        return True, None

    def format_input(self, input_text: str, add_newline: bool = True) -> str:
        """格式化输入（添加换行符等）"""
        if add_newline and not input_text.endswith("\n"):
            return input_text + "\n"
        return input_text

    def process(self, input_text: str, add_newline: bool = True) -> tuple[bool, str | None, str]:
        """处理输入（完整流程）"""
        # 验证
        is_valid, error = self.validate_input(input_text)
        if not is_valid:
            return False, error, ""

        # 格式化
        formatted = self.format_input(input_text, add_newline)

        return True, None, formatted

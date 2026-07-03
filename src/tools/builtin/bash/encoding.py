"""
Bash 工具编码自适应模块

解决 Windows 中文环境下 subprocess 输入/输出编码不匹配问题：
- CMD (cmd.exe) 使用系统代码页（GBK/CP936）输出
- Git Bash / WSL 使用 UTF-8 输出
- Python 默认按 UTF-8 解码 → 中文乱码
- WSL 通过 cmd /c 执行时，cmd.exe 会污染 WSL 的 UTF-8 输出

暴露接口：
- get_system_encoding() -> str：获取系统首选编码
- decode_output_line(data: bytes) -> str：自适应解码 subprocess 输出行
- decode_output_text(data: bytes) -> str：自适应解码完整输出
- safe_cmd_encode(text: str) -> str：安全编码命令文本（CMD 路径用）
"""

from __future__ import annotations

import locale
import logging
import platform
from typing import ClassVar  # noqa: F401

logger = logging.getLogger(__name__)

# Windows 下 CMD 常见代码页
_WIN_CMD_ENCODINGS: tuple[str, ...] = (
    "cp936",  # 简体中文 GBK
    "cp950",  # 繁体中文 Big5
    "cp932",  # 日文 Shift-JIS
    "cp949",  # 韩文
    "cp1252",  # 西欧
)


class EncodingHandler:
    """编码自适应处理器。

    统一处理 Windows 环境下 subprocess 的编码问题，
    包括输出解码和命令编码两个方向。
    """

    @staticmethod
    def get_system_encoding() -> str:
        """获取当前系统的首选编码。

        Windows 中文环境返回 'cp936'（GBK），
        其他环境返回 locale 配置的首选编码。

        Returns:
            编码名称字符串（如 'cp936', 'utf-8', 'cp1252'）
        """
        try:
            enc = locale.getpreferredencoding()
            if enc:
                return enc
        except Exception:
            pass
        return "utf-8"

    @classmethod
    def decode_output_line(cls, data: bytes) -> str:
        """自适应解码 subprocess 输出行。

        解码优先级：
        1. UTF-8 严格解码（Git Bash / WSL / 现代工具）
        2. UTF-8 + surrogateescape（保留少量无效字节，处理 WSL 通过 CMD 的污染）
        3. 系统编码（CMD 输出 GBK/CP936）
        4. Windows 其他代码页
        5. UTF-8 + replace（兜底）

        Args:
            data: 从 subprocess stdout/stderr 读取的原始字节

        Returns:
            解码后的字符串（已去除 null bytes）
        """
        if not data:
            return ""

        text = cls._try_decode(data)
        return text.replace("\x00", "")

    @classmethod
    def decode_output_text(cls, data: bytes) -> str:
        """自适应解码完整的 subprocess 输出文本。

        与 decode_output_line 相同的逻辑，但适用于多行输出。

        Args:
            data: 完整的输出字节

        Returns:
            解码后的完整字符串（已去除 null bytes）
        """
        return cls.decode_output_line(data)

    @classmethod
    def _try_decode(cls, data: bytes) -> str:
        """按优先级尝试多种编码解码字节数据。

        策略：
        1. UTF-8 严格 → 成功说明源是 UTF-8（Git Bash / WSL）
        2. UTF-8 + surrogateescape → 大部分是 UTF-8 但混入少量无效字节
           （常见于 WSL 通过 cmd /c 执行时的编码污染）
        3. 系统编码 → CMD 原生输出（GBK）
        4. Windows 其他代码页
        5. UTF-8 + replace → 最后兜底

        Args:
            data: 原始字节数据

        Returns:
            解码后的字符串
        """
        # 第一优先：UTF-8 严格模式
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass

        # 第二优先：UTF-8 + surrogateescape
        # 处理大部分文本是 UTF-8 但混入少量无效字节的场景
        # 如 WSL 输出通过 cmd.exe 管道时可能混入非 UTF-8 字节
        try:
            result = data.decode("utf-8", errors="surrogateescape")
            # 检查 surrogate 字符比例：如果 < 15%，说明大部分是有效 UTF-8
            surrogate_count = sum(1 for c in result if "\ud800" <= c <= "\udfff")
            if surrogate_count == 0 or surrogate_count < max(len(result) * 0.15, 3):
                if surrogate_count > 0:
                    logger.debug(
                        "UTF-8 surrogateescape used: %d surrogates in %d chars",
                        surrogate_count,
                        len(result),
                    )
                return result
        except UnicodeDecodeError:
            pass

        # 第三优先：系统编码
        system_enc = cls.get_system_encoding()
        if system_enc.lower() not in ("utf-8", "utf8"):
            try:
                return data.decode(system_enc)
            except UnicodeDecodeError:
                pass

        # Windows 下额外尝试常见 CMD 代码页
        if platform.system() == "Windows":
            for enc in _WIN_CMD_ENCODINGS:
                if enc == system_enc:
                    continue  # 已尝试过
                try:
                    return data.decode(enc)
                except UnicodeDecodeError:
                    continue

        # 最后兜底：UTF-8 + replace
        logger.debug(
            "Falling back to UTF-8+replace for %d bytes",
            len(data),
        )
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def safe_cmd_encode(text: str) -> str:
        """将文本编码为 CMD 可以安全处理的格式。

        在 Windows 上，当命令通过 cmd /c 执行时，
        命令字符串需要与 CMD 的代码页兼容。
        如果文本中包含系统编码无法表示的字符，
        则进行安全替换。

        Args:
            text: 原始命令文本（Unicode）

        Returns:
            编码安全的命令文本
        """
        try:
            system_enc = locale.getpreferredencoding() or "utf-8"
            # 检查文本是否能无损编码为系统编码
            text.encode(system_enc)
            return text  # 所有字符可表示，无需转换
        except (UnicodeEncodeError, LookupError):
            # 部分字符无法表示，进行安全替换
            try:
                return text.encode(system_enc, errors="replace").decode(system_enc)
            except Exception:
                return text  # 实在不行就返回原文本

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
- safe_cmd_encode(text: str) -> str：安全编码命令文本（CMD 路径用；
  实现单源在 shell_route.py，本模块不再自持）
"""

from __future__ import annotations

import locale
import logging
import platform

logger = logging.getLogger(__name__)

# Windows 下 CMD 常见代码页
_WIN_CMD_ENCODINGS: tuple[str, ...] = (
    "cp936",  # 简体中文 GBK
    "cp950",  # 繁体中文 Big5
    "cp932",  # 日文 Shift-JIS
    "cp949",  # 韩文
    "cp1252",  # 西欧
)


# 孤立代理（U+D800-U+DFFF）→ U+FFFD 映射表：str.translate 的 C 级实现查表，
# 供出口清洗用（str.translate 按 Unicode 码位键查表，1:1 替换不改变文本长度）
_LONE_SURROGATE_TO_FFFD: dict[int, str] = dict.fromkeys(
    range(0xD800, 0xDFFF + 1), "\ufffd"
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
        except Exception as exc:  # noqa: BLE001 —— 探测失败回退 utf-8，但留痕
            logging.getLogger(__name__).debug(
                "[encoding] locale 首选编码探测失败（回退 utf-8）: %s: %s",
                type(exc).__name__,
                exc,
            )
        return "utf-8"

    @classmethod
    def decode_output_line(cls, data: bytes) -> str:
        """自适应解码 subprocess 输出行。

        解码优先级：
        1. UTF-8 严格解码（Git Bash / WSL / 现代工具）
        2. UTF-8 + surrogateescape（混入少量无效字节暂存为代理字符，出口统一清洗，
           处理 WSL 通过 CMD 的污染）
        3. 系统编码（CMD 输出 GBK/CP936）
        4. Windows 其他代码页
        5. UTF-8 + replace（兜底）

        出口契约：返回文本保证无孤立代理字符（U+D800-U+DFFF）——孤立代理无法被
        JSON 序列化，会在 MCP stdio 响应序列化时炸掉插件进程。

        Args:
            data: 从 subprocess stdout/stderr 读取的原始字节

        Returns:
            解码后的字符串（已去除 null bytes，无孤立代理）
        """
        if not data:
            return ""

        text = cls._try_decode(data)
        text = text.replace("\x00", "")
        return cls._sanitize_lone_surrogates(text, len(data))

    @staticmethod
    def _sanitize_lone_surrogates(text: str, original_size: int) -> str:
        """出口清洗孤立代理字符，保证文本可被 JSON 序列化。

        孤立代理（U+D800-U+DFFF）无法被 JSON 序列化（pydantic
        model_dump_json 抛 UnicodeEncodeError，会炸掉 MCP stdio 插件进程），
        故解码出口强制清洗：干净文本经 C 级试编码直通（零成本）；含孤立代理的
        文本将代理字符逐个替换为 U+FFFD，并打 debug 日志（替换字符数 +
        原字节数）供装机侧采集修复频率数据。

        Args:
            text: 待清洗文本
            original_size: 产生该文本的原始输入字节数（仅用于日志）

        Returns:
            无孤立代理的文本
        """
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            pass
        else:
            return text

        surrogate_count = sum(
            1 for c in text if "\ud800" <= c <= "\udfff"
        )
        cleaned = text.translate(_LONE_SURROGATE_TO_FFFD)
        logger.debug(
            "lone surrogates sanitized: %d replaced with U+FFFD, input %d bytes",
            surrogate_count,
            original_size,
        )
        return cleaned

    @classmethod
    def decode_output_text(cls, data: bytes) -> str:
        """自适应解码完整的 subprocess 输出文本。

        与 decode_output_line 相同的逻辑，但适用于多行输出。

        Args:
            data: 完整的输出字节

        Returns:
            解码后的完整字符串（已去除 null bytes，无孤立代理）
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
        # （surrogateescape 解码对任意字节序列不抛错，无需 except 包裹）
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

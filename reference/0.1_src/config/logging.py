"""
日志配置

解决Windows环境下的Unicode编码问题，添加彩色输出支持
修复Windows下日志文件轮转被占用问题
"""

import contextlib
import logging
import logging.handlers
import os


class SafeEncodingStreamHandler(logging.StreamHandler):
    """处理编码问题的 StreamHandler"""

    def __init__(self, stream=None):
        super().__init__(stream)
        # 设置编码和错误处理模式
        if stream and hasattr(stream, "buffer"):
            self.encoding = "utf-8"
            self.errors = "replace"
        else:
            self.encoding = getattr(stream, "encoding", None)
            self.errors = getattr(stream, "errors", "replace")

    def emit(self, record):
        try:
            msg = self.format(record)
            # 确保消息可以被正确编码
            if hasattr(self.stream, "buffer"):
                # 对于有 buffer 的流（如 sys.stdout.buffer）
                self.stream.buffer.write(msg.encode("utf-8", errors="replace") + b"\n")
            else:
                # 对于没有 buffer 的流
                self.stream.write(msg + "\n")
            self.flush()
        except Exception:
            self.handleError(record)


class ColoredFormatter(logging.Formatter):
    """彩色日志格式化器"""

    # ANSI 颜色代码
    COLORS = {
        "DEBUG": "\033[36m",  # 青色
        "INFO": "\033[32m",  # 绿色
        "WARNING": "\033[33m",  # 黄色
        "ERROR": "\033[31m",  # 红色
        "CRITICAL": "\033[35m",  # 紫色
    }
    RESET = "\033[0m"

    def format(self, record):
        # 添加颜色
        if record.levelname in self.COLORS:
            record.levelname = f"{self.COLORS[record.levelname]}{record.levelname}{self.RESET}"
        return super().format(record)


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    安全的日志文件轮转处理器

    修复 Windows 下文件被占用时无法轮转的问题
    """

    def emit(self, record):
        try:
            super().emit(record)
        except PermissionError:
            # Windows 下文件被占用时，尝试使用备用文件名
            try:
                # 尝试直接写入，不重命名
                if self.stream:
                    self.stream.flush()
            except Exception:
                pass
            # 不抛出异常，避免影响程序运行
            self.handleError(record)

    def doRollover(self):  # noqa: N802
        """
        执行日志轮转，处理 Windows 文件占用问题
        """
        if self.stream:
            self.stream.close()
            self.stream = None

        try:
            # 尝试重命名文件
            if os.path.exists(self.baseFilename):  # noqa: PTH110
                # 检查备份文件数量
                for i in range(self.backupCount - 1, 0, -1):
                    sfn = f"{self.baseFilename}.{i}"
                    dfn = f"{self.baseFilename}.{i + 1}"
                    if os.path.exists(sfn):  # noqa: PTH110
                        try:
                            if os.path.exists(dfn):  # noqa: PTH110
                                os.remove(dfn)  # noqa: PTH107
                            os.rename(sfn, dfn)  # noqa: PTH104
                        except (OSError, PermissionError):
                            # 文件被占用，跳过
                            pass

                dfn = f"{self.baseFilename}.1"
                if os.path.exists(dfn):  # noqa: PTH110
                    with contextlib.suppress(OSError, PermissionError):
                        os.remove(dfn)  # noqa: PTH107
                try:
                    os.rename(self.baseFilename, dfn)  # noqa: PTH104
                except (OSError, PermissionError):
                    # 文件被占用，清空当前文件继续写入
                    with open(self.baseFilename, "w", encoding=self.encoding):
                        pass
        except Exception:
            pass

        # 重新打开文件
        self.stream = self._open()


# 标记是否已配置
_logging_configured = False


def setup_logging(console_level: str = None):
    """设置日志配置（已转发到统一日志模块）。

    Args:
        console_level: 控制台日志级别 (DEBUG/INFO/WARNING/ERROR)，默认从环境变量读取
    """
    global _logging_configured  # noqa: PLW0603

    # 避免重复配置
    if _logging_configured:
        return logging.getLogger(__name__)

    # 转发到统一日志系统 src.core.logging
    from src.core.logging import LoggingConfig, setup_logging as _unified_setup  # noqa: PLC0415

    level_str = console_level or os.getenv("LOG_LEVEL", "INFO")
    config = LoggingConfig.from_env()
    # 覆盖级别（调用方显式传入时优先）
    config = LoggingConfig(
        level=getattr(logging, level_str.upper(), logging.INFO),
        json_output=config.json_output,
        output=config.output,
        file_path=config.file_path,
        file_max_bytes=config.file_max_bytes,
        file_backup_count=config.file_backup_count,
        third_party_level=config.third_party_level,
        context_fields=config.context_fields,
    )
    _unified_setup(config, reset=True)
    _logging_configured = True

    return logging.getLogger(__name__)


# 创建默认logger（延迟导入时才初始化）
logger = None


def _get_logger():
    global logger  # noqa: PLW0603
    if logger is None:
        logger = setup_logging()
    return logger


# 向后兼容
def __getattr__(name):
    if name == "logger":
        return _get_logger()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

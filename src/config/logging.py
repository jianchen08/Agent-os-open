"""
日志配置

解决Windows环境下的Unicode编码问题，添加彩色输出支持
修复Windows下日志文件轮转被占用问题
"""

import logging
import logging.config
import logging.handlers
import os
import sys
from pathlib import Path


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
            record.levelname = (
                f"{self.COLORS[record.levelname]}{record.levelname}{self.RESET}"
            )
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

    def doRollover(self):
        """
        执行日志轮转，处理 Windows 文件占用问题
        """
        if self.stream:
            self.stream.close()
            self.stream = None

        try:
            # 尝试重命名文件
            if os.path.exists(self.baseFilename):
                # 检查备份文件数量
                for i in range(self.backupCount - 1, 0, -1):
                    sfn = f"{self.baseFilename}.{i}"
                    dfn = f"{self.baseFilename}.{i + 1}"
                    if os.path.exists(sfn):
                        try:
                            if os.path.exists(dfn):
                                os.remove(dfn)
                            os.rename(sfn, dfn)
                        except (OSError, PermissionError):
                            # 文件被占用，跳过
                            pass

                dfn = f"{self.baseFilename}.1"
                if os.path.exists(dfn):
                    try:
                        os.remove(dfn)
                    except (OSError, PermissionError):
                        pass
                try:
                    os.rename(self.baseFilename, dfn)
                except (OSError, PermissionError):
                    # 文件被占用，清空当前文件继续写入
                    with open(self.baseFilename, 'w', encoding=self.encoding):
                        pass
        except Exception:
            pass

        # 重新打开文件
        self.stream = self._open()


# 标记是否已配置
_logging_configured = False


def setup_logging(console_level: str = None):
    """
    设置日志配置

    Args:
        console_level: 控制台日志级别 (DEBUG/INFO/WARNING/ERROR)，默认从环境变量读取
    """
    global _logging_configured

    # 避免重复配置
    if _logging_configured:
        return logging.getLogger(__name__)

    # 从环境变量获取控制台日志级别
    if console_level is None:
        console_level = os.getenv("LOG_LEVEL", "INFO").upper()

    # 检查是否禁用文件日志（用于测试环境）
    disable_file_logging = os.getenv("DISABLE_FILE_LOGGING", "false").lower() == "true"

    # 确保日志目录存在
    log_dir = Path("logs")
    if not disable_file_logging:
        log_dir.mkdir(exist_ok=True)

    # 简洁的控制台格式（模块名:级别名 消息）
    console_format = "%(name)-25s | %(levelname)-8s | %(message)s"

    # 详细的文件格式（包含时间、文件、行号）
    file_format = (
        "%(asctime)s | %(name)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s"
    )

    # 检测是否支持彩色输出
    supports_color = sys.platform != "win32" or os.getenv("WT_SESSION")

    # 日志配置
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "console": {
                "format": console_format,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": console_level,
                "formatter": "console",
                "stream": sys.stdout,
            },
        },
        "loggers": {
            "": {  # root logger
                "handlers": ["console"],
                "level": "DEBUG",  # 允许所有级别的日志通过
                "propagate": False,
            },
            "src": {
                "handlers": ["console"],
                "level": "DEBUG",  # 业务代码使用 DEBUG 级别
                "propagate": False,
            },
            "uvicorn": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["console"],
                "level": "WARNING",  # 减少访问日志噪音
                "propagate": False,
            },
            "sqlalchemy.engine": {
                "handlers": ["console"],
                "level": "WARNING",  # 只显示警告和错误
                "propagate": False,
            },
            "sqlalchemy.pool": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
            "sqlalchemy.dialects": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
            "sqlalchemy.orm": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
        },
    }

    # 如果未禁用文件日志，添加文件处理器
    if not disable_file_logging:
        config["formatters"]["file"] = {
            "format": file_format,
            "datefmt": "%Y-%m-%d %H:%M:%S",
        }
        # 使用安全的日志轮转处理器
        config["handlers"]["file"] = {
            "class": "src.config.logging.SafeRotatingFileHandler",
            "level": "DEBUG",
            "formatter": "file",
            "filename": str(log_dir / "backend.log"),
            "maxBytes": 10485760,  # 10MB
            "backupCount": 5,
            "encoding": "utf-8",
        }
        config["handlers"]["error_file"] = {
            "class": "src.config.logging.SafeRotatingFileHandler",
            "level": "ERROR",
            "formatter": "file",
            "filename": str(log_dir / "error.log"),
            "maxBytes": 10485760,  # 10MB
            "backupCount": 5,
            "encoding": "utf-8",
        }

        # 更新 logger 配置以包含文件处理器
        for logger_name in ["", "src", "uvicorn", "uvicorn.access"]:
            if logger_name in config["loggers"]:
                config["loggers"][logger_name]["handlers"] = ["console", "file"]

        # SQL 日志只写入文件
        for logger_name in [
            "sqlalchemy.engine",
            "sqlalchemy.pool",
            "sqlalchemy.dialects",
            "sqlalchemy.orm",
        ]:
            if logger_name in config["loggers"]:
                config["loggers"][logger_name]["handlers"] = ["file"]

    # 如果不支持彩色输出，移除自定义格式化器
    if not supports_color:
        config["formatters"]["console"] = {"format": console_format}

    logging.config.dictConfig(config)
    _logging_configured = True

    # Windows 编码修复：使用环境变量或直接配置
    if sys.platform == "win32":
        # 设置环境变量以确保控制台使用 UTF-8
        os.environ["PYTHONIOENCODING"] = "utf-8"

    # 显示日志配置信息
    logger = logging.getLogger(__name__)
    logger.debug(f"日志已配置 | 控制台级别: {console_level} | 文件级别: DEBUG")

    return logger


# 创建默认logger（延迟导入时才初始化）
logger = None


def _get_logger():
    global logger
    if logger is None:
        logger = setup_logging()
    return logger


# 向后兼容
def __getattr__(name):
    if name == "logger":
        return _get_logger()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

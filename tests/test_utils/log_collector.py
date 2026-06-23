"""测试日志收集器。

在测试运行期间捕获统一日志系统的输出，测试失败时自动提供相关日志。
通过 pytest fixture 或手动 API 两种方式使用。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from src.core.logging.context import LogContext


@dataclass
class LogEntry:
    """单条日志记录。"""

    timestamp: str
    level: str
    logger_name: str
    message: str
    context: dict[str, str]
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LogCaptureResult:
    """日志收集结果。"""

    entries: list[LogEntry] = field(default_factory=list)
    _level_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def error_count(self) -> int:
        return self._level_counts.get("ERROR", 0) + self._level_counts.get("CRITICAL", 0)

    @property
    def warning_count(self) -> int:
        return self._level_counts.get("WARNING", 0)

    def errors(self) -> list[LogEntry]:
        """仅返回 ERROR 及以上级别的日志。"""
        return [e for e in self.entries if e.level in ("ERROR", "CRITICAL")]

    def warnings(self) -> list[LogEntry]:
        """仅返回 WARNING 级别的日志。"""
        return [e for e in self.entries if e.level == "WARNING"]

    def for_logger(self, name_prefix: str) -> list[LogEntry]:
        """按 logger 名称前缀过滤。"""
        return [e for e in self.entries if e.logger_name.startswith(name_prefix)]

    def format_errors(self) -> str:
        """格式化错误日志为可读字符串。"""
        if not self.errors():
            return "无错误日志。"
        lines: list[str] = [f"📋 捕获到 {self.error_count} 条错误日志:"]
        for entry in self.errors():
            ctx_str = " ".join(f"{k}={v}" for k, v in entry.context.items() if v != "-")
            lines.append(f"  [{entry.level}] {entry.logger_name}: {entry.message}")
            if ctx_str:
                lines.append(f"    上下文: {ctx_str}")
        return "\n".join(lines)


class _CaptureHandler(logging.Handler):
    """将日志记录转发到 LogCollector 的 handler。"""

    def __init__(self, collector: LogCollector) -> None:
        super().__init__()
        self._collector = collector

    def emit(self, record: logging.LogRecord) -> None:
        entry = LogEntry(
            timestamp=self.formatter.formatTime(record) if self.formatter else "",
            level=record.levelname,
            logger_name=record.name,
            message=record.getMessage(),
            context=LogContext.snapshot(),
        )
        self._collector._add_entry(entry)


class LogCollector:
    """日志收集器。

    用法::

        collector = LogCollector()
        collector.start()

        # ... 运行测试 ...

        collector.stop()
        result = collector.get_result()
        print(result.format_errors())

    也可作为 pytest fixture 使用（见 conftest.py 中的 ``log_collector`` fixture）。
    """

    def __init__(self) -> None:
        self._result: LogCaptureResult = LogCaptureResult()
        self._handler: _CaptureHandler | None = None
        self._active: bool = False
        self._saved_root_level: int = logging.WARNING

    @property
    def active(self) -> bool:
        return self._active

    def start(self, min_level: int = logging.WARNING) -> None:
        """开始收集日志。

        Args:
            min_level: 最低收集级别，默认 WARNING。
        """
        if self._active:
            return
        self._result = LogCaptureResult()
        self._handler = _CaptureHandler(self)
        self._handler.setLevel(min_level)
        logging.root.addHandler(self._handler)
        # 确保 root logger 级别不低于 min_level，否则日志被 root 过滤掉
        self._saved_root_level = logging.root.level
        if logging.root.level > min_level:
            logging.root.setLevel(min_level)
        self._active = True

    def stop(self) -> None:
        """停止收集日志。"""
        if not self._active or self._handler is None:
            return
        logging.root.removeHandler(self._handler)
        logging.root.setLevel(self._saved_root_level)
        self._handler = None
        self._active = False

    def get_result(self) -> LogCaptureResult:
        """获取收集结果。"""
        return self._result

    def _add_entry(self, entry: LogEntry) -> None:
        """内部方法：由 _CaptureHandler 调用添加日志条目。"""
        self._result.entries.append(entry)
        self._result._level_counts[entry.level] += 1


__all__ = ["LogCollector", "LogCaptureResult", "LogEntry"]

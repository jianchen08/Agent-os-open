"""统一日志配置。

提供 LoggingConfig 数据类，从环境变量或字典初始化日志行为。
所有字段均有合理默认值，零配置即可使用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from logging import CRITICAL, DEBUG, ERROR, INFO, WARNING
from typing import Literal

# 级别名称 → logging 常量 的映射
_LEVEL_MAP: dict[str, int] = {
    "DEBUG": DEBUG,
    "INFO": INFO,
    "WARNING": WARNING,
    "ERROR": ERROR,
    "CRITICAL": CRITICAL,
}

OutputTarget = Literal["console", "file", "both"]


@dataclass(frozen=True)
class LoggingConfig:
    """日志配置（不可变数据类）。

    所有字段均可通过同名环境变量覆盖，例如 ``LOG_LEVEL=DEBUG``。
    """

    level: int = INFO
    format_string: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(context)s | %(message)s"
    date_format: str = "%Y-%m-%d %H:%M:%S"
    json_output: bool = False
    output: OutputTarget = "console"
    file_path: str = "logs/app.log"
    file_max_bytes: int = 50 * 1024 * 1024  # 50 MB
    file_backup_count: int = 5
    third_party_level: int = WARNING
    context_fields: tuple[str, ...] = (
        "trace_id",
        "request_id",
        "task_id",
        "session_id",
        "pipeline_id",
        "thread_id",
        "agent_name",
    )

    @classmethod
    def from_env(cls) -> LoggingConfig:
        """从环境变量构建配置。

        支持的环境变量::

            LOG_LEVEL          — DEBUG / INFO / WARNING / ERROR / CRITICAL
            LOG_FORMAT         — 格式字符串
            LOG_JSON           — 1 / true → JSON 输出
            LOG_OUTPUT         — console / file / both
            LOG_FILE           — 日志文件路径
            LOG_FILE_MAX_BYTES — 单文件最大字节数
            LOG_FILE_BACKUPS   — 保留的轮转文件数
        """
        return cls(
            level=_LEVEL_MAP.get(os.getenv("LOG_LEVEL", "").upper(), INFO),
            format_string=os.getenv("LOG_FORMAT", cls.format_string),
            json_output=os.getenv("LOG_JSON", "").lower() in ("1", "true"),
            output=_parse_output(os.getenv("LOG_OUTPUT", "console")),
            file_path=os.getenv("LOG_FILE", cls.file_path),
            file_max_bytes=int(os.getenv("LOG_FILE_MAX_BYTES", str(cls.file_max_bytes))),
            file_backup_count=int(os.getenv("LOG_FILE_BACKUPS", str(cls.file_backup_count))),
            third_party_level=_LEVEL_MAP.get(os.getenv("LOG_THIRD_PARTY_LEVEL", "WARNING").upper(), WARNING),
        )

    @classmethod
    def from_dict(cls, data: dict) -> LoggingConfig:
        """从字典构建配置，忽略未知键。"""
        known_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_keys}
        if "level" in filtered and isinstance(filtered["level"], str):
            filtered["level"] = _LEVEL_MAP.get(filtered["level"].upper(), INFO)
        if "third_party_level" in filtered and isinstance(filtered["third_party_level"], str):
            filtered["third_party_level"] = _LEVEL_MAP.get(filtered["third_party_level"].upper(), WARNING)
        return cls(**filtered)


def _parse_output(value: str) -> OutputTarget:
    """解析输出目标字符串。"""
    if value == "file":
        return "file"
    if value == "both":
        return "both"
    return "console"


__all__ = ["LoggingConfig", "OutputTarget"]

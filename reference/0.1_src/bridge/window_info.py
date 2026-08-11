"""Electron 窗口信息桥接层。

将 Electron 窗口信息采集模块（TypeScript）产出的 WindowInfo 数据
规范化为 Python 端 ToolContextPlugin 可消费的统一格式。

Electron WindowInfo（TypeScript）字段：
    title, processName, x, y, width, height

ToolContextPlugin 消费格式（Python）：
    title, processName, x, y, width, height, platform

桥接层职责：
    1. 定义标准化的窗口信息数据结构
    2. 提供从原始 Electron IPC 数据到标准格式的转换
    3. 处理字段缺失、类型不匹配等边界情况

暴露接口：
- WindowInfoData：规范化窗口信息数据类
- normalize_window_info：将原始 Electron 数据转换为标准格式
- validate_window_info：校验窗口信息数据完整性
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowInfoData:
    """规范化后的窗口信息。

    与 Electron 端 WindowInfo 接口字段完全对齐：
    electron/window-info.ts -> WindowInfo

    Attributes:
        title: 窗口标题
        processName: 进程名称
        x: 窗口左上角 X 坐标
        y: 窗口左上角 Y 坐标
        width: 窗口宽度
        height: 窗口高度
    """

    title: str = ""
    processName: str = ""  # noqa: N815
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式，用于注入到 tool_context。"""
        return {
            "title": self.title,
            "processName": self.processName,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }

    @property
    def is_valid(self) -> bool:
        """窗口信息是否有效（至少有标题或进程名）。"""
        return bool(self.title or self.processName)


def normalize_window_info(raw: Any) -> WindowInfoData | None:
    """将原始 Electron 窗口信息数据规范化。

    Args:
        raw: 原始窗口信息，通常来自 ctx.state["electron_window"]

    Returns:
        规范化后的 WindowInfoData，输入无效时返回 None
    """
    if not isinstance(raw, dict):
        return None

    if not raw:
        return None

    try:
        return _from_standard_format(raw)
    except Exception as exc:
        logger.warning("[WindowInfo] 窗口信息规范化失败: %s", exc)
        return None


def _from_standard_format(raw: dict[str, Any]) -> WindowInfoData:
    """从标准 Electron WindowInfo 格式解析。

    字段：title, processName, x, y, width, height
    兼容旧格式：app → processName, bounds → x/y/width/height
    """
    process_name = raw.get("processName") or raw.get("app", "")
    # 旧格式 bounds 嵌套：bounds.x / bounds.y / bounds.width / bounds.height
    bounds = raw.get("bounds", {})
    x = raw.get("x", bounds.get("x", 0))
    y = raw.get("y", bounds.get("y", 0))
    width = raw.get("width", bounds.get("width", 0))
    height = raw.get("height", bounds.get("height", 0))

    return WindowInfoData(
        title=str(raw.get("title", "")),
        processName=str(process_name),
        x=_safe_int(x),
        y=_safe_int(y),
        width=_safe_int(width),
        height=_safe_int(height),
    )


def validate_window_info(data: WindowInfoData) -> list[str]:
    """校验窗口信息数据完整性。

    Args:
        data: 规范化后的窗口信息

    Returns:
        校验问题列表，空列表表示校验通过
    """
    issues: list[str] = []

    if not data.title and not data.processName:
        issues.append("窗口标题和进程名均为空")
    if data.width < 0:
        issues.append(f"窗口宽度为负数: {data.width}")
    if data.height < 0:
        issues.append(f"窗口高度为负数: {data.height}")

    return issues


def _safe_int(value: Any) -> int:
    """安全转换为整数，处理 None、字符串、浮点数等类型。"""
    if value is None:
        return 0
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0

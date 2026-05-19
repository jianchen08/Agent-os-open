"""桥接层模块。

提供跨组件数据格式转换和规范化能力，
确保各组件之间的数据格式兼容。

暴露接口：
- WindowInfoData：规范化窗口信息数据类
- normalize_window_info：将原始 Electron 数据转换为标准格式
- validate_window_info：校验窗口信息数据完整性
"""

from bridge.window_info import (
    WindowInfoData,
    normalize_window_info,
    validate_window_info,
)

__all__ = [
    "WindowInfoData",
    "normalize_window_info",
    "validate_window_info",
]

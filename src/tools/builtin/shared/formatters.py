"""
格式化工具函数

提供文件大小、时间等格式化功能
"""


def format_size(size_bytes: int) -> str:
    """
    格式化文件大小为人类可读格式

    Args:
        size_bytes: 文件大小（字节）

    Returns:
        人类可读的文件大小字符串，如 "1.5KB"、"2.3MB"

    Example:
        >>> format_size(1024)
        '1.0KB'
        >>> format_size(1536)
        '1.5KB'
        >>> format_size(1048576)
        '1.0MB'
    """
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"

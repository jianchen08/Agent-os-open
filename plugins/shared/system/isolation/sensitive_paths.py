"""敏感系统目录黑名单（共享常量）。

供 security_check 插件（host 模式对所有工具保留的敏感目录拦截）
和 enhanced_search 工具内部的路径校验复用，避免两处各维护一份。

暴露接口：
- SENSITIVE_DIRS_WINDOWS : Windows 敏感系统目录前缀元组（小写、正斜杠规范）
- SENSITIVE_DIRS_LINUX : Linux 敏感系统目录前缀元组
- is_sensitive_path(path: str) -> tuple[bool, str] : 判定路径是否命中黑名单
"""

from __future__ import annotations

import os
from pathlib import Path

# 敏感系统目录黑名单（不允许访问的路径前缀，小写、正斜杠规范形式）
# 与原 enhanced_search 中的 _SENSITIVE_DIRS_* 保持一致
SENSITIVE_DIRS_WINDOWS: tuple[str, ...] = (
    "c:/windows",
    "c:/windows/system32",
    "c:/windows/syswow64",
    "c:/program files",
    "c:/program files (x86)",
    "c:/$recycle.bin",
    "c:/system volume information",
)

SENSITIVE_DIRS_LINUX: tuple[str, ...] = (
    "/etc",
    "/proc",
    "/sys",
    "/boot",
    "/dev",
    "/run",
)


def is_sensitive_path(path: str) -> tuple[bool, str]:
    """判定路径是否命中敏感系统目录黑名单。

    将输入路径 resolve 后规范化为小写正斜杠形式，再与当前平台
    的敏感目录前缀做「相等或前缀」匹配。

    Args:
        path: 待判定的路径（相对或绝对）

    Returns:
        元组 (hit, matched_prefix)：
        - hit 为 True 时 matched_prefix 是命中的黑名单前缀
        - hit 为 False 时 matched_prefix 为空字符串
    """
    if not path:
        return False, ""

    try:
        resolved = str(Path(path).resolve())
    except (OSError, ValueError):
        resolved = path

    sp_lower = resolved.replace("\\", "/").lower()

    sensitive_dirs = SENSITIVE_DIRS_WINDOWS if os.name == "nt" else SENSITIVE_DIRS_LINUX

    for forbidden in sensitive_dirs:
        if sp_lower == forbidden or sp_lower.startswith(forbidden + "/"):
            return True, forbidden

    return False, ""

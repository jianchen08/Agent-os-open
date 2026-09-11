"""强制删除目录树（SDK 单一真值源）。

worktree_merge 与 isolation workspace 生命周期共用的删除原语集中于此，
兼容 Windows 下 .git 只读文件。

暴露接口：
- force_rmtree(path)：强制删除目录树（Windows 只读文件先去只读再重试）
"""

from __future__ import annotations

import os
import shutil
import stat
from typing import Any

__all__ = ["force_rmtree"]


def force_rmtree(path: str) -> None:
    """强制删除目录树，兼容 Windows 下 .git 只读文件。

    Windows 上 git objects 文件为只读属性，shutil.rmtree 默认无法删除。
    通过 onerror 回调去除只读属性后重试。
    """

    def _on_error(func: Any, filepath: str, exc_info: Any) -> None:
        if os.name == "nt":
            os.chmod(filepath, stat.S_IWRITE)  # noqa: PTH101  # pragma: no cover —— Windows 只读文件修复路径（Linux 不触 Handler）
            func(filepath)
        else:
            raise  # noqa: PLE0704  # pragma: no cover —— 非 Windows 平台分支

    try:
        shutil.rmtree(path, onerror=_on_error)
    except OSError:
        shutil.rmtree(path, onerror=_on_error)

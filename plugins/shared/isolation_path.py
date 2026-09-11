"""isolation 插件目录 sys.path 注入单源。

isolation_guard / workspace_lifecycle 等管道插件需要懒加载
``isolation.manager`` / ``isolation.workspace``（IsolationManager 与工作区根
解析所在，位于 plugins/shared/system/isolation/）。本模块为该注入的唯一实现。

裸名导入（isolation_path），调用点零改动——plugins/shared 根经
bootstrap_plugin 已在 sys.path。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["ensure_isolation_path"]


def ensure_isolation_path() -> None:
    """把 isolation 插件目录与其组根（system/）加入 sys.path。

    system/ 入列是懒加载 ``from isolation.xxx import …`` 的前提
    （isolation 包所在目录）；幂等，重复调用无害。
    """
    _system_dir = Path(__file__).resolve().parent / "system"
    _iso_dir = _system_dir / "isolation"
    for _p in (str(_system_dir), str(_iso_dir)):
        if _p not in sys.path:
            sys.path.insert(0, _p)

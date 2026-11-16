"""isolation 插件目录 sys.path 注入单源。

isolation_guard / workspace_lifecycle 等管道插件需要懒加载
``isolation.manager`` / ``isolation.workspace``（IsolationManager 与工作区根
解析所在，位于共享根的 isolation/ 包）。本模块为该注入的唯一实现。

裸名导入（isolation_path），调用点零改动——共享根经 bootstrap_plugin 已在
sys.path，``from isolation.xxx import …`` 由共享根直接解析；isolation 包内
模块另用裸名兄弟导入（如 ``from _workspace_git_ops import …``），这要求
isolation 包目录自身也在 sys.path——本模块的存在理由即此。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["ensure_isolation_path"]


def ensure_isolation_path() -> None:
    """把 isolation 包目录（及其旧布局组根）加入 sys.path。

    双布局（拍平 ADR 2026-10-13）：拍平布局 isolation 直属共享根
    （<共享根>/isolation/）；旧嵌套布局在其下 system/isolation/。按实际
    存在的目录注入，包目录入列是包内裸名兄弟导入的前提；幂等，重复调用
    无害。
    """
    _root = Path(__file__).resolve().parent
    _candidates = (
        _root / "isolation",
        _root / "system",
        _root / "system" / "isolation",
    )
    for _p in _candidates:
        if _p.is_dir() and str(_p) not in sys.path:
            sys.path.insert(0, str(_p))

"""multimodal 测试 conftest — 0.2 平铺 import 路径。

0.2 架构下多模态模块位于 plugins/shared/system/multimodal/，内部用平铺 import
（from mm_types import ...）。本 conftest 把该目录加入 sys.path。

注意：0.1 的 multimodal.types 在 0.2 重命名为 multimodal.mm_types（模块文件名也变了）。

本目录测试文件各自做了文件级路径锁定 + 裸名重载（test_disk_storage.py），
不再在此处逐出裸模块——逐出 tasks 插件已缓存的 storage/task_types 实例
会导致同进程 tasks 测试枚举实例错位。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MULTIMODAL_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "multimodal"

_s = str(_MULTIMODAL_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)

import pytest


@pytest.fixture(autouse=True)
def _multimodal_path_guard():
    """每个测试执行前确保 multimodal 目录在 sys.path[0] 并逐出裸名缓存。

    收集期模块级 import 用 sys.modules 缓存直取（不看 sys.path），
    只有 conftest 模块级逐出能挡——执行期再逐出是防函数内延迟导入
    被同会话后续模块污染。
    """
    if sys.path[0] != _s:
        sys.path.insert(0, _s)
    for _m in ("storage", "mm_types", "asr", "adapter", "capabilities"):
        sys.modules.pop(_m, None)
    yield

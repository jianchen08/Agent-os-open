"""把 pipeline 插件目录加入 sys.path（供引用 plugins.input/output/core 的测试复用）。

0.2 架构下 pipeline 插件位于 plugins/shared/pipeline/{input,output,core}/<name>/，
插件内部用平铺 import（from plugin import X）。本模块把指定插件的源目录
加入 sys.path，使 `from plugin import X` 可解析。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_PIPELINE_DIR = _SHARED_DIR / "pipeline"

# 已加入的目录（防重复）
_added: set[str] = set()

# pipeline 兼容 shim（from pipeline.plugin/types import）需要 plugins/shared/ 作为 namespace 包
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))
_added.add(str(_SHARED_DIR))


def add_plugin_dir(category: str, name: str) -> None:
    """把 plugins/shared/pipeline/<category>/<name>/ 加入 sys.path。

    多个插件目录都有 plugin.py 等同名模块，先入的会被 sys.modules 缓存，
    导致后导入的 ``from plugin import X`` 命中错误模块。故每次调用都把目标
    目录置于 sys.path 最前，并逐出已缓存的同名裸模块。

    Args:
        category: "input" | "output" | "core"
        name: 插件目录名（如 "tool_context"、"track"、"security_check"）
    """
    d = str(_PIPELINE_DIR / category / name)
    # 总是把目标目录置于 sys.path 最前（即使已存在，也要确保它在 position 0，
    # 否则先收集测试插入的其它插件目录会抢走裸名解析）。
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    _added.add(d)
    # 同名裸模块逐出：pytest 收集时各文件模块级代码按顺序执行，
    # 后收集的测试可能命中先导入的 plugin 模块缓存，必须每次逐出。
    for m in ("plugin", "tool", "types", "models"):
        sys.modules.pop(m, None)

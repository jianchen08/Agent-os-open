"""bash 工具测试公共配置。

0.2 架构下 bash 工具插件位于 ``plugins/shared/tools/bash``，其 ``server.py``
把自身目录加入 sys.path 以支持平铺 import（如 ``from tool import ...``）。
本 conftest 复刻该行为，使测试可直接 ``from tool import BashTool`` /
``from process_manager import ProcessManager`` / ``from bash_types import ...``。

注意：0.1 的 ``tools.builtin.bash.types`` 在 0.2 重命名为 ``bash_types.py``
（避免与标准库 ``types`` 冲突），故历史测试里的
``from tools.builtin.bash.types import X`` 需改写为 ``from bash_types import X``。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BASH_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "bash"

# bash 插件目录下的平铺模块名（与其它工具插件可能同名，需在切换时清理缓存）
_AMBIGUOUS_MODULES = {
    "tool",
    "process_manager",
    "bash_types",
    "encoding",
    "input_handler",
    "log_compressor",
    "result_types",
    "workspace_aware",
}


def _ensure_bash_on_path() -> None:
    d = str(_BASH_DIR)
    if not sys.path or sys.path[0] != d:
        sys.path.insert(0, d)
    for m in _AMBIGUOUS_MODULES:
        sys.modules.pop(m, None)


_ensure_bash_on_path()


import pytest


@pytest.fixture(autouse=True)
def _bash_path_guard():
    """每个测试前确保 bash 插件目录在 sys.path[0] 并逐出裸名缓存。

    模块级 _ensure_bash_on_path 只在 conftest 加载时生效——pytest 收集
    其他测试文件（test_lsp_gateway_loop 等）的模块级 import 会改写
    sys.path[0]，函数内的 `from tool import BashTool` 因此命中错误插件
    （task_submit/tool.py 等）。本 fixture 在每个测试前重置。
    """
    _ensure_bash_on_path()
    yield

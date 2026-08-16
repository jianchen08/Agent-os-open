# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""测试装配：把 builtin_tools 的 ``src`` 注入 ``sys.path``。

背景：本包源码在 ``plugins/shared/tools/builtin_tools/src/``，但 CI 的
``python-plugins-test`` 默认 ``PYTHONPATH`` 不含该 ``src``，也未
``pip install -e`` 本包，导致 ``from agentos_builtin_tools...`` 在收集期即
``ModuleNotFoundError`` —— 48 个有效测试对 CI 0 贡献。

本 conftest 与 ``server.py`` 的 ``sys.path`` 注入保持一致，使测试在本地与
CI 上均可收集运行。
"""

import os
import sys

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
)
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# 工具共享层根目录（url_security / workspace_aware 平铺模块）——
# 与 server.py 的注入保持一致，使包内平铺导入在测试下同样可解析。
_TOOLS_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if os.path.isdir(_TOOLS_ROOT) and _TOOLS_ROOT not in sys.path:
    sys.path.insert(0, _TOOLS_ROOT)

# bash 插件目录（C17，与 server.py 同步注入）：bash_tool 从 bash.tool 导入
# DANGEROUS_PATTERNS（单一事实源），其平铺依赖（bash_types 等）需本目录在
# sys.path。**append 而非 insert(0)**：bash/ 内旧版 workspace_aware.py
# （无 check_path_allowed）不得遮蔽共享层根目录的 canonical 副本，
# 否则同进程后续加载的 download 等插件会拿到错误 Mixin（pytest 合并运行）。
_BASH_DIR = os.path.join(_TOOLS_ROOT, "bash")
if os.path.isdir(_BASH_DIR) and _BASH_DIR not in sys.path:
    sys.path.append(_BASH_DIR)

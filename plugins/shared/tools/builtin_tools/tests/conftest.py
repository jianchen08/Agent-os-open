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

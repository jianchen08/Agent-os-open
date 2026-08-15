# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""plugins/shared/tools/tests 装配：sys.path 注入。

本目录全部测试共享的装配（仿 tests/test_migration.py 的 sys.path 模式集中化，
及 builtin_tools/tests/conftest.py 的 conftest 形态）：
- 仓库 SDK 源码目录 plugins/sdk/src（agentos_plugin_sdk）
- 工具共享层 plugins/shared/tools（url_security.py / workspace_aware.py 平铺模块）
- 各插件目录 download / web_ext / media（与 server.py 的 flat 导入语义一致）

工具模块加载用 importlib（唯一模块名），避免同一 pytest 进程内
其它插件 server.py 的裸名导入互相污染 sys.path。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
_SDK_DIR = Path(__file__).resolve().parents[4] / "plugins" / "sdk" / "src"
_PLUGIN_DIRS = ["download", "web_ext", "media"]

for _p in [_TOOLS_ROOT, _SDK_DIR, *(_TOOLS_ROOT / d for d in _PLUGIN_DIRS)]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

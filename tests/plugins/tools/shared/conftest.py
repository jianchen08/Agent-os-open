# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""tests/plugins/tools/shared 装配（镜像 plugins/shared/tools 的工具层测试）：
sys.path 注入 + 裸名串扰治理声明。

本目录全部测试共享的装配：
- 仓库 SDK 源码目录 plugins/sdk/src（agentos_plugin_sdk——url_security /
  workspace_aware 等共享单一真值源所在）
- 工具共享层 plugins/shared/tools（download/web_ext 等插件目录的父目录，
  供 ``web_ext.tool`` 等 PEP-420 命名空间导入）
- 各插件目录 download / web_ext / media（与 server.py 的 flat 导入语义一致）

_PLUGIN_SOURCE_DIRS 供 tests/plugins/conftest.py 的 pytest_runtest_setup
在每个测试执行前重新提升源目录并逐出裸名缓存（与 bash/ conftest 同款纪律）。

工具模块加载用 importlib（唯一模块名），避免同一 pytest 进程内
其它插件 server.py 的裸名导入互相污染 sys.path。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TOOLS_ROOT = _REPO_ROOT / "plugins" / "shared" / "tools"
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_PLUGIN_DIRS = ["download", "web_ext", "media"]

# 按优先级排序（第一个最优先），供 tests/plugins/conftest.py 治理钩子消费
_PLUGIN_SOURCE_DIRS = [
    *(str(_TOOLS_ROOT / d) for d in _PLUGIN_DIRS),
    str(_SDK_DIR),
    str(_TOOLS_ROOT),
]

for _p in [_TOOLS_ROOT, _SDK_DIR, *(_TOOLS_ROOT / d for d in _PLUGIN_DIRS)]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

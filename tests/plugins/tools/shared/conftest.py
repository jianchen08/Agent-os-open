# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""tests/plugins/tools/shared 装配（镜像 plugins/shared/tools 的工具层测试）：
sys.path 注入 + 裸名串扰治理声明。

本目录全部测试共享的装配：
- 仓库 SDK 源码目录 plugins/sdk/src（agentos_plugin_sdk——url_security /
  workspace_aware 等共享单一真值源所在）
- 插件共享根 plugins/shared（拍平布局，web_ext/media 等插件目录的
  父目录，供 ``web_ext.tool`` 等 PEP-420 命名空间导入）
- 各插件目录 web_ext / media / task_submit（与 server.py 的 flat 导入语义一致；
  task_submit 的 tool.py 拆分出 acceptance/prompts/validation 裸名同目录模块，
  运行时由 server.py 的 bootstrap_plugin 垫目录，测试装载器同契约）

_PLUGIN_SOURCE_DIRS 供 tests/plugins/conftest.py 的 pytest_runtest_setup
在每个测试执行前重新提升源目录并逐出裸名缓存（与 bash/ conftest 同款纪律）。

工具模块加载用 importlib（唯一模块名），避免同一 pytest 进程内
其它插件 server.py 的裸名导入互相污染 sys.path。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_PLUGIN_DIRS = ["web_ext", "media", "task_submit"]

# 按优先级排序（第一个最优先），供 tests/plugins/conftest.py 治理钩子消费
_PLUGIN_SOURCE_DIRS = [
    *(str(_SHARED_DIR / d) for d in _PLUGIN_DIRS),
    str(_SDK_DIR),
    str(_SHARED_DIR),
]

for _p in [_SHARED_DIR, _SDK_DIR, *(_SHARED_DIR / d for d in _PLUGIN_DIRS)]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

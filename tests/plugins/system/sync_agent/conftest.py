# @feature: FP-0.2.多端同步 2b sync_agent | @ci: python-coverage
"""tests/plugins/system/sync_agent 装配：插件目录 + plugins/shared 根入 sys.path。"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "shared" / "sync_agent"
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"

# 供 tests/plugins/conftest.py 裸名治理 hook 识别（源目录优先级）。
_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SHARED_ROOT)]

for _p in (str(_PLUGIN_DIR), str(_SHARED_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# @feature: FP-0.2.多端同步 2c 分布式路由 | @ci: python-coverage
"""tests/plugins/system/distributed_router 装配：插件目录 + plugins/shared 根入 sys.path。"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "shared" / "distributed_router"
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
# 验收车道跨插件消费路由表面（relay_store/routing_view 属 sync_relay 插件模块）。
_SYNC_RELAY_DIR = _REPO_ROOT / "plugins" / "shared" / "sync_relay"

# 供 tests/plugins/conftest.py 裸名治理 hook 识别（源目录优先级）。
_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SHARED_ROOT), str(_SYNC_RELAY_DIR)]

for _p in (str(_PLUGIN_DIR), str(_SHARED_ROOT), str(_SYNC_RELAY_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# exec 通道端侧面（exec_channel 属 sync_agent 插件模块；T4 续刀跨插件消费）。
_SYNC_AGENT_DIR = _REPO_ROOT / "plugins" / "shared" / "sync_agent"

# 供 tests/plugins/conftest.py 裸名治理 hook 识别（源目录优先级）。
_PLUGIN_SOURCE_DIRS.append(str(_SYNC_AGENT_DIR))

if str(_SYNC_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_SYNC_AGENT_DIR))

# 统一树锚定（tree_anchor 属 workspace_lifecycle 插件模块；R19 车道跨插件消费）。
_WORKSPACE_LIFECYCLE_DIR = _REPO_ROOT / "plugins" / "shared" / "workspace_lifecycle"

# 供 tests/plugins/conftest.py 裸名治理 hook 识别（源目录优先级）。
_PLUGIN_SOURCE_DIRS.append(str(_WORKSPACE_LIFECYCLE_DIR))

if str(_WORKSPACE_LIFECYCLE_DIR) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_LIFECYCLE_DIR))

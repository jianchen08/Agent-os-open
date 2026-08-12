"""连接器测试 conftest — 0.2 平铺 import 路径。

0.2 架构下连接器位于 plugins/shared/system/connectors/，内部用平铺 import
（from connector_types import ... / from vscode.connector import ...）。
本 conftest 把 connectors 目录加入 sys.path，使其内部平铺 import 可用；
同时把其父目录 system/ 也加入，以便 ``from vscode.connector import`` 解析。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONNECTORS_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "connectors"
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"

for _d in (_CONNECTORS_DIR, _SYSTEM_DIR):
    _s = str(_d)
    if _s not in sys.path:
        sys.path.insert(0, _s)


# test_integration.py 依赖 0.1 的 channels.input_adapter/output_adapter 通用接口
#（0.2 各通道各自定义，无统一抽象），需重构测试逻辑，属 Phase 1d。
collect_ignore = ["test_integration.py"]

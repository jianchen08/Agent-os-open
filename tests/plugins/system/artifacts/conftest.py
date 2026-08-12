"""artifacts 测试 conftest——把 system sidecar 父目录注入 sys.path。

插件位于 plugins/shared/system/artifacts/，但其内部用
``from artifacts.models import ...`` 风格导入（包名 artifacts），
故需把 plugins/shared/system/ 放到 sys.path，使 ``artifacts`` 包可解析。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰。
"""

from __future__ import annotations

import sys
from pathlib import Path

# artifacts 包位于 plugins/shared/system/artifacts/，所以父目录 system/ 要在 path 上
_SYSTEM_DIR = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "system"
)

_PLUGIN_SOURCE_DIRS = [str(_SYSTEM_DIR)]

_s = str(_SYSTEM_DIR)
if _s not in sys.path:
    sys.path.insert(0, _s)

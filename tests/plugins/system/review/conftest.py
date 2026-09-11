"""review 测试 conftest——把 review 插件源目录注入 sys.path。

server.py 平铺导入同目录 models/review_service/media_review_service 等
（命名空间包）。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰（models/review_service 等裸名逐测试重定向）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "review"

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR)]

for _s in _PLUGIN_SOURCE_DIRS:
    if _s not in sys.path:
        sys.path.insert(0, _s)

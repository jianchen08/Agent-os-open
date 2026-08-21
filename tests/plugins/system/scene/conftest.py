"""scene 插件 http 测试 conftest——把 scene 源目录与 system sidecar 父目录注入 sys.path。

server.py / routes_scene.py 以 ``from scene.X import ...`` 绝对导入
（scene 是命名空间包 = 插件目录本身），且 server.py 分发时 ``import
routes_scene``（相对插件目录平铺解析）→ 需 plugins/shared/system/scene/
（及命名空间包父级 plugins/shared/system/）在 path 上。

_PLUGIN_SOURCE_DIRS 暴露给 tests/plugins/conftest.py 的 pytest_runtest_setup，
治理同进程多插件裸名串扰。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "scene"
_SYSTEM_DIR = _PLUGIN_DIR.parent

_PLUGIN_SOURCE_DIRS = [str(_PLUGIN_DIR), str(_SYSTEM_DIR)]

for _s in _PLUGIN_SOURCE_DIRS:
    if _s not in sys.path:
        sys.path.insert(0, _s)

"""共享辅助：清理平铺 import 的裸模块缓存，避免跨插件测试串扰。

背景：0.2 插件内部用平铺 import（``from plugin import X``、``from models import Y``），
多个插件的 server.py / plugin.py / models.py 同名。当 pytest 在同一会话里收集多个
插件的测试时，先加载的插件会把 ``plugin`` 缓存到 sys.modules，后一个插件的
``from plugin import ...`` 会命中缓存拿到错误模块
（ImportError: cannot import name 'StuckDetector' from 'plugin' (.../tool_progress/plugin.py)）。

策略：tests/plugins/conftest.py 的 pytest_runtest_setup 在每个测试执行前，
根据该测试所在目录的 conftest 声明的源目录（``_PLUGIN_SOURCE_DIRS``），
把源目录推到 sys.path 最前，并踢掉这些裸名（连同子模块）的缓存，
使测试内的 ``from plugin import ...`` 按 sys.path[0] 重新解析到正确文件。
"""

from __future__ import annotations

import sys

# 已知的平铺模块名（跨插件可能冲突）。保守覆盖所有插件常见同名模块。
_COLLIDING_NAMES = frozenset(
    {
        "plugin",
        "server",
        "models",
        "manager",
        "reversers",
        "decorators",
        "integration",
        "artifact_service",
        "annotation_service",
        # pipeline 是 namespace 包，剔除后会被各 conftest 的 sys.path[0] 重新定位
        "pipeline",
    }
)


def evict_bare_modules() -> None:
    """踢掉可能冲突的裸模块缓存及其直接子模块。

    只清 _COLLIDING_NAMES 中的顶层名及其 ``top.sub`` 子模块，
    保留无关模块（pytest / _pytest / 测试自身包等）。
    """
    to_remove = []
    for name in list(sys.modules):
        top = name.split(".", 1)[0]
        if top in _COLLIDING_NAMES:
            to_remove.append(name)
    for name in to_remove:
        del sys.modules[name]


def promote_source_dirs(dirs: list[str]) -> None:
    """把给定源目录推到 sys.path 最前（保持参数顺序，第一个最优先）。

    已存在的条目先移除再插入到首位，确保本插件目录是 sys.path[0]。
    """
    # 先清掉已有的同名条目，避免重复
    for d in dirs:
        while d in sys.path:
            sys.path.remove(d)
    # 按「最后一个应在最前」的顺序插入头部
    for d in reversed(dirs):
        sys.path.insert(0, d)

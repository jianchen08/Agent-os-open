"""dsh_adapter 测试隔离：裸名兄弟模块跨插件劫持防护。

平铺插件布局下各插件目录内是裸名 import（server.py → ``from bridge import``、
``from manager import``、``from translator import``、``import runtime_workspace``）。
``plugins/shared/**`` 的测试不在 ``tests/conftest.py`` 的 conftest 链上，单进程
车道里先收集/执行的插件会把同名模块写入 sys.modules（典型：scene/manager.py
劫持 dsh 的 manager → server.py 中途 ImportError，后续用例 AttributeError）。

策略（身份稳定 + 会话无污染）：
- conftest 导入期按文件路径显式加载本插件兄弟模块，建立**唯一模块身份**
  ``_OWN``；加载期间临时占用裸名，完成后立刻还原现场（其它插件的同名
  模块缓存原样不动）；
- 每个用例执行前把 ``_OWN`` 装回 sys.modules，结束后还原现场——dsh 测试
  内部模块身份全程一致（monkeypatch 模块级常量对被测代码生效），也不在
  会话里永久污染其它插件的同名模块。

2026-11-04 实测：scene 测试在前时 dsh 19 红 / 64 error；本 conftest 后
dsh 单跑 391 绿、scene+dsh 454 绿。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent

# 兄弟裸模块族（server.py 及其传递依赖；顺序=依赖先序，加载期互不串味）。
_SIBLINGS = (
    "npm_semver",
    "market_index",
    "runtime_workspace",
    "translator",
    "manager",
    "bridge",
)


def _promote() -> None:
    """把本插件目录推到 sys.path 最前（幂等；不动其它条目）。"""
    target = str(_PLUGIN_DIR)
    while target in sys.path:
        sys.path.remove(target)
    sys.path.insert(0, target)


def _load_own() -> tuple[dict[str, object], dict[str, object | None]]:
    """按文件路径加载本插件兄弟模块（唯一身份）并驻留裸名槽位。

    返回 ``(own, foreign)``：own=本插件唯一身份（测试模块级 import 会绑定到
    它）；foreign=加载前槽位里的其它插件同名模块（会话结束时还原）。
    """
    foreign = {name: sys.modules.pop(name, None) for name in _SIBLINGS}
    own: dict[str, object] = {}
    for name in _SIBLINGS:
        spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / f"{name}.py")
        assert spec is not None
        assert spec.loader is not None, f"cannot load {name}.py"
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        own[name] = module
    return own, foreign


_promote()
_OWN, _FOREIGN = _load_own()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """会话结束：把加载前其它插件的同名模块缓存还原回槽位（收尾不留污染）。"""
    for name, mod in _FOREIGN.items():
        if _OWN.get(name) is not sys.modules.get(name):
            continue
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """用例执行前：装回本插件模块身份（保存现场供 teardown 还原）。"""
    item._dsh_prev_siblings = {name: sys.modules.get(name) for name in _SIBLINGS}  # type: ignore[attr-defined]
    sys.modules.update(_OWN)
    _promote()


def pytest_runtest_teardown(item: pytest.Item) -> None:
    """用例结束后：还原现场（不把本插件模块永久留在会话缓存里）。"""
    prev = getattr(item, "_dsh_prev_siblings", None)
    if prev is None:
        return
    for name, mod in prev.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod

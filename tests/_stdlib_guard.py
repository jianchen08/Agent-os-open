"""stdlib 裸名守卫——与插件同名模块劫持的统一防线。

车道共跑时，平铺插件目录会进入 sys.path 且某些导入链会把插件同名模块
（如 ``plugins/shared/pipeline/types.py``）装进 ``sys.modules["types"]``，
此后 ``from types import ...`` 按 sys.path 重解析会命中插件模块并在其包内
相对导入处炸（``attempted relative import with no known parent package``）。

``ensure_stdlib_module`` 把指定名字的 sys.modules 条目强制重置为 stdlib
本体，供根 conftest 的收集钩子与个别测试文件的模块级导入前调用。
"""

from __future__ import annotations

import importlib.util
import sys
import sysconfig
from pathlib import Path


def is_stdlib_module(mod: object) -> bool:
    """是否 stdlib 模块（裸名逐出名单内与 stdlib 同名的场景）。"""
    f = getattr(mod, "__file__", None)
    if not f:
        return True
    try:
        return Path(f).resolve().is_relative_to(
            Path(sysconfig.get_path("stdlib")).resolve()
        )
    except (OSError, ValueError):
        return False


def ensure_stdlib_module(name: str) -> None:
    """强制 ``sys.modules[name]`` 绑定 stdlib 本体（无缓存/被劫持均重装）。"""
    mod = sys.modules.get(name)
    if mod is not None and is_stdlib_module(mod):
        return
    stdlib = Path(sysconfig.get_path("stdlib"))
    path = stdlib / f"{name}.py"
    if not path.is_file():  # 名单外的包形态 stdlib 不适用本守卫
        sys.modules.pop(name, None)
        return
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    restored = importlib.util.module_from_spec(spec)
    sys.modules[name] = restored
    spec.loader.exec_module(restored)

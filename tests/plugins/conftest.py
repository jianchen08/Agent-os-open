"""tests/plugins 的 conftest——TDD 分层 marker 强制检查 + 裸模块串扰治理。

只作用于 tests/plugins/ 目录（CI 必跑范围），不对全仓库 437 个历史测试开炮。
检查规则：每个测试至少有一个分类 marker（unit/integration/e2e），
否则收集失败——这是 TDD 规范的门禁，确保分层可执行（CI 按 marker 调度）。

与 pyproject.toml 的 --strict-markers 配合：marker 名必须已注册。

另一个职责（pytest_runtest_setup）：治理 0.2 插件平铺 import 的裸名串扰。
多个插件的 plugin.py / models.py / server.py 同名，同进程批量加载会互相覆盖
sys.modules，导致后收集的插件 ``from plugin import X`` 命中错误模块。
插件测试目录的 conftest.py 在模块级暴露 ``_PLUGIN_SOURCE_DIRS: list[str]``
（插件源目录，按优先级排序），本 hook 在每个测试执行前据此把源目录推到
sys.path 最前并踢掉裸名缓存，使每个测试都解析到正确文件。
"""

from __future__ import annotations

import os
import sys

import pytest

# TDD 分层 marker 白名单：测试必须至少命中一个
_REQUIRED_LAYER_MARKERS = frozenset({"unit", "integration", "e2e"})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """收集后、执行前：检查每个测试是否有分层 marker。

    无 marker 的测试会让整个收集失败（而非静默 skip），
    强制开发者标注——这是 TDD 分层规范可落地的前提。

    作用域：仅 tests/plugins/ 下的测试。本 conftest 虽在 tests/plugins/，
    但 pytest 会把它对会话内所有收集到的 item 生效，故按 path 过滤，
    避免误伤 tests/channels/、tests/unit/ 等历史目录。
    """
    missing: list[str] = []
    for item in items:
        fspath = str(item.fspath).replace("\\", "/")
        if "/tests/plugins/" not in fspath:
            continue
        marks = {m.name for m in item.iter_markers()}
        if not (marks & _REQUIRED_LAYER_MARKERS):
            missing.append(item.nodeid)

    if missing:
        pytest.fail(
            f"TDD 分层规范：以下 {len(missing)} 个测试缺少分类 marker"
            f"（必须标注 unit/integration/e2d 之一）：\n"
            + "\n".join(f"  {n}" for n in missing[:30])
            + (f"\n  ...（共 {len(missing)} 个）" if len(missing) > 30 else "")
            + "\n\n修复：在测试文件顶部加\n"
            "  pytestmark = pytest.mark.unit      # 纯单测，零依赖\n"
            "  pytestmark = pytest.mark.integration  # 集成，多模块/sidecar\n"
            "  pytestmark = pytest.mark.e2e        # 端到端\n",
            pytrace=False,
        )


def _find_plugin_source_dirs(item: pytest.Item) -> list[str]:
    """沿测试文件目录向上找最近的、声明了 _PLUGIN_SOURCE_DIRS 的 conftest 模块。

    返回其声明的源目录列表（str 路径），未找到则返回空列表。
    """
    test_dir = os.path.dirname(str(item.fspath))
    current = test_dir
    for _ in range(10):  # 最多向上查 10 层
        conftest_path = os.path.join(current, "conftest.py")
        if os.path.isfile(conftest_path):
            # 读 conftest 模块的 _PLUGIN_SOURCE_DIRS 属性（已加载则直接取）
            # 用文件路径定位已加载的 conftest 模块
            rel = os.path.relpath(current)
            for mod in list(sys.modules.values()):
                if getattr(mod, "__file__", None) and os.path.abspath(mod.__file__) == os.path.abspath(conftest_path):
                    dirs = getattr(mod, "_PLUGIN_SOURCE_DIRS", None)
                    if dirs:
                        return list(dirs)
                    break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return []


def pytest_runtest_setup(item: pytest.Item) -> None:
    """每个测试执行前：治理裸名模块串扰。

    若测试所在目录的 conftest 声明了 _PLUGIN_SOURCE_DIRS，
    则把这些目录推到 sys.path 最前，并踢掉平铺 import 的裸名缓存，
    使 ``from plugin import ...`` 等按本插件目录重新解析。
    """
    fspath = str(item.fspath).replace("\\", "/")
    if "/tests/plugins/" not in fspath:
        return
    dirs = _find_plugin_source_dirs(item)
    if not dirs:
        return
    # 延迟导入，避免在无插件源目录的测试中加载本模块
    from tests.plugins._bare_module_evict import evict_bare_modules, promote_source_dirs

    evict_bare_modules()
    promote_source_dirs(dirs)

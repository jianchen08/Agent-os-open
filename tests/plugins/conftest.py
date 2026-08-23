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

    2026-08-23 修复：改从 pytest pluginmanager 注册表中按 __file__ 定位
    conftest 模块。原先扫 sys.modules——但**无 __init__.py 的测试目录**下
    pytest 会把 conftest 以裸名 ``conftest`` 导入，多个此类目录互相顶掉
    sys.modules['conftest'] 槽位（仅最后一个存活），monitoring/multimodal/
    isolation_guard 等目录的 conftest 因此查不到 → 钩子静默失效 →
    裸名串扰复发。pluginmanager 对每个 conftest 模块都有独立注册，
    不受 sys.modules 命名冲突影响。
    """
    test_dir = os.path.dirname(str(item.fspath))
    current = test_dir
    for _ in range(10):  # 最多向上查 10 层
        conftest_path = os.path.join(current, "conftest.py")
        if os.path.isfile(conftest_path):
            for plug in item.config.pluginmanager.get_plugins():
                plug_file = getattr(plug, "__file__", None)
                if plug_file and os.path.abspath(plug_file) == os.path.abspath(conftest_path):
                    dirs = getattr(plug, "_PLUGIN_SOURCE_DIRS", None)
                    if dirs:
                        return list(dirs)
                    break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return []


def _find_plugin_conflict_dirs(item: pytest.Item) -> list[str]:
    """同 _find_plugin_source_dirs，但取可选声明 _PLUGIN_CONFLICT_DIRS。

    冲突目录 = 含与目标插件 namespace 包同名的裸模块的目录（如
    tasks/、isolation/ 之于 system/workspace/ 包），测试期需从 sys.path
    摘除（PathFinder 普通模块优先于 namespace portion）。未声明返回空。
    """
    test_dir = os.path.dirname(str(item.fspath))
    current = test_dir
    for _ in range(10):
        conftest_path = os.path.join(current, "conftest.py")
        if os.path.isfile(conftest_path):
            for plug in item.config.pluginmanager.get_plugins():
                plug_file = getattr(plug, "__file__", None)
                if plug_file and os.path.abspath(plug_file) == os.path.abspath(conftest_path):
                    dirs = getattr(plug, "_PLUGIN_CONFLICT_DIRS", None)
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
    若声明了 _PLUGIN_CONFLICT_DIRS（可选），另把冲突目录从 sys.path 摘除
    （namespace 包被同名裸模块压制的场景，见 _bare_module_evict 注释）。
    """
    fspath = str(item.fspath).replace("\\", "/")
    if "/tests/plugins/" not in fspath:
        return
    dirs = _find_plugin_source_dirs(item)
    if not dirs:
        return
    # 延迟导入，避免在无插件源目录的测试中加载本模块
    from tests.plugins._bare_module_evict import demote_conflict_dirs, evict_bare_modules, promote_source_dirs

    evict_bare_modules()
    demote_conflict_dirs(_find_plugin_conflict_dirs(item))
    promote_source_dirs(dirs)

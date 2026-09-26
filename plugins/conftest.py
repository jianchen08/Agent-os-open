"""plugins/ 树的裸名串扰驱动（执行方案 0-5，2026-09-24）。

背景：0.2 插件树（plugins/shared/**）内是平铺 import（from plugin/models/
adapter import ...），跨插件同名。整树收集口径（``pytest tests/ plugins/``）
下，tests/ 与 tests/plugins/ 两棵树各有收集期/运行期逐出驱动（见
tests/conftest.py 的 pytest_collect_file 与 tests/plugins/conftest.py 的
pytest_runtest_setup），唯独 plugins/ 树两级驱动都缺失——先收集插件缓存的
裸名（如 rollback/models.py、channel_qq/adapter.py、duplicate_check/plugin.py）
会一直驻留，后收集插件的 ``from models import ...`` 命中错误模块
（成熟度评估 §0.3(4)b 类 9 条失败的机制根因，官方车道按单插件收集不可见）。

本 conftest 给 plugins/ 树补齐同款两级驱动：

- pytest_collect_file：每个 .py 收集导入前逐出共享裸名缓存（与 tests/
  conftest.py 同款，复用 _bare_module_evict 单一实现）；
- pytest_runtest_setup：每个测试执行前再逐出一次（运行期防御，覆盖
  「收集期干净、运行期被兄弟测试污染」的窗口）。

源目录晋升（_PLUGIN_SOURCE_DIRS）沿用既有机制：插件目录内 conftest 显式
声明（见 llm_core/review 的 conftest 范例），未声明的测试依赖自身 sys.path
注入 + 本驱动的逐出。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# sdk-test 等车道以 plugins/sdk 为 cwd 起 pytest，仓根不在 sys.path——本驱动
# 的惰性 import（tests.plugins._bare_module_evict）依赖仓根可导入，任何 cwd
# 下都显式注入（与源目录晋升同款 sys.path 注入约定）。
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _in_plugins_tree(fspath: str) -> bool:
    return "/plugins/" in fspath.replace("\\", "/")


def pytest_collect_file(file_path, parent):  # noqa: ANN001 —— pytest 钩子签名
    if file_path.suffix == ".py" and _in_plugins_tree(str(file_path)):
        from tests.plugins._bare_module_evict import evict_bare_modules

        evict_bare_modules()


def pytest_runtest_setup(item: pytest.Item) -> None:
    if not _in_plugins_tree(str(item.fspath)):
        return
    from tests.plugins._bare_module_evict import evict_bare_modules

    evict_bare_modules()

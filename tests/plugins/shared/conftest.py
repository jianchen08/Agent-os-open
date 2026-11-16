# @feature: FP-0.2.八 多租户 | @ci: python-coverage
"""tests/plugins/shared conftest — 把 ``plugins/shared`` 推上 sys.path。

本目录测试共享咽喉点 ``plugins/shared/tenant_data.py`` 等跨插件工具。
``tenant_data`` 是唯一模块名（不在 _COLLIDING_NAMES 中），但仍声明
``_PLUGIN_SOURCE_DIRS`` 与 tests/plugins/conftest.py 的裸名治理 hook 保持一致，
确保 ``from tenant_data import ...`` 解析到 ``plugins/shared/tenant_data.py``。

task_evaluate 目录一并注入：``worktree_merge``（任务域合并门控 git 机制）
为该插件内部模块，本目录机制层测试（test_worktree_merge* /
test_artifact_preservation_boundary）经此路径解析。

[来源: config/rules/testing_rules.md §9 测试追溯；tests/plugins/conftest.py 裸名治理]
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/plugins/shared/conftest.py → parents[3] = 仓库根
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_TASK_EVALUATE_DIR = _SHARED_DIR / "task_evaluate"

# 供 tests/plugins/conftest.py 的 pytest_runtest_setup 识别（源目录优先级）。
_PLUGIN_SOURCE_DIRS = [str(_SHARED_DIR)]

for _p in (_SHARED_DIR, _TASK_EVALUATE_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

"""tests/plugins 的 conftest——TDD 分层 marker 强制检查。

只作用于 tests/plugins/ 目录（CI 必跑范围），不对全仓库 437 个历史测试开炮。
检查规则：每个测试至少有一个分类 marker（unit/integration/e2e），
否则收集失败——这是 TDD 规范的门禁，确保分层可执行（CI 按 marker 调度）。

与 pyproject.toml 的 --strict-markers 配合：marker 名必须已注册。
"""

from __future__ import annotations

import pytest

# TDD 分层 marker 白名单：测试必须至少命中一个
_REQUIRED_LAYER_MARKERS = frozenset({"unit", "integration", "e2e"})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """收集后、执行前：检查每个测试是否有分层 marker。

    无 marker 的测试会让整个收集失败（而非静默 skip），
    强制开发者标注——这是 TDD 分层规范可落地的前提。
    """
    missing: list[str] = []
    for item in items:
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

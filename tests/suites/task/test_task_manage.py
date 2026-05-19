"""task_manage 单元测试 — 已标记为跳过（task_manage 工具已重构迁移）。

此测试文件引用的 tools.builtin.task_manage 模块已不存在，
对应功能已迁移到 src/tools/builtin/task/ 中。
保留测试文件结构以便后续迁移。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="tools.builtin.task_manage 模块已移除，待迁移测试用例")


class TestTaskManagePlaceholder:
    """占位测试类，确保模块可被收集。"""

    def test_placeholder(self) -> None:
        """占位测试。"""
        pass

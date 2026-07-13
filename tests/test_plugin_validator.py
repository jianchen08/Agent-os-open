"""插件验证器测试 — 已标记为跳过（验证器模块已迁移/移除）。

此测试文件引用的 tools.plugin_validator 模块已不存在，
对应功能已整合到 src/pipeline/ 和 src/plugins/ 中。
保留测试文件结构以便后续迁移。
"""

from __future__ import annotations

import pytest

# 模块已移除，跳过整个模块的全部测试
pytestmark = pytest.mark.skip(reason="tools.plugin_validator 模块已移除，待迁移测试用例")


class TestPluginValidatorPlaceholder:
    """占位测试类，确保模块可被收集。"""

    def test_placeholder(self) -> None:
        """占位测试。"""
        pass

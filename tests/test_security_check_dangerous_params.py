# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""security_check 危险工具参数级判定测试（GAP 修复）。

背景：原 `_is_dangerous_tool` 轨道 2 按"声明了 dangerous_operations 即危险"
的工具级判定，导致 file_read/file_write 常规读写（workspace 内）也 100% 弹审批。
修复后改为参数级判定：`read:/etc/`（路径前缀）、`delete_lines:`（操作参数）、
`rm -rf`（命令子串）命中才判危险。

本测试 mock 轨道 1（policy.execution）与轨道 2 数据源（builtin_tools_config），
独立验证参数级判定逻辑。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "security_check")

import plugin as sc_mod  # noqa: E402
from pipeline.plugin import PluginContext  # noqa: E402
from plugin import SecurityCheckPlugin  # noqa: E402

pytestmark = pytest.mark.unit


def _make_plugin() -> Any:
    """构造插件，mock 轨道 1（policy 非 command_in_container）+ 注入轨道 2 数据源。"""
    mock_policy = MagicMock()
    mock_policy.execution = "host_direct"
    # 直接赋值（非 with 上下文）：pytest 收集时逐出裸模块，不跨文件污染
    sc_mod._policy_loader.resolve = MagicMock(return_value=mock_policy)  # type: ignore[method-assign]
    p = SecurityCheckPlugin(config={"enabled": True, "rules": []})
    p._dangerous_ops_by_tool = {
        "file_read": ["read:/etc/", "read:/sys/", "read:C:\\Windows\\"],
        "file_write": ["write:/etc/", "write:/sys/", "write:C:\\Windows\\", "delete_lines:"],
        "bash_execute": ["rm -rf", "curl"],
    }
    return p


def _is_dangerous(p: Any, tool: str, args: dict[str, Any]) -> bool:
    ctx = PluginContext(state={}, config={})
    return p._is_dangerous_tool(ctx, tool, args)


class TestTrack2ParamMatching:
    """轨道 2：参数命中声明才危险（常规操作放行）。"""

    def test_读敏感路径命中前缀(self) -> None:
        p = _make_plugin()
        assert _is_dangerous(p, "file_read", {"path": "/etc/passwd"}) is True
        assert _is_dangerous(p, "file_read", {"path": "/sys/kernel/x"}) is True

    def test_读windows敏感目录命中(self) -> None:
        p = _make_plugin()
        assert _is_dangerous(p, "file_read", {"file_path": "C:/Windows/System32/x"}) is True

    def test_常规读写放行(self) -> None:
        p = _make_plugin()
        assert _is_dangerous(p, "file_read", {"path": "D:/myproject/x.txt"}) is False
        assert _is_dangerous(p, "file_write", {"path": "/tmp/x", "content": "hi"}) is False

    def test_写敏感路径命中(self) -> None:
        p = _make_plugin()
        assert _is_dangerous(p, "file_write", {"path": "/etc/hosts", "content": "x"}) is True

    def test_危险操作参数命中(self) -> None:
        p = _make_plugin()
        assert _is_dangerous(p, "file_write", {"operation": "delete_lines", "path": "/x"}) is True
        assert _is_dangerous(p, "file_write", {"operation": "write", "path": "/x"}) is False

    def test_非危险工具放行(self) -> None:
        p = _make_plugin()
        assert _is_dangerous(p, "enhanced_search", {"query": "x"}) is False

    def test_命令子串命中(self) -> None:
        p = _make_plugin()
        assert _is_dangerous(p, "bash_execute", {"command": "rm -rf /tmp/a"}) is True
        assert _is_dangerous(p, "bash_execute", {"command": "ls -la"}) is False

    def test_无参数不命中(self) -> None:
        p = _make_plugin()
        assert _is_dangerous(p, "file_read", {}) is False

    def test_无声明工具放行(self) -> None:
        p = _make_plugin()
        p._dangerous_ops_by_tool = {}
        assert _is_dangerous(p, "file_read", {"path": "/etc/passwd"}) is False

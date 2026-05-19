"""T1~T4 组件集成测试公共配置。

提供共享 fixture 和辅助工具：
- 创建 mock ToolRegistry
- 创建 mock PluginContext
- 加载真实 capability_adapters.yaml
- 规范化 Electron 窗口信息辅助函数
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# 将 src 目录加入 Python 搜索路径
_src = str(Path(__file__).resolve().parent.parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from pipeline.plugin import PluginContext


# ── 通用 Fixture ──────────────────────────────────────────


@pytest.fixture
def project_root() -> Path:
    """项目根目录路径。"""
    return Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def adapter_config_path(project_root: Path) -> Path:
    """T1 配置文件路径：config/capability_adapters.yaml。"""
    return project_root / "config" / "capability_adapters.yaml"


# ── ToolRegistry Mock ─────────────────────────────────────


def make_mock_registry(
    tool_names: list[str] | None = None,
    has_handlers: dict[str, bool] | None = None,
) -> MagicMock:
    """构建 mock ToolRegistry。

    Args:
        tool_names: 注册的工具名称列表
        has_handlers: 工具是否有 handler 的映射

    Returns:
        MagicMock 实例，模拟 ToolRegistry 行为
    """
    registry = MagicMock()
    mock_tools = []
    for name in tool_names or []:
        tool = MagicMock()
        tool.name = name
        mock_tools.append(tool)

    registry.list_all.return_value = mock_tools

    if has_handlers:
        registry.has_handler.side_effect = lambda n: has_handlers.get(n, False)
    else:
        registry.has_handler.return_value = True

    return registry


@pytest.fixture
def empty_registry() -> MagicMock:
    """空 ToolRegistry mock。"""
    return make_mock_registry(tool_names=[])


@pytest.fixture
def standard_registry() -> MagicMock:
    """包含标准工具的 ToolRegistry mock。"""
    return make_mock_registry(
        tool_names=["bash_execute", "file_read", "file_write"],
    )


# ── PluginContext 构造 ────────────────────────────────────


def make_ctx(
    state: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
) -> PluginContext:
    """构建测试用 PluginContext。

    Args:
        state: 初始状态字典
        services: 服务注册表

    Returns:
        配置好的 PluginContext 实例
    """
    if state is None:
        state = {}
    if services is None:
        services = {}
    return PluginContext(state=state, _services=services)


@pytest.fixture
def empty_ctx() -> PluginContext:
    """空 PluginContext。"""
    return make_ctx(state={}, services={})


# ── Electron 窗口信息数据 ─────────────────────────────────


@pytest.fixture
def electron_window_standard() -> dict[str, Any]:
    """标准 Electron WindowInfo 格式（来自 Electron IPC）。

    与 electron/window-info.ts 中 WindowInfo 接口字段一致。
    """
    return {
        "title": "test.py - Visual Studio Code",
        "processName": "Code",
        "x": 100,
        "y": 50,
        "width": 1920,
        "height": 1080,
    }


@pytest.fixture
def electron_window_legacy() -> dict[str, Any]:
    """旧格式窗口信息（用于兼容性测试）。"""
    return {
        "title": "test.py - VSCode",
        "app": "VSCode",
        "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
    }


@pytest.fixture
def electron_window_empty() -> dict[str, Any]:
    """空窗口信息（Electron 未启动）。"""
    return {}

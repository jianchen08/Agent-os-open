# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-coverage
"""resource_merge 工具 0.2 迁移 TDD 测试（hot_swap 部分随 2026-08-25 下线删除）。

迁移（FP-MIGR，F-MIGR-2）：
- resource_merge/tool.py + git_helpers.py：0.1 死依赖（tools.builtin.base /
  tools.types / tools.builtin.resource_merge.git_helpers）已删除 → SDK 类型面 +
  本目录平铺模块 git_helpers.py；ResourceMergeTool 行为（参数校验/分派）保留。

验证：
- 模块可加载（顶层 import 不再命中已删除的 0.1 模块）；
- get_tool_definition() 返回合法 SDK Tool；
- resource_merge：MISSING_ACTION / MISSING_WORKSPACE / INVALID_ACTION。

装配：conftest.py 注入 sdk / tools 共享层；本文件把 resource_merge 目录加入
sys.path（与 server.py 的 0.2 装配语义一致）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TOOLS_DIR = Path(__file__).resolve().parent.parent
_RESOURCE_MERGE_DIR = _TOOLS_DIR / "resource_merge"

if str(_RESOURCE_MERGE_DIR) not in sys.path:
    sys.path.insert(0, str(_RESOURCE_MERGE_DIR))


def _load_module(src_dir: Path, mod_name: str) -> Any:
    """按显式路径加载插件 tool 模块（唯一模块名，进程内缓存）。"""
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    module_path = src_dir / "tool.py"
    assert module_path.exists(), f"tool.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, f"cannot load {mod_name} tool.py"
    assert spec.loader is not None, f"cannot load {mod_name} tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ═══════════════════════════════════════════════════════════════
# resource_merge：迁移验证 + 参数校验
# ═══════════════════════════════════════════════════════════════


class TestResourceMergeMigration:
    """resource_merge 迁移成功：模块可 import、类型来自 agentos_plugin_sdk。"""

    @pytest.fixture
    def mod(self) -> Any:
        return _load_module(_RESOURCE_MERGE_DIR, "resource_merge_tool_under_test")

    def test_module_imports_ok(self, mod):
        """顶层 import 不再命中已删除的 0.1 模块（迁移成功）。"""
        assert mod.ResourceMergeTool is not None
        assert callable(mod.ResourceMergeTool.get_tool_definition)

    def test_definition_is_sdk_tool(self, mod):
        from agentos_plugin_sdk import Tool as SdkTool

        tool = mod.ResourceMergeTool.get_tool_definition()
        assert isinstance(tool, SdkTool)
        assert tool.name == "resource_merge"
        assert tool.category.value == "task"

    def test_execute_returns_tool_execution_result(self, mod):
        assert isinstance(mod.ResourceMergeTool(), mod.BuiltinTool)

    def test_git_helpers_rebuilt_locally(self, mod):
        """0.1 tools.builtin.resource_merge.git_helpers → 本目录平铺模块。"""
        from git_helpers import GitHelpers

        assert callable(GitHelpers.run_git)

    @pytest.mark.asyncio
    async def test_missing_action(self, mod):
        result = await mod.ResourceMergeTool().execute({})
        assert result.success is False
        assert result.error_code == "MISSING_ACTION"

    @pytest.mark.asyncio
    async def test_missing_workspace(self, mod):
        result = await mod.ResourceMergeTool().execute({"action": "git_status"})
        assert result.success is False
        assert result.error_code == "MISSING_WORKSPACE"

    @pytest.mark.asyncio
    async def test_invalid_action(self, mod):
        result = await mod.ResourceMergeTool().execute(
            {"action": "bogus", "workspace": "/tmp/ws"}
        )
        assert result.success is False
        assert result.error_code == "INVALID_ACTION"

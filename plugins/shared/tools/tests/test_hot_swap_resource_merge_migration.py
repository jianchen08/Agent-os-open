# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-coverage
"""hot_swap / resource_merge 工具 0.2 迁移 TDD 测试。

迁移（FP-MIGR，F-MIGR-2）：
1. hot_swap/tool.py：0.1 死依赖（tools.tool_context.HotSwapManager/PluginRegistry/
   RollbackManager + channels.cli.cli_main 服务注册表）已删除 → 0.2 文档化降级
   工具壳（HotSwapTool + hot_swap_func）：顶层类型走 agentos_plugin_sdk，参数校验
   错误码面保留，能力未注入时返回 HOT_SWAP_UNAVAILABLE（不静默空转）。
2. resource_merge/tool.py + git_helpers.py：0.1 死依赖（tools.builtin.base /
   tools.types / tools.builtin.resource_merge.git_helpers）已删除 → SDK 类型面 +
   本目录平铺模块 git_helpers.py；ResourceMergeTool 行为（参数校验/分派）保留。

验证：
- 模块可加载（顶层 import 不再命中已删除的 0.1 模块）；
- get_tool_definition() 返回合法 SDK Tool；
- hot_swap：MISSING_ACTION / INVALID_ACTION / 必填参数错误码 / 降级
  HOT_SWAP_UNAVAILABLE；
- resource_merge：MISSING_ACTION / MISSING_WORKSPACE / INVALID_ACTION。

装配：conftest.py 注入 sdk / tools 共享层；本文件把各插件目录加入 sys.path
（与各自 server.py 的 0.2 装配语义一致）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TOOLS_DIR = Path(__file__).resolve().parent.parent
_HOT_SWAP_DIR = _TOOLS_DIR / "hot_swap"
_RESOURCE_MERGE_DIR = _TOOLS_DIR / "resource_merge"

for _d in [_HOT_SWAP_DIR, _RESOURCE_MERGE_DIR]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


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
# hot_swap：迁移验证 + 降级行为
# ═══════════════════════════════════════════════════════════════


class TestHotSwapMigration:
    """hot_swap 迁移成功：模块可 import、类型来自 agentos_plugin_sdk。"""

    @pytest.fixture
    def mod(self) -> Any:
        return _load_module(_HOT_SWAP_DIR, "hot_swap_tool_under_test")

    def test_module_imports_ok(self, mod):
        """顶层 import 不再命中已删除的 0.1 模块（迁移成功）。"""
        assert mod.HotSwapTool is not None
        assert callable(mod.HotSwapTool.get_tool_definition)

    def test_definition_is_sdk_tool(self, mod):
        from agentos_plugin_sdk import Tool as SdkTool

        tool = mod.HotSwapTool.get_tool_definition()
        assert isinstance(tool, SdkTool)
        assert tool.name == "hot_swap"
        assert tool.category.value == "system"

    def test_execute_returns_tool_execution_result(self, mod):
        assert isinstance(mod.HotSwapTool(), mod.BuiltinTool)

    @pytest.mark.asyncio
    async def test_missing_action(self, mod):
        result = await mod.HotSwapTool().execute({})
        assert result.success is False
        assert result.error_code == "MISSING_ACTION"

    @pytest.mark.asyncio
    async def test_invalid_action(self, mod):
        result = await mod.HotSwapTool().execute({"action": "bogus"})
        assert result.success is False
        assert result.error_code == "INVALID_ACTION"

    @pytest.mark.asyncio
    async def test_swap_plugin_missing_params(self, mod):
        """swap_plugin 缺 plugin_name/new_plugin_class → 明确错误码。"""
        r1 = await mod.HotSwapTool().execute({"action": "swap_plugin"})
        assert r1.error_code == "MISSING_PLUGIN_NAME"
        r2 = await mod.HotSwapTool().execute(
            {"action": "swap_plugin", "plugin_name": "p"}
        )
        assert r2.error_code == "MISSING_NEW_PLUGIN_CLASS"

    @pytest.mark.asyncio
    async def test_rollback_plugin_missing_swap_id(self, mod):
        result = await mod.HotSwapTool().execute({"action": "rollback_plugin"})
        assert result.success is False
        assert result.error_code == "MISSING_SWAP_ID"

    @pytest.mark.asyncio
    async def test_config_ops_missing_params(self, mod):
        """save_config_version / rollback_config / list_versions 参数校验。"""
        r1 = await mod.HotSwapTool().execute({"action": "save_config_version"})
        assert r1.error_code == "MISSING_CONFIG_ID"
        r2 = await mod.HotSwapTool().execute(
            {"action": "save_config_version", "config_id": "c"}
        )
        assert r2.error_code == "MISSING_CONFIG_DATA"
        r3 = await mod.HotSwapTool().execute({"action": "rollback_config"})
        assert r3.error_code == "MISSING_VERSION_ID"
        r4 = await mod.HotSwapTool().execute({"action": "list_versions"})
        assert r4.error_code == "MISSING_CONFIG_ID"

    @pytest.mark.asyncio
    async def test_unavailable_degradation(self, mod):
        """参数合法 → 0.2 未注入热替换能力 → HOT_SWAP_UNAVAILABLE（不假装成功）。"""
        result = await mod.HotSwapTool().execute(
            {
                "action": "swap_plugin",
                "plugin_name": "input_plugin",
                "new_plugin_class": "agent_os.plugins.input.my_plugin.MyPlugin",
            }
        )
        assert result.success is False
        assert result.error_code == "HOT_SWAP_UNAVAILABLE"
        assert "0.2" in (result.error or "")


class TestHotSwapFunc:
    """hot_swap_func 纯校验路径（简单场景助手）。"""

    @pytest.fixture
    def mod(self) -> Any:
        return _load_module(_HOT_SWAP_DIR, "hot_swap_tool_under_test")

    def test_missing_action(self, mod):
        result = mod.hot_swap_func({})
        assert result["error_code"] == "MISSING_ACTION"

    def test_invalid_action(self, mod):
        result = mod.hot_swap_func({"action": "bogus"})
        assert result["error_code"] == "INVALID_ACTION"

    def test_missing_plugin_name(self, mod):
        result = mod.hot_swap_func({"action": "swap_plugin"})
        assert result["error_code"] == "MISSING_PLUGIN_NAME"

    def test_unavailable(self, mod):
        result = mod.hot_swap_func(
            {"action": "list_versions", "config_id": "pipeline" }
        )
        assert result["success"] is False
        assert result["error_code"] == "HOT_SWAP_UNAVAILABLE"


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

# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""search server.py 合宿裸名遮蔽防护测试。

裸名 ``constants`` 是多成员同名面：cost_control（同 light 组成员）的 config.py
在装载期 import constants，占据静息态槽位，且其 constants 只有 CostControl
没有 ToolLimits——loader 若不摘槽，call-time exec 的
``from constants import ToolLimits`` 必命中异成员（ImportError 连败或取值污染）。

- 单元面：decoy 常驻/不常驻两组输入下 loader 命中本目录 tool.py；取值可区分
  decoy（RESOURCE_SEARCH_DEFAULT=999 ≠ 本成员 20）不得泄入工具契约；exec
  窗口结束后静息态槽位原样恢复；缓存幂等。
- 端到端面：真实 _MemberLoader 装载持有同名 constants decoy 的邻成员 +
  本插件，先/后两种装载序下 resource_search handler 可调用且行为是本目录实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_IMPL_KEY = "resource_search_tool_impl"
_MOD_NAME = "search_server_test"

# 端到端涉及的裸名槽位（邻成员 exec 期引入 + 本插件 loader 窗口触碰的）
_BARE_NAMES = ("constants",)


def _load_server() -> Any:
    """加载 server.py（每次重建，隔离 _rs_tool_cls 模块级缓存）。"""
    if _MOD_NAME in sys.modules:
        del sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


def _plant_decoy(with_limits: bool) -> Any:
    """向裸名 ``constants`` 槽位植入异成员 decoy，返回 decoy 模块。

    with_limits=False 复刻 cost_control 真实形态（只有 CostControl，无
    ToolLimits——命中即 ImportError）；with_limits=True 携带取值可区分的
    ToolLimits（RESOURCE_SEARCH_DEFAULT=999，命中即取值污染）。
    """
    decoy = types.ModuleType("constants")

    class _ForeignCostControl:
        WARNING_THRESHOLD = 0.8

    decoy.CostControl = _ForeignCostControl
    if with_limits:

        class _ForeignToolLimits:
            RESOURCE_SEARCH_DEFAULT = 999

        decoy.ToolLimits = _ForeignToolLimits
    sys.modules["constants"] = decoy
    return decoy


@pytest.fixture
def constants_slot_guard() -> Iterator[None]:
    """裸名 constants 槽位与 sys.path 的保存/恢复护栏。"""
    saved_module = sys.modules.get("constants")
    saved_path = list(sys.path)
    yield
    sys.path[:] = saved_path
    if saved_module is not None:
        sys.modules["constants"] = saved_module
    else:
        sys.modules.pop("constants", None)


class TestCohostShadowing:
    """合宿平铺下裸名 ``constants`` 槽位被异成员占据时，loader 仍取本目录实现。"""

    @pytest.mark.parametrize("decoy_resident", [True, False])
    def test_loader_resolves_local_impl(
        self, decoy_resident: bool, constants_slot_guard: None
    ) -> None:
        """decoy 常驻/不常驻两组输入：loader 命中本目录实现，静息态槽位原样恢复。

        常驻组复刻 cost_control 真实 decoy 形态（有 CostControl 无
        ToolLimits）——不摘槽则 tool.py 的 ``from constants import ToolLimits``
        必 ImportError（sys.modules 命中优先于 sys.path，仅置顶路径挡不住）。
        """
        server = _load_server()
        decoy = _plant_decoy(with_limits=False) if decoy_resident else None

        cls = server._load_resource_search_tool()
        assert cls.__module__ == _IMPL_KEY
        assert hasattr(cls, "execute")
        # 缓存幂等：二次取用同一类（不重复 exec tool.py）
        assert server._load_resource_search_tool() is cls
        # 静息态不扰动：exec 窗口的摘除在结束后原样恢复（host loader 同款语义）
        if decoy is not None:
            assert sys.modules.get("constants") is decoy

    def test_impl_binds_local_tool_limits(self, constants_slot_guard: None) -> None:
        """ToolLimits 取值来自本成员：取值可区分 decoy（999）不得泄入工具契约。"""
        server = _load_server()
        _plant_decoy(with_limits=True)

        definition = server._load_resource_search_tool()().get_tool_definition()
        limit_prop = definition.input_schema["properties"]["limit"]
        assert limit_prop["default"] == 20
        assert limit_prop["maximum"] == 20


class TestCohostEndToEnd:
    """真实 _MemberLoader 端到端：邻成员 constants decoy 与本插件同进程合宿。

    邻成员 server.py exec 期 import constants（cost_control 装载期同机制），
    无论装载序，call-time 静息态槽位终属邻成员（宿主恢复逻辑回写）——两种
    装载序 = 两组有区分度输入；resource_search handler 必须可调用且行为是
    本目录实现（不摘槽即 ImportError，工具连败）。
    """

    @staticmethod
    def _make_neighbor_member(tmp_path: Path) -> Path:
        """构造邻成员桩：持有同名 constants decoy + 模块级 plugin 实例。"""
        neighbor_dir = tmp_path / "neighbor_member"
        neighbor_dir.mkdir()
        (neighbor_dir / "constants.py").write_text(
            "class CostControl:\n"
            "    WARNING_THRESHOLD = 0.8\n",
            encoding="utf-8",
        )
        (neighbor_dir / "server.py").write_text(
            "from constants import CostControl\n"
            "from agentos_plugin_sdk import AgentOSPlugin\n"
            "\n"
            "plugin = AgentOSPlugin('cohost_shadow_neighbor')\n"
            "plugin.register_tool('noop', {'type': 'object'}, lambda **kw: {'ok': True}, 'decoy')\n",
            encoding="utf-8",
        )
        return neighbor_dir

    @staticmethod
    def _member_loader() -> Any:
        host_path = _PLUGIN_DIR.parents[1] / "_host" / "host.py"
        host_spec = importlib.util.spec_from_file_location(
            "search_cohost_host_under_test", host_path
        )
        assert host_spec is not None
        assert host_spec.loader is not None
        host_mod = importlib.util.module_from_spec(host_spec)
        host_spec.loader.exec_module(host_mod)
        return host_mod._MemberLoader()

    @pytest.mark.parametrize("order", ["neighbor_first", "search_first"])
    def test_handler_callable_under_both_load_orders(
        self, tmp_path: Path, order: str, constants_slot_guard: None
    ) -> None:
        loader = self._member_loader()
        neighbor_dir = self._make_neighbor_member(tmp_path)
        if order == "neighbor_first":
            loader.load("cohost_shadow_neighbor", neighbor_dir)
            plugin = loader.load("resource_search_tool", _PLUGIN_DIR)
        else:
            plugin = loader.load("resource_search_tool", _PLUGIN_DIR)
            loader.load("cohost_shadow_neighbor", neighbor_dir)

        assert set(plugin._tools.keys()) == {"resource_search"}
        handler = plugin._tools["resource_search"].handler
        # 行为区分：本目录实现扫描本地清单返回表结构（命中即 tool_c ≥ 1 且
        # limit 收敛本成员 ToolLimits.RESOURCE_SEARCH_DEFAULT=20）；异成员
        # constants 命中时 handler 直接 ImportError（error 形态都到不了）
        result = asyncio.run(handler(resource_type="tool", query="download"))
        assert "error" not in result, result
        assert result.get("tool_c", 0) >= 1
        assert result["tool_c"] <= 20
        # 静息态槽位终属邻成员（宿主恢复语义），不因本插件 call-time exec 翻转
        assert sys.modules["constants"].CostControl.WARNING_THRESHOLD == 0.8

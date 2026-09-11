# @feature: FP-0.2.二 内部模块 manifest | @ci: none-local
"""simple/server.py 插件封装层补测（覆盖率 A5.3）。

覆盖：create_plugin 注册 2 工具 + on_load 能力注入（含缺失降级）、
on_load 注入的 caller 按 method 前缀路由到对应 capability、
TOOL_REGISTRY 映射、run() 启动路径。

server.py 经 importlib 显式路径 + 唯一模块名加载；加载前逐出模块
依赖的裸名（system_tools / agentos_plugin_sdk），避免污染其它测试
加载的同名模块实例。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

MOD_NAME = "simple_server_test"


@pytest.fixture
def server_mod() -> Any:
    """加载 server.py（每次重建，隔离模块级状态）。

    返回的模块上挂 _system_tools 属性指向本次加载的 system_tools 模块
    （server.py 内部 `from system_tools import ...` 会把该模块注册进
    sys.modules），供断言注入的 caller 状态使用。
    """
    if MOD_NAME in sys.modules:
        del sys.modules[MOD_NAME]
    for bare in ("system", "system_tools", "agentos_plugin_sdk"):
        sys.modules.pop(bare, None)
    spec = importlib.util.spec_from_file_location(MOD_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[MOD_NAME] = module
    spec.loader.exec_module(module)
    module._system_tools = sys.modules["system_tools"]
    return module


class _FakeCapability:
    """SDK CapabilityHandle 同形替身：记录 call 调用。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, dict]] = []
        self._fn = AsyncMock()

    async def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
        self.calls.append((method, params))
        return await self._fn(method, params)


class TestCreatePlugin:
    def test_registers_two_tools(self, server_mod: Any) -> None:
        plugin = server_mod.create_plugin()
        assert set(plugin._tools.keys()) == {"yaml_validate", "read_execution_detail"}
        # 每个工具都有 schema + handler
        for name in ("yaml_validate", "read_execution_detail"):
            assert plugin._tools[name].schema is not None
            assert callable(plugin._tools[name].handler)

    def test_registered_schemas_match_declarations(self, server_mod: Any) -> None:
        plugin = server_mod.create_plugin()
        assert plugin._tools["yaml_validate"].schema is server_mod.YAML_VALIDATE_SCHEMA
        assert plugin._tools["read_execution_detail"].schema is server_mod.READ_EXECUTION_DETAIL_SCHEMA

    def test_tool_registry_mapping(self, server_mod: Any) -> None:
        """TOOL_REGISTRY 映射到声明 schema 与函数对象。"""
        assert set(server_mod.TOOL_REGISTRY.keys()) == {"yaml_validate", "read_execution_detail"}
        assert server_mod.TOOL_REGISTRY["yaml_validate"][0] is server_mod.YAML_VALIDATE_SCHEMA
        assert server_mod.TOOL_REGISTRY["yaml_validate"][1] is server_mod.yaml_validate
        assert server_mod.TOOL_REGISTRY["read_execution_detail"][1] is server_mod.read_execution_detail


class TestOnLoad:
    async def test_on_load_injects_routing_caller(self, server_mod: Any) -> None:
        """on_load 注入的 caller 按 capability 前缀路由 method。"""
        plugin = server_mod.create_plugin()
        sr = _FakeCapability("service-registry")
        te = _FakeCapability("tool-executor")
        plugin._capabilities = {"service-registry": sr, "tool-executor": te}

        await plugin._lifecycle_handlers["on_load"]({})

        caller = server_mod._system_tools._capability_caller
        assert caller is not None
        # messages.list 不带任何能力名前缀 → 走默认 service-registry 原样转发
        # （内核按首段点号拆分 capability/method）；tool-executor.invoke 带前缀 → 剥离后转发
        await caller("messages.list", {"pipeline_id": "p1"})
        await caller("tool-executor.invoke", {"tool_name": "hindsight.recall"})
        await caller("other.method", {"x": 1})  # 无前缀 → 默认 service-registry
        assert sr.calls == [("messages.list", {"pipeline_id": "p1"}), ("other.method", {"x": 1})]
        assert te.calls == [("invoke", {"tool_name": "hindsight.recall"})]

    async def test_on_load_missing_service_registry_warns_and_returns(
        self, server_mod: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """service-registry 缺失时 on_load 直接返回，不注入 caller。"""
        plugin = server_mod.create_plugin()
        plugin._capabilities = {"tool-executor": _FakeCapability("tool-executor")}
        with caplog.at_level("WARNING"):
            await plugin._lifecycle_handlers["on_load"]({})
        assert "service-registry 缺失" in caplog.text
        assert server_mod._system_tools._capability_caller is None

    async def test_on_load_missing_both_caps(self, server_mod: Any) -> None:
        """两个能力都缺失：同样安全返回且 caller 保持未注入。"""
        plugin = server_mod.create_plugin()
        plugin._capabilities = {}
        with patch("logging.Logger.warning") as warn:
            await plugin._lifecycle_handlers["on_load"]({})
            assert warn.call_count >= 2  # 两个能力各告警一次
        assert server_mod._system_tools._capability_caller is None

    async def test_on_load_injected_caller_end_to_end(self, server_mod: Any) -> None:
        """注入后 read_execution_detail 经路由 caller 读取内核记录。"""
        plugin = server_mod.create_plugin()
        sr = _FakeCapability("service-registry")
        sr._fn.side_effect = [[{"role": "user", "content_preview": "hi", "seq_in_branch": 1}]]
        plugin._capabilities = {"service-registry": sr}
        await plugin._lifecycle_handlers["on_load"]({})

        result = await server_mod.read_execution_detail(pipeline_run_id="p1", level="L0")
        assert result["level"] == "L0"
        assert result["records"][0]["role"] == "user"
        # messages.list 原样交给 service-registry（内核按首段点号拆分能力名与方法名）
        assert sr.calls == [("messages.list", {"pipeline_id": "p1", "limit": 500})]


class TestRun:
    def test_run_starts_server(self, server_mod: Any) -> None:
        """run() 启动模块级 plugin 的阻塞入口（验证接线）。"""
        started: list[str] = []

        class _FakePlugin:
            def run(self) -> None:
                started.append("started")

        with patch.object(server_mod, "plugin", _FakePlugin()):
            assert server_mod.run() is None  # run() 无返回值，副作用是启动 MCP 服务
        assert started == ["started"]


class TestCohostContract:
    """合宿 light 组契约：host.py getattr(module, "plugin") 直接取用模块级实例。

    实例必须暴露且已在装载期携带全量工具（CohostServer 聚合 plugin._tools）；
    邻成员持有的同名裸名模块（system_tools decoy）不得遮蔽本成员的 handler。
    """

    def test_module_level_plugin_exposed_with_tools(self, server_mod: Any) -> None:
        """exec 期即暴露 AgentOSPlugin 实例且携带 2 工具；create_plugin 幂等同实例。"""
        from agentos_plugin_sdk import AgentOSPlugin

        assert isinstance(server_mod.plugin, AgentOSPlugin)
        assert set(server_mod.plugin._tools.keys()) == {"yaml_validate", "read_execution_detail"}
        assert server_mod.create_plugin() is server_mod.plugin
        # 装载期注册与工厂注册是同一 handler（无第二份注册）
        assert server_mod.plugin._tools["yaml_validate"].handler is server_mod.yaml_validate

    @staticmethod
    def _make_neighbor_member(tmp_path: Any) -> Path:
        """构造邻成员桩：持有同名 system_tools decoy + 模块级 plugin 实例。"""
        neighbor_dir = tmp_path / "neighbor_member"
        neighbor_dir.mkdir()
        (neighbor_dir / "system_tools.py").write_text(
            "async def yaml_validate(**kwargs):\n"
            "    return {'decoy': True}\n",
            encoding="utf-8",
        )
        (neighbor_dir / "server.py").write_text(
            "import system_tools\n"
            "from agentos_plugin_sdk import AgentOSPlugin\n"
            "\n"
            "plugin = AgentOSPlugin('cohost_shadow_neighbor')\n"
            "plugin.register_tool('yaml_validate', {'type': 'object'}, system_tools.yaml_validate, 'decoy')\n",
            encoding="utf-8",
        )
        return neighbor_dir

    @staticmethod
    def _member_loader() -> Any:
        host_path = _PLUGIN_DIR.parents[1] / "_host" / "host.py"
        host_spec = importlib.util.spec_from_file_location(
            "simple_cohost_host_under_test", host_path
        )
        assert host_spec is not None
        assert host_spec.loader is not None
        host_mod = importlib.util.module_from_spec(host_spec)
        host_spec.loader.exec_module(host_mod)
        return host_mod._MemberLoader()

    @pytest.mark.parametrize("order", ["neighbor_first", "simple_first"])
    def test_host_loader_returns_fully_loaded_plugin(self, tmp_path: Any, order: str) -> None:
        """真实 _MemberLoader 端到端：无论装载序，simple 都暴露带全量工具的
        plugin 实例，且 handler 是本目录实现（decoy 不遮蔽）。

        两种装载序 = 两组有区分度输入：邻成员先装载时其 decoy 曾占据
        system_tools 裸名槽位（随宿主恢复逻辑回写）；simple 先装载时 handler
        绑定先于 decoy 进入进程——两条路径都不得影响 plugin 上已冻结的绑定。
        """
        # 真实宿主进程里静息态裸名模块都属某成员（装载前必被摘除）；测试进程
        # 里对齐该不变式：先逐出本测试可见的 system_tools 残留，decoy 才能真实进场。
        popped = {name: sys.modules.pop(name) for name in ("system_tools",) if name in sys.modules}
        try:
            neighbor_dir = self._make_neighbor_member(tmp_path)
            loader = self._member_loader()
            if order == "neighbor_first":
                neighbor_plugin = loader.load("cohost_shadow_neighbor", neighbor_dir)
                plugin = loader.load("simple_tools", _PLUGIN_DIR)
                assert neighbor_plugin._tools["yaml_validate"].handler.__module__ == "system_tools"
            else:
                plugin = loader.load("simple_tools", _PLUGIN_DIR)
                loader.load("cohost_shadow_neighbor", neighbor_dir)

            assert set(plugin._tools.keys()) == {"yaml_validate", "read_execution_detail"}
            handler = plugin._tools["yaml_validate"].handler
            # 行为区分：本目录实现走真实校验语义（content 缺失 → valid=False），
            # decoy 恒返回 {'decoy': True}
            result = asyncio.run(handler(content="", schema_type="generic"))
            assert result["valid"] is False
            assert "decoy" not in result
        finally:
            for name in ("system_tools",):
                sys.modules.pop(name, None)
            sys.modules.update(popped)
            for name in [n for n in sys.modules if n.startswith("_cohost_member_")]:
                sys.modules.pop(name, None)

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
import json
import sys
import threading
import types
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class TestFailureEnvelopeExplicitSuccess:
    """handler 失败路径显式 success=False（ADR 2026-09-28 收编漏网回归）。

    裸 ``{"error": ...}`` 会被内核归一层包成 success 伪信封，LLM/前端把
    失败当成功消费。工具类经模块级缓存槽位注入桩（handler 的依赖边界即
    工具类），不触真实清单扫描。
    """

    @staticmethod
    def _stub(result: Any) -> Any:
        class _StubTool:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __call__(self) -> _StubTool:
                return self

            async def execute(self, inputs: dict[str, Any]) -> Any:
                return self._inner

        return _StubTool(result)

    def test_failure_envelope(self, monkeypatch) -> None:
        from agentos_plugin_sdk import create_failure_result

        server = _load_server()
        monkeypatch.setattr(server, "_rs_tool_cls", self._stub(create_failure_result("boom")))
        out = asyncio.run(server.resource_search(resource_type="tool"))
        assert out["success"] is False
        assert out["error"] == "boom"

    def test_success_output_passthrough(self, monkeypatch) -> None:
        from agentos_plugin_sdk import create_success_result

        server = _load_server()
        payload = {"tool_c": 3, "results": []}
        monkeypatch.setattr(server, "_rs_tool_cls", self._stub(create_success_result(payload)))
        out = asyncio.run(server.resource_search(resource_type="tool"))
        assert out == payload


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
        host_path = _PLUGIN_DIR.parents[0] / "_host" / "host.py"
        host_spec = importlib.util.spec_from_file_location(
            "search_cohost_host_under_test", host_path
        )
        assert host_spec is not None
        assert host_spec.loader is not None
        host_mod = importlib.util.module_from_spec(host_spec)
        # exec 前必须注册 sys.modules（importlib 动态执行契约，与 _load_server
        # 同款）：e76f38f3f 起模块级 @dataclass 装饰期经 sys.modules 反查所属
        # 模块命名空间，未注册即 AttributeError（'NoneType' object has no
        # attribute '__dict__'）。
        sys.modules[str(host_spec.name)] = host_mod
        host_spec.loader.exec_module(host_mod)
        return host_mod._MemberLoader()

    @pytest.mark.parametrize("order", ["neighbor_first", "search_first"])
    def test_handler_callable_under_both_load_orders(
        self, tmp_path: Path, order: str, constants_slot_guard: None,
        monkeypatch: pytest.MonkeyPatch,
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
        # T4 fail-closed 端到端：合宿 loader 只 exec server.py 不触发 on_load
        # → 对账通道未接线，工具面显式失败信封（真实清单扫描面在侧也不回落）
        pre = asyncio.run(handler(resource_type="tool", query="download"))
        assert pre.get("success") is False
        assert "未注入" in (pre.get("error") or "")

        # 注入缝装活注册表桩（外部依赖 = 内核 GET /api/v1/tools；条目名与
        # 查询词一致即可，桩值）后：行为是本目录实现（扫描面 ∩ 活列表；异
        # 成员 constants 命中时 handler 直接 ImportError，error 形态都到不了）
        impl = sys.modules["resource_search_tool_impl"]

        # 扫描面行源自持（_mini_repo 同款惯例，不依赖真仓清单内容——download
        # 退役后真仓不再有同名声明，3cd9132d8 随动债）：mini 仓 manifest 声明
        # 与桩条目、查询词同名，交集聚焦对账语义本身。
        scan_root = tmp_path / "scan_plugins_root"
        scan_plugin = scan_root / "plugins" / "download_like"
        scan_plugin.mkdir(parents=True)
        (scan_plugin / "plugin.json").write_text(json.dumps({
            "plugin_type": "tool",
            "capabilities": {"tools": [
                {"name": "download", "description": "scan-face decoy"},
            ]},
        }), encoding="utf-8")
        monkeypatch.setattr(impl, "_PROJECT_ROOT", scan_root)

        async def _stub_live() -> dict[str, Any]:
            return {"items": [
                {"name": "download", "description": "", "plugin_id": "download",
                 "category": "file", "source": "builtin"},
            ], "total": 1}

        impl.set_tool_surface_caller(_stub_live)
        result = asyncio.run(handler(resource_type="tool", query="download"))
        assert "error" not in result, result
        assert result["tool_c"] == 1
        assert result["tool_d"][0][0] == "download"
        # 静息态槽位终属邻成员（宿主恢复语义），不因本插件 call-time exec 翻转
        assert sys.modules["constants"].CostControl.WARNING_THRESHOLD == 0.8


class TestAgentServiceWiring:
    """on_load/on_unload 注入 agent 注册表服务调用方（T2 服务化接线）。

    tool-executor 句柄打桩（外部依赖），断言接线行为：on_load 后工具 agent 面
    经句柄直达 agent_manager（信封带显式 plugin_id）；on_unload 摘除后 agent 面
    显式失败（fail-closed，不回落扫描）。
    """

    def test_on_load_wires_service_and_tool_consumes_it(self, monkeypatch) -> None:
        server = _load_server()
        calls: list[tuple[str, dict]] = []

        class _FakeHandle:
            async def call(self, method: str, params: dict, timeout: float | None = None) -> dict:
                calls.append((method, dict(params)))
                return {"success": True, "data": {"items": [], "total": 0}}

        fake = _FakeHandle()
        monkeypatch.setattr(
            server.plugin, "get_capability", lambda name: fake if name == "tool-executor" else None
        )
        asyncio.run(server._on_load({}))

        tool = server._load_resource_search_tool()()
        out = asyncio.run(tool.execute({"resource_type": "agent"}))
        assert out.output == {}  # 空注册表 = 三键全缺席（空结果键条件出现）
        # bind_capability_caller 剥掉句柄命名空间前缀：句柄收到短方法 invoke，
        # 目标服务由信封内 tool_name + plugin_id 表达
        assert calls == [
            ("invoke", {
                "tool_name": "agent.list",
                "plugin_id": "agent_manager",
                "args": {},
            }),
        ]

    def test_on_unload_detaches_caller_fail_closed(self, monkeypatch) -> None:
        server = _load_server()
        monkeypatch.setattr(
            server.plugin,
            "get_capability",
            lambda name: (_ for _ in ()).throw(KeyError(f"capability not injected: {name}")),
        )
        # on_load 本身不读句柄（协程内懒解析），注入的调用方在调用时才显式失败
        asyncio.run(server._on_load({}))
        tool = server._load_resource_search_tool()()
        failed = asyncio.run(tool.execute({"resource_type": "agent"}))
        assert failed.success is False

        asyncio.run(server._on_unload({}))
        detached = asyncio.run(tool.execute({"resource_type": "agent"}))
        assert detached.success is False
        assert "未注入" in (detached.error or "")


class TestSkillServiceWiring:
    """on_load/on_unload 注入 skill 注册表服务调用方（T3 服务化接线）。

    provides namespace ``skill`` 句柄打桩（外部依赖），断言接线行为：on_load
    后工具 skill 面经句柄直连 skill_manager（裸 dict 契约，短方法名）；
    on_unload 摘除后 skill 面显式失败（fail-closed，不回落扫描）。
    """

    def test_on_load_wires_skill_service_and_tool_consumes_it(self, monkeypatch) -> None:
        server = _load_server()
        calls: list[tuple[str, dict]] = []

        class _FakeHandle:
            async def call(self, method: str, params: dict, timeout: float | None = None) -> dict:
                calls.append((method, dict(params)))
                return {"items": []}

        fake = _FakeHandle()
        monkeypatch.setattr(
            server.plugin, "get_capability", lambda name: fake if name == "skill" else None
        )
        asyncio.run(server._on_load({}))

        tool = server._load_resource_search_tool()()
        out = asyncio.run(tool.execute({"resource_type": "skill"}))
        assert out.output == {}  # 空注册表 = 三键全缺席（空结果键条件出现）
        # handle 直连：句柄收到短方法名（ns 前缀由 SDK call_fn 拼接）
        assert calls == [("list", {})]

    def test_on_unload_detaches_skill_caller_fail_closed(self, monkeypatch) -> None:
        server = _load_server()
        monkeypatch.setattr(
            server.plugin,
            "get_capability",
            lambda name: (_ for _ in ()).throw(KeyError(f"capability not injected: {name}")),
        )
        asyncio.run(server._on_load({}))
        tool = server._load_resource_search_tool()()
        failed = asyncio.run(tool.execute({"resource_type": "skill"}))
        assert failed.success is False

        asyncio.run(server._on_unload({}))
        detached = asyncio.run(tool.execute({"resource_type": "skill"}))
        assert detached.success is False
        assert "未注入" in (detached.error or "")


class TestToolSurfaceWiring:
    """on_load/on_unload 接线工具面对账通道（T4 服务化接线）。

    内核 HTTP 通道（_fetch_live_tools）打桩（外部依赖），断言接线行为：
    on_load 后工具面 = manifest 扫描 ∩ 活列表（行源=扫描，对账键=工具名）；
    on_unload 摘除后工具面显式失败（fail-closed，不回落扫描）。
    """

    @staticmethod
    def _mini_repo(tmp_path: Path) -> Path:
        d = tmp_path / "plugins" / "tool_a"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps({
            "plugin_type": "tool",
            "capabilities": {"tools": [
                {"name": "download_file", "description": "download remote files"},
            ]},
        }), encoding="utf-8")
        return tmp_path

    def test_on_load_wires_surface_and_tool_consumes_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        server = _load_server()
        fetches: list[int] = []

        def fake_fetch() -> dict[str, Any]:
            fetches.append(1)
            # GET /api/v1/tools 真实响应形态（routes.rs tools_handler 信封 +
            # 条目五字段）；ghost 条目扫描面没有 → 对账交集天然剔除
            return {"items": [
                {"name": "download_file", "description": "LIVE DESC",
                 "plugin_id": "tool_a", "category": "file", "source": "builtin"},
                {"name": "ghost_tool", "description": "", "plugin_id": "elsewhere",
                 "category": "web", "source": "builtin"},
            ], "total": 2}

        monkeypatch.setattr(server, "_fetch_live_tools", fake_fetch)
        asyncio.run(server._on_load({}))  # 懒载 impl 并接线
        monkeypatch.setattr(
            sys.modules["resource_search_tool_impl"], "_PROJECT_ROOT", self._mini_repo(tmp_path)
        )

        tool = server._load_resource_search_tool()()
        out = asyncio.run(tool.execute({"resource_type": "tool"}))
        # 行源 = manifest 扫描（描述取 plugin.json 而非活条目），对账键 = 工具名
        assert out.output == {
            "tool_h": ["tool_name", "tool_description"],
            "tool_d": [["download_file", "download remote files"]],
            "tool_c": 1,
        }
        assert len(fetches) == 1

    def test_on_unload_detaches_tool_surface_fail_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        server = _load_server()
        monkeypatch.setattr(
            sys.modules["resource_search_tool_impl"], "_PROJECT_ROOT", self._mini_repo(tmp_path)
        )
        asyncio.run(server._on_load({}))
        asyncio.run(server._on_unload({}))

        tool = server._load_resource_search_tool()()
        detached = asyncio.run(tool.execute({"resource_type": "tool"}))
        assert detached.success is False
        assert "未注入" in (detached.error or "")
        assert detached.error_code == "tool_surface_unavailable"


class TestFetchLiveTools:
    """_fetch_live_tools 内核 HTTP 通道（登录链 + 端点信封透传）。

    本地 ThreadingHTTPServer 替身（mode_evolution test_ledger_contract_reads
    同款手法）：断言 admin 登录换 Bearer 的鉴权链与 ``{items, total}`` 透传；
    环境未设 AGENTOS_ADMIN_PASSWORD = 显式失败（不内置默认凭据）。
    """

    @staticmethod
    def _stub_server(seen_auth: list[str], login_body: str | None = None) -> ThreadingHTTPServer:
        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # 静默访问日志
                pass

            def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 命名约定
                assert self.path == "/api/v1/auth/login"
                self._send(login_body or json.dumps({"access_token": "tok-123", "token_type": "bearer"}))

            def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 命名约定
                assert self.path == "/api/v1/tools"
                seen_auth.append(self.headers.get("Authorization") or "")
                self._send(json.dumps({"items": [
                    {"name": "download_file", "description": "d", "plugin_id": "p",
                     "category": "file", "source": "builtin"},
                ], "total": 1}))

            def _send(self, body: str) -> None:
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return ThreadingHTTPServer(("127.0.0.1", 0), _Handler)

    @staticmethod
    def _serve_with_env(
        monkeypatch: Any, stub: ThreadingHTTPServer
    ) -> None:
        monkeypatch.setenv("AGENTOS_KERNEL_PORT", str(stub.server_address[1]))
        monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "pw")

    def test_login_chain_and_envelope_passthrough(self, monkeypatch) -> None:
        server = _load_server()
        seen_auth: list[str] = []
        stub = self._stub_server(seen_auth)
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        try:
            self._serve_with_env(monkeypatch, stub)
            payload = server._fetch_live_tools()
        finally:
            stub.shutdown()
            stub.server_close()
        assert payload["items"][0]["name"] == "download_file"
        assert payload["total"] == 1
        assert seen_auth == ["Bearer tok-123"]

    def test_login_response_without_token_is_failure(self, monkeypatch) -> None:
        server = _load_server()
        stub = self._stub_server([], login_body=json.dumps({"error": "口令不匹配"}))
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        try:
            self._serve_with_env(monkeypatch, stub)
            with pytest.raises(RuntimeError, match="access_token"):
                server._fetch_live_tools()
        finally:
            stub.shutdown()
            stub.server_close()

    def test_missing_password_env_fails_closed(self, monkeypatch) -> None:
        server = _load_server()
        monkeypatch.delenv("AGENTOS_ADMIN_PASSWORD", raising=False)
        with pytest.raises(RuntimeError, match="AGENTOS_ADMIN_PASSWORD"):
            server._fetch_live_tools()

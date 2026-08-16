# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""dsh_adapter 插件测试（task_dsh_plugin_adapter 任务 2 + 4）。

分层：
- translator：纯函数（package.json/dsh 声明解析、toolview 扫描、失败隔离）；
- bridge：协议层（mock Node runtime 脚本验证 JSON-RPC 帧/超时/错误映射）；
- plugin.json 契约一致性（manifest 声明 = server.py @tool 注册）；
- e2e（真实 DSH Node runtime）：默认跳过（需 DSH 仓库构建产物），
  ``AGENTOS_DSH_E2E=1`` 启用——验证 dsh_read/dsh_glob 全链路。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "system" / "dsh_adapter"
sys.path.insert(0, str(PLUGIN_DIR))

from bridge import DshRuntimeBridge  # noqa: E402
from translator import (  # noqa: E402
    DSH_SOURCE_COMMIT,
    discover_dsh_plugins,
    dsh_params_to_json_schema,
    load_installed_plugins,
    to_lingxi_tool_entry,
    translate_package,
    translate_packages,
)

# ── translator：纯函数 ─────────────────────────────────────────────────


class TestParamsToSchema:
    def test_dsl_to_json_schema(self):
        schema = dsh_params_to_json_schema({
            "file_path": {"type": "string", "required": True, "description": "Path"},
            "offset": {"type": "number", "description": "1-based"},
        })
        assert schema["type"] == "object"
        assert schema["required"] == ["file_path"]
        assert schema["properties"]["file_path"] == {"type": "string", "description": "Path"}
        assert "required" not in schema["properties"]["offset"]

    def test_empty_params(self):
        assert dsh_params_to_json_schema(None) == {"type": "object", "properties": {}}
        assert dsh_params_to_json_schema({}) == {"type": "object", "properties": {}}


class TestTranslatePackage:
    @pytest.fixture
    def client_pkg(self, tmp_path: Path) -> Path:
        """构造一个 DSH client 插件包样例（toolview 注册代码 + dsh.client 声明）。"""
        pkg = tmp_path / "ui-mock"
        (pkg / "src" / "client").mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({
            "name": "@deepseek-ai/dsh-client-ui-mock",
            "version": "0.1.0-rc.5",
            "dsh": {"client": {"platform": "web", "inject": ["@deepseek-ai/dsh-client-runtime"]}},
        }), encoding="utf-8")
        (pkg / "src" / "client" / "read-row.tsx").write_text(
            "export const readToolview = {\n"
            "  inject: ['slots'],\n"
            "  apply(ctx) {\n"
            "    ctx.slots.register({ name: 'tool.call.toolview', key: 'read', locale: NS }, ReadRow)\n"
            "  },\n"
            "}\n",
            encoding="utf-8",
        )
        return pkg

    def test_client_package_translation(self, client_pkg: Path):
        m = translate_package(client_pkg)
        assert m["source"]["package"] == "@deepseek-ai/dsh-client-ui-mock"
        assert m["source"]["kind"] == "dsh-plugin"
        assert m["source"]["dsh"]["vendor_pinned"]["commit"] == DSH_SOURCE_COMMIT
        assert m["client"]["is_client_plugin"] is True
        assert m["client"]["renderers"] == [{"tool": "read", "source": "src/client/read-row.tsx"}]
        assert m["warnings"] == []

    def test_non_plugin_dir_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="no package.json"):
            translate_package(tmp_path / "nope")

    def test_bad_json_raises(self, tmp_path: Path):
        p = tmp_path / "bad"
        p.mkdir()
        (p / "package.json").write_text("{broken", encoding="utf-8")
        with pytest.raises(ValueError, match="bad package.json"):
            translate_package(p)

    def test_library_package_no_client(self, tmp_path: Path):
        p = tmp_path / "lib"
        p.mkdir()
        (p / "package.json").write_text(json.dumps({"name": "x", "version": "1"}), encoding="utf-8")
        m = translate_package(p)
        assert m["client"]["is_client_plugin"] is False
        assert m["client"]["renderers"] == []

    def test_batch_failure_isolation(self, tmp_path: Path):
        good = tmp_path / "good"
        good.mkdir()
        (good / "package.json").write_text(json.dumps({"name": "g"}), encoding="utf-8")
        out = translate_packages([good, tmp_path / "missing"])
        assert len(out["packages"]) == 1
        assert len(out["errors"]) == 1
        assert out["errors"][0]["package"] == str(tmp_path / "missing")

    @pytest.fixture
    def npm_pkg(self, tmp_path: Path) -> Path:
        """npm pack 产物形态：无 src/，lib/client.js 保留 slots.register（打包后
        name/key 相邻），CSS 被 stub——task 验证翻译器吃真实下载包。"""
        pkg = tmp_path / "dsh-client-ui-tool-npm"
        (pkg / "lib").mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({
            "name": "@deepseek-ai/dsh-client-ui-tool",
            "version": "0.0.1-rc.1",
            "dsh": {"client": {"platform": "web", "inject": ["@deepseek-ai/dsh-client-runtime"]}},
        }), encoding="utf-8")
        (pkg / "lib" / "client.js").write_text(
            "ctx.slots.inject('tool.call.toolview', () => ctx.slots.register({\n" +
            "  name: 'tool.call.toolview',\n" +
            "  key: 'bash',\n" +
            '  locale: CONVERSATION_NS\n' +
            '}, BashRow));\n' +
            'yield ctx.slots.register({\n' +
            "  name: 'tool.call.toolview',\n" +
            "  key: 'edit',\n" +
            '  locale: CONVERSATION_NS\n' +
            '}, FileMutationRow);\n',
            encoding="utf-8",
        )
        return pkg

    def test_npm_lib_artifact_scanned(self, npm_pkg: Path):
        """npm 构建产物（lib/*.js）的 toolview 键可扫出（下载包无 src）。"""
        m = translate_package(npm_pkg)
        assert m["client"]["is_client_plugin"] is True
        keys = [r["tool"] for r in m["client"]["renderers"]]
        assert keys == ["bash", "edit"]
        assert m["client"]["renderers"][0]["source"] == "lib/client.js"
        # 来源版本记录：包自身版本 + vendor 锁定基线分开
        assert m["source"]["version"] == "0.0.1-rc.1"
        assert m["source"]["dsh"]["vendor_pinned"]["commit"] == DSH_SOURCE_COMMIT
        assert m["source"]["dsh"]["vendor_pinned"]["package_version"] == "0.1.0-rc.5"


class TestToLingxiToolEntry:
    def test_contract_passthrough(self):
        entry = to_lingxi_tool_entry({
            "name": "read",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {"type": "object", "required": ["path"]},
            "render": {"card": "read"},
        })
        assert entry["name"] == "read"
        assert entry["output_schema"]["required"] == ["path"]
        assert entry["render"] == {"card": "read"}

    def test_dsl_fallback(self):
        entry = to_lingxi_tool_entry({
            "name": "x",
            "description": "",
            "parameters": {"a": {"type": "string", "required": True}},
        })
        assert entry["input_schema"]["required"] == ["a"]
        assert "output_schema" not in entry


# ── plugin.json 契约一致性 ─────────────────────────────────────────────


class TestManifestContract:
    @pytest.fixture
    def manifest(self) -> dict:
        return json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    def test_bridge_tools_declared_with_output_and_render(self, manifest: dict):
        tools = {t["name"]: t for t in manifest["capabilities"]["tools"]}
        for name in ("dsh_read", "dsh_glob"):
            t = tools[name]
            assert t.get("output_schema"), f"{name} 必须声明 output_schema（任务 1 消费端闭环）"
            assert t.get("render", {}).get("card") in ("read", "search", "terminal", "diff", "web", "generic"), name

    def test_server_registry_matches_manifest(self, manifest: dict):
        """server.py 的 @plugin.tool 注册面 = plugin.json 声明面（防漂移）。"""
        # 显式路径 + 唯一模块名加载（同 test_migration._load_simple_server 约定）：
        # 裸 `import server` 会被同 pytest 进程里其它插件目录（后插入 sys.path[0]，
        # 如 memory）的 server.py 劫持，导致合并运行时误判注册面漂移。
        import importlib.util as _ilu

        mod_name = "dsh_adapter_server_under_test"
        if mod_name not in sys.modules:
            spec = _ilu.spec_from_file_location(mod_name, PLUGIN_DIR / "server.py")
            assert spec is not None, "cannot load dsh_adapter server.py"
            assert spec.loader is not None, "cannot load dsh_adapter server.py"
            module = _ilu.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        server = sys.modules[mod_name]

        registered = set(server.plugin._tools.keys())  # noqa: SLF001
        declared = {t["name"] for t in manifest["capabilities"]["tools"]}
        assert declared <= registered, f"manifest 声明未注册: {declared - registered}"
        # 契约字段一致
        for t in manifest["capabilities"]["tools"]:
            td = server.plugin._tools[t["name"]]  # noqa: SLF001
            if t.get("render"):
                assert td.render == t["render"], t["name"]
            if t.get("output_schema"):
                assert td.output_schema == t["output_schema"], t["name"]

    def test_contributes_frontend_contract(self, manifest: dict):
        c = manifest["contributes"]
        assert c["dsh_adapter"]["source_commit"] == DSH_SOURCE_COMMIT
        assert {r["tool"] for r in c["renderers"]} == {"dsh_read", "dsh_glob"}


# ── bridge：协议层（mock Node runtime） ────────────────────────────────

MOCK_RUNTIME = r"""
import { createInterface } from 'node:readline'
const rl = createInterface({ input: process.stdin })
rl.on('line', (line) => {
  const text = line.trim()
  if (!text) return
  const msg = JSON.parse(text)
  let resp
  if (msg.method === 'initialize') {
    resp = { jsonrpc: '2.0', id: msg.id, result: { serverInfo: { name: 'mock' }, tools: [{ name: 'read' }] } }
  } else if (msg.method === 'tool/call') {
    if (msg.params.name === 'boom') {
      resp = { jsonrpc: '2.0', id: msg.id, result: { success: false, data: null, error: 'unknown tool: boom', duration_ms: 0 } }
    } else if (msg.params.name === 'hang') {
      return // 永不回复（超时路径）
    } else {
      resp = { jsonrpc: '2.0', id: msg.id, result: { success: true, data: { echoed: msg.params.args }, error: null, duration_ms: 1.0 } }
    }
  } else if (msg.method === 'shutdown') {
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: msg.id, result: {} }) + '\n')
    process.exit(0)
  } else {
    resp = { jsonrpc: '2.0', id: msg.id, error: { code: -32000, message: 'unknown method' } }
  }
  process.stdout.write(JSON.stringify(resp) + '\n')
})
"""


@pytest.fixture
def mock_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DshRuntimeBridge:
    """把 runtime 脚本替换为 mock，仓库根指向临时目录。"""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "dsh-rpc-bridge.mjs").write_text(MOCK_RUNTIME, encoding="utf-8")
    repo = tmp_path / "dsh-repo"
    repo.mkdir()
    import bridge as bridge_mod  # noqa: PLC0415

    monkeypatch.setattr(bridge_mod, "_RUNTIME_SCRIPT", runtime_dir / "dsh-rpc-bridge.mjs")
    return DshRuntimeBridge(repo_root=str(repo), cwd=str(tmp_path), boot_timeout_s=15, call_timeout_s=5)


class TestBridgeProtocol:
    def test_initialize_lists_tools(self, mock_bridge: DshRuntimeBridge):
        tools = asyncio.run(mock_bridge.initialize())
        assert [t["name"] for t in tools] == ["read"]

    def test_call_tool_roundtrip(self, mock_bridge: DshRuntimeBridge):
        out = asyncio.run(mock_bridge.call_tool("read", {"file_path": "x"}))
        assert out["success"] is True
        assert out["data"] == {"echoed": {"file_path": "x"}}

    def test_error_mapping(self, mock_bridge: DshRuntimeBridge):
        out = asyncio.run(mock_bridge.call_tool("boom", {}))
        # 工具级错误经 result.error 呈现（mjs 侧 tool/call 总回 result）
        assert out["success"] is False
        assert "unknown tool" in (out.get("error") or "")

    def test_request_level_error_raises(self, mock_bridge: DshRuntimeBridge):
        with pytest.raises(RuntimeError, match="dsh bridge error"):
            asyncio.run(mock_bridge._request("bogus/method", {}, timeout_s=5))  # noqa: SLF001

    def test_timeout_returns_failure_envelope(self, mock_bridge: DshRuntimeBridge):
        with pytest.raises(TimeoutError):
            asyncio.run(mock_bridge.call_tool("hang", {}))

    def test_missing_repo_fails_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import bridge as bridge_mod  # noqa: PLC0415

        monkeypatch.setattr(bridge_mod, "_RUNTIME_SCRIPT", tmp_path / "nope.mjs")
        b = DshRuntimeBridge(repo_root=str(tmp_path))
        out = asyncio.run(b.call_tool("read", {}))
        assert out["success"] is False
        assert "missing" in out["error"]

    def test_shutdown_terminates_process(self, mock_bridge: DshRuntimeBridge):
        async def scenario() -> None:
            await mock_bridge.initialize()
            proc = mock_bridge._proc  # noqa: SLF001
            assert proc is not None
            await mock_bridge.shutdown()
            assert mock_bridge._proc is None  # noqa: SLF001
            assert proc.returncode is not None

        asyncio.run(scenario())


# ── e2e：真实 DSH Node runtime（默认跳过） ─────────────────────────────

E2E = os.environ.get("AGENTOS_DSH_E2E") == "1"


@pytest.mark.skipif(not E2E, reason="需 DSH 仓库构建产物（AGENTOS_DSH_E2E=1 启用）")
class TestRealRuntimeE2E:
    def test_read(self):
        async def scenario() -> dict:
            b = DshRuntimeBridge()
            try:
                return await b.call_tool("read", {"file_path": str(PLUGIN_DIR / "plugin.json"), "limit": 5})
            finally:
                await b.shutdown()

        out = asyncio.run(scenario())
        assert out["success"] is True, out
        data = out["data"]
        assert data["path"].endswith("plugin.json")
        assert data["lines"][0]["number"] == 1
        assert data["totalLines"] > 5

    def test_glob(self):
        async def scenario() -> dict:
            b = DshRuntimeBridge()
            try:
                return await b.call_tool("glob", {"pattern": "*.json", "path": str(PLUGIN_DIR)})
            finally:
                await b.shutdown()

        out = asyncio.run(scenario())
        assert out["success"] is True, out
        # paths 相对 DSH 工作区（桥 cwd = 进程 cwd），断言后缀
        assert any(p.replace("\\", "/").endswith("dsh_adapter/plugin.json") for p in out["data"]["paths"]), out["data"]


# ── dsh_plugins/ 装载（真实 npm 下载包，离线验证） ─────────────────────


class TestInstalledDshPlugins:
    """适配器目录下已放置的 DSH 插件包被发现并正确翻译（装载即生效）。"""

    def test_two_packages_discovered(self):
        packages = discover_dsh_plugins()
        names = {p.name for p in packages}
        assert names == {"ui-primitives", "ui-tool"}, names

    def test_ui_tool_translated_with_renderers(self):
        loaded = load_installed_plugins()
        assert loaded["count"] == 2
        by_name = {p["source"]["package"]: p for p in loaded["packages"]}
        ui_tool = by_name["@deepseek-ai/dsh-client-ui-tool"]
        assert ui_tool["client"]["is_client_plugin"] is True
        keys = {r["tool"] for r in ui_tool["client"]["renderers"]}
        # npm 0.0.1-rc.1 lib/client.js 实测的 toolview 键
        assert {"read", "bash", "edit", "write", "grep", "glob"} <= keys, keys

    def test_ui_primitives_is_library_not_plugin(self):
        loaded = load_installed_plugins()
        by_name = {p["source"]["package"]: p for p in loaded["packages"]}
        prim = by_name["@deepseek-ai/dsh-client-ui-primitives"]
        assert prim["client"]["is_client_plugin"] is False
        assert prim["client"]["renderers"] == []

    def test_manifest_declares_list_tool(self):
        import json as _json  # noqa: PLC0415

        manifest = _json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        names = {t["name"] for t in manifest["capabilities"]["tools"]}
        assert "dsh_list_plugins" in names
        assert manifest["plugin_type"] == "system"
        # D.6 槽位拆分：声明即注册，无类型豁免字段
        assert "llm_tools" not in manifest

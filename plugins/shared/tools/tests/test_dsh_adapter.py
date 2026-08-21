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
import base64
import json
import re
import os
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "system" / "dsh_adapter"
sys.path.insert(0, str(PLUGIN_DIR))

from bridge import DshRuntimeBridge  # noqa: E402
from translator import (  # noqa: E402
    DSH_SOURCE_COMMIT,
    describe_available_skins,
    skins_to_plugin_themes,
    DSH_SOURCE_VERSION,
    discover_dsh_plugins,
    dsh_params_to_json_schema,
    list_available_skins,
    load_installed_plugins,
    load_plugin_config,
    load_skin_selection,
    map_dsh_slot,
    resolve_skin_background,
    resolve_skin_css,
    to_lingxi_tool_entry,
    translate_hooks_config,
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
        # 组件映射翻译：DSH 键 → 灵汐组件 + render 卡（映射表单一事实源）
        assert m["client"]["renderers"] == [{
            "tool": "read",
            "source": "src/client/read-row.tsx",
            "dsh_component": "ReadBlock",
            "lingxi_component": "ReadBlock",
            "card": "read",
        }]
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
            '}, FileMutationRow);\n' +
            "ctx.slots.inject('conversation.chat.node', () => ctx.slots.register({\n" +
            "  name: 'conversation.chat.node',\n" +
            "  key: 'tool-call',\n" +
            "}, ToolCallTree));\n",
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
        # 槽位语义映射：DSH slot → 灵汐槽位（sidebar→sidebar 式翻译）
        slots = m["client"]["slots"]
        assert slots["tool.call.toolview"]["lingxi_slot"] == "chatMessages"
        assert slots["conversation.chat.node"]["lingxi_slot"] == "chatMessages"
        # 映射表内：详情面板 → 浮窗
        assert map_dsh_slot("conversation.details.tool")["lingxi_slot"] == "floating"
        # 未收录槽位回退 direct（灵汐无对应 → 直接渲染，诚实边界）
        assert map_dsh_slot("conversation.trajectory")["lingxi_slot"] == "direct"
        # 来源版本记录：包自身版本 + vendor 锁定基线分开
        assert m["source"]["version"] == "0.0.1-rc.1"
        assert m["source"]["dsh"]["vendor_pinned"]["commit"] == DSH_SOURCE_COMMIT
        assert m["source"]["dsh"]["vendor_pinned"]["package_version"] == DSH_SOURCE_VERSION

    def test_dsh_dir_client_scanned(self, tmp_path: Path):
        """modlens 布局（client 面在 dsh/*.js）的 settings 槽位可扫出（registry 视觉类）。"""
        pkg = tmp_path / "modlens"
        (pkg / "dsh").mkdir(parents=True)
        (pkg / "package.json").write_text(
            json.dumps({
                "name": "@liustack/modlens",
                "version": "3.17.3",
                "dsh": {"bundle": {"patch": "./cordis.patch.yml"}, "client": {"platform": "web", "immediately": True}},
            }),
            encoding="utf-8",
        )
        (pkg / "dsh" / "client.js").write_text(
            "yield ctx.slots.register({ name: 'settings.plugin.item', id: 'modlens', order: 30 }, Card);\n",
            encoding="utf-8",
        )
        m = translate_package(pkg)
        assert m["client"]["is_client_plugin"] is True
        slots = m["client"]["slots"]
        assert "settings.plugin.item" in slots
        assert slots["settings.plugin.item"]["lingxi_slot"] == "settingsPanels"
        assert m["client"]["renderers"] == []

    def test_extra_tools_flag(self, tmp_path: Path):
        """含 lib/index.js 的工具包被标记为通道 A 可装载（extra-tools）。"""
        pkg = tmp_path / "tool-time"
        (pkg / "lib").mkdir(parents=True)
        (pkg / "lib" / "index.js").write_text("export const apply = () => {};\n", encoding="utf-8")
        (pkg / "package.json").write_text(
            json.dumps({"name": "@deepseek-ai/dsh-tool-time", "version": "0.0.1"}),
            encoding="utf-8",
        )
        m = translate_package(pkg)
        assert m["backend"]["extra_tools"] is True
        # 无 lib/ 的包（服务类等）不标
        (pkg / "lib").rename(tmp_path / "lib-backup")
        m2 = translate_package(pkg)
        assert m2["backend"]["extra_tools"] is False


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


# ── dsh_plugins/ 装载（真实 registry 插件包，离线验证） ────────────────


class TestInstalledDshPlugins:
    """适配器目录下已放置的 DSH 插件包被发现并正确翻译（装载即生效）。

    2026-08-16 起 dsh_plugins/ 清空过 npm 演示包；随后放入 registry 真实包
    （dsh-tool-time / dsh-hooks / dsh-interconnect / modlens）做四类试装。
    空目录行为用 base_dir 隔离验证。
    """

    def test_empty_dir_discovered(self, tmp_path: Path):
        packages = discover_dsh_plugins(tmp_path)
        assert packages == [], packages

    def test_installed_plugins_loaded(self):
        loaded = load_installed_plugins()
        assert loaded["count"] >= 1
        packages = {p["source"]["package"] for p in loaded["packages"]}
        assert "@deepseek-ai/dsh-tool-time" in packages, packages

    def test_manifest_declares_list_tool(self):
        import json as _json  # noqa: PLC0415

        manifest = _json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        names = {t["name"] for t in manifest["capabilities"]["tools"]}
        assert "dsh_list_plugins" in names
        assert manifest["plugin_type"] == "system"
        # D.6 槽位拆分：声明即注册，无类型豁免字段
        assert "llm_tools" not in manifest
        # 正式贡献面：renderers + 适配器元信息 + client_styles（DSH 视觉 CSS 通道）
        assert set(manifest["contributes"].keys()) == {"renderers", "dsh_adapter", "client_styles", "themes"}
        # dsh-bg 演示残留已撤（2026-08-21 与皮肤 body 打架）；皮肤 CSS 通道唯一
        assert [c["id"] for c in manifest["contributes"]["client_styles"]] == ["dsh-skin"]
        # 配置入口：DSH 插件装载管理（config/dsh_adapter.yaml）
        assert manifest["config_files"] == [
            {"id": "dsh_plugins", "path": "config/dsh_adapter.yaml", "label": "DSH 插件配置"}
        ]


# ── 配置装载过滤（config/dsh_adapter.yaml：DSH 插件逐包启用/禁用） ─────


class TestPluginConfigFilter:
    def test_load_plugin_config_parses_plugins_map(self, tmp_path, monkeypatch):
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "dsh_adapter.yaml").write_text(
            "plugins:\n  my-plugin:\n    enabled: false\n  other-plugin: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path))
        cfg = load_plugin_config()
        assert cfg == {"my-plugin": {"enabled": False}, "other-plugin": True}

    def test_load_plugin_config_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path))
        assert load_plugin_config() == {}

    def test_plugin_enabled_rules(self, monkeypatch):
        from translator import _plugin_enabled  # noqa: PLC0415

        config = {"off": {"enabled": False}, "on": {"enabled": True}, "bare-off": False}
        # 未列出 = 默认启用；{enabled:false} / 裸 false = 禁用
        assert _plugin_enabled("unlisted", config) is True
        assert _plugin_enabled("off", config) is False
        assert _plugin_enabled("on", config) is True
        assert _plugin_enabled("bare-off", config) is False
        assert _plugin_enabled("missing", {}) is True

    def test_load_installed_plugins_filters_disabled(self, monkeypatch):
        import translator as translator_mod  # noqa: PLC0415

        fake_pkgs = [
            PLUGIN_DIR / "dsh_plugins" / "good-plugin",
            PLUGIN_DIR / "dsh_plugins" / "bad-plugin",
        ]
        monkeypatch.setattr(translator_mod, "discover_dsh_plugins", lambda: fake_pkgs)
        monkeypatch.setattr(
            translator_mod, "load_plugin_config", lambda: {"bad-plugin": {"enabled": False}}
        )
        monkeypatch.setattr(translator_mod, "translate_packages", lambda pkgs: {"packages": list(pkgs)})
        loaded = load_installed_plugins()
        assert loaded["count"] == 1
        assert loaded["packages"] == [fake_pkgs[0]]
        assert loaded["disabled"] == ["bad-plugin"]


# ── DSH 插件形态分类（hook/service/io/tool/visual 五形态） ───────────────


class TestClassifyPlugin:
    def _make_pkg(self, tmp_path: Path, name: str, files: dict[str, str]) -> Path:
        pkg = tmp_path / name
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(
            json.dumps({"name": name, "version": "1.0.0"}),
            encoding="utf-8",
        )
        for rel, content in files.items():
            p = pkg / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return pkg

    def test_hook_kind(self, tmp_path: Path):
        pkg = self._make_pkg(tmp_path, "dsh-hooks", {
            "src/index.ts": (
                "export const inject = ['sessions']\n"
                "export function apply(ctx) {\n"
                "  ctx.on('session/event', async (ev) => { spawn('node notify.mjs'); })\n"
                "  ctx.on('agent/created', () => {})\n"
                "}\n"
            ),
        })
        kinds = translate_package(pkg)["kinds"]
        assert kinds["hook"]["events"] == ["agent/created", "session/event"]
        assert "triggers_ext" in kinds["hook"]["lingxi"]

    def test_service_kind(self, tmp_path: Path):
        pkg = self._make_pkg(tmp_path, "dsh-interconnect", {
            "src/interconnect/index.ts": (
                "export class InterconnectService extends Service {\n"
                "  static inject = ['webServer', 'agents', 'credentials']\n"
                "  constructor(ctx, config) { super(ctx, 'interconnect') }\n"
                "}\n"
            ),
        })
        kinds = translate_package(pkg)["kinds"]
        assert kinds["service"]["names"] == ["interconnect"]
        assert "webServer" in kinds["service"]["inject"]
        assert "capabilities.services" in kinds["service"]["lingxi"]

    def test_io_kind_input_output(self, tmp_path: Path):
        pkg = self._make_pkg(tmp_path, "agent-instructions", {
            "src/index.ts": (
                "export function apply(ctx) {\n"
                "  ctx.on('agent/pre-step', async ({agent, messages}, next) => {\n"
                "    agent.inbox.prepend(instructionMessage)\n"
                "    return next()\n"
                "  })\n"
                "  ctx.on('tools/result', () => {})\n"
                "}\n"
            ),
        })
        kinds = translate_package(pkg)["kinds"]
        assert kinds["io"]["roles"] == ["input", "output"]
        assert "pipeline input/output" in kinds["io"]["lingxi"]

    def test_tool_kind(self, tmp_path: Path):
        pkg = self._make_pkg(tmp_path, "dsh-tool-time", {
            "lib/index.js": "export const apply = (ctx) => { ctx.tools.register(defineTool({})) }\n",
        })
        kinds = translate_package(pkg)["kinds"]
        assert "tool" in kinds

    def test_visual_kind(self, tmp_path: Path):
        pkg = self._make_pkg(tmp_path, "ui-tool", {
            "dsh/client.js": "yield ctx.slots.register({ name: 'tool.call.toolview', key: 'read' }, ReadRow);\n",
        })
        kinds = translate_package(pkg)["kinds"]
        assert "visual" in kinds

    def test_multikind_interconnect_service_and_tool(self, tmp_path: Path):
        """interconnect 是多形态样例：service + tool 并存。"""
        pkg = self._make_pkg(tmp_path, "dsh-interconnect", {
            "src/interconnect/index.ts": (
                "export class InterconnectService extends Service {\n"
                "  constructor(ctx, config) { super(ctx, 'interconnect') }\n"
                "}\n"
            ),
            "src/tool-interconnect/index.ts": (
                "export const apply = (ctx) => { ctx.tools.register(defineTool({ name: 'interconnect_send' })) }\n"
            ),
        })
        kinds = translate_package(pkg)["kinds"]
        assert "service" in kinds and "tool" in kinds


# ── DSH hooks 配置翻译（事件 → 灵汐触发器参数） ─────────────────────────


class TestTranslateHooksConfig:
    def test_turn_end_reason_mapping(self):
        r = translate_hooks_config([
            {"on": "turn/end", "when": "completed", "run": "node a.mjs"},
            {"on": "turn/end", "when": "error", "run": "node b.mjs"},
            {"on": "turn/end", "when": "aborted", "run": "node c.mjs"},
            {"on": "turn/end", "when": "max-tokens", "run": "node d.mjs"},
        ])
        assert r["mapped"] == 4
        events = [t["event_type"] for t in r["triggers"]]
        assert events == ["run.completed", "run.failed", "run.suspended", "run.suspended"]

    def test_direct_event_mapping(self):
        r = translate_hooks_config([
            {"on": "turn/start", "run": "echo s"},
            {"on": "approval/asked", "run": "echo a"},
            {"on": "agent/created", "run": "echo c"},
            {"on": "agent/disposed", "run": "echo d"},
            {"on": "agent/error", "run": "echo e"},
        ])
        events = [t["event_type"] for t in r["triggers"]]
        assert events == ["run.started", "approval.created", "session.created", "session.deleted", "run.failed"]

    def test_command_action_params(self):
        r = translate_hooks_config([{"on": "turn/end", "when": "completed", "run": "node n.mjs", "timeoutMs": 5000}])
        t = r["triggers"][0]
        assert t["action"] == "command"
        assert t["action_params"] == {"command": "node n.mjs", "timeout_ms": 5000}

    def test_unmapped_honest(self):
        r = translate_hooks_config([{"on": "mystery/event", "run": "echo x"}])
        assert r["mapped"] == 0
        assert r["unmapped"][0]["reason"] == "no lingxi domain event equivalent"

    def test_yaml_input(self):
        r = translate_hooks_config(
            "hooks:\n  - on: 'turn/end'\n    when: 'completed'\n    run: 'node n.mjs'\n"
        )
        assert r["mapped"] == 1
        assert r["triggers"][0]["event_type"] == "run.completed"


# ── 皮肤中心令牌层注入（skin-center 通道，2026-08-21） ─────────────────

class TestSkinCenterResolution:
    """translator 皮肤解析纯函数（对真实 skin-center 资产跑，仓库内自带 15 套）。"""

    def test_list_available_skins(self):
        skins = list_available_skins()
        assert "matrix" in skins and "miku" in skins
        assert len(skins) >= 15

    def test_resolve_known_skin(self):
        css = resolve_skin_css("matrix")
        assert css is not None and css.is_file()
        text = css.read_text(encoding="utf-8")
        assert "--dsw-" in text  # 令牌层特征（CSS 变量）

    def test_resolve_unknown_skin(self):
        assert resolve_skin_css("no-such-skin") is None

    def test_path_traversal_guard(self):
        assert resolve_skin_css("../ui-theme") is None
        assert resolve_skin_css("a/b") is None


class TestSkinServeHandler:
    """server._http_handle_style 的皮肤路由（dispatcher 契约 body base64）。"""

    def _get_skin_css(self) -> tuple[int, str]:
        import server  # noqa: PLC0415

        resp = asyncio.run(
            server._http_handle_style(path="/ext/dsh_adapter/styles/skin.css", method="GET")
        )
        body = base64.b64decode(resp["body"]).decode("utf-8") if resp["body"] else ""
        return resp["status"], body

    def test_serve_selected_skin(self, monkeypatch: pytest.MonkeyPatch):
        import server  # noqa: PLC0415

        monkeypatch.setattr("server.load_skin_selection", lambda: "miku")
        status, body = self._get_skin_css()
        assert status == 200
        assert "--dsw-" in body  # miku 的 :root 令牌原文（皮肤间令牌有无不一，固定样例）
        assert "!important" not in body  # 补充层无对抗 hack（主体走主题管线）

    def test_disabled_skin_returns_empty_css(self, monkeypatch: pytest.MonkeyPatch):
        import server  # noqa: PLC0415

        monkeypatch.setattr("server.load_skin_selection", lambda: None)
        resp = asyncio.run(
            server._http_handle_style(path="/ext/dsh_adapter/styles/skin.css", method="GET")
        )
        assert resp["status"] == 200
        assert base64.b64decode(resp["body"]).decode("utf-8").startswith("/*")

    def test_unknown_skin_falls_back_empty(self, monkeypatch: pytest.MonkeyPatch):
        import server  # noqa: PLC0415

        monkeypatch.setattr("server.load_skin_selection", lambda: "no-such-skin")
        resp = asyncio.run(
            server._http_handle_style(path="/ext/dsh_adapter/styles/skin.css", method="GET")
        )
        assert resp["status"] == 200
        assert base64.b64decode(resp["body"]).decode("utf-8").startswith("/*")


class TestSkinPluginThemes:
    """皮肤 → contributes.themes 声明（形态路由终态：插件主题通道原生渲染）。"""

    def test_all_skins_declared(self):
        themes = skins_to_plugin_themes()
        assert len(themes) == len(list_available_skins()) >= 15
        ids = {t["id"] for t in themes}
        assert "dsh-skin-miku" in ids and "dsh-skin-matrix" in ids

    def test_miku_light_theme_with_bg(self):
        themes = skins_to_plugin_themes()
        miku = next(t for t in themes if t["id"] == "dsh-skin-miku")
        assert miku["base"] == "light"  # 画布 #eef5ff 亮度判定
        img = miku["backgrounds"]["image"]
        assert img["enabled"] is True and "miku-art" in img["url"]
        assert img["url"].startswith("/ext/dsh_adapter/styles/skin-assets/")
        assert img["overlay"].startswith("rgba(")  # scrim → 纯色 overlay
        vars_ = miku["variables"]
        assert vars_["--ds-bg-canvas"] == "#eef5ff"
        # shadcn 桥必须 H S% L% 纯串（hsl(var(--x)) 内 color-mix 非法）
        assert re.fullmatch(r"\d+ \d+% \d+%", vars_["--background"])

    def test_matrix_dark_plain_theme(self):
        themes = skins_to_plugin_themes()
        matrix = next(t for t in themes if t["id"] == "dsh-skin-matrix")
        assert matrix["base"] == "dark"
        assert "backgrounds" not in matrix or matrix["backgrounds"]["image"]["enabled"] is False

    def test_manifest_themes_auto_declared(self):
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        declared = manifest["contributes"]["themes"]
        assert len(declared) >= 15  # on_load 幂等同步产物（静态 themes 禁令解除：自动声明非手工翻译）



class TestSkinAssetRoute:
    """背景图资产路由（白名单 + 防穿越）。"""

    def _serve(self, path: str) -> dict:
        import server  # noqa: PLC0415

        return asyncio.run(server._http_handle_style(path=path, method="GET"))

    def test_serve_image_asset(self):
        resp = self._serve("/ext/dsh_adapter/styles/skin-assets/miku/assets/miku-art.webp")
        assert resp["status"] == 200
        assert resp["headers"]["content-type"] == "image/webp"
        assert len(base64.b64decode(resp["body"])) > 10000  # 真图（非空体）

    def test_reject_non_image(self):
        resp = self._serve("/ext/dsh_adapter/styles/skin-assets/miku/skin.json")
        assert resp["status"] == 404

    def test_reject_traversal(self):
        resp = self._serve("/ext/dsh_adapter/styles/skin-assets/miku/../../skin.json")
        assert resp["status"] == 404

    def test_skin_css_includes_background(self, monkeypatch: pytest.MonkeyPatch):
        # 与真机状态隔离：固定 miku（有 backgroundMedia）验证注入组合三段
        monkeypatch.setattr("server.load_skin_selection", lambda: "miku")
        resp = self._serve("/ext/dsh_adapter/styles/skin.css")
        assert resp["status"] == 200
        body = base64.b64decode(resp["body"]).decode("utf-8")
        assert "--dsw-" in body  # 令牌层仍在
        assert "font-family" in body  # 补充层：字体注入
        assert "lingxi adaptation" not in body  # hack 适配层已退役


class TestSkinListAndSelect:
    """动态皮肤清单端点 + 选择写回（PUT 用 tmp config，不污染仓库配置）。"""

    def _call(self, path: str, method: str, raw_body: str = "") -> dict:
        import server  # noqa: PLC0415

        return asyncio.run(
            server._http_handle_style(path=path, method=method, raw_body=raw_body, plugin_id="", headers=None, query=None)
        )

    def test_list_skins_dynamic(self):
        resp = self._call("/ext/dsh_adapter/skins", "GET")
        assert resp["status"] == 200
        data = json.loads(base64.b64decode(resp["body"]).decode("utf-8"))
        assert data["count"] == len(data["skins"]) >= 15
        miku = next(s for s in data["skins"] if s["id"] == "miku")
        assert miku["has_background_media"] is True
        assert miku["accent"].startswith("#")
        matrix = next(s for s in data["skins"] if s["id"] == "matrix")
        assert matrix["has_background_media"] is False

    def test_select_skin_writes_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        import server  # noqa: PLC0415

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "dsh_adapter.yaml").write_text(
            "# 注释保留验证\nplugins: {}\nskin: miku\n", encoding="utf-8"
        )
        monkeypatch.setattr("server._skin_config_path", lambda: str(cfg_dir))
        body = base64.b64encode(json.dumps({"skin": "matrix"}).encode()).decode()
        resp = self._call("/ext/dsh_adapter/skins/current", "PUT", raw_body=body)
        assert resp["status"] == 200
        text = (cfg_dir / "dsh_adapter.yaml").read_text(encoding="utf-8")
        assert "skin: matrix" in text
        assert text.startswith("# 注释保留验证")  # 注释未被破坏
        assert "plugins: {}" in text

    def test_select_skin_unknown_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "dsh_adapter.yaml").write_text("skin: miku\n", encoding="utf-8")
        monkeypatch.setattr("server._skin_config_path", lambda: str(cfg_dir))
        body = base64.b64encode(json.dumps({"skin": "hack"}).encode()).decode()
        resp = self._call("/ext/dsh_adapter/skins/current", "PUT", raw_body=body)
        assert resp["status"] == 400
        assert (cfg_dir / "dsh_adapter.yaml").read_text(encoding="utf-8") == "skin: miku\n"  # 未被改写

    def test_manifest_http_endpoints_declared(self):
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        routes = {(e["method"], e["path"]) for e in manifest["http_endpoints"]}
        assert ("GET", "/ext/dsh_adapter/skins") in routes
        assert ("PUT", "/ext/dsh_adapter/skins/current") in routes
        assert ("GET", "/ext/dsh_adapter/styles/skin-assets/{skin}/{file:path}") in routes
        # 静态 themes 已撤：皮肤清单必须动态（用户裁决：加新皮肤插件零 manifest 改动）
        # 终态：themes 必须存在（on_load 自动声明，静态禁令解除）
        assert len(manifest["contributes"].get("themes", [])) >= 15

# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""dsh_adapter 插件测试（task_dsh_plugin_adapter 任务 2 + 4）。

分层：
- translator：纯函数（package.json/dsh 声明解析、toolview 扫描、失败隔离）；
- bridge：协议层（mock Node runtime 脚本验证 JSON-RPC 帧/超时/错误映射）；
- plugin.json 契约一致性（manifest 声明 = server.py @tool 注册）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import translator as translator_mod  # noqa: E402 — 皮肤真值位隔离 patch 用
from bridge import DshRuntimeBridge  # noqa: E402
from translator import (  # noqa: E402
    DSH_SOURCE_COMMIT,
    DSH_SOURCE_VERSION,
    discover_dsh_plugins,
    dsh_params_to_json_schema,
    list_available_skins,
    load_installed_plugins,
    load_plugin_config,
    map_dsh_slot,
    skins_to_plugin_themes,
    to_lingxi_tool_entry,
    translate_dialect_hooks,
    translate_hooks_config,
    translate_native_hooks,
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
        # renderers：官方页工具 → 渲染卡映射（2026-11-19 官方页同步批次起非空）。
        # 契约 = 每条 {tool, card} 双键齐 + 卡型落在已知词表 + 声明工具不重复 +
        # 关键卡型抽查（终端/读取/差异/搜索/网页走专属卡，其余 generic）。
        renderers = c["renderers"]
        assert renderers, "renderers 不应回空（官方页按卡型路由）"
        cards = {r["card"] for r in renderers}
        assert cards <= {"generic", "terminal", "diff", "read", "search", "web"}
        tools_mapped = [r["tool"] for r in renderers]
        assert len(tools_mapped) == len(set(tools_mapped)), "同一工具重复映射"
        spot = {r["tool"]: r["card"] for r in renderers}
        assert spot.get("bash") == "terminal"
        assert spot.get("read") == "read"
        assert spot.get("edit") == "diff"
        assert spot.get("write") == "diff"
        assert spot.get("grep") == "search"
        assert spot.get("web_search") == "web"
        assert spot.get("web_fetch") == "web"


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
      resp = { jsonrpc: '2.0', id: msg.id, result: { success: true, data: { echoed: msg.params.args }, error: null, duration_ms: 1 } }
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
    """把 runtime 脚本替换为 mock，运行时工作区指向临时目录。"""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "dsh-rpc-bridge.mjs").write_text(MOCK_RUNTIME, encoding="utf-8")
    repo = tmp_path / "dsh-repo"
    repo.mkdir()
    import bridge as bridge_mod  # noqa: PLC0415

    monkeypatch.setattr(bridge_mod, "_RUNTIME_SCRIPT", runtime_dir / "dsh-rpc-bridge.mjs")
    return DshRuntimeBridge(runtime_root=str(repo), cwd=str(tmp_path), boot_timeout_s=15, call_timeout_s=5)


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

    def test_missing_workspace_fails_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import bridge as bridge_mod  # noqa: PLC0415

        monkeypatch.setattr(bridge_mod, "_RUNTIME_SCRIPT", tmp_path / "nope.mjs")
        b = DshRuntimeBridge(runtime_root=str(tmp_path))
        out = asyncio.run(b.call_tool("read", {}))
        assert out["success"] is False
        assert "missing" in out["error"]

    def test_unconfigured_runtime_root_fails_visible_with_env_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """未配置运行时工作区（无参/无 env）：fail-visible 且文案指明需设的环境变量名。

        E2 修复回归：曾硬编码机器特定缺省路径，未配置机器静默指向不存在目录。
        """
        import bridge as bridge_mod  # noqa: PLC0415

        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / "dsh-rpc-bridge.mjs").write_text("// stub\n", encoding="utf-8")
        monkeypatch.setattr(bridge_mod, "_RUNTIME_SCRIPT", runtime_dir / "dsh-rpc-bridge.mjs")
        monkeypatch.delenv("AGENTOS_DSH_RUNTIME_ROOT", raising=False)
        b = DshRuntimeBridge()
        out = asyncio.run(b.call_tool("read", {}))
        assert out["success"] is False
        assert "AGENTOS_DSH_RUNTIME_ROOT" in (out.get("error") or "")

    def test_runtime_root_resolved_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """显式参数缺省时从 AGENTOS_DSH_RUNTIME_ROOT 解析（env 配置路径回归）。"""
        import bridge as bridge_mod  # noqa: PLC0415

        repo = tmp_path / "env-repo"
        repo.mkdir()
        monkeypatch.setenv("AGENTOS_DSH_RUNTIME_ROOT", str(repo))
        b = DshRuntimeBridge()
        assert b._runtime_root == str(repo)  # noqa: SLF001 — 配置解析契约直读内部委派字段

    def test_explicit_runtime_root_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """显式参数优先于环境变量（解析序契约）。"""
        monkeypatch.setenv("AGENTOS_DSH_RUNTIME_ROOT", str(tmp_path / "from-env"))
        b = DshRuntimeBridge(runtime_root=str(tmp_path / "explicit"))
        assert b._runtime_root == str(tmp_path / "explicit")  # noqa: SLF001

    def test_relative_runtime_root_normalized_to_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """相对 root 归一成绝对路径（Node 侧官方 profile loader 拒绝相对路径）。"""
        monkeypatch.chdir(tmp_path)
        b = DshRuntimeBridge(runtime_root="rel/ws")
        assert b._runtime_root == str(tmp_path / "rel" / "ws")  # noqa: SLF001

    def test_shutdown_terminates_process(self, mock_bridge: DshRuntimeBridge):
        async def scenario() -> None:
            await mock_bridge.initialize()
            proc = mock_bridge._proc  # noqa: SLF001
            assert proc is not None
            await mock_bridge.shutdown()
            assert mock_bridge._proc is None  # noqa: SLF001
            assert proc.returncode is not None

        asyncio.run(scenario())


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

    def test_manifest_llm_tools_contract(self):
        import json as _json  # noqa: PLC0415

        manifest = _json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        # LLM 面精确 = DSH 生态桥两件（2026-11-10 修订：dsh.tool.list/call 把已装
        # DSH 插件自己的工具接进灵汐工具面，供真实试用；general_agent.yaml 持有）。
        # 服务/拦截点仍不进 LLM 面（2026-10-19 工具面审查裁定）：dsh.service.call/
        # dsh.hook.emit/dsh.web.request 只住 services 轴，经显式插件通道消费。
        tools = manifest["capabilities"]["tools"]
        assert {t["name"] for t in tools} == {"dsh.tool.list", "dsh.tool.call"}
        assert manifest["plugin_type"] == "system"
        # D.6 槽位拆分：声明即注册，无类型豁免字段
        assert "llm_tools" not in manifest
        # 正式贡献面：renderers + 适配器元信息 + pages（皮肤中心页面）+ themes
        # （皮肤 CSS 走 merged.css 端点，manifest 无 client_styles 贡献位）
        assert set(manifest["contributes"].keys()) == {"renderers", "dsh_adapter", "pages", "themes"}
        # 配置入口：DSH 插件装载管理 + LLM 默认模型注入（settings:false 仅供
        # agentDefaultModel 翻译取用，不出口为配置面板）
        config_files = manifest["config_files"]
        assert [c["id"] for c in config_files] == ["dsh_plugins", "llm"]
        assert config_files[0]["path"].endswith("dsh_adapter.yaml")
        assert config_files[1]["path"].endswith("llm.yaml")
        assert config_files[1]["settings"] is False
        # 服务轴不得混入 LLM 面两件（两轴各归其位）
        svc_names = {s["name"] for s in manifest["capabilities"]["services"]}
        assert svc_names == {"dsh.service.call", "dsh.hook.emit", "dsh.web.request"}


# ── 配置装载过滤（config/plugins/dsh_adapter/dsh_adapter.yaml：DSH 插件逐包启用/禁用） ─────


class TestPluginConfigFilter:
    def test_load_plugin_config_parses_plugins_map(self, tmp_path, monkeypatch):
        cfg_dir = tmp_path / "config" / "plugins" / "dsh_adapter"
        cfg_dir.mkdir(parents=True)
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
        from translator import plugin_enabled  # noqa: PLC0415

        config = {"off": {"enabled": False}, "on": {"enabled": True}, "bare-off": False}
        # 未列出 = 默认启用；{enabled:false} / 裸 false = 禁用
        assert plugin_enabled("unlisted", config) is True
        assert plugin_enabled("off", config) is False
        assert plugin_enabled("on", config) is True
        assert plugin_enabled("bare-off", config) is False
        assert plugin_enabled("missing", {}) is True

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
        assert "checks" in kinds["hook"]["lingxi"]

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


# ── DSH hooks 配置翻译（事件 → 管道链位步骤条目） ─────────────────────────


class TestTranslateHooksConfig:
    @pytest.mark.parametrize(
        ("on", "boundary"),
        [
            ("turn/start", "pre"),
            ("agent/created", "pre"),
            ("turn/end", "post"),
        ],
    )
    def test_chain_mapping(self, on: str, boundary: str):
        r = translate_hooks_config([{"on": on, "run": "node n.mjs"}])
        assert r["mapped"] == 1
        assert r["checks"][0]["at"] == f"{boundary}:<step>"
        assert r["checks"][0]["then"] == "run:<step>"
        run = r["runs"][0]
        assert run["event"] == on
        assert run["command"] == ["node", "n.mjs"]
        assert run["timeout_ms"] == 10000

    def test_command_quoted_argv_split(self):
        r = translate_hooks_config([{"on": "turn/start", "run": "bash 'my hook.sh' --x=1"}])
        assert r["runs"][0]["command"] == ["bash", "my hook.sh", "--x=1"]

    def test_timeout_custom_and_default(self):
        r = translate_hooks_config([{"on": "turn/end", "run": "node a.mjs", "timeoutMs": 5000}])
        assert r["runs"][0]["timeout_ms"] == 5000
        r2 = translate_hooks_config([{"on": "turn/end", "run": "node a.mjs"}])
        assert r2["runs"][0]["timeout_ms"] == 10000

    def test_turn_end_with_reason_not_translated(self):
        # checks when 是 state 谓词：带 when 的 turn/end 宁可 unmapped 也不过度触发
        # （post 边界每轮都跑，gate 缺失 = 钩子在错误轮之外的所有轮都会响）。
        r = translate_hooks_config([{"on": "turn/end", "when": "error", "run": "node b.mjs"}])
        assert r["mapped"] == 0
        assert r["checks"] == []
        assert r["unmapped"][0]["reason"] == "turn-end reason gate 无法静态映射为 checks when 谓词"

    @pytest.mark.parametrize(
        "on",
        ["approval/asked", "agent/disposed", "agent/error", "mystery/event"],
    )
    def test_unmapped_no_chain_position(self, on: str):
        r = translate_hooks_config([{"on": on, "run": "echo x"}])
        assert r["mapped"] == 0
        assert r["unmapped"][0]["on"] == on
        assert r["unmapped"][0]["reason"]

    def test_unbalanced_quotes_unmapped(self):
        r = translate_hooks_config([{"on": "turn/start", "run": "bash 'unclosed.mjs"}])
        assert r["mapped"] == 0

    def test_yaml_input_bare_on_key(self):
        # PyYAML YAML 1.1：裸键 on 解析为布尔 True，翻译器必须回取
        r = translate_hooks_config("hooks:\n  - on: turn/end\n    run: 'node n.mjs'\n")
        assert r["mapped"] == 1
        assert r["checks"][0]["at"] == "post:<step>"

    def test_hooks_wrapper_and_none(self):
        r = translate_hooks_config({"hooks": [{"on": "turn/start", "run": "echo s"}]})
        assert r["mapped"] == 1
        assert translate_hooks_config(None) == {"checks": [], "runs": [], "mapped": 0, "unmapped": []}

    def test_shape_invariants(self):
        # 性质断言：mapped==len(checks)==len(runs)；check 词表合法、run 参数完备
        r = translate_hooks_config([
            {"on": "turn/start", "run": "node a.mjs"},
            {"on": "agent/created", "run": "bash 'x y.sh'", "timeoutMs": 500},
        ])
        assert r["mapped"] == len(r["checks"]) == len(r["runs"])
        for check in r["checks"]:
            assert check["at"].split(":", 1)[0] in ("pre", "post")
            assert check["then"].startswith("run:")
        for item in r["runs"]:
            cmd = item["command"]
            assert isinstance(cmd, list)
            assert cmd
            assert all(isinstance(c, str) for c in cmd)
            assert item["timeout_ms"] > 0


# ── 皮肤：位置路由转译（CSS + hooks 同源映射表，2026-08-22） ────────────

# ── 皮肤语料 fixture（真值位隔离） ──────────────────────────────────────
#
# 皮肤唯一真值位是用户目录（~/.dsh/skins，translator.SKIN_CENTER_SKINS_DIR），
# 属用户数据——CI/新机为空目录，皮肤类测试曾隐式依赖开发者本机的 16 套真实
# 皮肤（干净环境必红）。这里按 skin-manifest-v2 契约自建最小充分语料，
# monkeypatch 完全接管 translator 与 server 测试模块的皮肤根路径：本机真实
# 数据不参与任何断言（语料缺失时同批断言照红，不是跳过）。

_SERVER_TEST_MODULES = (
    "dsh_adapter_server_under_test",
    "dsh_adapter_server_pos_test",
    "dsh_adapter_server_delivery_test",
    "dsh_adapter_server_asset_test",
)


def _skin_css(canvas: str, text: str) -> str:
    return f":root {{\n  background-color: {canvas};\n  color: {text};\n}}\n"


def _write_skin(root: Path, skin_id: str, css: str, *, manifest: dict | None = None,
                patches: str | None = None, hooks: str | None = None,
                assets: dict[str, bytes] | None = None) -> None:
    d = root / skin_id
    d.mkdir(parents=True)
    # newline="\n"：语料字节跨平台确定（Windows 默认会把 \n 转成 \r\n）
    (d / "skin.css").write_text(css, encoding="utf-8", newline="\n")
    if manifest is not None:
        (d / "skin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8", newline="\n")
    if patches is not None:
        (d / "patches.css").write_text(patches, encoding="utf-8", newline="\n")
    if hooks is not None:
        (d / "hooks.mjs").write_text(hooks, encoding="utf-8", newline="\n")
    for rel, data in (assets or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _build_skin_corpus(root: Path) -> Path:
    """16 款最小皮肤（id 集合 = plugin.json contributes.themes 基线）。

    每款按当批断言的最小充分面供给：miku（亮基准 + backgroundMedia 声明 +
    背景图资产）、maid-atelier（patches.css/hooks.mjs 同源 DSH 位置词汇）、
    cyber-night/xp（hooks 双引号属性形态样本）、三款纯 CSS 皮肤（无 hooks
    = 200 空文本契约素材）。
    """
    for skin_id, canvas, text in (
        ("blue-fantasy", "#eef1f8", "#2a3550"),
        ("deep-current", "#0c1420", "#d5e2ee"),
        ("dragon-heir", "#140f0a", "#e8d8b8"),
        ("furina", "#0c1020", "#d8e0ff"),
        ("harbor", "#0e161c", "#d0dde4"),
        ("matrix", "#0a0f0a", "#c8f0c8"),
        ("minecraft", "#121a10", "#d8e8c8"),
        ("mint", "#eafaf2", "#1c3a2c"),
        ("summer-liquid-glass", "#f0f6fb", "#223244"),
        ("trading", "#0c1016", "#d0dae8"),
        ("whale-mom", "#0b1424", "#dce8f8"),
        ("whale-song", "#0a121e", "#d4e2f4"),
    ):
        _write_skin(root, skin_id, _skin_css(canvas, text))
    # miku：亮基准（画布亮度判定）+ 背景媒体声明（主题背景图层 + 资产路由素材）
    _write_skin(
        root, "miku", _skin_css("#eef5ff", "#14263f"),
        manifest={
            "skinManifestVersion": 2, "id": "miku", "name": "初音未来 · 电子歌姬",
            "accent": "#0f4da8",
            "contributes": {"backgroundMedia": {
                "light": {"type": "image", "src": "assets/miku-art.webp",
                          "scrim": "rgba(238, 245, 255, 0.45)"},
            }},
        },
        assets={"assets/miku-art.webp": b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20480},
    )
    # maid-atelier：DSH 位置词汇（patches.css 与 hooks.mjs 同源选择器，
    # hooks 为双引号 JS 串 + 单引号属性值——与真实皮肤同形）
    _write_skin(
        root, "maid-atelier", _skin_css("#eef2fb", "#3a4356"),
        patches=(
            '[data-pane="sidebar"] { color: #123456; }\n'
            '[data-phase="hero"] { color: #234567; }\n'
            '[data-slot="sidebar.settings"] { color: #345678; }\n'
            ':is([data-pane="sidebar"], [class*="sidebarCol"]) { color: #456789; }\n'
        ),
        hooks=(
            "export const SELECTORS = [\n"
            "  \"[data-phase='hero'], [data-phase='active']\",\n"
            "  \"[data-slot='sidebar.settings']\",\n"
            "  \":is([data-pane='sidebar'], [class*='sidebarCol'])\",\n"
            "  \"[data-pane='sidebar'] > div > :last-child\",\n"
            "].join(', ')\n"
            "\n"
            "export function apply(ctx) {\n"
            "  const root = document.querySelector(SELECTORS)\n"
            "  if (root) {\n"
            "    ctx.onCleanup(() => {\n"
            "      root.dataset.hookBound = ''\n"
            "    })\n"
            "  }\n"
            "}\n"
        ),
    )
    # cyber-night / xp 带 hooks（JS 转译合法性冒烟的属性形态多样性样本）
    _write_skin(
        root, "cyber-night", _skin_css("#0a0f1e", "#cfe8ff"),
        hooks=(
            "export const SIDEBAR = '[data-pane=\"sidebar\"]'\n"
            "export const DARK = 'body[data-ds-dark-theme]'\n"
            "export function apply(ctx) {\n"
            "  const el = document.querySelector(SIDEBAR)\n"
            "  if (el) ctx.onCleanup(() => { el.dataset.dark = document.body.matches(DARK) ? '1' : '0' })\n"
            "}\n"
        ),
    )
    _write_skin(
        root, "xp", _skin_css("#101014", "#d0d4e0"),
        hooks=(
            "export const COMPOSER = '[data-slot=\"conversation.composer\"]'\n"
            "export function apply(ctx) {\n"
            "  ctx.onCleanup(() => {})\n"
            "  return document.querySelector(COMPOSER) !== null\n"
            "}\n"
        ),
    )
    return root


@pytest.fixture(autouse=True)
def _isolated_skin_center(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """皮肤真值位隔离：fixture 语料完全接管，本机真实 ~/.dsh 不参与断言。"""
    skins_dir = _build_skin_corpus(tmp_path / "skins")
    monkeypatch.setattr(translator_mod, "SKIN_CENTER_SKINS_DIR", skins_dir)
    for name in _SERVER_TEST_MODULES:
        mod = sys.modules.get(name)
        if mod is not None:
            # server.py 是 from-import 绑定：已加载的测试模块（绑着上一用例
            # 的临时目录）必须随语料换绑，否则读到过期路径
            monkeypatch.setattr(mod, "SKIN_CENTER_SKINS_DIR", skins_dir)
    return skins_dir


class TestSkinCenterResolution:
    """translator 皮肤解析纯函数（fixture 语料隔离真值位，16 套最小皮肤）。"""

    def test_list_available_skins(self):
        skins = list_available_skins()
        assert "matrix" in skins and "miku" in skins
        assert len(skins) >= 15


class TestSkinPositionRoutingCss:
    """server._rewrite_dsh_positions：DSH 选择器 → 我方锚点（逐规则翻译）。

    位置映射（2026-08-22 用户裁决）：DSH 词汇在递送层转译，灵汐 DOM 不贴
    DSH 名字——target 一律 data-region / data-testid / data-chat-state。
    """

    def _server(self):
        # 显式文件级加载（唯一模块名）：裸 `import server` 会被同 pytest
        # 进程里其它插件目录（先收集的测试把各自目录插到 sys.path[0]）的
        # server.py 劫持。约定同 test_server_registry_matches_manifest。
        import importlib.util as _ilu

        mod_name = "dsh_adapter_server_pos_test"
        if mod_name not in sys.modules:
            spec = _ilu.spec_from_file_location(mod_name, PLUGIN_DIR / "server.py")
            assert spec is not None and spec.loader is not None, "cannot load dsh_adapter server.py"
            module = _ilu.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        return sys.modules[mod_name]

    def _rw(self, css: str) -> str:
        return self._server()._rewrite_dsh_positions(css)

    def test_pane_triple(self):
        out = self._rw('[data-pane="sidebar"]{a:b}[data-pane="details"]{a:b}'
                       '[data-pane="conversation"]{a:b}[data-pane="detail"]{a:b}')
        assert '[data-region="sidebar"]{a:b}' in out
        # workspace 容器裸规则经表面剥离路径（块重建补分号）
        assert '[data-region="workspace"]{a:b;}' in out
        assert '[data-region="chat"]{a:b}' in out
        assert 'data-pane' not in out

    def test_workspace_container_surface_stripped(self):
        """工作区表面让位（用户裁决：工作区=对话区延伸，背景图透出）：
        details 容器裸规则的实色纸面剥离，边框等位置装饰保留。"""
        out = self._rw(':is([data-pane="details"], [class*="detailsCol"]){'
                       'background:#f2f6fdd1;border-left-color:#c5a46885;padding:8px}')
        assert 'background' not in out
        assert 'border-left-color' in out and 'padding' in out
        # 全是背景的规则整条剔除；descendant 规则不动
        assert self._rw('[data-pane="details"]{background:#0b1737e6}') == ''
        kept = self._rw('[data-pane="details"] .card{background:#111}')
        assert 'background:#111' in kept

    def test_chat_phase_and_flow(self):
        out = self._rw('[data-phase="hero"]{a:b}[data-phase="active"]{a:b}'
                       '[data-pane="conversation"] [data-chat-flow]{a:b}')
        assert '[data-chat-state="empty"]{a:b}' in out
        assert '[data-chat-state="active"]{a:b}' in out
        assert '[data-region="chat"] [data-testid="message-list"]{a:b}' in out
        assert 'data-phase' not in out and 'data-chat-flow' not in out

    def test_titlebar_row_maps_to_top_band(self):
        """桌面壳标题栏行 → 顶带（CSS 专属映射）：皮肤给标题栏上深色渐变+金线，
        不映射则顶带透明、标题栏与花边檐口成两条叠在一起的条（用户 2026-11-10
        报「顶部/底部挂载重叠」）。单双引号形态都保持。"""
        out = self._rw('[class*="titlebar"]{background:linear-gradient(#1f3876e6,#091333fa)}'
                       "[class*='titlebar']{a:b}")
        assert '[data-testid="chat-top-band"]{background:linear-gradient' in out
        assert "[data-testid='chat-top-band']{a:b}" in out
        assert 'class*="titlebar"' not in out

    def test_titlebar_row_not_mapped_in_hooks(self):
        """hooks 侧不映射 titlebar：同一选择器在 hooks 里是「品牌文字挂载」
        （titlebar-brand prepend）——挂到顶带会把品牌塞成三区布局首子、
        挤歪与列对齐的区宽。"""
        src = "const t = document.querySelector(\"[class*='titlebar']\")\n"
        out = self._server()._sub_position(src)
        assert "[class*='titlebar']" in out
        assert 'chat-top-band' not in out

    def test_slots_and_surfaces(self):
        out = self._rw(
            '[data-slot="sidebar.settings"]{a:b}'
            '[data-slot="sidebar.settings"] > :is(button, [role="button"]){a:b}'
            '[data-slot="sidebar.footer.action"]{a:b}'
            '[data-slot="settings.trigger"]{a:b}'
            '[data-slot="conversation.session.header.actions"]{a:b}'
            '[data-slot="conversation.session.header"]{a:b}'
            '[data-slot="conversation.composer.dock"]{a:b}'
            '[data-slot="conversation.composer"]{a:b}'
            '[data-slot="conversation.chat.node"]{a:b}'
            '[data-dsh-surface="sidebar"]{a:b}'
            '[data-dsh-surface="conversation"]{a:b}'
            '[data-dsh-surface="settings"]{a:b}'
            '[data-dsh-surface="composer"]{a:b}'
            '[data-dsh-surface="session-header"]{a:b}'
        )
        # settings 复合形与裸形同指触发器（hooks footer 走查落标记用）
        assert '[data-testid="sidebar-user-area"]{a:b}' in out
        assert 'data-slot' not in out and 'data-dsh-surface' not in out
        for target in ('[data-testid="agent-tab-bar"]', '[data-testid="chat-session-header"]',
                       '[data-testid="chat-composer"]', '[data-testid="message-item"]',
                       '[data-testid="settings-page"]', '[data-region="sidebar"]',
                       '[data-region="chat"]'):
            assert target in out

    def test_scope_and_dark_variant_translated(self):
        """平台 scope：html[data-dsh-skin] 四形态 → data-skin="dsh_adapter:<id>"；
        暗色开关 body[data-ds-dark-theme] → data-skin-dark（hooks 裸字符串同译）。"""
        out = self._rw('html[data-dsh-skin="miku"]{a:b}body[data-ds-dark-theme]{a:b}')
        assert 'html[data-skin="dsh_adapter:miku"]{a:b}' in out
        assert 'body[data-skin-dark]{a:b}' in out
        assert 'data-dsh-skin' not in out and 'data-ds-dark-theme' not in out
        js = 'html[data-dsh-skin=\\"miku\\"]{a:b} html[data-dsh-skin="${ctx.scopeAttr}"]{} getAttribute(\'data-ds-dark-theme\')'
        r = self._server()._sub_position(js)
        assert 'html[data-skin=\\"dsh_adapter:miku\\"]' in r  # JS 转义引号形（miku 44 处）
        assert 'html[data-skin="${ctx.scopeAttr}"]' in r  # 运行时插值形
        assert "'data-skin-dark'" in r  # 属性名字符串（观察器/读取）

    def test_message_dom_stems_translated(self):
        """会话内部 DOM 词干 → 我方消息区锚点（2026-11-10 whale-mom 对账：
        18 个内部选择器此前零命中 = 皮肤效果对不上 DSH 的根因）。词干级
        替换，compound 选择器自动组合；两种引号形态同译。"""
        out = self._rw(
            '[class*="markdown"]:not([class*="Payload"]){background:v2}'
            '[class*="markdown"] :is(p, li){color:strong}'
            "[class*='thinkBody']{background:v4}"
            '[class*="summary"]{background:v3}'
            '[class*="timeEnd"],[class*="timeStart"],[class*="turnStatus"]{color:mid}'
            '[class*="usage"]{color:mid}'
            '[class*="footerItem"],[class*="footerHint"]{color:mid}'
            '[class*="chipLabel"]{color:strong}'
            '[class*="previewBadge"]{color:badge}'
            '[class*="bubble"]{color:strong}'
            "[class$='modal']{background:#fff}"
            '[data-phase="active"] [class*="composerSeat"]:before{content:""}'
            '[class*="root"][data-open] [class*="body"]{background:v4}'
            '[data-context-source="true"]{background:v3}'
        )
        assert '[data-testid="message-markdown"]:not([class*="Payload"]){background:v2}' in out
        assert '[data-testid="message-markdown"] :is(p, li){color:strong}' in out
        assert "[data-testid='message-think-body']{background:v4}" in out
        assert '[data-testid="message-think"]{background:v3}' in out
        assert '[data-testid="message-time"],[data-testid="message-time"],[data-testid="message-time"]{color:mid}' in out
        assert '[data-testid="context-usage-indicator"]{color:mid}' in out
        assert '[data-testid="chat-input-footer"],[data-testid="chat-input-footer"]{color:mid}' in out
        assert ':is([data-testid^="declared-widget-"], [data-testid^="slot-default-"]){color:strong}' in out
        assert '[data-testid="attachment-preview"]{color:badge}' in out
        assert '[data-bubble]{color:strong}' in out
        assert ".ant-modal{background:#fff}" in out
        assert '[data-chat-state="active"] [data-testid="chat-composer"]:before{content:""}' in out
        assert '.ant-popover:not(.ant-popover-hidden), .ant-dropdown:not(.ant-dropdown-hidden){background:v4}' in out
        assert '[data-bubble] blockquote{background:v3}' in out
        # DSH 词汇不出现在我方选择器（已译词干形态全数转出）
        for stem in ('thinkBody', 'timeEnd', 'timeStart', 'turnStatus', 'composerSeat',
                     'footerItem', 'footerHint', 'data-context-source'):
            assert stem not in out

    def test_media_nested_rules_translated(self):
        """@media 嵌套规则头同样翻译；@keyframes 体透传（重建会规整头部空格）。"""
        out = self._rw('@media (min-width: 768px) { [data-pane="sidebar"] { a: b } }')
        assert '[data-region="sidebar"]' in out
        kf = self._rw('@keyframes spin { 0% { opacity: 0 } }')
        assert '@keyframes spin' in kf and '0% { opacity: 0 }' in kf

    def test_component_classes_position_mapped(self):
        out = self._rw('[class*="newSession"]{a:b}[class*="searchButton"]{a:b}'
                       '[class*="composerSeat"]{a:b}[class*="sidebarCol"]{a:b}'
                       '[class*="centerCol"]{a:b}[class*="detailsCol"]{a:b}'
                       '[class*="settingsRoot"]{a:b}[class*="navCell"]{a:b}'
                       '[class*="userRow"]{a:b}')
        assert '[data-testid="new-session-button"]' in out
        assert '[data-testid="sidebar-search-section"]' in out
        assert '[data-testid="chat-composer"]' in out
        assert '[data-region="sidebar"]' in out
        assert '[data-region="chat"]' in out
        assert '[data-region="workspace"]' in out
        assert '[data-testid="settings-page"]' in out
        assert '[data-testid="sidebar-nav"] button' in out
        assert '[data-testid="sidebar-user-area"]' in out

    def test_generic_selectors_inert(self):
        """单词泛型（item/menu/header…）与皮肤私有状态词无法按位置裁决 → 原样透传。"""
        css = '[class*="item"]{a:b}[class*="menu"]{a:b}[data-phase="trading"]{a:b}'
        assert self._rw(css) == css

    def test_root_frame_stripped_background_kept(self):
        out = self._rw('[id="root"]{background:#fff;border:1px solid #000;box-shadow:0 0 4px #000}')
        assert 'background' in out and 'border' not in out and 'box-shadow' not in out
        # 全是窗框装饰的规则整条剔除
        assert self._rw('[id="root"]{outline:none}') == ''

    def test_hooks_structural_full_form_first(self):
        """结构全形优先：只换 pane 部分会把页脚装饰挂到整条侧栏；
        输出镜像输入引号风格（JS 字符串安全）。"""
        out = self._rw("[data-pane='sidebar'] > div > :last-child{a:b}")
        assert out == "[data-testid='sidebar-footer']{a:b}"
        assert self._rw('[data-pane="sidebar"] > div > :last-child{a:b}') == '[data-testid="sidebar-footer"]{a:b}'


class TestSkinPositionRoutingDelivery:
    """merged.css / hooks.mjs 递送端点：对 fixture 皮肤语料跑位置转译。"""

    def _server(self):
        # 同 TestSkinPositionRoutingCss._server：显式文件级加载防裸名劫持。
        import importlib.util as _ilu

        mod_name = "dsh_adapter_server_delivery_test"
        if mod_name not in sys.modules:
            spec = _ilu.spec_from_file_location(mod_name, PLUGIN_DIR / "server.py")
            assert spec is not None and spec.loader is not None, "cannot load dsh_adapter server.py"
            module = _ilu.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        return sys.modules[mod_name]

    def _serve(self, path: str) -> dict:
        return asyncio.run(self._server()._http_handle_style(path=path, method="GET"))

    def test_merged_css_translated(self):
        resp = self._serve("/ext/dsh_adapter/styles/skin/maid-atelier/merged.css")
        assert resp["status"] == 200
        body = base64.b64decode(resp["body"]).decode("utf-8")
        assert 'data-pane' not in body and 'data-dsh-surface' not in body
        assert 'data-slot' not in body
        assert '[data-region="sidebar"]' in body  # 语料 pane 选择器已转译
        assert '[data-chat-state="empty"]' in body  # hero 空态

    def test_hooks_translated_same_map(self):
        """hooks.mjs 的 JS 选择器与 CSS 同源转译（不转译则 querySelector 恒空）。"""
        resp = self._serve("/ext/dsh_adapter/styles/skin/maid-atelier/hooks.mjs")
        assert resp["status"] == 200
        body = base64.b64decode(resp["body"]).decode("utf-8")
        assert 'data-pane' not in body
        assert "[data-chat-state='empty']" in body  # hooks 源用单引号 → 输出同风格
        # settings 槽 → 触发器（footer 走查由此把装饰标记落到页脚容器）
        assert "[data-testid='sidebar-user-area']" in body
        # :is([data-pane='sidebar'], [class*='sidebarCol']) 两支同位转译到侧栏
        assert ":is([data-region='sidebar'], [data-region='sidebar'])" in body

    def test_hooks_translation_preserves_js_string_quotes(self):
        """引号守恒（回归锁定）：值形替换必须复用原引号风格——双引号输出插进
        hooks 的单引号字符串字面量 = SyntaxError → import 抛 → 整个动态层
        静默死（maid-atelier/xp 实锤，2026-08-22 真机排查根因）。"""
        src = ("const A = \":is([data-pane='sidebar'], [class*='sidebarCol'])\"\n"
               "const B = '[data-pane=\"conversation\"]'\n")
        out = self._server()._sub_position(src)
        assert '"[data-pane=' not in out.replace('="sidebar"', '')  # 无双引号泄漏进单引号串
        assert "\":is([data-region='sidebar'], [data-region='sidebar'])\"" in out
        assert "'[data-region=\"chat\"]'" in out  # 双引号输入保持双引号

    def test_all_skin_hooks_remain_valid_js(self, tmp_path: Path):
        """全部皮肤 hooks 转译后仍为合法 JS（node --check 冒烟；无 node 跳过）。

        语料面 = fixture 皮肤（maid-atelier/cyber-night/xp 三款 hooks，覆盖
        单双引号属性形态）——皮肤真值位是用户数据，仓库不自带语料。
        """
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        node = shutil.which("node")
        if node is None:
            pytest.skip("node 不可用，跳过 JS 语法冒烟")
        for hook in (tmp_path / "skins").glob("*/hooks.mjs"):
            translated = self._server()._sub_position(hook.read_text(encoding="utf-8"))
            tmp_file = tmp_path / f"{hook.parent.name}-hooks.mjs"
            tmp_file.write_text(translated, encoding="utf-8")
            r = subprocess.run([node, "--check", str(tmp_file)],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=30)
            assert r.returncode == 0, f"{hook.parent.name}: {r.stderr.decode()[:200]}"

    def test_unknown_skin_404(self):
        assert self._serve("/ext/dsh_adapter/styles/skin/no-such-skin/merged.css")["status"] == 404
        assert self._serve("/ext/dsh_adapter/styles/skin/../skin-center/hooks.mjs")["status"] == 404

    def test_missing_hooks_empty_200(self):
        """纯 CSS 皮肤（无 hooks.mjs）是常态：回 200 空文本供前端跳过动态层——
        404 会被前端 API 拦截器当错误上报，正常切皮肤不应产生错误。"""
        for skin in ("summer-liquid-glass", "mint", "deep-current"):
            resp = self._serve(f"/ext/dsh_adapter/styles/skin/{skin}/hooks.mjs")
            assert resp["status"] == 200
            assert resp["body"] == ""


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
        assert miku["skin"] == "miku"  # 平台皮肤运行时声明（按择注入端点段）
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

    def test_surface_tokens_alias(self, tmp_path: Path):
        """表面令牌别名（2026-11-10 用户裁定：侧栏/输入面与 DSH 一致）：
        皮肤定义 --dsw-specific-sidebar-fill / --dsw-specific-input-major 时，
        --region-sidebar-bg / --chat-input-bg 发 var() 别名（明暗态由浏览器
        按令牌解析，不吃静态首匹配的亮值）；未定义则维持静态抽取。"""
        root = tmp_path / "skins"
        _write_skin(root, "tokens-skin", (
            ":root {\n  background-color: #16244a;\n  color: #eef2f8;\n"
            "  --dsw-specific-sidebar-fill: #ecf4fcd9;\n"
            "  --dsw-specific-input-major: #060a14db;\n}\n"
            "body[data-ds-dark-theme] {\n"
            "  --dsw-specific-sidebar-fill: #050810d1;\n}\n"
        ))
        _write_skin(root, "plain-skin", _skin_css("#101014", "#d0d4e0"))
        themes = skins_to_plugin_themes(base_dir=root)
        tokens = next(t for t in themes if t["id"] == "dsh-skin-tokens-skin")
        assert tokens["variables"]["--region-sidebar-bg"] == "var(--dsw-specific-sidebar-fill)"
        assert tokens["variables"]["--chat-input-bg"] == "var(--dsw-specific-input-major)"
        plain = next(t for t in themes if t["id"] == "dsh-skin-plain-skin")
        assert plain["variables"].get("--chat-input-bg") is None
        assert plain["variables"].get("--region-sidebar-bg") != "var(--dsw-specific-sidebar-fill)"

    def test_manifest_themes_auto_declared(self):
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        declared = manifest["contributes"]["themes"]
        # on_load 幂等同步契约（2026-10-08）：清单 themes 是装载时同步产物，
        # 文件中出现的条目必须是当次语料翻译的合法产物 id（防手写漂移/陈旧
        # 残留）。语料面 = fixture 语料（id 集合与已提交清单基线一致），干净
        # 环境不依赖本机真实皮肤数据。
        valid_ids = {t["id"] for t in skins_to_plugin_themes()}
        assert {t["id"] for t in declared} <= valid_ids



class TestSkinAssetRoute:
    """背景图资产路由（白名单 + 防穿越）。"""

    def _server(self):
        # 同 TestSkinPositionRoutingCss._server：显式文件级加载防裸名劫持。
        import importlib.util as _ilu

        mod_name = "dsh_adapter_server_asset_test"
        if mod_name not in sys.modules:
            spec = _ilu.spec_from_file_location(mod_name, PLUGIN_DIR / "server.py")
            assert spec is not None and spec.loader is not None, "cannot load dsh_adapter server.py"
            module = _ilu.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        return sys.modules[mod_name]

    def _serve(self, path: str) -> dict:
        return asyncio.run(self._server()._http_handle_style(path=path, method="GET"))

    def test_serve_image_asset(self):
        resp = self._serve("/ext/dsh_adapter/styles/skin-assets/miku/assets/miku-art.webp")
        assert resp["status"] == 200
        assert resp["headers"]["content-type"] == "image/webp"
        assert len(base64.b64decode(resp["body"])) > 10000  # 语料资产（非空体）

    def test_reject_non_image(self):
        resp = self._serve("/ext/dsh_adapter/styles/skin-assets/miku/skin.json")
        assert resp["status"] == 404

    def test_reject_traversal(self):
        resp = self._serve("/ext/dsh_adapter/styles/skin-assets/miku/../../skin.json")
        assert resp["status"] == 404


class TestManifestSkinRoutes:
    """plugin.json 皮肤路由声明 = 皮肤=主题一物终态（清单/选择走主题通道，
    适配器只递送 merged.css / hooks.mjs / 资产三端点）。"""

    def test_manifest_http_endpoints_declared(self):
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        routes = {(e["method"], e["path"]) for e in manifest["http_endpoints"]}
        assert ("GET", "/ext/dsh_adapter/styles/skin/{skin}/merged.css") in routes
        assert ("GET", "/ext/dsh_adapter/styles/skin/{skin}/hooks.mjs") in routes
        assert ("GET", "/ext/dsh_adapter/styles/skin-assets/{skin}/{file:path}") in routes
        # themes 自动声明走 on_load 幂等同步契约（合法产物 id 子集），专测见
        # TestSkinPluginThemes.test_manifest_themes_auto_declared
        valid_ids = {t["id"] for t in skins_to_plugin_themes()}
        assert {t["id"] for t in manifest["contributes"].get("themes", [])} <= valid_ids

    def test_asset_route_is_anonymous_for_native_image_loads(self):
        """资产路由必须 auth:none——皮肤图由浏览器原生加载（背景图 CSS url()／
        hooks 立绘 <img>／favicon），这类请求带不了 Authorization 头，声明 user
        会被内核 /ext 鉴权闸 401，症状即"皮肤配色在、背景图没了"（merged.css /
        hooks.mjs 走 apiClient 带 Bearer，故只有图坏）。与 /uploads 同判例：
        暴露面由图片扩展名白名单 + resolve 目录约束收敛。"""
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        by_path = {e["path"]: e for e in manifest["http_endpoints"]}
        assets = by_path["/ext/dsh_adapter/styles/skin-assets/{skin}/{file:path}"]
        assert assets["auth"] == "none"
        # 反面对照：CSS/脚本走 apiClient（带 Bearer），保持 user——收窄而非全开
        assert by_path["/ext/dsh_adapter/styles/skin/{skin}/merged.css"]["auth"] == "user"
        assert by_path["/ext/dsh_adapter/styles/skin/{skin}/hooks.mjs"]["auth"] == "user"


# ── X3：原生拦截点词表（AC-X3 全量覆盖 + fail-visible） ─────────────────


class TestNativeHookVocabulary:
    def test_mapped_events_yield_checks_and_emit_runs(self):
        r = translate_native_hooks([
            "agent/created", "turn/start", "agent/pre-step", "step/start", "turn/end",
        ])
        assert r["mapped"] == 5
        assert {c["then"] for c in r["checks"]} == {"run:<step>"}
        assert {run["event"] for run in r["runs"]} == {
            "agent/created", "turn/start", "agent/pre-step", "step/start", "turn/end",
        }
        assert all(run["emit"] == {"service": "dsh.hook.emit", "event": run["event"]}
                   for run in r["runs"])
        assert r["unmapped"] == []
        assert r["unknown"] == []

    def test_boundary_pre_post_split(self):
        r = translate_native_hooks(["turn/start", "turn/end"])
        assert [c["at"] for c in r["checks"]] == ["pre:<step>", "post:<step>"]

    def test_substep_guard_unmapped_with_reason(self):
        # 亚步工具守门唯一落点=审批系统（设计 §4.3），禁静默丢弃
        r = translate_native_hooks(["tools/pre-execute", "ctx.tools.guard"])
        assert r["mapped"] == 0
        reasons = {u["event"]: u["reason"] for u in r["unmapped"]}
        assert "审批系统" in reasons["tools/pre-execute"]
        assert "审批系统" in reasons["ctx.tools.guard"]

    def test_known_no_boundary_events_are_unmapped(self):
        r = translate_native_hooks([
            "agent/disposed", "session/event", "subagent/start", "system-prompt/assemble",
            "workspace/session-stop", "internal/status",
        ])
        assert r["mapped"] == 0
        assert len(r["unmapped"]) == 6
        assert all(u["reason"] for u in r["unmapped"])

    def test_unknown_event_fail_visible(self):
        r = translate_native_hooks(["mystery/event"])
        assert r["mapped"] == 0
        assert r["unknown"][0]["event"] == "mystery/event"
        assert "词表外" in r["unknown"][0]["reason"]

    def test_dedup_and_empty(self):
        assert translate_native_hooks(None)["mapped"] == 0
        r = translate_native_hooks(["turn/start", "turn/start"])
        assert r["mapped"] == 1

    def test_vocabulary_covers_rc5_installed_events(self):
        # rc.5 实装包实扫的事件串全部在词表内（无 unknown）——词表漂移即红
        from translator import DSH_NATIVE_HOOK_EVENTS
        scanned = {
            "agent/created", "agent/disposed", "agent/pre-step", "agent/request",
            "turn/start", "turn/end", "step/start", "system-prompt/assemble",
            "tools/pre-execute", "tools/execute", "tools/post-execute", "tools/result",
            "tools/change", "tools/ptc-dispatch-log", "session/created", "session/disposed",
            "session/event", "session/flush", "session/title", "session/title-llm-request",
            "session/end-seed", "subagent/start", "subagent/end", "subagent/catalog",
            "subagent/descriptor", "subagent/model-selection-policy",
            "workspace/session-activity", "workspace/session-stop", "internal/status",
        }
        assert scanned <= set(DSH_NATIVE_HOOK_EVENTS)
        r = translate_native_hooks(sorted(scanned))
        assert r["unknown"] == []

    def test_classifier_detects_extended_events(self, tmp_path):
        pkg = tmp_path / "dsh-guard"
        pkg.mkdir()
        (pkg / "package.json").write_text(json.dumps({"name": "dsh-guard", "version": "1.0.0"}),
                                          encoding="utf-8")
        (pkg / "src").mkdir()
        (pkg / "src" / "index.ts").write_text(
            "export function apply(ctx) {\n"
            "  ctx.on('tools/pre-execute', () => { spawn('node g.mjs'); })\n"
            "  ctx.on('turn/end', () => { spawn('node done.mjs'); })\n"
            "}\n",
            encoding="utf-8",
        )
        kinds = translate_package(pkg)["kinds"]
        assert kinds["hook"]["events"] == ["tools/pre-execute", "turn/end"]
        assert kinds["hook"]["boundaries"] == ["turn/end"]
        assert kinds["hook"]["unmapped"][0]["event"] == "tools/pre-execute"


# ── X4：CC/Codex 方言翻译（AC-X4 静态翻译 + 决策词表） ──────────────────


class TestDialectHooks:
    def test_cc_session_start_maps_to_pre_boundary(self):
        cfg = {"SessionStart": [{"hooks": [{"type": "command", "command": "node init.mjs",
                                            "timeout": 5}]}]}
        r = translate_dialect_hooks(cfg, "claude-code")
        assert r["mapped"] == 1
        assert r["checks"] == [{"at": "pre:<step>", "then": "run:<step>"}]
        run = r["runs"][0]
        assert run["dsh_event"] == "agent/created"
        assert run["command"] == ["node", "init.mjs"]
        assert run["timeout_ms"] == 5000  # 方言 timeout 是秒
        assert r["decision_map"]["deny"].startswith("审批拒绝")
        assert "待单独拍板" in r["decision_map"]["ask"]

    def test_cc_pretooluse_unmapped_to_approval(self):
        cfg = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "g.sh"}]}]}
        r = translate_dialect_hooks(cfg)
        assert r["mapped"] == 0
        assert r["unmapped"][0]["event"] == "PreToolUse"
        assert "审批系统" in r["unmapped"][0]["reason"]

    def test_cc_stop_maps_post_with_matcher_passthrough(self):
        cfg = {"Stop": [{"matcher": "x", "hooks": [{"type": "command", "command": "node s.mjs"}]}]}
        r = translate_dialect_hooks(cfg)
        assert r["checks"][0]["at"] == "post:<step>"
        assert r["runs"][0]["matcher"] == "x"

    def test_codex_vocabulary_differs(self):
        # Codex 词表无 SubagentStart/Stop → 词表外 fail-visible
        cfg = {"SubagentStart": [{"hooks": [{"command": "a.sh"}]}]}
        r = translate_dialect_hooks(cfg, "codex")
        assert r["mapped"] == 0
        assert "词表外" in r["unmapped"][0]["reason"]
        ok = translate_dialect_hooks({"UserPromptSubmit": [{"hooks": [{"command": "b.sh"}]}]}, "codex")
        assert ok["mapped"] == 1

    def test_string_config_json_and_yaml(self):
        r = translate_dialect_hooks(
            json.dumps({"Stop": [{"hooks": [{"command": "node s.mjs"}]}]}))
        assert r["mapped"] == 1
        r2 = translate_dialect_hooks("Stop:\n  - hooks:\n      - command: node s.mjs\n")
        assert r2["mapped"] == 1

    def test_invalid_config_roots(self):
        assert translate_dialect_hooks(None)["mapped"] == 0
        assert translate_dialect_hooks(":::not json nor yaml{{{")["unmapped"][0]["reason"]
        assert translate_dialect_hooks(["not-a-dict"])["unmapped"][0]["reason"]

    def test_invalid_hook_forms_fail_visible(self):
        cfg = {
            "Stop": "not-a-list",
            "SessionStart": [
                {"hooks": "no"},
                {"hooks": [{"type": "script", "command": "x"}]},
                {"hooks": [{"type": "command", "command": ""}]},
                {"hooks": [{"type": "command", "command": "bash 'unclosed"}]},
                {"hooks": [{"type": "command", "command": "x", "timeout": "abc"}]},
                {"hooks": [{"type": "command", "command": "x", "timeout": 0}]},
                "not-a-dict",
            ],
        }
        r = translate_dialect_hooks(cfg)
        assert r["mapped"] == 0
        reasons = [u["reason"] for u in r["unmapped"]]
        assert any("数组" in x for x in reasons)
        assert any("hook type" in x for x in reasons)
        assert any("command 缺失" in x for x in reasons)
        assert any("引号" in x for x in reasons)
        assert any("timeout 非法" in x for x in reasons)
        assert any("timeout 非正" in x for x in reasons)
        assert any("条目不是对象" in x for x in reasons)

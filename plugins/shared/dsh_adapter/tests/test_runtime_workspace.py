# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""DSH npm 运行时工作区（设计 §4.2 C2）——落位/实扫/生命周期状态/boot 裁剪轴。

契约：
1. 解析序 = 显式配置 > ``AGENTOS_DSH_RUNTIME_ROOT`` > 用户空间默认
   （``user_root()/dsh_runtime``，dev=user_root、装机=%APPDATA%/agentos 同源）；
2. ``ensure_workspace`` 幂等：缺 package.json 才脚手架（npm install --prefix 的
   落点，npm 会改写 package.json——已有内容绝不覆盖）；
3. ``installed_packages`` = node_modules 实扫（含 @scope 两层），不读任何写死清单；
4. 生命周期状态落 ``<workspace>/.agentos/dsh-plugins-state.json``（用户数据，
   随工作区走；node_modules 只有 installed 集，enabled/failed/error 在状态文件）；
5. ``boot_plan_for`` = 已启用包贡献面并集（裁剪轴）：空集 → ``{chain: false}``
   （空工作区 boot 不加载任何 DSH 包，AC-R4）；有工具形包 → 最小链 + 包条目；
   webServer/钩子轴为声明位（P1 引擎，本轮恒 false 并记录信号）；
6. 零硬编码绝对路径（AC-R2）：任何路径出自显式配置/env/用户空间解析。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import runtime_workspace as rw  # noqa: E402

pytestmark = pytest.mark.unit


def _mk_pkg(root: Path, node_rel: str, name: str, version: str, *, engines_dsh: str | None = None,
            lib: bool = True, blob: str = "", dependencies: dict | None = None) -> Path:
    """在 node_modules 里造一个 DSH 包目录（npm 装入形态）。"""
    pkg_dir = root / "node_modules" / node_rel
    (pkg_dir / "lib").mkdir(parents=True, exist_ok=True)
    pkg_json: dict = {"name": name, "version": version, "dsh": {}}
    if engines_dsh is not None:
        pkg_json["engines"] = {"dsh": engines_dsh}
    if dependencies:
        pkg_json["dependencies"] = dependencies
    (pkg_dir / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    if lib:
        (pkg_dir / "lib" / "index.js").write_text(blob or "export const apply = () => {}\n", encoding="utf-8")
    return pkg_dir


class TestResolveRuntimeRoot:
    def test_explicit_config_wins_over_env_and_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTOS_DSH_RUNTIME_ROOT", str(tmp_path / "from-env"))
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "from-userroot"))
        got = rw.resolve_runtime_root(str(tmp_path / "explicit"))
        assert got == Path(str(tmp_path / "explicit"))

    def test_env_beats_user_space_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTOS_DSH_RUNTIME_ROOT", str(tmp_path / "from-env"))
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
        assert rw.resolve_runtime_root(None) == tmp_path / "from-env"

    def test_default_lands_in_user_space(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENTOS_DSH_RUNTIME_ROOT", raising=False)
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
        got = rw.resolve_runtime_root(None)
        assert got == tmp_path / "user" / "dsh_runtime"  # 用户空间默认（AC-R2）

    def test_env_empty_string_falls_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTOS_DSH_RUNTIME_ROOT", "   ")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
        assert rw.resolve_runtime_root(None) == tmp_path / "user" / "dsh_runtime"


class TestEnsureWorkspace:
    def test_scaffolds_package_json_once(self, tmp_path: Path) -> None:
        ws = rw.ensure_workspace(tmp_path / "ws")
        assert (ws / "package.json").is_file()
        meta = json.loads((ws / "package.json").read_text(encoding="utf-8"))
        assert meta["name"]  # npm 项目形态（--prefix 落点）
        # 幂等：已有 package.json 不覆盖（npm install 会改写它——再 ensure 不动）
        (ws / "package.json").write_text('{"name": "user-ws", "private": true}', encoding="utf-8")
        rw.ensure_workspace(ws)
        assert json.loads((ws / "package.json").read_text(encoding="utf-8"))["name"] == "user-ws"

    def test_returns_existing_workspace_unchanged(self, tmp_path: Path) -> None:
        ws = tmp_path / "already"
        ws.mkdir()
        assert rw.ensure_workspace(ws) == ws


class TestInstalledPackagesScan:
    def test_scans_plain_and_scoped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _mk_pkg(ws, "dsh-plugin-finance-data", "dsh-plugin-finance-data", "0.3.2")
        _mk_pkg(ws, "@deepseek-ai/dsh-tools", "@deepseek-ai/dsh-tools", "0.2.1-rc.5")
        pkgs = {p["name"]: p for p in rw.installed_packages(ws)}
        assert set(pkgs) == {"dsh-plugin-finance-data", "@deepseek-ai/dsh-tools"}
        assert pkgs["dsh-plugin-finance-data"]["version"] == "0.3.2"
        assert pkgs["dsh-plugin-finance-data"]["dir"] == str(ws / "node_modules" / "dsh-plugin-finance-data")

    def test_missing_node_modules_is_empty_not_error(self, tmp_path: Path) -> None:
        assert rw.installed_packages(tmp_path / "ws") == []  # 空工作区 = initialized:false 语义源

    def test_corrupt_package_json_skipped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        (ws / "node_modules" / "broken").mkdir(parents=True)
        (ws / "node_modules" / "broken" / "package.json").write_text("{not json", encoding="utf-8")
        assert rw.installed_packages(ws) == []


class TestLifecycleState:
    def test_state_roundtrip_and_merge(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        assert rw.read_state(ws) == {"packages": {}}  # 缺省形态
        rw.mark_package_state(ws, "a-pkg", status="installed", requested_version="1.2.3")
        rw.mark_package_state(ws, "a-pkg", enabled=False)  # 合并不丢字段
        st = rw.read_state(ws)
        entry = st["packages"]["a-pkg"]
        assert entry["status"] == "installed"
        assert entry["requested_version"] == "1.2.3"
        assert entry["enabled"] is False
        # 状态文件在 .agentos 下（npm install 不触碰）
        assert (ws / ".agentos" / "dsh-plugins-state.json").is_file()

    def test_corrupt_state_resets_to_default(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        (ws / ".agentos").mkdir(parents=True)
        (ws / ".agentos" / "dsh-plugins-state.json").write_text("{bad", encoding="utf-8")
        assert rw.read_state(ws) == {"packages": {}}


class TestBootPlanTrim:
    def test_empty_workspace_no_boot(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        plan = rw.boot_plan_for(ws)
        assert plan["chain"] is False  # AC-R4：空工作区 boot 不加载任何 DSH 包
        assert plan["packages"] == []

    def test_peers_only_workspace_also_no_boot(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _mk_pkg(ws, "@deepseek-ai/dsh-tools", "@deepseek-ai/dsh-tools", "0.2.1-rc.5")
        plan = rw.boot_plan_for(ws)
        assert plan["chain"] is False  # 只有 boot 基础包 ≠ 有插件

    def test_tool_package_yields_minimal_chain_and_entry(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _mk_pkg(ws, "dsh-plugin-finance-data", "dsh-plugin-finance-data", "0.3.2",
                blob="import { defineTool } from '@deepseek-ai/dsh-tools';\nctx.tools.register(defineTool({name:'finance_data'}))\n")
        plan = rw.boot_plan_for(ws)
        assert plan["chain"] is True
        assert plan["webServer"] is False
        assert plan["hooks"] is False
        assert [p["name"] for p in plan["packages"]] == ["dsh-plugin-finance-data"]
        assert Path(plan["packages"][0]["entry"]) == (
            ws / "node_modules" / "dsh-plugin-finance-data" / "lib" / "index.js"
        )

    def test_web_server_provider_not_loaded_as_plugin_entry(self, tmp_path: Path) -> None:
        # 轴 provider 由轴自身装载：进 packages 会双装载（重复注册路由/服务）
        ws = tmp_path / "ws"
        ws.mkdir()
        _mk_pkg(ws, "dsh-plugin-x", "dsh-plugin-x", "1.0.0",
                blob="ctx.webServer.register({})\n")
        _mk_pkg(ws, "@deepseek-ai/dsh-host-webserver", "@deepseek-ai/dsh-host-webserver",
                "0.2.1-rc.5", blob="export const apply = () => {}\n")
        plan = rw.boot_plan_for(ws)
        names = [p["name"] for p in plan["packages"]]
        assert names == ["dsh-plugin-x"]
        assert plan["webServer"] is True

    def test_disabled_package_excluded_from_plan(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _mk_pkg(ws, "pkg-a", "pkg-a", "1.0.0")
        _mk_pkg(ws, "pkg-b", "pkg-b", "1.0.0")
        rw.mark_package_state(ws, "pkg-b", enabled=False)
        plan = rw.boot_plan_for(ws)
        assert [p["name"] for p in plan["packages"]] == ["pkg-a"]
        assert plan["chain"] is True

    def test_service_signals_enable_webserver_axis(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _mk_pkg(ws, "svc-pkg", "svc-pkg", "1.0.0",
                blob="class S extends Service {}\nctx.webServer.register({})\n")
        plan = rw.boot_plan_for(ws)
        # AC-X2：服务类包的 webServer 路由信号 → 轴开（provider 缺包时桥侧
        # fail-visible，不拖垮工具面）
        assert plan["webServer"] is True
        assert plan["signals"] == {"webServer": True, "hooks": False}

    def test_webserver_axis_off_without_service_signals(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _mk_pkg(ws, "tool-pkg", "tool-pkg", "1.0.0",
                blob="ctx.tools.register(defineTool({name:'t'}))\n")
        plan = rw.boot_plan_for(ws)
        assert plan["webServer"] is False


class TestDshEngineRange:
    def test_reads_engines_dsh_declaration(self, tmp_path: Path) -> None:
        d = _mk_pkg(tmp_path, "x", "x", "1.0.0", engines_dsh=">=0.2.1-rc.2", lib=False)
        meta = json.loads((d / "package.json").read_text(encoding="utf-8"))
        assert rw.dsh_engine_range(meta) == ">=0.2.1-rc.2"

    def test_missing_engines_returns_none(self) -> None:
        assert rw.dsh_engine_range({"name": "x"}) is None


class TestNpmCommandBuilders:
    """npm 命令构造契约：精确版本 specs 由调用方拼、镜像 registry 固定、首元素裸 npm。"""

    def test_install_command_shape(self, tmp_path: Path) -> None:
        cmd = rw.npm_install_command(tmp_path, ["pkg-a@1.2.3", "@scope/b@0.2.1-rc.5"])
        assert cmd[0] == "npm"  # 可移植契约；执行方经 resolve_npm_binary 换平台形态
        assert "--prefix" in cmd and cmd[cmd.index("--prefix") + 1] == str(tmp_path)
        assert "--registry" in cmd and cmd[cmd.index("--registry") + 1] == rw.NPM_REGISTRY_DEFAULT
        assert cmd[-2:] == ["pkg-a@1.2.3", "@scope/b@0.2.1-rc.5"]

    def test_uninstall_command_shape(self, tmp_path: Path) -> None:
        cmd = rw.npm_uninstall_command(tmp_path, ["pkg-a"])
        assert cmd[0] == "npm" and cmd[1] == "uninstall"
        assert cmd[-1] == "pkg-a"

    def test_install_command_legacy_peers_flag_before_specs(self, tmp_path: Path) -> None:
        cmd = rw.npm_install_command(tmp_path, ["pkg-a@1.2.3"], legacy_peers=True)
        assert "--legacy-peer-deps" in cmd
        assert cmd.index("--legacy-peer-deps") < cmd.index("pkg-a@1.2.3")
        assert cmd[-1] == "pkg-a@1.2.3"  # spec 仍占末位（调用方口径）
        assert "--legacy-peer-deps" not in rw.npm_install_command(tmp_path, ["pkg-a@1.2.3"])

    def test_repair_command_force_flag(self, tmp_path: Path) -> None:
        cmd = rw.npm_repair_command(tmp_path)
        assert cmd[0] == "npm"
        assert "--force" in cmd
        assert str(tmp_path) in cmd

    def test_resolve_npm_binary_finds_executable(self) -> None:
        got = rw.resolve_npm_binary()
        assert "npm" in Path(got).name.lower()

    def test_bootstrap_spec_pins_all_peers(self) -> None:
        spec = rw.bootstrap_spec()
        assert len(spec) == len(rw.BOOT_PEER_PACKAGES)
        for item in spec:
            name, _, ver = item.rpartition("@")
            assert rw.BOOT_PEER_PACKAGES[name] == ver  # 全量精确版本 pin


class TestEntryPath:
    def test_prefers_lib_index_js(self, tmp_path: Path) -> None:
        d = _mk_pkg(tmp_path, "x", "x", "1.0.0")
        meta = json.loads((d / "package.json").read_text(encoding="utf-8"))
        assert Path(rw.entry_path(str(d), meta)) == d / "lib" / "index.js"

    def test_falls_back_to_main_field(self, tmp_path: Path) -> None:
        d = tmp_path / "pkg"
        (d / "dist").mkdir(parents=True)
        (d / "dist" / "entry.js").write_text("export {}\n", encoding="utf-8")
        (d / "package.json").write_text(json.dumps({"name": "y", "main": "dist/entry.js"}), encoding="utf-8")
        assert Path(rw.entry_path(str(d), {"main": "dist/entry.js"})) == d / "dist" / "entry.js"

    def test_none_when_no_entry(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        assert rw.entry_path(str(d), {"name": "z"}) is None

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
from typing import Any

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


# ── DSH profile（官方 plugin-manager 落位契约，2026-11-09）────────


class _RunnerStub:
    """bootstrap npm 替身：按 --prefix 造出 manager/pnpm 文件，记录命令。"""

    def __init__(self, *, fail: bool = False, fail_normalize: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail
        self.fail_normalize = fail_normalize

    def __call__(self, cmd: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append(list(cmd))
        if self.fail:
            return (1, "", "boom")
        if "--dir" in cmd:
            if self.fail_normalize:
                return (1, "", "normalize boom")
            ws = Path(cmd[cmd.index("--dir") + 1])
            (ws / "node_modules").mkdir(parents=True, exist_ok=True)
            (ws / "node_modules" / ".modules.yaml").write_text(
                "layoutVersion: 5\n", encoding="utf-8")
            return (0, "", "")
        prefix = Path(cmd[cmd.index("--prefix") + 1])
        joined = " ".join(cmd)
        if "@pnpm/exe" in joined:
            pnpm = prefix / "node_modules" / "@pnpm" / "exe" / "pnpm"
            pnpm.parent.mkdir(parents=True, exist_ok=True)
            pnpm.write_bytes(b"x" * 1_000_001)
        else:
            mgr_dir = prefix / "node_modules" / "@deepseek-ai" / "dsh-plugin-manager"
            mgr_dir.mkdir(parents=True, exist_ok=True)
            (mgr_dir / "package.json").write_text(json.dumps({
                "name": "@deepseek-ai/dsh-plugin-manager", "version": rw.MANAGER_VERSION,
            }), encoding="utf-8")
        return (0, "", "")


class TestProfileWorkspace:
    def test_ensure_workspace_writes_profile_files(self, tmp_path: Path) -> None:
        ws = rw.ensure_workspace(tmp_path / "ws")
        manifest = json.loads((ws / "package.json").read_text(encoding="utf-8"))
        assert manifest["dsh"]["profile"]["bundles"] == []
        assert (ws / "cordis.yml").read_text(encoding="utf-8").strip() == "[]"
        assert (ws / "cordis.patch.yml").read_text(encoding="utf-8").strip() == "[]"
        workspace_yaml = (ws / "pnpm-workspace.yaml").read_text(encoding="utf-8")
        assert "nodeLinker: hoisted" in workspace_yaml
        assert "autoInstallPeers: true" in workspace_yaml
        assert "allowBuilds:" in workspace_yaml
        assert "'koffi': true" in workspace_yaml
        assert (ws / ".agentos" / "install-anchor" / "package.json").is_file()
        assert (ws / ".dsh-home").is_dir()

    def test_ensure_workspace_idempotent_keeps_user_edits(self, tmp_path: Path) -> None:
        ws = rw.ensure_workspace(tmp_path / "ws")
        (ws / "cordis.patch.yml").write_text("- id: custom\n", encoding="utf-8")
        rw.ensure_workspace(ws)
        assert (ws / "cordis.patch.yml").read_text(encoding="utf-8") == "- id: custom\n"

    def test_migrates_bundle_dependencies(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        pkg = ws / "node_modules" / "bundle-a"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({
            "name": "bundle-a", "version": "1.0.0",
            "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
        }), encoding="utf-8")
        (ws / "package.json").write_text(json.dumps({
            "name": "old", "dependencies": {"bundle-a": "1.0.0", "plain-a": "1.0.0"},
        }), encoding="utf-8")
        rw.ensure_workspace(ws)
        assert rw.profile_bundles(ws) == ["bundle-a"]

    def test_drops_pnpm_exe_from_profile_dependencies(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "package.json").write_text(json.dumps({
            "name": "old", "dependencies": {rw.PNPM_PACKAGE: rw.PNPM_VERSION},
            "dsh": {"profile": {"bundles": []}},
        }), encoding="utf-8")
        rw.ensure_workspace(ws)
        assert rw.PNPM_PACKAGE not in rw.profile_manifest(ws)["dependencies"]

    def test_corrupt_manifest_rebuilt(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "package.json").write_text("{not-json", encoding="utf-8")
        rw.ensure_workspace(ws)
        assert rw.profile_manifest(ws)["dsh"]["profile"]["bundles"] == []

    def test_manifest_missing_name_is_filled(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "package.json").write_text(json.dumps({"private": True}), encoding="utf-8")
        rw.ensure_workspace(ws)
        assert rw.profile_manifest(ws)["name"] == "agentos-dsh-profile"

    def test_profile_bundles_tolerates_non_list_shapes(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "package.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
        assert rw.profile_bundles(ws) == []
        (ws / "package.json").write_text(
            json.dumps({"dsh": {"profile": []}}), encoding="utf-8")
        assert rw.profile_bundles(ws) == []
        (ws / "package.json").write_text(
            json.dumps({"dsh": {"profile": {"bundles": "x"}}}), encoding="utf-8")
        assert rw.profile_bundles(ws) == []

    def test_profile_boot_plan_chain_follows_bundles(self, tmp_path: Path) -> None:
        ws = rw.ensure_workspace(tmp_path / "ws")
        assert rw.profile_boot_plan(ws)["chain"] is False
        manifest = rw.profile_manifest(ws)
        manifest["dsh"]["profile"]["bundles"] = ["bundle-a"]
        (ws / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        plan = rw.profile_boot_plan(ws)
        assert plan["source"] == "profile"
        assert plan["chain"] is True
        assert plan["webServer"] is True  # 有 bundle 即挂 webServer 轴
        assert "fallback" in plan

    def test_bootstrapped_profile_ignores_scan_chain(self, tmp_path: Path) -> None:
        """bootstrap 后 node_modules 里是 manager 栈/peer：空 bundles 不得 boot 链。"""
        ws = rw.ensure_workspace(tmp_path / "ws")
        pkg = ws / "node_modules" / "tool-ish"
        (pkg / "lib").mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({
            "name": "tool-ish", "version": "1.0.0", "main": "lib/index.js",
        }), encoding="utf-8")
        (pkg / "lib" / "index.js").write_text(
            "ctx.tools.register(defineTool({}))\n", encoding="utf-8")
        assert rw.boot_plan_for(ws)["chain"] is True
        assert rw.profile_boot_plan(ws)["chain"] is True  # 未 bootstrap：跟随扫描
        mgr_dir = ws / "node_modules" / "@deepseek-ai" / "dsh-plugin-manager"
        mgr_dir.mkdir(parents=True)
        (mgr_dir / "package.json").write_text(json.dumps({
            "name": rw.MANAGER_PACKAGE, "version": rw.MANAGER_VERSION,
        }), encoding="utf-8")
        assert rw.profile_boot_plan(ws)["chain"] is False
        assert rw.profile_boot_plan(ws)["webServer"] is False  # 空 profile 不挂 webServer


class TestPnpmResolution:
    def test_valid_runtime_binary(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        pnpm = ws / rw.PNPM_RUNTIME_DIR / "node_modules" / "@pnpm" / "exe" / "pnpm"
        pnpm.parent.mkdir(parents=True)
        pnpm.write_bytes(b"x" * 1_000_001)
        assert rw.resolve_pnpm_binary(ws) == str(pnpm)
        assert rw.resolve_pnpm_invocation(ws) == (str(pnpm), [])

    def test_placeholder_rejected(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        pnpm = ws / "node_modules" / "@pnpm" / "exe" / "pnpm"
        pnpm.parent.mkdir(parents=True)
        pnpm.write_bytes(b"placeholder")
        assert rw.resolve_pnpm_binary(ws) is None

    def test_dist_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = tmp_path / "ws"
        dist = ws / "node_modules" / "@pnpm" / "exe" / "dist" / "pnpm.mjs"
        dist.parent.mkdir(parents=True)
        dist.write_text("// pnpm\n", encoding="utf-8")
        monkeypatch.setattr(rw.shutil, "which", lambda name: "node.exe" if name == "node" else None)
        assert rw.resolve_pnpm_invocation(ws) == ("node.exe", [str(dist)])

    def test_missing_returns_none(self, tmp_path: Path) -> None:
        assert rw.resolve_pnpm_invocation(tmp_path / "empty") == (None, [])

    def test_relative_root_yields_absolute_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """命令跨进程交给 Node/pnpm：相对 root 必须归一成绝对路径。"""
        monkeypatch.chdir(tmp_path)
        ws = Path("ws")
        pnpm = ws / rw.PNPM_RUNTIME_DIR / "node_modules" / "@pnpm" / "exe" / "pnpm"
        pnpm.parent.mkdir(parents=True)
        pnpm.write_bytes(b"x" * 1_000_001)
        binary = rw.resolve_pnpm_binary(ws)
        assert binary == str(pnpm.resolve())
        assert Path(binary).is_absolute()
        assert rw.resolve_pnpm_invocation(ws) == (str(pnpm.resolve()), [])

    def test_binary_stat_error_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_self: Path, *_args: Any, **_kwargs: Any) -> Any:
            raise OSError("stat failed")

        monkeypatch.setattr(rw.Path, "stat", boom)
        assert rw._valid_pnpm_binary(rw.Path("pnpm")) is False


class TestNodeVersion:
    def test_parses_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Proc:
            returncode = 0
            stdout = "v22.19.0\n"

        monkeypatch.setattr(rw.subprocess, "run", lambda *_a, **_k: Proc())
        assert rw.node_version() == (22, 19, 0)

    def test_missing_or_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Proc:
            returncode = 1
            stdout = ""

        monkeypatch.setattr(rw.subprocess, "run", lambda *_a, **_k: Proc())
        assert rw.node_version() is None

        class Bad:
            returncode = 0
            stdout = "garbage"

        monkeypatch.setattr(rw.subprocess, "run", lambda *_a, **_k: Bad())
        assert rw.node_version() is None

        def boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("no node")

        monkeypatch.setattr(rw.subprocess, "run", boom)
        assert rw.node_version() is None


class TestBootstrap:
    def test_profile_specs_exclude_pnpm(self) -> None:
        specs = rw.profile_bootstrap_specs()
        assert any(spec.startswith("@deepseek-ai/dsh-plugin-manager@") for spec in specs)
        assert not any(spec.startswith(f"{rw.PNPM_PACKAGE}@") for spec in specs)
        # webServer 轴：社区 bundle 的 inject 目标，bootstrap 必须预装
        assert any(spec.startswith("@deepseek-ai/dsh-host-webserver@") for spec in specs)
        # 宿主服务提供包：插件只 peer 接缝，实现包必须由 bootstrap 预置
        # （缺 credentials-local → connection 服务缺席 → pet 恒 pending，dev 实测）
        for provider in ("dsh-credentials-local", "dsh-attachment-local",
                         "dsh-storage-json", "dsh-storage-domain",
                         "dsh-llm-deepseek-api-key", "cordis-plugin-timer"):
            assert any(spec.startswith(f"@deepseek-ai/{provider}@") for spec in specs), provider

    def test_bootstrap_commands(self, tmp_path: Path) -> None:
        cmd = rw.bootstrap_command(tmp_path)
        # 链包 peer 是导入期硬依赖：必须让 npm 自动补齐（实测缺 dsh-sandbox 即 boot 失败）
        assert "--legacy-peer-deps" not in cmd
        assert "--package-lock=false" in cmd
        pnpm_cmd = rw.pnpm_bootstrap_command(tmp_path)
        assert str(tmp_path / rw.PNPM_RUNTIME_DIR) in pnpm_cmd
        assert f"{rw.PNPM_PACKAGE}@{rw.PNPM_VERSION}" in pnpm_cmd
        norm = rw.pnpm_normalize_command(tmp_path, "/abs/pnpm", ["--arg"], "https://r/")
        assert norm[:3] == ["/abs/pnpm", "--arg", "--dir"]
        assert str(tmp_path.resolve()) in norm
        assert "--registry=https://r/" in norm

    def test_ready_short_circuits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = rw.ensure_workspace(tmp_path / "ws")
        mgr_dir = ws / "node_modules" / "@deepseek-ai" / "dsh-plugin-manager"
        mgr_dir.mkdir(parents=True)
        (mgr_dir / "package.json").write_text(json.dumps({
            "name": "@deepseek-ai/dsh-plugin-manager", "version": rw.MANAGER_VERSION,
        }), encoding="utf-8")
        pnpm = ws / rw.PNPM_RUNTIME_DIR / "node_modules" / "@pnpm" / "exe" / "pnpm"
        pnpm.parent.mkdir(parents=True)
        pnpm.write_bytes(b"x" * 1_000_001)
        (ws / "node_modules" / ".modules.yaml").write_text(
            "layoutVersion: 5\n", encoding="utf-8")
        runner = _RunnerStub()
        out = rw.ensure_bootstrapped(ws, runner)
        assert out["ready"] is True
        assert out["bootstrapped"] is False
        assert runner.calls == []

    def test_existing_npm_tree_gets_normalized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """旧版 bootstrap 现场（manager+pnpm 在位、缺 .modules.yaml）只补归一。"""
        monkeypatch.setattr(rw, "node_version", lambda: (22, 19, 0))
        monkeypatch.setattr(rw.shutil, "which", lambda name: "npm.cmd" if name == "npm" else None)
        ws = rw.ensure_workspace(tmp_path / "ws")
        mgr_dir = ws / "node_modules" / "@deepseek-ai" / "dsh-plugin-manager"
        mgr_dir.mkdir(parents=True)
        (mgr_dir / "package.json").write_text(json.dumps({
            "name": "@deepseek-ai/dsh-plugin-manager", "version": rw.MANAGER_VERSION,
        }), encoding="utf-8")
        pnpm = ws / rw.PNPM_RUNTIME_DIR / "node_modules" / "@pnpm" / "exe" / "pnpm"
        pnpm.parent.mkdir(parents=True)
        pnpm.write_bytes(b"x" * 1_000_001)
        runner = _RunnerStub()
        out = rw.ensure_bootstrapped(ws, runner)
        assert out["bootstrapped"] is True
        assert len(runner.calls) == 1
        assert "--dir" in runner.calls[0]

    def test_bootstraps_manager_and_pnpm(self, tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rw, "node_version", lambda: (22, 19, 0))
        monkeypatch.setattr(rw.shutil, "which", lambda name: "npm.cmd" if name == "npm" else None)
        runner = _RunnerStub()
        out = rw.ensure_bootstrapped(tmp_path / "ws", runner)
        assert out["bootstrapped"] is True
        assert len(runner.calls) == 3
        assert any("@deepseek-ai/dsh-plugin-manager" in " ".join(c) for c in runner.calls)
        assert any("@pnpm/exe" in " ".join(c) for c in runner.calls)
        normalize = [c for c in runner.calls if "--dir" in c]
        assert len(normalize) == 1
        assert normalize[0][-1].startswith("--registry=")

    def test_node_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rw, "node_version", lambda: None)
        with pytest.raises(rw.WorkspaceBootstrapError) as ei:
            rw.ensure_bootstrapped(tmp_path / "ws", _RunnerStub())
        assert ei.value.code == "node_missing"

    def test_node_too_old(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rw, "node_version", lambda: (20, 0, 0))
        with pytest.raises(rw.WorkspaceBootstrapError) as ei:
            rw.ensure_bootstrapped(tmp_path / "ws", _RunnerStub())
        assert ei.value.code == "node_too_old"

    def test_npm_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rw, "node_version", lambda: (22, 19, 0))
        monkeypatch.setattr(rw.shutil, "which", lambda _name: None)
        with pytest.raises(rw.WorkspaceBootstrapError) as ei:
            rw.ensure_bootstrapped(tmp_path / "ws", _RunnerStub())
        assert ei.value.code == "npm_missing"

    def test_bootstrap_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rw, "node_version", lambda: (22, 19, 0))
        monkeypatch.setattr(rw.shutil, "which", lambda name: "npm.cmd" if name == "npm" else None)
        with pytest.raises(rw.WorkspaceBootstrapError) as ei:
            rw.ensure_bootstrapped(tmp_path / "ws", _RunnerStub(fail=True))
        assert ei.value.code == "bootstrap_failed"
        assert "boom" in ei.value.message

    def test_pnpm_bootstrap_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rw, "node_version", lambda: (22, 19, 0))
        monkeypatch.setattr(rw.shutil, "which", lambda name: "npm.cmd" if name == "npm" else None)
        ws = rw.ensure_workspace(tmp_path / "ws")
        mgr_dir = ws / "node_modules" / "@deepseek-ai" / "dsh-plugin-manager"
        mgr_dir.mkdir(parents=True)
        (mgr_dir / "package.json").write_text(json.dumps({
            "name": "@deepseek-ai/dsh-plugin-manager", "version": rw.MANAGER_VERSION,
        }), encoding="utf-8")
        with pytest.raises(rw.WorkspaceBootstrapError) as ei:
            rw.ensure_bootstrapped(ws, _RunnerStub(fail=True))
        assert ei.value.code == "bootstrap_failed"
        assert "pnpm bootstrap" in ei.value.message

    def test_pnpm_still_missing_after_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rw, "node_version", lambda: (22, 19, 0))
        monkeypatch.setattr(rw.shutil, "which", lambda name: "npm.cmd" if name == "npm" else None)

        def manager_only(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
            prefix = Path(cmd[cmd.index("--prefix") + 1])
            mgr_dir = prefix / "node_modules" / "@deepseek-ai" / "dsh-plugin-manager"
            mgr_dir.mkdir(parents=True, exist_ok=True)
            (mgr_dir / "package.json").write_text(json.dumps({
                "name": "@deepseek-ai/dsh-plugin-manager", "version": rw.MANAGER_VERSION,
            }), encoding="utf-8")
            return (0, "", "")

        with pytest.raises(rw.WorkspaceBootstrapError) as ei:
            rw.ensure_bootstrapped(tmp_path / "ws", manager_only)
        assert ei.value.code == "bootstrap_failed"
        assert "未找到 @pnpm/exe" in ei.value.message

    def test_pnpm_normalize_failure(self, tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rw, "node_version", lambda: (22, 19, 0))
        monkeypatch.setattr(rw.shutil, "which", lambda name: "npm.cmd" if name == "npm" else None)
        with pytest.raises(rw.WorkspaceBootstrapError) as ei:
            rw.ensure_bootstrapped(tmp_path / "ws", _RunnerStub(fail_normalize=True))
        assert ei.value.code == "bootstrap_failed"
        assert "pnpm 树归一失败" in ei.value.message

    def test_default_runner_returns_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        monkeypatch.setattr(rw.subprocess, "run", lambda *_a, **_k: Proc())
        assert rw._default_runner(["npm", "install"], 10) == (0, "ok", "")

    def test_default_runner_passes_pnpm_command_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, list[str]] = {}

        class Proc:
            returncode = 0
            stdout = "11.26.0"
            stderr = ""

        def fake_run(cmd: list[str], **_kwargs: Any) -> Proc:
            seen["cmd"] = cmd
            return Proc()

        monkeypatch.setattr(rw.subprocess, "run", fake_run)
        rw._default_runner(["/abs/pnpm", "--version"], 10)
        assert seen["cmd"] == ["/abs/pnpm", "--version"]

    def test_default_runner_spawn_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("no npm")

        monkeypatch.setattr(rw.subprocess, "run", boom)
        rc, _out, err = rw._default_runner(["npm", "install"], 10)
        assert rc == 1
        assert "no npm" in err


class TestDefaultAgentModel:
    """灵汐模型配置 → DSH agentDefaultModel 服务配置（功能等价翻译）。

    配置源 = 内核按 manifest ``config_files[id=llm]`` 注入的 llm.yaml 视图
    （``plugin.get_config()``），本模块不直读 config/（门禁口径）。
    """

    def test_translates_defaults_chat_model(self) -> None:
        cfg = {
            "defaults": {"chat": "m1"},
            "models": {"m1": {"provider": "deepseek", "model_name": "deepseek-v4-flash"}},
        }
        assert rw.default_agent_model(cfg) == {
            "provider": "deepseek", "model": "deepseek-v4-flash"}

    def test_missing_config_returns_none(self) -> None:
        assert rw.default_agent_model(None) is None

    def test_provider_supplies_injected_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = {
            "defaults": {"chat": "m2"},
            "models": {"m2": {"provider": "minimax", "model_name": "minimax-m3"}},
        }
        monkeypatch.setattr(rw, "_llm_config_provider", lambda: cfg)
        assert rw.default_agent_model() == {"provider": "minimax", "model": "minimax-m3"}

    def test_provider_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Any:
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(rw, "_llm_config_provider", boom)
        assert rw.default_agent_model() is None

    def test_incomplete_entry_returns_none(self) -> None:
        cfg = {"defaults": {"chat": "m1"}, "models": {"m1": {"provider": "x"}}}
        assert rw.default_agent_model(cfg) is None

    @pytest.mark.parametrize("cfg", [
        ["not-a-mapping"],
        {},
        {"defaults": {}, "models": {}},
        {"defaults": {"chat": "m1"}, "models": {"m1": "plain-string"}},
        {"defaults": {"chat": "m1"}, "models": {"m1": {"provider": "", "model_name": "x"}}},
        {"defaults": {"chat": "m1"}, "models": {"m1": {"provider": "x", "model_name": ""}}},
    ])
    def test_malformed_config_returns_none(self, cfg: Any) -> None:
        assert rw.default_agent_model(cfg) is None

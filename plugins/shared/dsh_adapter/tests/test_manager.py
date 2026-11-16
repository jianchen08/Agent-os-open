# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""DSH 包管理器（设计 §4.1 C1：list/install/uninstall/enable/disable 五件套）。

契约：
- **实扫清单（AC-M1）**：数据来自运行时工作区 node_modules 实扫 + 状态文件，
  不读写死清单；空工作区 → ``{initialized: false}``；
- **install 审批（§九.1 用户裁定面）**：第三方 npm 装 = 第三方代码进执行面，
  install 走 human-interaction capability（create_choice/wait_for_choice 既有
  面——sidecar HTTP handler 无 pipeline run 可 suspend，pending 返回 + 背景任务
  由用户裁决 resume 即同构复用）；审批卡内容 = 翻译预览（装什么注册什么）；
  审批通道不可用 → 结构化错误 fail-visible（不静默绕过）；deny/超时 → 拒绝；
- **engines.dsh 安装前拦截**：npm view 元数据查询（零下载）判 engines，
  不兼容在审批与工作区写盘**之前**拦截（结构化错误码）；
- **缺省版本 = 索引最新精确版本**（禁 dist-tag）；不在索引且无显式版本 →
  结构化错误（要显式版本）；
- **uninstall 依赖保护**：其他已装包 dependencies/peerDependencies 引用即拒
  （列依赖者）；皮肤包卸载回收已同步的皮肤目录并重翻译主题面；
- **enable/disable（AC-R3）**：状态文件即生效（下轮 boot 裁剪轴重算），皮肤
  包 disable = 皮肤目录回收（主题选择器即消）；未安装包报错；
- **install_local（测试通道专用）**：本地目录装入工作区（不走审批——非第三方
  registry 拉 取），engines 检查对本地 package.json 直接判。

npm 执行/索引抓取/审批 capability 全部注入（外部依赖边界），单测零在线零
真实 npm。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import manager as mgr  # noqa: E402

pytestmark = pytest.mark.unit


# ── 桩件 ─────────────────────────────────────────────────


class NpmStub:
    """npm 命令替身：按 argv 关键词路由到预置行为（外部边界可观察面）。

    成功路径模拟 npm 的文件系统效果（uninstall 删 node_modules 条目、本地
    install 拷入）——manager 信任 npm 的落盘语义，替身如实呈现同款效果。
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.view_results: dict[str, str] = {}  # pkg@ver → stdout（--json）
        self.view_errors: dict[str, str] = {}
        self.pack_dirs: dict[str, Path] = {}  # pkg@ver → 预置包目录（pack 输出 tarball 用）
        self.install_ok: set[str] = set()  # 允许成功的 install spec
        self.uninstall_ok: set[str] = set()

    def __call__(self, cmd: list[str], timeout: float) -> tuple[int, str, str]:
        import shutil as _shutil

        self.calls.append(list(cmd))
        if "view" in cmd:
            spec = cmd[cmd.index("view") + 1]
            if spec in self.view_errors:
                return (1, "", self.view_errors[spec])
            out = self.view_results.get(spec)
            if out is None:
                return (1, "", "npm error code E404")
            return (0, out, "")
        if "pack" in cmd:
            spec = cmd[cmd.index("pack") + 1]
            src = self.pack_dirs.get(spec)
            if src is None:
                return (1, "", "npm error pack failed")
            dest = Path(cmd[cmd.index("--pack-destination") + 1]) / f"{src.name}.tgz"
            _make_tgz(src, dest)
            return (0, f"{dest}\n", "")
        if "install" in cmd:
            prefix = Path(cmd[cmd.index("--prefix") + 1]) if "--prefix" in cmd else None
            spec = cmd[-1]
            if spec not in self.install_ok:
                return (1, "", f"npm error install failed: {spec}")
            # 本地路径 install：模拟 npm 拷入 node_modules/<pkg.name>
            src = Path(spec)
            if src.is_dir() and prefix is not None:
                try:
                    pj = json.loads((src / "package.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pj = {}
                name = str(pj.get("name") or src.name)
                dest = prefix / "node_modules" / name
                if dest.exists():
                    _shutil.rmtree(dest)
                _shutil.copytree(src, dest)
            return (0, "added 1 package", "")
        if "uninstall" in cmd:
            spec = cmd[-1]
            if spec not in self.uninstall_ok:
                return (1, "", f"npm error not installed: {spec}")
            if "--prefix" in cmd:
                prefix = Path(cmd[cmd.index("--prefix") + 1])
                for cand in (prefix / "node_modules" / spec,):
                    if cand.exists():
                        _shutil.rmtree(cand)
            return (0, "removed 1 package", "")
        return (0, "", "")


def _manifest_view(pkg_json: dict[str, Any]) -> str:
    keep = {k: pkg_json[k] for k in ("name", "version", "engines") if k in pkg_json}
    return json.dumps(keep)


def _make_tgz(src: Path, dest: Path) -> None:
    """打包目录为 npm 形态 tarball（package/<files>，真 tar.gz——preview 解包走真代码）。"""
    import io  # noqa: PLC0415
    import tarfile  # noqa: PLC0415

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, mode="w:gz") as tf:
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            arcname = "package/" + str(p.relative_to(src)).replace("\\", "/")
            data = p.read_bytes()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


class ApprovalStub:
    """human-interaction capability 替身：记录卡片内容，裁决可编程。"""

    def __init__(self, decision: str = "approve", *, available: bool = True) -> None:
        self.available = available
        self.decision = decision
        self.cards: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if not self.available:
            raise KeyError("human-interaction")
        if method == "create_choice":
            self.cards.append(params)
            return {"request_id": "req-1", "ok": True}
        if method == "wait_for_choice":
            if self.decision == "timeout":
                return {"ok": False, "status": "timeout"}
            return {"ok": True, "status": "answered",
                    "response": {"response_type": "answered", "selected_option": self.decision}}
        if method == "cancel":
            self.cancelled.append(str(params.get("request_id")))
            return {"ok": True}
        raise AssertionError(f"unexpected method {method}")


def _mk_workspace_pkg(ws: Path, node_rel: str, name: str, version: str, *,
                      deps: dict | None = None, peers: dict | None = None,
                      engines_dsh: str | None = None, blob: str = "",
                      skins: list[str] | None = None) -> Path:
    pkg_dir = ws / "node_modules" / node_rel
    pkg_dir.mkdir(parents=True, exist_ok=True)
    pj: dict[str, Any] = {"name": name, "version": version}
    if deps:
        pj["dependencies"] = deps
    if peers:
        pj["peerDependencies"] = peers
    if engines_dsh:
        pj["engines"] = {"dsh": engines_dsh}
    (pkg_dir / "package.json").write_text(json.dumps(pj), encoding="utf-8")
    (pkg_dir / "lib").mkdir(exist_ok=True)
    (pkg_dir / "lib" / "index.js").write_text(blob or "export const apply = () => {}\n", encoding="utf-8")
    for skin in skins or []:
        sd = pkg_dir / "skins" / skin
        sd.mkdir(parents=True)
        (sd / "skin.json").write_text(json.dumps({
            "skinManifestVersion": 2, "id": skin, "name": skin,
            "contributes": {"stylesheet": "skin.css"},
        }), encoding="utf-8")
        (sd / "skin.css").write_text(":root { --dsw-alias-bg-base: #eef5ff; color: #14263f; }", encoding="utf-8")
    return pkg_dir


def _local_pkg(src: Path, name: str, version: str, *, engines_dsh: str | None = None,
              skins: list[str] | None = None) -> Path:
    src.mkdir(parents=True, exist_ok=True)
    pj: dict[str, Any] = {"name": name, "version": version}
    if engines_dsh:
        pj["engines"] = {"dsh": engines_dsh}
    (src / "package.json").write_text(json.dumps(pj), encoding="utf-8")
    (src / "lib").mkdir(exist_ok=True)
    (src / "lib" / "index.js").write_text("export const apply = () => {}\n", encoding="utf-8")
    for skin in skins or []:
        sd = src / "skins" / skin
        sd.mkdir(parents=True)
        (sd / "skin.json").write_text(json.dumps({
            "skinManifestVersion": 2, "id": skin, "name": skin,
            "contributes": {"stylesheet": "skin.css"},
        }), encoding="utf-8")
        (sd / "skin.css").write_text(":root { --dsw-alias-bg-base: #f4efe4; color: #262319; }", encoding="utf-8")
    return src


def _make_manager(tmp_path: Path, npm: NpmStub, approval: ApprovalStub | None = None,
                  *, skins_dir: Path | None = None, config: dict | None = None,
                  index_plugins: list[dict] | None = None) -> mgr.DshPackageManager:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    from runtime_workspace import ensure_workspace

    ensure_workspace(ws)
    skins = skins_dir or (tmp_path / "skins-dir")

    async def fetcher(url: str, timeout_s: float) -> bytes:
        raise OSError("no network in unit tests")  # 索引经 fetch_result 注入替代

    m = mgr.DshPackageManager(
        runtime_root=str(ws), skins_dir=str(skins),
        npm_runner=npm, capability_factory=(lambda: approval) if approval is not None else (lambda: None),
        fetcher=fetcher,
        config=config or {},
    )
    if index_plugins is not None:
        m.set_index_plugins(index_plugins)
    return m


# ── list_installed（AC-M1）────────────────────────────────


class TestListInstalled:
    def test_empty_workspace_not_initialized(self, tmp_path: Path) -> None:
        m = _make_manager(tmp_path, NpmStub())
        out = asyncio.run(m.list_installed())
        assert out["initialized"] is False
        assert out["packages"] == []

    def test_real_scan_with_state_and_kinds(self, tmp_path: Path) -> None:
        m = _make_manager(tmp_path, NpmStub())
        ws = Path(m.runtime_root)
        _mk_workspace_pkg(ws, "dsh-plugin-finance-data", "dsh-plugin-finance-data", "0.3.2",
                          blob="ctx.tools.register(defineTool({name:'finance_data'}))\n")
        _mk_workspace_pkg(ws, "@deepseek-ai/dsh-tools", "@deepseek-ai/dsh-tools", "0.2.1-rc.5")
        _mk_workspace_pkg(ws, "skin-pkg", "skin-pkg", "1.0.0", blob="", skins=["mint-test"])
        out = asyncio.run(m.list_installed())
        assert out["initialized"] is True
        by = {p["name"]: p for p in out["packages"]}
        assert "@deepseek-ai/dsh-tools" not in by  # 基础包不是插件条目
        fin = by["dsh-plugin-finance-data"]
        assert fin["version"] == "0.3.2"
        assert fin["status"] == "enabled"  # 默认启用
        assert fin["enabled"] is True
        assert fin["contribution"]["tools"] is True
        skin = by["skin-pkg"]
        assert skin["contribution"]["themes"] == 1  # 翻译预览摘要（M3）

    def test_disabled_and_failed_states_visible(self, tmp_path: Path) -> None:
        m = _make_manager(tmp_path, NpmStub())
        ws = Path(m.runtime_root)
        _mk_workspace_pkg(ws, "pkg-a", "pkg-a", "1.0.0")
        _mk_workspace_pkg(ws, "pkg-b", "pkg-b", "1.0.0")
        mgr.mark_package_state(ws, "pkg-a", enabled=False, status="disabled")
        mgr.mark_package_state(ws, "pkg-b", status="failed", error="boot failed: boom")
        out = asyncio.run(m.list_installed())
        by = {p["name"]: p for p in out["packages"]}
        assert by["pkg-a"]["status"] == "disabled"
        assert by["pkg-b"]["status"] == "failed"
        assert "boom" in by["pkg-b"]["error"]


# ── install：版本解析与 engines 拦截 ──────────────────────


class TestInstallGuards:
    def test_not_in_index_without_version_requires_explicit(self, tmp_path: Path) -> None:
        npm = NpmStub()
        m = _make_manager(tmp_path, npm, index_plugins=[])
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install("some-unknown-pkg"))
        assert ei.value.code == "version_required_not_in_index"
        assert npm.calls == []  # 零 npm 副作用

    def test_engines_incompatible_blocked_before_download(self, tmp_path: Path) -> None:
        npm = NpmStub()
        npm.view_results["future-pkg@9.9.9"] = _manifest_view(
            {"name": "future-pkg", "version": "9.9.9", "engines": {"dsh": ">=0.9.0"}})
        m = _make_manager(tmp_path, npm, index_plugins=[])
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install("future-pkg", version="9.9.9"))
        assert ei.value.code == "engines_incompatible"
        assert ei.value.detail["engines"] == ">=0.9.0"
        assert not any("pack" in c or "install" in c for c in npm.calls)  # 审批/写盘之前拦截

    def test_package_not_found_is_structured(self, tmp_path: Path) -> None:
        npm = NpmStub()
        npm.view_errors["ghost@1.0.0"] = "npm error code E404\nnpm error 404 Not Found"
        m = _make_manager(tmp_path, npm, index_plugins=[])
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install("ghost", version="1.0.0"))
        assert ei.value.code == "package_not_found"

    def test_registry_unreachable_is_structured(self, tmp_path: Path) -> None:
        npm = NpmStub()
        npm.view_errors["net-pkg@1.0.0"] = "npm error network request failed"
        m = _make_manager(tmp_path, npm, index_plugins=[])
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install("net-pkg", version="1.0.0"))
        assert ei.value.code == "registry_unreachable"

    def test_engines_compatible_proceeds_to_preview_and_approval(self, tmp_path: Path) -> None:
        npm = NpmStub()
        src = _local_pkg(tmp_path / "pkgsrc", "dsh-plugin-finance-data", "0.3.2",
                         engines_dsh=">=0.2.1-rc.2")
        npm.view_results["dsh-plugin-finance-data@0.3.2"] = _manifest_view(
            {"name": "dsh-plugin-finance-data", "version": "0.3.2", "engines": {"dsh": ">=0.2.1-rc.2"}})
        npm.pack_dirs["dsh-plugin-finance-data@0.3.2"] = src
        npm.install_ok.add("dsh-plugin-finance-data@0.3.2")
        approval = ApprovalStub(decision="approve")
        m = _make_manager(tmp_path, npm, approval)
        out = asyncio.run(m.install("dsh-plugin-finance-data", version="0.3.2"))
        assert out["status"] == "pending_approval"
        assert out["request_id"] == "req-1"
        # 审批卡 = 翻译预览（卡内 description 携带装什么注册什么）
        card = approval.cards[0]
        assert "dsh-plugin-finance-data" in (card.get("title") or "")
        assert "翻译预览" in (card.get("description") or "")
        assert "dsh-plugin-finance-data" in (card.get("description") or "")
        opts = {o["id"] for o in card.get("options", [])}
        assert "approve" in opts
        assert "deny" in opts


# ── install：审批裁决三态 ─────────────────────────────────


class TestInstallApprovalOutcomes:
    def _prepare(self, tmp_path: Path, decision: str) -> tuple[mgr.DshPackageManager, NpmStub, ApprovalStub]:
        npm = NpmStub()
        src = _local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0")
        npm.view_results["ok-pkg@1.0.0"] = _manifest_view({"name": "ok-pkg", "version": "1.0.0"})
        npm.pack_dirs["ok-pkg@1.0.0"] = src
        npm.install_ok.add("ok-pkg@1.0.0")
        approval = ApprovalStub(decision=decision)
        m = _make_manager(tmp_path, npm, approval)
        return m, npm, approval

    def test_approved_runs_npm_install_and_marks_installed(self, tmp_path: Path) -> None:
        m, npm, _approval = self._prepare(tmp_path, "approve")

        async def scenario() -> tuple[dict[str, Any], list[str]]:
            out = await m.install("ok-pkg", version="1.0.0")
            done = await m.wait_background()  # 同一事件循环内收尾（生产=sidecar 单循环）
            return out, done

        out, installs = asyncio.run(scenario())
        assert out["status"] == "pending_approval"
        assert installs == ["ok-pkg"]
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "installed"
        assert st["requested_version"] == "1.0.0"
        assert any("install" in c for c in npm.calls)

    def test_denied_leaves_workspace_untouched(self, tmp_path: Path) -> None:
        m, npm, approval = self._prepare(tmp_path, "deny")

        async def scenario() -> None:
            await m.install("ok-pkg", version="1.0.0")
            await m.wait_background()

        asyncio.run(scenario())
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "approval_denied"
        assert not any("install" in " ".join(c) for c in npm.calls)
        assert approval.cancelled == ["req-1"]

    def test_timeout_denies(self, tmp_path: Path) -> None:
        m, _npm, _approval = self._prepare(tmp_path, "timeout")

        async def scenario() -> None:
            await m.install("ok-pkg", version="1.0.0")
            await m.wait_background()

        asyncio.run(scenario())
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "approval_denied"

    def test_capability_unavailable_fails_visible(self, tmp_path: Path) -> None:
        npm = NpmStub()
        src = _local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0")
        npm.view_results["ok-pkg@1.0.0"] = _manifest_view({"name": "ok-pkg", "version": "1.0.0"})
        npm.pack_dirs["ok-pkg@1.0.0"] = src
        m = _make_manager(tmp_path, npm, ApprovalStub(available=False))
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install("ok-pkg", version="1.0.0"))
        assert ei.value.code == "approval_unavailable"
        # 工作区零写副作用
        assert not (Path(m.runtime_root) / "node_modules" / "ok-pkg").exists()

    def test_default_version_from_index_latest_exact(self, tmp_path: Path) -> None:
        npm = NpmStub()
        src = _local_pkg(tmp_path / "pkgsrc", "idx-pkg", "2.1.0")
        npm.view_results["idx-pkg@2.1.0"] = _manifest_view({"name": "idx-pkg", "version": "2.1.0"})
        npm.pack_dirs["idx-pkg@2.1.0"] = src
        npm.install_ok.add("idx-pkg@2.1.0")
        approval = ApprovalStub()
        m = _make_manager(tmp_path, npm, approval, index_plugins=[
            {"name": "idx-pkg", "versions": [
                {"version": "2.0.0", "source": {"type": "npm"}, "engines": {}},
                {"version": "2.1.0", "source": {"type": "npm"}, "engines": {}},
                {"version": "2.2.0-beta.1", "source": {"type": "npm"}, "engines": {}},
            ]},
        ])
        out = asyncio.run(m.install("idx-pkg"))
        assert out["status"] == "pending_approval"
        view_spec = [c for c in npm.calls if "view" in c][0][2]
        assert view_spec == "idx-pkg@2.1.0"  # 发行版最新精确版本（非 2.2.0-beta.1，非 dist-tag）


# ── uninstall：依赖保护与皮肤回收 ─────────────────────────


class TestUninstall:
    def test_dependents_block_uninstall(self, tmp_path: Path) -> None:
        npm = NpmStub()
        m = _make_manager(tmp_path, npm)
        ws = Path(m.runtime_root)
        _mk_workspace_pkg(ws, "dep-a", "dep-a", "1.0.0",
                          peers={"base-lib": "*"})
        _mk_workspace_pkg(ws, "dep-b", "dep-b", "1.0.0",
                          peers={"dep-a": "^1.0.0"})
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.uninstall("dep-a"))
        assert ei.value.code == "has_dependents"
        assert ei.value.detail["dependents"] == ["dep-b"]
        assert not any("uninstall" in c for c in npm.calls)

    def test_uninstall_runs_and_cleans_state_and_skins(self, tmp_path: Path) -> None:
        npm = NpmStub()
        npm.uninstall_ok.add("skin-pkg")
        m = _make_manager(tmp_path, npm)
        ws = Path(m.runtime_root)
        _mk_workspace_pkg(ws, "skin-pkg", "skin-pkg", "1.0.0", skins=["mint-test"], blob="")
        skins_dir = Path(m.skins_dir)
        # 模拟已同步过皮肤（install_local 的产物）
        import shutil  # noqa: PLC0415

        shutil.copytree(ws / "node_modules" / "skin-pkg" / "skins" / "mint-test", skins_dir / "mint-test")
        mgr.mark_package_state(ws, "skin-pkg", skins=["mint-test"], status="installed")
        out = asyncio.run(m.uninstall("skin-pkg"))
        assert out["ok"] is True
        assert not (ws / "node_modules" / "skin-pkg").exists()
        assert not (skins_dir / "mint-test").exists()  # 皮肤目录回收
        assert mgr.package_state(ws, "skin-pkg") == {}  # 状态清理

    def test_uninstall_unknown_package_is_structured(self, tmp_path: Path) -> None:
        m = _make_manager(tmp_path, NpmStub())
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.uninstall("nope"))
        assert ei.value.code == "package_not_installed"


# ── enable / disable（AC-R3）──────────────────────────────


class TestEnableDisable:
    def _prep_skin(self, tmp_path: Path, npm: NpmStub) -> mgr.DshPackageManager:
        m = _make_manager(tmp_path, npm)
        ws = Path(m.runtime_root)
        _mk_workspace_pkg(ws, "skin-pkg", "skin-pkg", "1.0.0", skins=["mint-test"], blob="")
        import shutil  # noqa: PLC0415

        shutil.copytree(ws / "node_modules" / "skin-pkg" / "skins" / "mint-test",
                        Path(m.skins_dir) / "mint-test")
        mgr.mark_package_state(ws, "skin-pkg", skins=["mint-test"], status="installed")
        return m

    def test_disable_removes_skin_and_marks_state(self, tmp_path: Path) -> None:
        m = self._prep_skin(tmp_path, NpmStub())
        out = asyncio.run(m.disable("skin-pkg"))
        assert out["ok"] is True
        ws = Path(m.runtime_root)
        st = mgr.package_state(ws, "skin-pkg")
        assert st["enabled"] is False
        assert st["status"] == "disabled"
        assert not (Path(m.skins_dir) / "mint-test").exists()  # 主题选择器即消（M2 逆向）

    def test_enable_restores_skin(self, tmp_path: Path) -> None:
        m = self._prep_skin(tmp_path, NpmStub())
        asyncio.run(m.disable("skin-pkg"))
        out = asyncio.run(m.enable("skin-pkg"))
        assert out["ok"] is True
        ws = Path(m.runtime_root)
        st = mgr.package_state(ws, "skin-pkg")
        assert st["enabled"] is True
        assert st["status"] == "enabled"
        assert (Path(m.skins_dir) / "mint-test").is_dir()  # 主题选择器即现

    def test_unknown_package_rejected(self, tmp_path: Path) -> None:
        m = _make_manager(tmp_path, NpmStub())
        for op in (m.enable, m.disable):
            with pytest.raises(mgr.ManagerError) as ei:
                asyncio.run(op("nope"))
            assert ei.value.code == "package_not_installed"


# ── install_local（测试通道专用）──────────────────────────


class TestInstallLocal:
    def test_local_install_without_approval(self, tmp_path: Path) -> None:
        npm = NpmStub()
        npm.install_ok.add("placeholder")  # install spec 是本地路径，见下
        m = _make_manager(tmp_path, npm)
        src = _local_pkg(tmp_path / "sample", "dsh-tool-csv", "0.0.1")
        npm.install_ok.add(str(src))
        out = asyncio.run(m.install_local(str(src)))
        assert out["status"] == "installed"
        st = mgr.package_state(Path(m.runtime_root), "dsh-tool-csv")
        assert st["status"] == "installed"
        assert (Path(m.runtime_root) / "node_modules" / "dsh-tool-csv" / "package.json").exists() or npm.calls

    def test_local_engines_incompatible_blocked(self, tmp_path: Path) -> None:
        npm = NpmStub()
        m = _make_manager(tmp_path, npm)
        src = _local_pkg(tmp_path / "sample", "bad-pkg", "1.0.0", engines_dsh=">=0.9.0")
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install_local(str(src)))
        assert ei.value.code == "engines_incompatible"

    def test_local_skin_package_syncs_skins(self, tmp_path: Path) -> None:
        npm = NpmStub()
        m = _make_manager(tmp_path, npm)
        src = _local_pkg(tmp_path / "skin-src", "test-skin-pkg", "1.0.0", skins=["mint-test"])
        npm.install_ok.add(str(src))
        out = asyncio.run(m.install_local(str(src)))
        assert out["status"] == "installed"
        assert (Path(m.skins_dir) / "mint-test" / "skin.css").is_file()  # 主题选择器面（M2）
        st = mgr.package_state(Path(m.runtime_root), "test-skin-pkg")
        assert st.get("skins") == ["mint-test"]


# ── X 批：legacy_peers 装法 + 装后 peer 完整性自检（生态实测发现①）────────


class TestLegacyPeersInstall:
    def _prep(self, tmp_path: Path, *, peers: dict | None = None,
              optional: bool = False) -> tuple[mgr.DshPackageManager, NpmStub]:
        npm = NpmStub()
        src = _local_pkg(tmp_path / "pkgsrc", "peer-pkg", "1.0.0")
        npm.view_results["peer-pkg@1.0.0"] = _manifest_view({"name": "peer-pkg", "version": "1.0.0"})
        npm.pack_dirs["peer-pkg@1.0.0"] = src
        npm.install_ok.add("peer-pkg@1.0.0")
        npm.install_ok.add("--force")  # 补树命令末位是 --force（无新 spec）
        m = _make_manager(tmp_path, npm, ApprovalStub(decision="approve"))
        pj = {"name": "peer-pkg", "version": "1.0.0"}
        if peers:
            pj["peerDependencies"] = peers
            if optional:
                pj["peerDependenciesMeta"] = {k: {"optional": True} for k in peers}
        _mk_workspace_pkg(Path(m.runtime_root), "peer-pkg", "peer-pkg", "1.0.0",
                          peers=pj.get("peerDependencies"))
        if optional:
            d = Path(m.runtime_root) / "node_modules" / "peer-pkg"
            (d / "package.json").write_text(json.dumps(pj), encoding="utf-8")
        return m, npm

    def _install(self, m: mgr.DshPackageManager, *, legacy: bool) -> None:
        async def scenario() -> None:
            await m.install("peer-pkg", version="1.0.0", legacy_peers=legacy)
            await m.wait_background()

        asyncio.run(scenario())

    def test_legacy_flag_wire_and_state(self, tmp_path: Path) -> None:
        m, npm = self._prep(tmp_path)
        self._install(m, legacy=True)
        install_cmds = [c for c in npm.calls if "install" in c and "peer-pkg@1.0.0" in c]
        assert len(install_cmds) == 1
        assert "--legacy-peer-deps" in install_cmds[0]
        assert install_cmds[0][-1] == "peer-pkg@1.0.0"  # flag 不占末位（spec 契约）
        st = mgr.package_state(Path(m.runtime_root), "peer-pkg")
        assert st["status"] == "installed"
        assert st["legacy_peers"] is True
        assert st["peer_gaps"] is None

    def test_missing_peer_triggers_force_repair_and_is_visible(self, tmp_path: Path) -> None:
        m, npm = self._prep(tmp_path, peers={"@deepseek-ai/dsh-scope": "^0.2.1-rc.5"})
        self._install(m, legacy=True)
        assert any("--force" in c for c in npm.calls)  # legacy 装法漏 peer → 补树一次
        st = mgr.package_state(Path(m.runtime_root), "peer-pkg")
        assert st["peer_gaps"] == [{"package": "peer-pkg",
                                    "peer": "@deepseek-ai/dsh-scope",
                                    "range": "^0.2.1-rc.5"}]

    def test_missing_peer_without_legacy_not_repaired_but_visible(self, tmp_path: Path) -> None:
        m, npm = self._prep(tmp_path, peers={"@deepseek-ai/dsh-scope": "^0.2.1-rc.5"})
        self._install(m, legacy=False)
        install_cmds = [c for c in npm.calls if "install" in c]
        assert not any("--force" in c for c in install_cmds)
        st = mgr.package_state(Path(m.runtime_root), "peer-pkg")
        assert st["peer_gaps"][0]["peer"] == "@deepseek-ai/dsh-scope"

    def test_optional_peer_gap_skipped(self, tmp_path: Path) -> None:
        m, _npm = self._prep(tmp_path, peers={"@deepseek-ai/dsh-scope": "^0.2.1-rc.5"},
                             optional=True)
        self._install(m, legacy=True)
        st = mgr.package_state(Path(m.runtime_root), "peer-pkg")
        assert st["peer_gaps"] is None

    def test_list_exposes_peer_gaps_and_legacy_flag(self, tmp_path: Path) -> None:
        m, _npm = self._prep(tmp_path, peers={"@deepseek-ai/dsh-scope": "^0.2.1-rc.5"})
        self._install(m, legacy=True)
        listed = asyncio.run(m.list_installed())
        entry = next(p for p in listed["packages"] if p["name"] == "peer-pkg")
        assert entry["legacy_peers"] is True
        assert entry["peer_gaps"][0]["peer"] == "@deepseek-ai/dsh-scope"

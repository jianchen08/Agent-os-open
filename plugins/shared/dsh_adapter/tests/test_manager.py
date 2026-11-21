# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""DSH 包管理器（官方 plugin-manager 委托面：list/install/uninstall/enable/disable）。

契约：
- **list（AC-M1）**：官方 ``listBundles`` 为 installed/enabled 真值 + 状态文件
  只承载审批/预览/错误；manager 未 bootstrap → ``{initialized: false,
  bootstrap_required: true}`` 回退实扫；
- **install 审批（§九.1 用户裁定面）**：第三方包 = 第三方代码进执行面，
  install 走 human-interaction capability（create_choice/wait_for_choice）；
  审批卡内容 = 翻译预览；审批通道不可用 → 结构化错误 fail-visible；
  deny/超时 → 拒绝；
- **spec 归一化**：registry 包名 → 索引最新精确版本（禁 dist-tag）；本地绝对
  路径/``file:``/tarball/git 原样；相对路径拒绝；
- **官方 inspect/installBundle**：inspect 拒绝（not-a-bundle 等）原样映射；
  installBundle 的 ChangeResult（failed/cancelled/restart-required）映射到
  状态机；uninstall/enable/disable 委托 removeBundle/setBundleEnabled；
- **取消**：cancel_install 调官方 cancelInstall + 审批卡取消 + 状态 cancelled；
- **皮肤**：装后同步、卸载/禁用回收、启用回同步。

npm 执行（仅预览 pack）/索引抓取/审批 capability/桥全部注入，单测零在线。
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


class BridgeStub:
    """官方 plugin-manager 桥替身：记录调用，返回可编程 ChangeResult。"""

    def __init__(self) -> None:
        self.inspect_result: dict[str, Any] = {
            "ok": True,
            "data": {"status": "accepted", "kind": "path", "name": "ok-pkg",
                     "version": "1.0.0", "bundle": True},
            "error": None,
        }
        self.install_result: dict[str, Any] = {
            "ok": True,
            "data": {"application": "restart-required", "bundle": "ok-pkg",
                     "version": "1.0.0", "changed": True},
            "error": None,
        }
        self.remove_result: dict[str, Any] = {
            "ok": True, "data": {"application": "restart-required", "changed": True},
            "error": None,
        }
        self.enable_result: dict[str, Any] = {
            "ok": True, "data": {"application": "restart-required", "changed": True},
            "error": None,
        }
        self.list_result: dict[str, Any] = {
            "ok": True, "data": {"bundles": [], "plugins": [], "registries": None},
            "error": None,
        }
        self.cancel_result: dict[str, Any] = {
            "ok": True, "data": {"status": "cancelled"}, "error": None,
        }
        self.bundle_states: list[dict[str, Any]] = []
        self.provider_states: list[dict[str, Any]] = []
        self.calls: list[tuple[Any, ...]] = []

    async def manager_inspect(self, spec: str) -> dict[str, Any]:
        self.calls.append(("inspect", spec))
        return self.inspect_result

    async def manager_install(self, spec: str, request_id: str,
                              options: dict[str, Any] | None = None,
                              timeout_s: float = 900.0) -> dict[str, Any]:
        self.calls.append(("install", spec, request_id, options))
        return self.install_result

    async def manager_remove(self, name: str) -> dict[str, Any]:
        self.calls.append(("remove", name))
        return self.remove_result

    async def manager_set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        self.calls.append(("set_enabled", name, enabled))
        return self.enable_result

    async def manager_list(self) -> dict[str, Any]:
        self.calls.append(("list",))
        return self.list_result

    async def manager_cancel(self, request_id: str) -> dict[str, Any]:
        self.calls.append(("cancel", request_id))
        return self.cancel_result


class NpmStub:
    """npm 命令替身（仅预览 ``npm pack``；安装走官方 pnpm，不在 Python 侧）。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.pack_dirs: dict[str, Path] = {}

    def __call__(self, cmd: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append(list(cmd))
        if "pack" in cmd:
            spec = cmd[cmd.index("pack") + 1]
            src = self.pack_dirs.get(spec)
            if src is None:
                return (1, "", "npm error pack failed")
            dest = Path(cmd[cmd.index("--pack-destination") + 1]) / f"{src.name}.tgz"
            _make_tgz(src, dest)
            return (0, f"{dest}\n", "")
        return (0, "", "")


def _make_tgz(src: Path, dest: Path) -> None:
    """打包目录为 npm 形态 tarball（package/<files>，真 tar.gz）。"""
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
        self.responses: list[tuple[str, str | None, str | None]] = []

    async def call(self, method: str, params: dict[str, Any],
                   timeout: float | None = None) -> dict[str, Any]:
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
        if method == "respond":
            response = params.get("response") or {}
            self.responses.append((str(params.get("request_id")),
                                   response.get("response_type"),
                                   response.get("selected_option")))
            return {"ok": True}
        if method == "cancel":
            self.cancelled.append(str(params.get("request_id")))
            return {"ok": True}
        raise AssertionError(f"unexpected method {method}")


class _IsolatedBridgeStub(BridgeStub):
    """真实桥隔离路径替身：装/卸走一次性进程方法（manager_*_isolated）。"""

    def __init__(self) -> None:
        super().__init__()
        self.isolated: list[tuple[Any, ...]] = []

    async def manager_install_isolated(self, spec: str, request_id: str,
                                       options: dict[str, Any] | None = None,
                                       timeout_s: float = 900.0) -> dict[str, Any]:
        self.isolated.append(("install", spec, request_id, options))
        return await self.manager_install(spec, request_id, options, timeout_s)

    async def manager_remove_isolated(self, name: str,
                                      timeout_s: float = 900.0) -> dict[str, Any]:
        self.isolated.append(("remove", name))
        return await self.manager_remove(name)


class _WaitCancelledStub(ApprovalStub):
    """等待裁决时抛 CancelledError（任务被取消的收尾形态）。"""

    async def call(self, method: str, params: dict[str, Any],
                   timeout: float | None = None) -> dict[str, Any]:
        if method == "wait_for_choice":
            raise asyncio.CancelledError
        return await super().call(method, params, timeout)


class _WaitBoomStub(ApprovalStub):
    """等待裁决通道故障（审批通道掉线）。"""

    async def call(self, method: str, params: dict[str, Any],
                   timeout: float | None = None) -> dict[str, Any]:
        if method == "wait_for_choice":
            raise RuntimeError("channel down")
        return await super().call(method, params, timeout)


class _SemanticsApproveStub(ApprovalStub):
    """语义归一形态：human 服务把词形归一到选项规范 id（approved_once），
    裁决方只读 selected_semantics（ADR 2026-10-01）。"""

    async def call(self, method: str, params: dict[str, Any],
                   timeout: float | None = None) -> dict[str, Any]:
        if method == "wait_for_choice":
            # human 服务真实形态：扁平信封（无 status/response 包装）
            return {"request_id": "req-1", "response_type": "answered",
                    "selected_option": "approved_once",
                    "selected_semantics": "approve_once"}
        return await super().call(method, params, timeout)


class _CancelBoomStub(ApprovalStub):
    """取消卡片的通道故障（收尾尽力而为，不改变取消结果）。"""

    async def call(self, method: str, params: dict[str, Any],
                   timeout: float | None = None) -> dict[str, Any]:
        if method == "cancel":
            raise RuntimeError("cancel channel down")
        return await super().call(method, params, timeout)


def _install_fake_manager(ws: Path) -> None:
    """让 ``ensure_bootstrapped`` 认为 profile 已就绪（manager + 真尺寸 pnpm）。"""
    from runtime_workspace import MANAGER_PACKAGE, MANAGER_VERSION

    pkg_dir = ws / "node_modules" / MANAGER_PACKAGE
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "package.json").write_text(json.dumps({
        "name": MANAGER_PACKAGE, "version": MANAGER_VERSION,
    }), encoding="utf-8")
    pnpm = ws / ".agentos" / "pnpm-runtime" / "node_modules" / "@pnpm" / "exe" / "pnpm"
    pnpm.parent.mkdir(parents=True, exist_ok=True)
    pnpm.write_bytes(b"x" * 1_000_001)
    (ws / "node_modules" / ".modules.yaml").write_text("layoutVersion: 5\n", encoding="utf-8")


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
    (pkg_dir / "lib" / "index.js").write_text(
        blob or "export const apply = () => {}\n", encoding="utf-8")
    for skin in skins or []:
        sd = pkg_dir / "skins" / skin
        sd.mkdir(parents=True)
        (sd / "skin.json").write_text(json.dumps({
            "skinManifestVersion": 2, "id": skin, "name": skin,
            "contributes": {"stylesheet": "skin.css"},
        }), encoding="utf-8")
        (sd / "skin.css").write_text(
            ":root { --dsw-alias-bg-base: #eef5ff; color: #14263f; }", encoding="utf-8")
    return pkg_dir


def _local_pkg(src: Path, name: str, version: str, *, engines_dsh: str | None = None,
               skins: list[str] | None = None, blob: str = "") -> Path:
    src.mkdir(parents=True, exist_ok=True)
    pj: dict[str, Any] = {"name": name, "version": version,
                          "dsh": {"bundle": {"patch": "./cordis.patch.yml"}}}
    if engines_dsh:
        pj["engines"] = {"dsh": engines_dsh}
    (src / "package.json").write_text(json.dumps(pj), encoding="utf-8")
    (src / "lib").mkdir(exist_ok=True)
    (src / "lib" / "index.js").write_text(
        blob or "export const apply = () => {}\n", encoding="utf-8")
    for skin in skins or []:
        sd = src / "skins" / skin
        sd.mkdir(parents=True)
        (sd / "skin.json").write_text(json.dumps({
            "skinManifestVersion": 2, "id": skin, "name": skin,
            "contributes": {"stylesheet": "skin.css"},
        }), encoding="utf-8")
        (sd / "skin.css").write_text(
            ":root { --dsw-alias-bg-base: #f4efe4; color: #262319; }", encoding="utf-8")
    return src


def _make_manager(tmp_path: Path, *, bridge: BridgeStub | None = None,
                  approval: ApprovalStub | None = None,
                  config: dict | None = None,
                  index_plugins: list[dict] | None = None,
                  ready: bool = True) -> tuple[mgr.DshPackageManager, BridgeStub]:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    from runtime_workspace import ensure_workspace

    ensure_workspace(ws)
    if ready:
        _install_fake_manager(ws)
    stub = bridge or BridgeStub()

    async def fetcher(url: str, timeout_s: float) -> bytes:
        raise OSError("no network in unit tests")

    m = mgr.DshPackageManager(
        runtime_root=str(ws), skins_dir=str(tmp_path / "skins-dir"),
        npm_runner=NpmStub(),
        capability_factory=(lambda: approval) if approval is not None else (lambda: None),
        fetcher=fetcher,
        config=config or {},
        bridge_factory=lambda: stub,
    )
    if index_plugins is not None:
        m.set_index_plugins(index_plugins)
    return m, stub


def _install(m: mgr.DshPackageManager, spec: str, version: str | None = None) -> dict[str, Any]:
    """install + 同事件循环收尾（生产=sidecar 单循环；测试避免悬挂任务）。"""

    async def scenario() -> dict[str, Any]:
        out = await m.install(spec, version)
        await m.wait_background()
        return out

    return asyncio.run(scenario())


# ── list_installed（AC-M1）────────────────────────────────


class TestListInstalled:
    def test_bootstrap_required_falls_back_to_scan(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, ready=False)
        _mk_workspace_pkg(Path(m.runtime_root), "pkg-a", "pkg-a", "1.0.0")
        out = asyncio.run(m.list_installed())
        assert out["initialized"] is False
        assert out["bootstrap_required"] is True
        assert [p["name"] for p in out["packages"]] == ["pkg-a"]
        assert stub.calls == []  # 未 bootstrap 不碰桥

    def test_manager_bundles_are_source_of_truth(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        ws = Path(m.runtime_root)
        _mk_workspace_pkg(ws, "bundle-a", "bundle-a", "0.3.2",
                          blob="ctx.tools.register(defineTool({name:'finance_data'}))\n")
        _mk_workspace_pkg(ws, "skin-pkg", "skin-pkg", "1.0.0", blob="", skins=["mint-test"])
        stub.list_result = {"ok": True, "data": {"bundles": [
            {"name": "bundle-a", "version": "0.3.2", "enabled": True, "installed": True,
             "availability": "profile", "removable": True, "rows": [{"moduleName": "bundle-a"}]},
            {"name": "skin-pkg", "version": "1.0.0", "enabled": False, "installed": True,
             "availability": "profile", "removable": True, "rows": []},
            {"name": "@deepseek-ai/dsh-official-missing", "enabled": False,
             "installed": False, "availability": "missing"},
            {"name": "@deepseek-ai/dsh-tools", "enabled": True, "installed": True,
             "availability": "installation"},
        ], "plugins": [], "registries": None}, "error": None}
        out = asyncio.run(m.list_installed())
        assert out["initialized"] is True
        by = {p["name"]: p for p in out["packages"]}
        assert "@deepseek-ai/dsh-tools" not in by  # 基础设施包过滤
        assert "@deepseek-ai/dsh-official-missing" not in by  # 未装 official 目录过滤
        assert by["bundle-a"]["status"] == "enabled"
        assert by["bundle-a"]["contribution"]["tools"] is True
        assert by["bundle-a"]["rows"] == ["bundle-a"]
        assert by["skin-pkg"]["status"] == "disabled"
        assert by["skin-pkg"]["contribution"]["themes"] == 1

    def test_state_overrides_status_and_error(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        ws = Path(m.runtime_root)
        _mk_workspace_pkg(ws, "bundle-a", "bundle-a", "1.0.0")
        mgr.mark_package_state(ws, "bundle-a", status="failed", error="boom",
                               request_id="req-9")
        stub.list_result = {"ok": True, "data": {"bundles": [
            {"name": "bundle-a", "version": "1.0.0", "enabled": True, "installed": True,
             "availability": "profile"},
        ]}, "error": None}
        out = asyncio.run(m.list_installed())
        entry = out["packages"][0]
        assert entry["status"] == "failed"
        assert entry["error"] == "boom"
        assert entry["request_id"] == "req-9"

    def test_pending_install_not_in_bundles_still_listed(self, tmp_path: Path) -> None:
        """待审批记录（bundle 尚未落盘）必须进清单：管理页才有行可裁决。"""
        m, stub = _make_manager(tmp_path)
        ws = Path(m.runtime_root)
        m._approval_request_ids["req-p1"] = ("pending-pkg", "pending-pkg@0.1.0")
        mgr.mark_package_state(
            ws, "pending-pkg", status="pending_approval", spec="pending-pkg@0.1.0",
            request_id="req-p1",
            preview={"tools": True, "tool_names": ["receipt_write"], "themes": 0,
                     "pages": 0, "hooks": 0, "slots": [], "unmapped": [],
                     "host_dependencies": {"required": ["@deepseek-ai/cordis"], "missing": []}},
        )
        stub.list_result = {"ok": True, "data": {"bundles": []}, "error": None}
        out = asyncio.run(m.list_installed())
        by = {p["name"]: p for p in out["packages"]}
        row = by["pending-pkg"]
        assert row["status"] == "pending_approval"
        assert row["request_id"] == "req-p1"
        assert row["version"] == "0.1.0"
        assert row["contribution"]["tool_names"] == ["receipt_write"]
        assert row["contribution"]["host_dependencies"]["required"] == ["@deepseek-ai/cordis"]

    def test_stale_pending_self_heals_to_failed(self, tmp_path: Path) -> None:
        """进程重启后审批续行已失（request_id 不在内存表）→ 读路径落失败态，
        不让管理页挂一条点「批准」也无效的待审批行。"""
        m, stub = _make_manager(tmp_path)
        ws = Path(m.runtime_root)
        mgr.mark_package_state(
            ws, "pending-pkg", status="pending_approval", spec="pending-pkg@0.1.0",
            request_id="req-lost",
        )
        stub.list_result = {"ok": True, "data": {"bundles": []}, "error": None}
        out = asyncio.run(m.list_installed())
        row = {p["name"]: p for p in out["packages"]}["pending-pkg"]
        assert row["status"] == "failed"
        assert "重新发起安装" in str(row["error"])
        # 状态文件同步落账（下次读不再按 pending 处理）
        assert mgr.package_state(ws, "pending-pkg")["status"] == "failed"

    def test_state_only_row_skipped_when_bundle_present(self, tmp_path: Path) -> None:
        """同一包已落盘（bundle 在列）时不再补重复行。"""
        m, stub = _make_manager(tmp_path)
        ws = Path(m.runtime_root)
        _mk_workspace_pkg(ws, "bundle-a", "bundle-a", "1.0.0")
        mgr.mark_package_state(ws, "bundle-a", status="failed", error="boom")
        stub.list_result = {"ok": True, "data": {"bundles": [
            {"name": "bundle-a", "version": "1.0.0", "enabled": True, "installed": True,
             "availability": "profile"},
        ]}, "error": None}
        out = asyncio.run(m.list_installed())
        assert [p["name"] for p in out["packages"]] == ["bundle-a"]

    def test_manager_error_is_visible(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.list_result = {"ok": False, "data": None, "error": "manager down"}
        out = asyncio.run(m.list_installed())
        assert out["initialized"] is True
        assert out["manager_error"] == "manager down"
        assert out["packages"] == []

    def test_non_dict_bundle_rows_are_skipped(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        _mk_workspace_pkg(Path(m.runtime_root), "bundle-a", "bundle-a", "1.0.0")
        stub.list_result = {"ok": True, "data": {"bundles": [
            "junk-row",
            {"name": "bundle-a", "version": "1.0.0", "enabled": True, "installed": True,
             "availability": "profile"},
        ]}, "error": None}
        out = asyncio.run(m.list_installed())
        assert [p["name"] for p in out["packages"]] == ["bundle-a"]

    def test_bundle_error_without_state_marks_failed(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.list_result = {"ok": True, "data": {"bundles": [
            {"name": "bundle-a", "version": "1.0.0", "enabled": False,
             "installed": True, "availability": "profile", "error": "patch missing"},
        ]}, "error": None}
        out = asyncio.run(m.list_installed())
        assert out["packages"][0]["status"] == "failed"
        assert out["packages"][0]["error"] == "patch missing"


# ── install：spec 归一化与官方 inspect ─────────────────────


class TestInstallGuards:
    def test_not_in_index_without_version_requires_explicit(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, index_plugins=[])
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install("some-unknown-pkg"))
        assert ei.value.code == "version_required_not_in_index"
        assert stub.calls == []

    def test_relative_path_rejected(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        for spec in ("./bundle", "../bundle", "bundle.tgz"):
            with pytest.raises(mgr.ManagerError) as ei:
                asyncio.run(m.install(spec))
            assert ei.value.code == "invalid_request"
            assert "绝对路径" in str(ei.value)
        assert stub.calls == []

    def test_inspect_refusal_maps_to_error_code(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.inspect_result = {"ok": True, "data": {
            "status": "refused", "problem": "not-a-bundle", "reason": "no dsh.bundle",
        }, "error": None}
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install(str(tmp_path / "local-bundle")))
        assert ei.value.code == "not-a-bundle"
        assert "no dsh.bundle" in str(ei.value)

    def test_inspect_bridge_failure_is_structured(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.inspect_result = {"ok": False, "data": None, "error": "bridge down"}
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install(str(tmp_path / "local-bundle")))
        assert ei.value.code == "manager_unavailable"

    def test_registry_version_is_exact(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, index_plugins=[
            {"name": "idx-pkg", "versions": [
                {"version": "2.0.0", "source": {"type": "npm"}, "engines": {}},
                {"version": "2.1.0", "source": {"type": "npm"}, "engines": {}},
                {"version": "2.2.0-beta.1", "source": {"type": "npm"}, "engines": {}},
            ]},
        ], approval=ApprovalStub())
        out = _install(m, "idx-pkg")
        assert out["status"] == "pending_approval"
        assert stub.calls[0] == ("inspect", "idx-pkg@2.1.0")

    def test_inline_version_in_spec_passed_through(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, approval=ApprovalStub())
        _install(m, "idx-pkg@9.9.9")
        assert stub.calls[0] == ("inspect", "idx-pkg@9.9.9")
        stub.calls.clear()
        _install(m, "@scope/name@1.2.3")
        assert stub.calls[0] == ("inspect", "@scope/name@1.2.3")

    def test_explicit_version_passed_through(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, approval=ApprovalStub())
        _install(m, "idx-pkg", "9.9.9")
        assert stub.calls[0] == ("inspect", "idx-pkg@9.9.9")

    def test_git_and_file_specs_pass_through(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, approval=ApprovalStub())
        git = "https://github.com/x/y.git"
        _install(m, git)
        assert stub.calls[0] == ("inspect", git)
        stub.calls.clear()
        local = str(tmp_path / "local-bundle")
        _install(m, f"file:{local}")
        assert stub.calls[0] == ("inspect", f"file:{local}")

    def test_blank_and_flag_specs_rejected(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        for spec in ("", "   ", "-evil-flag"):
            with pytest.raises(mgr.ManagerError) as ei:
                asyncio.run(m.install(spec))
            assert ei.value.code == "invalid_request"
            assert "invalid package spec" in str(ei.value)
        assert stub.calls == []

    def test_invalid_package_name_rejected(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install("bad name!"))
        assert ei.value.code == "invalid_request"
        assert "invalid package name" in str(ei.value)
        assert stub.calls == []


# ── install：审批裁决与 ChangeResult 映射 ─────────────────


class TestInstallApprovalOutcomes:
    def _prepare(self, tmp_path: Path, decision: str) -> tuple[mgr.DshPackageManager, BridgeStub, ApprovalStub]:
        approval = ApprovalStub(decision=decision)
        m, stub = _make_manager(tmp_path, approval=approval)
        return m, stub, approval

    def test_approved_calls_official_install_and_marks_installed(self, tmp_path: Path) -> None:
        m, stub, _approval = self._prepare(tmp_path, "approve")
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> dict[str, Any]:
            out = await m.install(local)
            await m.wait_background()
            return out

        out = asyncio.run(scenario())
        assert out["status"] == "pending_approval"
        installs = [c for c in stub.calls if c[0] == "install"]
        assert installs
        assert installs[0][1] == local
        assert installs[0][3] == {"saveExact": True}
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "installed"
        assert st["application"] == "restart-required"
        assert st["restart_required"] is True

    def test_install_uses_isolated_bridge_when_available(self, tmp_path: Path) -> None:
        bridge = _IsolatedBridgeStub()
        approval = ApprovalStub(decision="approve")
        m, _stub = _make_manager(tmp_path, bridge=bridge, approval=approval)
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> None:
            await m.install(local)
            await m.wait_background()

        asyncio.run(scenario())
        assert [c[0] for c in bridge.isolated] == ["install"]
        assert mgr.package_state(Path(m.runtime_root), "ok-pkg")["status"] == "installed"

    def test_approved_by_semantics_not_option_id(self, tmp_path: Path) -> None:
        """human 归一后 selected_option 是规范 id（approved_once）——按语义放行。"""
        approval = _SemanticsApproveStub()
        m, stub = _make_manager(tmp_path, approval=approval)
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> None:
            await m.install(local)
            await m.wait_background()

        asyncio.run(scenario())
        assert any(c[0] == "install" for c in stub.calls)
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "installed"

    def test_approval_card_has_translation_preview(self, tmp_path: Path) -> None:
        m, _stub, approval = self._prepare(tmp_path, "approve")
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0", skins=["mint-test"]))
        _install(m, local)
        card = approval.cards[0]
        assert "ok-pkg" in (card.get("title") or "")
        assert "翻译预览" in (card.get("description") or "")
        assert '"themes": 1' in (card.get("description") or "")
        # human.create_choice 三键必填（无会话上下文的安装审批用固定合成命名空间）
        assert card.get("session_id") == "dsh_adapter:install"
        assert card.get("thread_id") == "dsh_adapter:install"
        assert card.get("tab_id") == "dsh_adapter:install"
        # 选项声明封闭语义（ADR 2026-10-01）：/approve 便利路由按 approve_once 代批
        assert [o.get("semantics") for o in card.get("options") or []] == ["approve_once", "deny"]

    def test_denied_does_not_install(self, tmp_path: Path) -> None:
        m, stub, approval = self._prepare(tmp_path, "deny")
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> None:
            await m.install(local)
            await m.wait_background()

        asyncio.run(scenario())
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "approval_denied"
        assert not any(c[0] == "install" for c in stub.calls)
        assert approval.cancelled == ["req-1"]

    def test_timeout_denies(self, tmp_path: Path) -> None:
        m, _stub, _approval = self._prepare(tmp_path, "timeout")
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> None:
            await m.install(local)
            await m.wait_background()

        asyncio.run(scenario())
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "approval_denied"

    def test_capability_unavailable_fails_visible(self, tmp_path: Path) -> None:
        m, _stub = _make_manager(tmp_path, approval=ApprovalStub(available=False))
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install(str(tmp_path / "local-bundle")))
        assert ei.value.code == "approval_unavailable"

    def test_failed_result_maps_to_state_error(self, tmp_path: Path) -> None:
        m, stub, _approval = self._prepare(tmp_path, "approve")
        stub.install_result = {"ok": True, "data": {
            "application": "failed",
            "error": {"code": "not-bundle", "message": "no dsh.bundle"},
            "packageResult": {"output": "pnpm failed"},
        }, "error": None}
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> None:
            await m.install(local)
            await m.wait_background()

        asyncio.run(scenario())
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "failed"
        assert "no dsh.bundle" in st["error"]
        assert st["diagnostics"]["output"] == "pnpm failed"

    def test_cancelled_result_maps_to_state_cancelled(self, tmp_path: Path) -> None:
        m, stub, _approval = self._prepare(tmp_path, "approve")
        stub.install_result = {"ok": True, "data": {"application": "cancelled"}, "error": None}
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> None:
            await m.install(local)
            await m.wait_background()

        asyncio.run(scenario())
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "cancelled"

    def test_bridge_install_failure_maps_to_failed(self, tmp_path: Path) -> None:
        m, stub, _approval = self._prepare(tmp_path, "approve")
        stub.install_result = {"ok": False, "data": None, "error": "bridge down"}
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> None:
            await m.install(local)
            await m.wait_background()

        asyncio.run(scenario())
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "failed"
        assert "bridge down" in st["error"]

    def test_bundle_name_renames_state_key(self, tmp_path: Path) -> None:
        m, stub, _approval = self._prepare(tmp_path, "approve")
        stub.inspect_result = {"ok": True, "data": {
            "status": "accepted", "kind": "path", "name": "alias-name",
            "version": "1.0.0", "bundle": True,
        }, "error": None}
        stub.install_result = {"ok": True, "data": {
            "application": "restart-required", "bundle": "real-bundle",
            "version": "1.0.0", "changed": True,
        }, "error": None}
        _mk_workspace_pkg(Path(m.runtime_root), "real-bundle", "real-bundle", "1.0.0")
        local = str(_local_pkg(tmp_path / "pkgsrc", "alias-name", "1.0.0"))

        async def scenario() -> None:
            await m.install(local)
            await m.wait_background()

        asyncio.run(scenario())
        ws = Path(m.runtime_root)
        assert mgr.package_state(ws, "alias-name") == {}
        assert mgr.package_state(ws, "real-bundle")["status"] == "installed"

    @pytest.mark.parametrize(("data", "expected"), [
        ({"application": "failed", "packageResult": {"output": "ERR_PNPM boom"}},
         "ERR_PNPM boom"),
        ({"application": "failed"}, "官方 plugin-manager 操作失败"),
    ])
    def test_failed_result_text_fallbacks(self, tmp_path: Path, data: dict[str, Any],
                                          expected: str) -> None:
        m, stub, _approval = self._prepare(tmp_path, "approve")
        stub.install_result = {"ok": True, "data": data, "error": None}
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> None:
            await m.install(local)
            await m.wait_background()

        asyncio.run(scenario())
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "failed"
        assert st["error"] == expected

    def test_finish_install_without_capability_marks_failed(self, tmp_path: Path) -> None:
        m, _stub = _make_manager(tmp_path)
        ws = Path(m.runtime_root)
        asyncio.run(m._finish_install(ws, "pkg-a", "spec-a", "req-1"))
        st = mgr.package_state(ws, "pkg-a")
        assert st["status"] == "failed"
        assert "approval channel unavailable" in st["error"]

    def test_finish_install_cancelled_wait_returns_silently(self, tmp_path: Path) -> None:
        m, _stub = _make_manager(tmp_path, approval=_WaitCancelledStub())
        ws = Path(m.runtime_root)
        asyncio.run(m._finish_install(ws, "pkg-a", "spec-a", "req-1"))
        assert mgr.package_state(ws, "pkg-a") == {}

    def test_finish_install_cancelled_before_install_returns(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, approval=ApprovalStub())
        ws = Path(m.runtime_root)
        m._cancelled.add("req-1")  # noqa: SLF001 — 取消竞态：裁决返回后、安装前
        asyncio.run(m._finish_install(ws, "pkg-a", "spec-a", "req-1"))
        assert mgr.package_state(ws, "pkg-a") == {}
        assert not any(c[0] == "install" for c in stub.calls)

    def test_finish_install_cancelled_during_install_returns(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, approval=ApprovalStub())
        original = stub.manager_install

        async def cancelling_install(spec: str, request_id: str,
                                     options: dict[str, Any] | None = None,
                                     timeout_s: float = 900.0) -> dict[str, Any]:
            m._cancelled.add(request_id)  # noqa: SLF001 — 取消竞态：安装执行中
            return await original(spec, request_id, options, timeout_s)

        stub.manager_install = cancelling_install  # type: ignore[method-assign]
        ws = Path(m.runtime_root)
        asyncio.run(m._finish_install(ws, "pkg-a", "spec-a", "req-1"))
        assert mgr.package_state(ws, "pkg-a") == {}

    def test_finish_install_wait_error_marks_failed(self, tmp_path: Path) -> None:
        m, _stub = _make_manager(tmp_path, approval=_WaitBoomStub())
        ws = Path(m.runtime_root)
        asyncio.run(m._finish_install(ws, "pkg-a", "spec-a", "req-1"))
        st = mgr.package_state(ws, "pkg-a")
        assert st["status"] == "failed"
        assert "approval wait failed" in st["error"]


# ── preview ──────────────────────────────────────────────


class TestPreview:
    def test_local_preview_reads_directory(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, approval=ApprovalStub())
        local = _local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0",
                           skins=["mint-test"],
                           blob="ctx.tools.register(defineTool({name:'x_tool'}))\n")
        out = _install(m, str(local))
        preview = out["preview"]
        assert preview["manager_kind"] == "path"
        assert preview["themes"] == 1
        assert preview["tool_names"] == ["x_tool"]

    def test_registry_preview_uses_npm_pack(self, tmp_path: Path) -> None:
        npm = NpmStub()
        src = _local_pkg(tmp_path / "pkgsrc", "idx-pkg", "2.1.0")
        npm.pack_dirs["idx-pkg@2.1.0"] = src
        m, stub = _make_manager(tmp_path, approval=ApprovalStub(), index_plugins=[
            {"name": "idx-pkg", "versions": [
                {"version": "2.1.0", "source": {"type": "npm"}, "engines": {}},
            ]},
        ])
        m._npm_runner = npm  # noqa: SLF001 — 预览 pack 注入
        stub.inspect_result = {"ok": True, "data": {
            "status": "accepted", "kind": "registry", "name": "idx-pkg",
            "version": "2.1.0", "bundle": True,
        }, "error": None}
        out = _install(m, "idx-pkg")
        assert out["preview"]["manager_kind"] == "registry"
        assert any("pack" in c for c in npm.calls)

    def test_preview_failure_is_recorded_not_fatal(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, approval=ApprovalStub(), index_plugins=[
            {"name": "idx-pkg", "versions": [
                {"version": "2.1.0", "source": {"type": "npm"}, "engines": {}},
            ]},
        ])
        stub.inspect_result = {"ok": True, "data": {
            "status": "accepted", "kind": "registry", "name": "idx-pkg",
            "version": "2.1.0", "bundle": True,
        }, "error": None}
        out = _install(m, "idx-pkg")
        assert out["preview"]["preview_error"]["code"] == "preview_failed"

    def test_path_kind_tarball_is_extracted(self, tmp_path: Path) -> None:
        src = _local_pkg(tmp_path / "pkgsrc", "tgz-pkg", "1.0.0", skins=["mint-test"])
        tgz = tmp_path / "tgz-pkg.tgz"
        _make_tgz(src, tgz)
        m, stub = _make_manager(tmp_path, approval=ApprovalStub())
        stub.inspect_result = {"ok": True, "data": {
            "status": "accepted", "kind": "path", "name": "tgz-pkg",
            "version": "1.0.0", "bundle": True,
        }, "error": None}
        out = _install(m, str(tgz))
        assert out["preview"]["themes"] == 1
        assert "preview_error" not in out["preview"]

    def test_tarball_kind_preview_reads_file(self, tmp_path: Path) -> None:
        src = _local_pkg(tmp_path / "pkgsrc", "tgz-pkg", "1.0.0", skins=["mint-test"])
        tgz = tmp_path / "tgz-pkg.tgz"
        _make_tgz(src, tgz)
        m, stub = _make_manager(tmp_path, approval=ApprovalStub())
        stub.inspect_result = {"ok": True, "data": {
            "status": "accepted", "kind": "tarball", "name": "tgz-pkg",
            "version": "1.0.0", "bundle": True,
        }, "error": None}
        out = _install(m, str(tgz))
        assert out["preview"]["themes"] == 1

    def test_tarball_kind_missing_file_preview_skipped(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, approval=ApprovalStub())
        stub.inspect_result = {"ok": True, "data": {
            "status": "accepted", "kind": "tarball", "name": "tgz-pkg",
            "version": "1.0.0", "bundle": True,
        }, "error": None}
        out = _install(m, str(tmp_path / "missing.tgz"))
        assert "themes" not in out["preview"]

    def test_npm_pack_zero_products_is_preview_error(self, tmp_path: Path) -> None:
        def empty_pack(_cmd: list[str], _timeout: float) -> tuple[int, str, str]:
            return (0, "", "")

        m, stub = _make_manager(tmp_path, approval=ApprovalStub(), index_plugins=[
            {"name": "idx-pkg", "versions": [
                {"version": "2.1.0", "source": {"type": "npm"}, "engines": {}},
            ]},
        ])
        m._npm_runner = empty_pack  # noqa: SLF001 — 预览 pack 注入
        stub.inspect_result = {"ok": True, "data": {
            "status": "accepted", "kind": "registry", "name": "idx-pkg",
            "version": "2.1.0", "bundle": True,
        }, "error": None}
        out = _install(m, "idx-pkg")
        assert out["preview"]["preview_error"]["code"] == "preview_failed"
        assert "零产物" in out["preview"]["preview_error"]["message"]


# ── uninstall / enable / disable ─────────────────────────


class TestLifecycle:
    def test_uninstall_cleans_state_and_skins(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        ws = Path(m.runtime_root)
        _mk_workspace_pkg(ws, "skin-pkg", "skin-pkg", "1.0.0", skins=["mint-test"], blob="")
        import shutil  # noqa: PLC0415

        skins_dir = Path(m.skins_dir)
        shutil.copytree(ws / "node_modules" / "skin-pkg" / "skins" / "mint-test",
                        skins_dir / "mint-test")
        mgr.mark_package_state(ws, "skin-pkg", skins=["mint-test"], status="installed")
        out = asyncio.run(m.uninstall("skin-pkg"))
        assert out["ok"] is True
        assert not (skins_dir / "mint-test").exists()
        assert mgr.package_state(ws, "skin-pkg") == {}
        assert ("remove", "skin-pkg") in stub.calls

    def test_uninstall_uses_isolated_bridge_when_available(self, tmp_path: Path) -> None:
        bridge = _IsolatedBridgeStub()
        m, _stub = _make_manager(tmp_path, bridge=bridge)
        ws = Path(m.runtime_root)
        mgr.mark_package_state(ws, "pkg-a", status="installed")
        out = asyncio.run(m.uninstall("pkg-a"))
        assert out["ok"] is True
        assert bridge.isolated == [("remove", "pkg-a")]

    def test_uninstall_error_is_structured(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.remove_result = {"ok": True, "data": {
            "application": "failed", "error": {"code": "not-removable", "message": "in use"},
        }, "error": None}
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.uninstall("pkg-a"))
        assert ei.value.code == "not-removable"

    def test_enable_disable_updates_state_and_skins(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        ws = Path(m.runtime_root)
        _mk_workspace_pkg(ws, "skin-pkg", "skin-pkg", "1.0.0", skins=["mint-test"], blob="")
        import shutil  # noqa: PLC0415

        shutil.copytree(ws / "node_modules" / "skin-pkg" / "skins" / "mint-test",
                        Path(m.skins_dir) / "mint-test")
        mgr.mark_package_state(ws, "skin-pkg", skins=["mint-test"], status="installed")
        out = asyncio.run(m.disable("skin-pkg"))
        assert out["enabled"] is False
        assert not (Path(m.skins_dir) / "mint-test").exists()
        out = asyncio.run(m.enable("skin-pkg"))
        assert out["enabled"] is True
        assert (Path(m.skins_dir) / "mint-test").is_dir()
        assert ("set_enabled", "skin-pkg", False) in stub.calls
        assert ("set_enabled", "skin-pkg", True) in stub.calls

    def test_enable_error_is_structured(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.enable_result = {"ok": True, "data": {
            "application": "failed", "error": {"code": "not-bundle", "message": "no patch"},
        }, "error": None}
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.enable("pkg-a"))
        assert ei.value.code == "not-bundle"

    def test_cancel_install_calls_manager_and_approval(self, tmp_path: Path) -> None:
        approval = ApprovalStub(decision="approve")
        m, stub = _make_manager(tmp_path, approval=approval)
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> dict[str, Any]:
            await m.install(local)
            out = await m.cancel_install("req-1")
            await m.wait_background()
            return out

        out = asyncio.run(scenario())
        assert out["status"] == "cancelled"
        assert ("cancel", "req-1") in stub.calls
        assert approval.cancelled == ["req-1"]
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "cancelled"

    def test_cancel_unknown_request(self, tmp_path: Path) -> None:
        m, _stub = _make_manager(tmp_path)
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.cancel_install("nope"))
        assert ei.value.code == "not_found"

    def test_respond_install_approve_calls_human_respond(self, tmp_path: Path) -> None:
        """管理页行内「批准」→ human-interaction respond（与卡片裁决同一条续行链）。"""
        approval = ApprovalStub(decision="approve")
        m, _stub = _make_manager(tmp_path, approval=approval)
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> tuple[dict[str, Any], str]:
            out = await m.install(local)
            rid = str(out["request_id"])
            res = await m.respond_install(rid, True)
            await m.cancel_install(rid)  # 替身的 wait 不会自行唤醒，收尾取消背景任务
            return res, rid

        res, rid = asyncio.run(scenario())
        assert res["approved"] is True
        assert res["request_id"] == rid
        assert approval.responses == [(rid, "answered", "approve")]

    def test_respond_install_deny_maps_to_deny_option(self, tmp_path: Path) -> None:
        approval = ApprovalStub(decision="approve")
        m, _stub = _make_manager(tmp_path, approval=approval)
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> tuple[dict[str, Any], str]:
            out = await m.install(local)
            rid = str(out["request_id"])
            res = await m.respond_install(rid, False)
            await m.cancel_install(rid)
            return res, rid

        res, rid = asyncio.run(scenario())
        assert res["approved"] is False
        assert approval.responses == [(rid, "answered", "deny")]

    def test_respond_install_unknown_request(self, tmp_path: Path) -> None:
        m, _stub = _make_manager(tmp_path)
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.respond_install("nope", True))
        assert ei.value.code == "not_found"

    def test_respond_install_channel_unavailable(self, tmp_path: Path) -> None:
        approval = ApprovalStub(decision="approve")
        m, _stub = _make_manager(tmp_path, approval=approval)
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> str:
            out = await m.install(local)
            rid = str(out["request_id"])
            m._capability_factory = None  # 通道在裁决时不可用
            try:
                with pytest.raises(mgr.ManagerError) as ei:
                    await m.respond_install(rid, True)
                return ei.value.code
            finally:
                await m.cancel_install(rid)

        assert asyncio.run(scenario()) == "approval_unavailable"

    def test_uninstall_bridge_failure_is_structured(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.remove_result = {"ok": False, "data": None, "error": "bridge down"}
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.uninstall("pkg-a"))
        assert ei.value.code == "manager_unavailable"
        assert "bridge down" in str(ei.value)

    def test_uninstall_string_error_defaults_code(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.remove_result = {"ok": True, "data": {
            "application": "failed", "error": "pnpm exploded",
        }, "error": None}
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.uninstall("pkg-a"))
        assert ei.value.code == "remove_failed"
        assert "pnpm exploded" in str(ei.value)

    def test_enable_bridge_failure_is_structured(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.enable_result = {"ok": False, "data": None, "error": "bridge down"}
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.enable("pkg-a"))
        assert ei.value.code == "manager_unavailable"

    def test_cancel_install_survives_approval_channel_error(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path, approval=_CancelBoomStub(decision="approve"))
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))

        async def scenario() -> dict[str, Any]:
            await m.install(local)
            out = await m.cancel_install("req-1")
            await m.wait_background()
            return out

        out = asyncio.run(scenario())
        assert out["status"] == "cancelled"
        assert ("cancel", "req-1") in stub.calls
        assert mgr.package_state(Path(m.runtime_root), "ok-pkg")["status"] == "cancelled"


# ── 管理面接线（config / bridge / bootstrap 错误映射）────────


class TestManagerPlumbing:
    def test_install_timeout_invalid_value_falls_back(self, tmp_path: Path) -> None:
        m, _stub = _make_manager(
            tmp_path, config={"manager": {"install_timeout_s": "oops"}})
        assert m._install_timeout_s() == 900.0

    def test_manager_config_maps_section_fields(self, tmp_path: Path) -> None:
        m, _stub = _make_manager(tmp_path, config={"manager": {
            "registry": "https://r.example/",
            "fallback_registries": ["https://f1.example/", 42, ""],
            "inspect_timeout_ms": 1234,
            "install_timeout_s": 5,
        }})
        cfg = m._manager_config()
        assert cfg["registry"] == "https://r.example/"
        assert cfg["fallbackRegistries"] == ["https://f1.example/"]
        assert cfg["inspectTimeoutMs"] == 1234
        assert cfg["idleTimeoutMs"] == 5000
        assert cfg["pnpmCommand"].endswith("pnpm")
        assert "pnpmArgs" not in cfg

    def test_manager_config_dist_fallback_sets_pnpm_args(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m, _stub = _make_manager(tmp_path)
        pnpm = (Path(m.runtime_root) / mgr.rw.PNPM_RUNTIME_DIR / "node_modules"
                / "@pnpm" / "exe" / "pnpm")
        pnpm.unlink()
        dist = pnpm.parent / "dist" / "pnpm.mjs"
        dist.parent.mkdir(parents=True)
        dist.write_text("// pnpm\n", encoding="utf-8")
        monkeypatch.setattr(
            mgr.rw.shutil, "which",
            lambda name: "node.exe" if name == "node" else None)
        cfg = m._manager_config()
        assert cfg["pnpmCommand"] == "node.exe"
        assert cfg["pnpmArgs"] == [str(dist)]

    def test_bridge_without_factory_uses_shared_singleton(self, tmp_path: Path) -> None:
        import bridge  # noqa: PLC0415 — 裸名兄弟模块（conftest 保证身份）

        bridge.reset_bridge()
        try:
            m = mgr.DshPackageManager(
                runtime_root=str(tmp_path), skins_dir=str(tmp_path / "skins"),
                config={}, capability_factory=lambda: None)
            instance = m._bridge()
            assert instance._runtime_root == str(tmp_path)
            assert instance._manager_config["registry"] == mgr.rw.NPM_REGISTRY_DEFAULT
        finally:
            bridge.reset_bridge()

    def test_bootstrap_error_maps_to_manager_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m, _stub = _make_manager(tmp_path, ready=False)

        def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise mgr.rw.WorkspaceBootstrapError(
                "node_too_old", "Node 版本过低", found="20.1.0")

        monkeypatch.setattr(mgr.rw, "ensure_bootstrapped", boom)
        with pytest.raises(mgr.ManagerError) as ei:
            asyncio.run(m.install(str(tmp_path / "local-bundle")))
        assert ei.value.code == "node_too_old"
        assert ei.value.detail["found"] == "20.1.0"

    def test_restart_without_factory_calls_shared_bridge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bridge  # noqa: PLC0415 — 裸名兄弟模块（conftest 保证身份）

        called: list[bool] = []

        async def fake_restart() -> None:
            called.append(True)

        monkeypatch.setattr(bridge, "restart_bridge", fake_restart)
        m = mgr.DshPackageManager(
            runtime_root=str(tmp_path), skins_dir=str(tmp_path / "skins"), config={})
        asyncio.run(m._restart())
        assert called == [True]


# ── 宿主依赖补装（2026-11-09 翻译面接通：官方 runProfilePnpm 最小链路）──


class HostDepBridgeStub(BridgeStub):
    """桥替身：记录补装 specs，结果可编程。"""

    def __init__(self) -> None:
        super().__init__()
        self.host_dep_result: dict[str, Any] = {
            "ok": True,
            "data": {"exitCode": 0, "timedOut": False, "output": "", "truncated": False},
            "error": None,
        }
        self.host_deps: list[list[str]] = []

    async def manager_add_host_deps(self, specs: list[str],
                                    timeout_s: float = 900.0) -> dict[str, Any]:
        self.host_deps.append(list(specs))
        return self.host_dep_result


_HOST_IMPORT_BLOB = (
    "import { credentialRef } from '@deepseek-ai/dsh-credentials';\n"
    "import { settings } from '@deepseek-ai/dsh-settings';\n"
)


class TestHostDependencies:
    def _prepare(self, tmp_path: Path, *, blob: str = _HOST_IMPORT_BLOB,
                 with_pkg_dir: bool = True) -> tuple[mgr.DshPackageManager, HostDepBridgeStub]:
        bridge = HostDepBridgeStub()
        m, _stub = _make_manager(tmp_path, bridge=bridge, approval=ApprovalStub())
        if with_pkg_dir:
            _mk_workspace_pkg(Path(m.runtime_root), "ok-pkg", "ok-pkg", "1.0.0", blob=blob)
        return m, bridge

    def test_preview_reports_missing_host_deps(self, tmp_path: Path) -> None:
        """审批预览可见「将补装」清单：bootstrap 已带的算满足，未装的是 missing。"""
        m, _bridge = self._prepare(tmp_path)
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0",
                               blob=_HOST_IMPORT_BLOB))
        _install(m, local)
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        deps = st["preview"]["host_dependencies"]
        assert deps["required"] == [
            "@deepseek-ai/dsh-credentials", "@deepseek-ai/dsh-settings"]
        assert deps["missing"] == ["@deepseek-ai/dsh-settings"]  # credentials 在 bootstrap 清单

    def test_post_install_installs_missing_host_deps(self, tmp_path: Path) -> None:
        """装后按宿主精确版本补装（官方 runProfilePnpm 面），状态落 host_dependencies。"""
        m, bridge = self._prepare(tmp_path)
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))
        _install(m, local)
        assert bridge.host_deps == [[f"@deepseek-ai/dsh-settings@{m._host_version()}"]]
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "installed"
        assert st["host_dependencies"] == [
            "@deepseek-ai/dsh-credentials", "@deepseek-ai/dsh-settings"]
        assert st["host_deps_installed"] == ["@deepseek-ai/dsh-settings"]
        assert st["host_deps_error"] is None

    def test_host_dep_failure_is_visible_not_blocking(self, tmp_path: Path) -> None:
        """补装失败不阻断安装：插件仍 installed，host_deps_error 如实落状态。"""
        m, bridge = self._prepare(tmp_path)
        bridge.host_dep_result = {"ok": False, "data": None, "error": "pnpm failed"}
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))
        _install(m, local)
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "installed"
        assert st["host_deps_error"] == "pnpm failed"

    def test_host_dep_bridge_exception_is_visible(self, tmp_path: Path) -> None:
        m, bridge = self._prepare(tmp_path)

        async def boom(specs: list[str], timeout_s: float = 900.0) -> dict[str, Any]:
            raise RuntimeError("bridge down")

        bridge.manager_add_host_deps = boom  # type: ignore[method-assign]
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))
        _install(m, local)
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["status"] == "installed"
        assert "bridge down" in st["host_deps_error"]

    def test_no_missing_deps_skips_install(self, tmp_path: Path) -> None:
        """全部依赖已在 bootstrap 清单 → 不触发补装，只落扫描结果。"""
        m, bridge = self._prepare(
            tmp_path, blob="import x from '@deepseek-ai/dsh-tools';\n")
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))
        _install(m, local)
        assert bridge.host_deps == []
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert st["host_dependencies"] == ["@deepseek-ai/dsh-tools"]
        assert st["host_deps_error"] is None

    def test_missing_package_dir_skips_scan(self, tmp_path: Path) -> None:
        """官方 install 未落盘（桩不建目录）→ 扫描面跳过，不碰桥。"""
        m, bridge = self._prepare(tmp_path, with_pkg_dir=False)
        local = str(_local_pkg(tmp_path / "pkgsrc", "ok-pkg", "1.0.0"))
        _install(m, local)
        assert bridge.host_deps == []
        st = mgr.package_state(Path(m.runtime_root), "ok-pkg")
        assert "host_dependencies" not in st

    def test_stale_host_dep_error_self_heals_when_deps_present(self, tmp_path: Path) -> None:
        """装机时 bootstrap 未就绪的陈旧失败记录，依赖已在位即清（读路径对账）。"""
        m, _bridge = self._prepare(tmp_path, blob="import x from '@deepseek-ai/dsh-agent';\n")
        root = Path(m.runtime_root)
        # 模拟陈旧记录：安装面失败（profile context unavailable 同款）+ 依赖随后到位
        mgr.mark_package_state(root, "ok-pkg", host_dependencies=["@deepseek-ai/dsh-agent"],
                               host_deps_error="host dep install failed: profile context unavailable")
        # dsh-agent 不在 bootstrap 预装清单 → missing；把依赖实体放进 node_modules
        dep = root / "node_modules" / "@deepseek-ai" / "dsh-agent"
        dep.mkdir(parents=True)
        (dep / "package.json").write_text('{"name":"@deepseek-ai/dsh-agent","version":"1.0.0"}',
                                          encoding="utf-8")
        state = mgr.rw.read_state(root)
        m._reconcile_host_dep_errors(root, state)
        st = mgr.package_state(root, "ok-pkg")
        assert st["host_deps_error"] is None
        # 当前列表视图同步反映（in-memory entry 已更新）
        assert state["packages"]["ok-pkg"]["host_deps_error"] is None

    def test_stale_host_dep_error_kept_while_still_missing(self, tmp_path: Path) -> None:
        """依赖仍缺失 → 失败记录保留（fail-visible，不掩盖真缺口）。"""
        m, _bridge = self._prepare(tmp_path, blob="import x from '@deepseek-ai/dsh-agent';\n")
        root = Path(m.runtime_root)
        stale = "host dep install failed: profile context unavailable"
        mgr.mark_package_state(root, "ok-pkg", host_dependencies=["@deepseek-ai/dsh-agent"],
                               host_deps_error=stale)
        state = mgr.rw.read_state(root)
        m._reconcile_host_dep_errors(root, state)
        st = mgr.package_state(root, "ok-pkg")
        assert st["host_deps_error"] == stale


# ── 运行态（cordis Fiber 状态）合并 ─────────────────────────────────


class TestActivationState:
    def test_bundle_activation_state_merged(self, tmp_path: Path) -> None:
        """桥侧 bundleStates 合并进管理行：pending + inject 服务清单可见。"""
        m, stub = _make_manager(tmp_path)
        _mk_workspace_pkg(Path(m.runtime_root), "bundle-a", "bundle-a", "1.0.0")
        stub.list_result = {"ok": True, "data": {"bundles": [
            {"name": "bundle-a", "version": "1.0.0", "enabled": True, "installed": True,
             "availability": "profile"},
        ]}, "error": None}
        stub.bundle_states = [{"id": "bundle-a", "name": "bundle-a", "state": 0,
                               "label": "pending", "injects": ["sessions"]}]
        out = asyncio.run(m.list_installed())
        row = out["packages"][0]
        assert row["activation"] == "pending"
        assert row["activation_injects"] == ["sessions"]

    def test_provider_states_exposed_in_listing(self, tmp_path: Path) -> None:
        """宿主服务挂载态随清单返回（fail-visible：缺哪个服务致 bundle pending）。"""
        m, stub = _make_manager(tmp_path)
        stub.list_result = {"ok": True, "data": {"bundles": [], "plugins": [], "registries": None},
                            "error": None}
        stub.provider_states = [
            {"name": "@deepseek-ai/dsh-credentials-local", "state": 2,
             "label": "active", "injects": []},
            {"name": "@deepseek-ai/dsh-config-editor", "state": 0,
             "label": "pending", "injects": ["loader", "profileContext"]},
        ]
        out = asyncio.run(m.list_installed())
        assert [p["name"] for p in out["providers"]] == [
            "@deepseek-ai/dsh-credentials-local", "@deepseek-ai/dsh-config-editor"]
        assert out["providers"][1]["label"] == "pending"

    def test_unknown_activation_without_bridge_states(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.list_result = {"ok": True, "data": {"bundles": [
            {"name": "bundle-a", "version": "1.0.0", "enabled": True, "installed": True,
             "availability": "profile"},
        ]}, "error": None}
        out = asyncio.run(m.list_installed())
        assert out["packages"][0]["activation"] == "unknown"

    def test_non_dict_state_entries_skipped_and_error_exposed(self, tmp_path: Path) -> None:
        m, stub = _make_manager(tmp_path)
        stub.list_result = {"ok": True, "data": {"bundles": [
            {"name": "bundle-a", "version": "1.0.0", "enabled": True, "installed": True,
             "availability": "profile"},
        ]}, "error": None}
        stub.bundle_states = [
            "junk-entry",
            {"id": "bundle-a", "name": "bundle-a", "state": 3, "label": "failed",
             "injects": [], "error": "boom"},
        ]
        out = asyncio.run(m.list_installed())
        row = out["packages"][0]
        assert row["activation"] == "failed"
        assert row["activation_error"] == "boom"


# ── 真实命令执行器（_default_npm_runner）─────────────────────


class TestDefaultNpmRunner:
    """npm 换形态只对 npm 命令生效；pnpm 绝对路径原样执行（首装归一命令）。"""

    def test_npm_command_swapped_to_platform_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[list[str]] = []

        class _Proc:
            returncode = 0
            stdout = b"ok"
            stderr = b""

        def fake_run(real: list[str], **kwargs: Any) -> _Proc:
            captured.append(list(real))
            return _Proc()

        monkeypatch.setattr(mgr.rw, "resolve_npm_binary", lambda: "NPM_BIN")
        monkeypatch.setattr(mgr.subprocess, "run", fake_run)
        rc, out, err = mgr._default_npm_runner(["npm", "install", "--prefix", "x"], 10)
        assert (rc, out, err) == (0, "ok", "")
        assert captured == [["NPM_BIN", "install", "--prefix", "x"]]

    def test_pnpm_command_passed_through_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[list[str]] = []

        class _Proc:
            returncode = 0
            stdout = b""
            stderr = b""

        def fake_run(real: list[str], **kwargs: Any) -> _Proc:
            captured.append(list(real))
            return _Proc()

        pnpm = r"C:\ws\.agentos\pnpm-runtime\node_modules\@pnpm\exe\pnpm.exe"
        monkeypatch.setattr(mgr.rw, "resolve_npm_binary", lambda: "NPM_BIN")
        monkeypatch.setattr(mgr.subprocess, "run", fake_run)
        cmd = [pnpm, "--dir", r"C:\ws", "install", "--config.confirmModulesPurge=false"]
        rc, _, _ = mgr._default_npm_runner(cmd, 10)
        assert rc == 0
        assert captured == [cmd]


class TestSplitNameVersion:
    """npm 内联版本拆解：裸名/scope 名不误拆，非法版本段原样回落。"""

    def test_bare_and_scoped_names(self) -> None:
        assert mgr._split_name_version("dsh-pet") == ("dsh-pet", None)
        assert mgr._split_name_version("@scope/name") == ("@scope/name", None)

    def test_inline_versions(self) -> None:
        assert mgr._split_name_version("dsh-pet@0.4.1") == ("dsh-pet", "0.4.1")
        assert mgr._split_name_version("@scope/name@1.2.3") == ("@scope/name", "1.2.3")

    def test_invalid_version_segment_falls_back_to_bare_name(self) -> None:
        for spec in ("pkg@1/2", "pkg@1 2", "pkg@", "pkg@a:b"):
            assert mgr._split_name_version(spec) == (spec, None)

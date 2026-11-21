# @feature: FP-0.2.可观测性 dsh_adapter 插件（客户端 UI 贡献） | @ci: python-coverage
"""client_ui 静态事实提取 + 组件包文件供给 + 两个端点的分支测试。

口径：
- 入口解析按包清单真值（exports["./client"] 多形态 > dsh.client.entry > 约定回退）；
- 槽位扫描是静态信号（不执行 DSH 代码）：压缩/动态构造扫不到就如实返回空；
- 槽位→页面位归 translator 单源（本模块只声明哪些挂载位渲染已接通）；
- 文件供给按「包名白名单形态 + 后缀白名单 + resolve 后仍在包目录内」三重约束。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import client_ui as cu  # noqa: E402

pytestmark = pytest.mark.unit


# ── 入口解析 ─────────────────────────────────────────────


def test_resolve_client_entry_exports_string() -> None:
    assert cu.resolve_client_entry({"exports": {"./client": "./lib/client.js"}}) == "lib/client.js"


def test_resolve_client_entry_exports_default_object() -> None:
    manifest = {"exports": {"./client": {"default": "./lib/composer.js"}}}
    assert cu.resolve_client_entry(manifest) == "lib/composer.js"


def test_resolve_client_entry_exports_nested_import() -> None:
    manifest = {"exports": {"./client": {"import": {"default": "./client.js"}}}}
    assert cu.resolve_client_entry(manifest) == "client.js"


def test_resolve_client_entry_dsh_client_entry() -> None:
    assert cu.resolve_client_entry({"dsh": {"client": {"entry": "./x/y.js"}}}) == "x/y.js"


def test_resolve_client_entry_fallback_requires_file(tmp_path: Path) -> None:
    assert cu.resolve_client_entry({}, tmp_path) is None
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "client.js").write_text("x", encoding="utf-8")
    assert cu.resolve_client_entry({}, tmp_path) == "lib/client.js"


def test_resolve_client_entry_absent() -> None:
    assert cu.resolve_client_entry({"exports": {".": "./lib/index.js"}}) is None


# ── 槽位扫描 ─────────────────────────────────────────────


def test_scan_client_slots_single_and_double_quotes() -> None:
    text = """
    slots.register({ name: "a.slot", id: "one", order: 50 }, Comp)
    slots.register({ name: 'b.slot', key: "two" }, Comp2)
    """
    assert cu.scan_client_slots(text) == [
        {"name": "a.slot", "id": "one", "label": None, "order": 50},
        {"name": "b.slot", "id": "two", "label": None, "order": None},
    ]


def test_scan_client_slots_dedupes_by_name() -> None:
    text = "slots.register({ name: 'a' }, C1)\nslots.register({ name: 'a' }, C2)"
    assert [s["name"] for s in cu.scan_client_slots(text)] == ["a"]


def test_scan_client_slots_label_captured() -> None:
    text = "slots.register({ name: 'a', label: '指挥台', order: 7 }, C)"
    slot = cu.scan_client_slots(text)[0]
    assert slot["label"] == "指挥台" and slot["order"] == 7


def test_scan_client_slots_minified_returns_empty() -> None:
    # 压缩包的注册写法没有可静态识别的字面量 → 空列表（能力边界如实返回）
    assert cu.scan_client_slots("t.register(e,n)") == []


def test_scan_client_injects_dedupes() -> None:
    text = "slots.inject('a', cb)\nslots.inject(\"b\", cb)\nslots.inject('a', cb)"
    assert cu.scan_client_injects(text) == ["a", "b"]


# ── 宿主依赖扫描（可运行性判定）──────────────────────────


def test_scan_client_requires_dedupes() -> None:
    text = "require('react')" + chr(10) + 'require("react/jsx-runtime")' + chr(10) + "require('react')"
    assert cu.scan_client_requires(text) == ["react", "react/jsx-runtime"]


def test_unsupported_requires_flags_missing() -> None:
    assert cu.unsupported_requires(["react", "x-primitives"]) == ["x-primitives"]
    assert cu.unsupported_requires(["react", "styles"]) == []


def test_blocking_requires_matches_unsupported_without_optional() -> None:
    # 可选依赖集为空：未提供即硬阻断
    assert cu.blocking_requires(["react", "x-primitives"]) == ["x-primitives"]
    assert cu.blocking_requires(["react"]) == []


def test_blocked_bundle_generates_no_pages(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        "blocked-ui",
        "lib/client.js",
        "require('@deepseek-ai/dsh-client-ui-primitives')" + chr(10) +
        "slots.register({ name: 'sidebar.right.pane.tab' }, C)",
        {"exports": {"./client": "./lib/client.js"}},
    )
    item = cu.client_ui_contributions([pkg])[0]
    assert item["blocked"] == ["@deepseek-ai/dsh-client-ui-primitives"]
    assert item["pages"] == []
    # 槽位本身仍如实报告（有 UI，只是宿主依赖未提供）
    assert item["slots"][0]["name"] == "sidebar.right.pane.tab"


# ── 槽位 → 页面位 ────────────────────────────────────────


def test_page_ids_distinct_per_slot_for_same_bundle(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        "multi-ui",
        "lib/client.js",
        "slots.register({ name: 'sidebar.right.pane.tab', id: 'w' }, C)" + chr(10) +
        "slots.register({ name: 'settings.section', id: 's', label: '设置' }, C2)",
        {"exports": {"./client": "./lib/client.js"}},
    )
    item = cu.client_ui_contributions([pkg])[0]
    ids = [p["id"] for p in item["pages"]]
    assert ids == ["dsh_ui_multi_ui_sidebar_right_pane_tab", "dsh_ui_multi_ui_settings_section"]
    spaces = [p["space"] for p in item["pages"]]
    assert spaces == ["workspace", "settings"]


def test_page_target_right_pane_maps_to_workspace_tab() -> None:
    assert cu.page_target("sidebar.right.pane.tab") == {"space": "workspace", "slot": "tab"}


def test_page_target_unknown_slot_returns_none() -> None:
    assert cu.page_target("no.such.slot") is None


def test_page_target_mapped_but_renderer_not_wired_returns_none() -> None:
    # conversation.input.dock 在 translator 有语义位（dockItems），但渲染面未接通
    assert cu.page_target("conversation.input.dock") is None


def test_page_for_slot_shape() -> None:
    page = cu.page_for_slot("dsh-desktop-sidecar", {"name": "sidebar.right.pane.tab", "id": "steer", "order": 10})
    assert page is not None
    assert page["id"] == "dsh_ui_dsh_desktop_sidecar_sidebar_right_pane_tab"
    assert page["widget"] == "dsh-slot"
    assert page["props"] == {"bundle": "dsh-desktop-sidecar", "slot": "sidebar.right.pane.tab", "contributionId": "steer"}
    assert page["space"] == "workspace" and page["slot"] == "tab"
    assert page["order"] == 70


def test_page_for_slot_unmapped_returns_none() -> None:
    assert cu.page_for_slot("x", {"name": "conversation.input.dock"}) is None


# ── 贡献聚合 ─────────────────────────────────────────────


def _make_pkg(root: Path, name: str, entry: str, body: str, manifest_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    pkg_dir = root / "node_modules" / Path(*name.split("/"))
    target = pkg_dir / entry
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    manifest = {"name": name, "version": "1.0.0", **(manifest_extra or {})}
    (pkg_dir / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    return {"name": name, "version": "1.0.0", "dir": str(pkg_dir), "package_json": manifest}


def test_client_ui_contributions_reports_pages_and_gaps(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path,
        "@scope/right-pane",
        "lib/client.js",
        "slots.register({ name: 'sidebar.right.pane.tab', id: 'tab1', order: 3 }, C)\n"
        "slots.register({ name: 'shell.overlay' }, C2)\n"
        "slots.register({ name: 'conversation.input.dock' }, C3)\n"
        "slots.inject('sidebar.right.pane.tab', cb)",
        {"exports": {"./client": "./lib/client.js"}, "dsh": {"title": "右栏"}},
    )
    out = cu.client_ui_contributions([pkg])
    assert len(out) == 1
    item = out[0]
    assert item["entry"] == "lib/client.js"
    assert item["entry_url"] == "/ext/dsh_adapter/client-bundle/@scope/right-pane/lib/client.js"
    assert item["scanned"] is True
    assert item["injects"] == ["sidebar.right.pane.tab"]
    assert [p["id"] for p in item["pages"]] == ["dsh_ui_scope_right_pane_sidebar_right_pane_tab"]
    assert item["pages"][0]["title"] == "右栏"
    assert item["unmapped"] == ["shell.overlay"]
    assert item["pending"] == ["conversation.input.dock"]


def test_client_ui_contributions_skips_package_without_name_or_dir() -> None:
    assert cu.client_ui_contributions([{"name": "", "dir": "x"}, {"name": "y", "dir": ""}]) == []


def test_client_ui_contributions_skips_packages_without_client(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, "no-ui", "lib/index.js", "x")
    assert cu.client_ui_contributions([pkg]) == []


def test_client_ui_contributions_unreadable_entry_reports_unscanned(tmp_path: Path) -> None:
    pkg = _make_pkg(
        tmp_path, "broken", "lib/client.js", "x",
        {"exports": {"./client": "./lib/client.js"}},
    )
    Path(pkg["dir"], "lib", "client.js").unlink()
    out = cu.client_ui_contributions([pkg])
    assert out[0]["scanned"] is False and out[0]["slots"] == []


# ── 组件包文件供给 ───────────────────────────────────────


def test_resolve_bundle_file_reads_with_content_type(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, "dsh-x", "lib/client.js", "console.log(1)")
    found = cu.resolve_bundle_file(tmp_path, "dsh-x", "lib/client.js")
    assert found is not None
    payload, ctype = found
    assert payload == b"console.log(1)" and ctype.startswith("text/javascript")


def test_resolve_bundle_file_scoped_package(tmp_path: Path) -> None:
    _make_pkg(tmp_path, "@s/p", "lib/client.js", "y")
    assert cu.resolve_bundle_file(tmp_path, "@s/p", "lib/client.js") is not None


@pytest.mark.parametrize("bundle", ["", "../etc", "/abs", "@scope", "a/b/c", "dsh-x/../other"])
def test_resolve_bundle_file_rejects_bad_bundle_names(tmp_path: Path, bundle: str) -> None:
    assert cu.resolve_bundle_file(tmp_path, bundle, "lib/client.js") is None


def test_resolve_bundle_file_rejects_traversal(tmp_path: Path) -> None:
    _make_pkg(tmp_path, "dsh-x", "lib/client.js", "z")
    (tmp_path / "secret.js").write_text("s", encoding="utf-8")
    assert cu.resolve_bundle_file(tmp_path, "dsh-x", "../../secret.js") is None


def test_resolve_bundle_file_rejects_unlisted_suffix(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, "dsh-x", "lib/client.js", "z")
    Path(pkg["dir"], "run.sh").write_text("echo hi", encoding="utf-8")
    assert cu.resolve_bundle_file(tmp_path, "dsh-x", "run.sh") is None


def test_resolve_bundle_file_read_error_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_pkg(tmp_path, "dsh-x", "lib/client.js", "z")
    real_read = Path.read_bytes

    def boom(self: Path) -> bytes:
        if self.name == "client.js":
            raise OSError("io")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert cu.resolve_bundle_file(tmp_path, "dsh-x", "lib/client.js") is None


def test_resolve_bundle_file_missing_package(tmp_path: Path) -> None:
    assert cu.resolve_bundle_file(tmp_path, "absent", "lib/client.js") is None


# ── 端点分发 ─────────────────────────────────────────────


def _load_server() -> Any:
    name = "dsh_adapter_server_client_ui_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def srv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    module = _load_server()

    class _Manager:
        runtime_root = str(tmp_path)

    monkeypatch.setattr(module, "_MANAGER", _Manager())
    return module


def _body(resp: dict[str, Any]) -> Any:
    return json.loads(base64.b64decode(resp["body"]).decode("utf-8"))


def test_client_ui_endpoint_returns_bundles(srv: Any, tmp_path: Path) -> None:
    _make_pkg(tmp_path, "dsh-x", "lib/client.js", "slots.register({ name: 'sidebar.right.pane.tab' }, C)")
    resp = srv._client_ui_response()
    assert resp["status"] == 200
    bundles = _body(resp)["bundles"]
    assert [b["bundle"] for b in bundles] == ["dsh-x"]
    assert bundles[0]["pages"][0]["id"] == "dsh_ui_dsh_x_sidebar_right_pane_tab"


def test_client_ui_endpoint_empty_without_node_modules(srv: Any) -> None:
    assert _body(srv._client_ui_response())["bundles"] == []


def test_client_bundle_endpoint_serves_bytes(srv: Any, tmp_path: Path) -> None:
    _make_pkg(tmp_path, "dsh-x", "lib/client.js", "console.log(1)")
    resp = srv._client_bundle_response("/ext/dsh_adapter/client-bundle/dsh-x/lib/client.js")
    assert resp["status"] == 200
    assert base64.b64decode(resp["body"]) == b"console.log(1)"
    assert resp["headers"]["Content-Type"].startswith("text/javascript")


def test_client_bundle_endpoint_serves_scoped_package(srv: Any, tmp_path: Path) -> None:
    _make_pkg(tmp_path, "@s/p", "lib/client.js", "q")
    resp = srv._client_bundle_response("/ext/dsh_adapter/client-bundle/@s/p/lib/client.js")
    assert resp["status"] == 200


@pytest.mark.parametrize(
    "path",
    [
        "/ext/dsh_adapter/client-bundle/",
        "/ext/dsh_adapter/client-bundle/dsh-x",
        "/ext/dsh_adapter/client-bundle/@s/p",
        "/ext/dsh_adapter/client-bundle/dsh-x/../x.js",
        "/ext/dsh_adapter/client-bundle/dsh-x/lib/missing.js",
    ],
)
def test_client_bundle_endpoint_404_paths(srv: Any, tmp_path: Path, path: str) -> None:
    _make_pkg(tmp_path, "dsh-x", "lib/client.js", "q")
    assert srv._client_bundle_response(path)["status"] == 404


def test_sync_manifest_strips_generated_pages(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    manifest = {
        "contributes": {
            "pages": [{"id": "dsh_plugin_manager"}, {"id": "dsh_ui_dsh_pet"}],
            "themes": [],
            "renderers": [],
        }
    }
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(srv, "__file__", str(plugin_dir / "server.py"))
    import translator as tr

    monkeypatch.setattr(tr, "skins_to_plugin_themes", lambda: [])
    monkeypatch.setattr(tr, "collect_installed_renderers", lambda dirs: [])  # noqa: ARG005
    srv._sync_manifest_contributions()
    data = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in data["contributes"]["pages"]] == ["dsh_plugin_manager"]


def test_http_handle_dispatches_client_ui(srv: Any, tmp_path: Path) -> None:
    _make_pkg(tmp_path, "dsh-x", "lib/client.js", "slots.register({ name: 'sidebar.right.pane.tab' }, C)")
    resp = asyncio.run(srv._http_handle_style(path=srv._CLIENT_UI_PATH, method="GET"))
    assert resp["status"] == 200


def test_http_handle_dispatches_client_bundle(srv: Any, tmp_path: Path) -> None:
    _make_pkg(tmp_path, "dsh-x", "lib/client.js", "q")
    resp = asyncio.run(srv._http_handle_style(path="/ext/dsh_adapter/client-bundle/dsh-x/lib/client.js", method="GET"))
    assert resp["status"] == 200

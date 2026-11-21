# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""server.py 分支覆盖补充测试（工具面锚定/皮肤递送/生命周期钩子/管理端点）。

打桩约定：
- 文件系统 IO 故障用 Path.read_text/read_bytes 定点注入 OSError（真实代码路径不变）；
- plugin.json / 工作区写副作用经 monkeypatch 重定向到 tmp 目录
  （模块 __file__ / AGENTOS_USER_ROOT / AGENTOS_PROJECT_ROOT），代码路径保持真实。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import translator as translator_mod  # noqa: E402

_SRV_MODULE = "dsh_adapter_server_branch_test"


def _load_server_module(module_name: str, source_file: Path) -> Any:
    """显式文件级加载（唯一模块名）：防裸 `import server` 被其它插件目录劫持。"""
    spec = importlib.util.spec_from_file_location(module_name, source_file)
    assert spec is not None and spec.loader is not None, "cannot load dsh_adapter server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def srv() -> Any:
    """真实位置的 server 模块（写面经 monkeypatch 重定向到 tmp）。"""
    return _load_server_module(_SRV_MODULE, PLUGIN_DIR / "server.py")


def _fail_read_text_for(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """文件系统故障注入：仅对 target 的 read_text 抛 OSError，其余委托真实实现。"""
    original = Path.read_text

    def failing(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            raise OSError(f"injected read failure: {self.name}")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing)


def _fail_read_bytes_for(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    original = Path.read_bytes

    def failing(self: Path, *args: Any, **kwargs: Any) -> bytes:
        if self == target:
            raise OSError(f"injected read failure: {self.name}")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", failing)


class TestTranslateManifestTools:
    def test_default_loads_installed_plugins(self) -> None:
        out = translator_mod.load_installed_plugins()
        assert out["count"] >= 1
        assert out["base_dir"].endswith("dsh_plugins")
        assert out["packages"], "仓库自带真实 DSH 插件包应可翻译"

    def test_package_path_translates_single_package(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mini"
        pkg.mkdir()
        (pkg / "package.json").write_text(
            json.dumps({"name": "@x/mini", "version": "9.9.9"}), encoding="utf-8"
        )
        out = translator_mod.translate_package(str(pkg))
        assert out["source"]["package"] == "@x/mini"
        assert out["source"]["version"] == "9.9.9"


# ── 生命周期钩子 ──────────────────────────────────────────────────────


@pytest.fixture
def srv_with_manifest(
    srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """模块 __file__ 重定向到 tmp 适配器目录：on_load 的 plugin.json 写面被隔离。"""
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "plugin.json").write_text('{"plugin_id": "dsh_adapter"}', encoding="utf-8")
    monkeypatch.setattr(srv, "__file__", str(adapter / "server.py"))
    # 已装包扫描面隔离（renderers 聚合走桩；单测不触真实用户空间工作区）
    monkeypatch.setattr(srv, "_installed_package_dirs", lambda: [])
    return adapter / "plugin.json"


class TestLifecycleHooks:
    def test_on_load_writes_new_themes(
        self,
        srv: Any,
        srv_with_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        themes = [{"id": "dsh-skin-test", "name": "T", "base": "dark"}]
        monkeypatch.setattr(translator_mod, "skins_to_plugin_themes", lambda: themes)
        with caplog.at_level(logging.INFO):
            asyncio.run(srv._on_dsh_adapter_load({}))  # noqa: SLF001
        manifest = json.loads(srv_with_manifest.read_text(encoding="utf-8"))
        assert manifest["contributes"]["themes"] == themes
        assert any("auto-synced" in r.message for r in caplog.records)

    def test_on_load_idempotent_no_rewrite(
        self,
        srv: Any,
        srv_with_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        themes = [{"id": "dsh-skin-test", "name": "T", "base": "dark"}]
        monkeypatch.setattr(translator_mod, "skins_to_plugin_themes", lambda: themes)
        asyncio.run(srv._on_dsh_adapter_load({}))  # noqa: SLF001
        before = srv_with_manifest.read_bytes()
        with caplog.at_level(logging.INFO):
            asyncio.run(srv._on_dsh_adapter_load({}))  # noqa: SLF001
        assert srv_with_manifest.read_bytes() == before  # 幂等：现值一致不写回
        assert not any("auto-synced" in r.message for r in caplog.records)

    def test_on_load_syncs_renderers_from_installed_packages(
        self,
        srv: Any,
        srv_with_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """已装 DSH 包的 toolview 渲染声明写回 contributes.renderers（前端消费面）。"""
        monkeypatch.setattr(translator_mod, "skins_to_plugin_themes", lambda: [])
        monkeypatch.setattr(
            translator_mod, "collect_installed_renderers",
            lambda _dirs: [{"tool": "read", "card": "read"}])
        monkeypatch.setattr(srv, "_installed_package_dirs", lambda: ["/ws/node_modules/pkg"])
        with caplog.at_level(logging.INFO):
            asyncio.run(srv._on_dsh_adapter_load({}))  # noqa: SLF001
        manifest = json.loads(srv_with_manifest.read_text(encoding="utf-8"))
        assert manifest["contributes"]["renderers"] == [{"tool": "read", "card": "read"}]
        assert any("renderers=1" in r.message for r in caplog.records)

    def test_on_load_failure_does_not_block(
        self,
        srv: Any,
        srv_with_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def boom() -> list[dict[str, str]]:
            raise RuntimeError("skin center broken")

        monkeypatch.setattr(translator_mod, "skins_to_plugin_themes", boom)
        with caplog.at_level(logging.WARNING):
            asyncio.run(srv._on_dsh_adapter_load({}))  # noqa: SLF001 — 不抛
        assert any("contributes auto-sync failed" in r.message for r in caplog.records)

    def test_on_unload_shuts_bridge_down(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shut: list[bool] = []

        async def fake_shutdown() -> None:
            shut.append(True)

        monkeypatch.setattr(srv, "shutdown_bridge", fake_shutdown)
        asyncio.run(srv._on_dsh_adapter_unload({}))  # noqa: SLF001
        assert shut == [True]


# ── ETag 协商缓存 ─────────────────────────────────────────────────────


class TestRevalidate:
    PAYLOAD = b"hello skin"

    def test_etag_is_weak_sha1_prefix(self, srv: Any) -> None:
        etag = srv._etag_of(self.PAYLOAD)
        assert etag.startswith('"') and etag.endswith('"')
        assert srv._etag_of(self.PAYLOAD) == etag  # 内容寻址稳定
        assert srv._etag_of(b"other") != etag

    @pytest.mark.parametrize(
        "headers,expected",
        [
            (None, False),
            ({}, False),
            ({"other-header": "x"}, False),
            ({"if-none-match": '"nope"'}, False),
        ],
    )
    def test_no_hit(
        self, srv: Any, headers: dict[str, str] | None, expected: bool
    ) -> None:
        assert srv._revalidate(headers, self.PAYLOAD) is expected

    @pytest.mark.parametrize(
        "key,value",
        [
            ("if-none-match", "MATCH"),
            ("If-None-Match", "MATCH"),
            ("if-none-match", '"stale", MATCH'),
            ("if-none-match", '"stale", MATCH, "other"'),
        ],
    )
    def test_hit_forms(self, srv: Any, key: str, value: str) -> None:
        etag = srv._etag_of(self.PAYLOAD)
        assert srv._revalidate({key: value.replace("MATCH", etag)}, self.PAYLOAD) is True


# ── 皮肤 CSS 位置路由：块级边界 ────────────────────────────────────────


class TestCssRewriteEdgeCases:
    def test_unbalanced_braces_fail_closed(self, srv: Any) -> None:
        css = '[data-pane="sidebar"]{a:b'
        assert srv._rewrite_dsh_positions(css) == css  # 原样返回不丢内容

    def test_content_after_last_rule_preserved(self, srv: Any) -> None:
        css = '[data-pane="sidebar"]{a:b}\n/* tail comment */'
        out = srv._rewrite_dsh_positions(css)
        assert out.endswith("/* tail comment */")
        assert '[data-region="sidebar"]{a:b}' in out

    def test_root_content_row_rewritten(self, srv: Any) -> None:
        out = srv._rewrite_dsh_positions('[id="root"] > div:has([data-pane]){background:#fff}')
        assert out == '#root > div:first-child{background:#fff;}'

    def test_root_child_with_pane_keeps_non_frame_decls(self, srv: Any) -> None:
        out = srv._rewrite_dsh_positions('[id="root"] > div > [data-pane="sidebar"]{color:red}')
        assert out == '[id="root"] > div > [data-region="sidebar"]{color:red;}'

    def test_root_frame_only_rule_dropped(self, srv: Any) -> None:
        assert srv._rewrite_dsh_positions('[id="root"] > div:has([data-pane]){outline:none}') == ""


# ── 皮肤递送端点（tmp 皮肤根，覆盖读失败/304/缺文件分支） ──────────────


def _make_skin(tmp_path: Path, skin_id: str, files: dict[str, str]) -> Path:
    root = tmp_path / "skins"
    skin = root / skin_id
    skin.mkdir(parents=True)
    for name, content in files.items():
        (skin / name).write_text(content, encoding="utf-8")
    return root


TOK_SKIN_CSS = (
    ':root{background-color:#111318;color:#e6e6e6;}\n'
    '[data-pane="sidebar"]{border-left:1px solid #333;}\n'
    '.card{background:url(assets/bg.webp) no-repeat;}\n'
    '.icon{background:url(data:image/png;base64,AAAA);}\n'
    '.logo{background:url(https://example.com/a.png);}\n'
    '.abs{background:url(/abs.png);}\n'
)
TOK_HOOKS_MJS = (
    "export const FOOTER = \"[data-pane='sidebar'] > div > :last-child\";\n"
    "const scope = 'html[data-dsh-skin=\"tok\"]';\n"
)


@pytest.fixture
def skin_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _make_skin(tmp_path, "tok", {"skin.css": TOK_SKIN_CSS, "hooks.mjs": TOK_HOOKS_MJS})
    _make_skin(tmp_path, "cssonly", {"skin.css": '[data-pane="conversation"]{color:#fff;}\n'})
    _make_skin(tmp_path, "badcss", {"skin.css": TOK_SKIN_CSS})
    _make_skin(tmp_path, "badhook", {"skin.css": ":root{color:#fff;}\n", "hooks.mjs": "export {};\n"})
    # 皮肤根常量在 translator（listing）与 server（skin_dir）两侧各自消费，须同指
    monkeypatch.setattr(translator_mod, "SKIN_CENTER_SKINS_DIR", root)
    return root


class TestServeMergedSkinCss:
    @pytest.fixture
    def srv_patched(self, srv: Any, skin_root: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setattr(srv, "SKIN_CENTER_SKINS_DIR", skin_root)
        return srv

    def _serve(self, srv_patched: Any, path: str, headers: dict[str, str] | None = None) -> dict:
        return srv_patched._serve_merged_skin_css(path, headers)  # noqa: SLF001

    def test_merged_css_translated_and_urls_rerouted(self, srv_patched: Any) -> None:
        resp = self._serve(srv_patched, "/ext/dsh_adapter/styles/skin/tok/merged.css")
        assert resp is not None and resp["status"] == 200
        assert resp["headers"]["content-type"].startswith("text/css")
        body = base64.b64decode(resp["body"]).decode("utf-8")
        assert "dsh_adapter merged skin css: tok" in body
        assert "data-pane" not in body
        assert '[data-region="sidebar"]' in body
        # 相对 url → 资产路由；data:/https:/绝对路径原样保留
        assert "url(/ext/dsh_adapter/styles/skin-assets/tok/assets/bg.webp)" in body
        assert "url(data:image/png;base64,AAAA)" in body
        assert "url(https://example.com/a.png)" in body
        assert "url(/abs.png)" in body

    def test_hooks_translated_with_quote_preserved(self, srv_patched: Any) -> None:
        resp = self._serve(srv_patched, "/ext/dsh_adapter/styles/skin/tok/hooks.mjs")
        assert resp is not None and resp["status"] == 200
        assert resp["headers"]["content-type"].startswith("text/javascript")
        body = base64.b64decode(resp["body"]).decode("utf-8")
        # 结构全形整段翻译（hooks footer 走查形态）；输出镜像输入引号风格
        assert '"[data-testid=\'sidebar-footer\']"' in body
        assert 'html[data-skin="dsh_adapter:tok"]' in body

    def test_hooks_304_on_etag_match(self, srv_patched: Any) -> None:
        first = self._serve(srv_patched, "/ext/dsh_adapter/styles/skin/tok/hooks.mjs")
        assert first is not None
        etag = first["headers"]["etag"]
        second = self._serve(
            srv_patched, "/ext/dsh_adapter/styles/skin/tok/hooks.mjs", {"if-none-match": etag}
        )
        assert second is not None
        assert second["status"] == 304
        assert second["body"] == ""

    def test_merged_css_304_on_etag_match(self, srv_patched: Any) -> None:
        path = "/ext/dsh_adapter/styles/skin/tok/merged.css"
        first = self._serve(srv_patched, path)
        assert first is not None
        second = self._serve(srv_patched, path, {"if-none-match": first["headers"]["etag"]})
        assert second is not None
        assert second["status"] == 304

    def test_skin_without_hooks_serves_empty_200(self, srv_patched: Any) -> None:
        resp = self._serve(srv_patched, "/ext/dsh_adapter/styles/skin/cssonly/hooks.mjs")
        assert resp is not None
        assert resp["status"] == 200
        assert resp["body"] == ""

    def test_skin_without_patches_serves_css_only(self, srv_patched: Any) -> None:
        resp = self._serve(srv_patched, "/ext/dsh_adapter/styles/skin/cssonly/merged.css")
        assert resp is not None and resp["status"] == 200
        body = base64.b64decode(resp["body"]).decode("utf-8")
        assert '[data-region="chat"]' in body

    def test_unreadable_skin_css_returns_404(
        self, srv_patched: Any, skin_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fail_read_text_for(monkeypatch, skin_root / "badcss" / "skin.css")
        resp = self._serve(srv_patched, "/ext/dsh_adapter/styles/skin/badcss/merged.css")
        assert resp is not None and resp["status"] == 404

    def test_unreadable_hooks_returns_500(
        self, srv_patched: Any, skin_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fail_read_text_for(monkeypatch, skin_root / "badhook" / "hooks.mjs")
        resp = self._serve(srv_patched, "/ext/dsh_adapter/styles/skin/badhook/hooks.mjs")
        assert resp is not None and resp["status"] == 500

    def test_non_matching_route_returns_none(self, srv_patched: Any) -> None:
        assert self._serve(srv_patched, "/ext/dsh_adapter/styles/other.css") is None


class TestServeSkinAsset:
    PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload" * 8

    @pytest.fixture
    def srv_patched(self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        root = tmp_path / "skins"
        asset_dir = root / "pica" / "assets"
        asset_dir.mkdir(parents=True)
        (asset_dir / "pic.png").write_bytes(self.PNG_BYTES)
        monkeypatch.setattr(translator_mod, "SKIN_CENTER_SKINS_DIR", root)
        monkeypatch.setattr(srv, "SKIN_CENTER_SKINS_DIR", root)
        return srv

    def _serve(
        self, srv_patched: Any, path: str, headers: dict[str, str] | None = None
    ) -> dict:
        return asyncio.run(
            srv_patched._http_handle_style(  # noqa: SLF001
                path=path, method="GET", headers=headers
            )
        )

    def test_serves_image_with_content_type(self, srv_patched: Any) -> None:
        resp = self._serve(srv_patched, "/ext/dsh_adapter/styles/skin-assets/pica/assets/pic.png")
        assert resp["status"] == 200
        assert resp["headers"]["content-type"] == "image/png"
        assert base64.b64decode(resp["body"]) == self.PNG_BYTES
        assert resp["headers"]["cache-control"] == "no-cache"

    def test_304_on_etag_match(self, srv_patched: Any) -> None:
        path = "/ext/dsh_adapter/styles/skin-assets/pica/assets/pic.png"
        first = self._serve(srv_patched, path)
        second = self._serve(srv_patched, path, {"if-none-match": first["headers"]["etag"]})
        assert second["status"] == 304
        assert second["body"] == ""

    @pytest.mark.parametrize(
        "suffix",
        [
            "",  # 无文件段（…/pica）
            "/",  # 空文件段（…/pica/）
            "/assets/missing.png",  # 白名单内但文件不存在
            "/../secret.png",  # 路径穿越
        ],
    )
    def test_404_rejections(self, srv_patched: Any, suffix: str) -> None:
        resp = self._serve(srv_patched, f"/ext/dsh_adapter/styles/skin-assets/pica{suffix}")
        assert resp["status"] == 404

    def test_404_non_image_extension(self, srv_patched: Any) -> None:
        resp = self._serve(srv_patched, "/ext/dsh_adapter/styles/skin-assets/pica/skin.json")
        assert resp["status"] == 404

    def test_500_on_unreadable_asset(
        self, srv_patched: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "skins" / "pica" / "assets" / "pic.png"
        _fail_read_bytes_for(monkeypatch, target)
        resp = self._serve(srv_patched, "/ext/dsh_adapter/styles/skin-assets/pica/assets/pic.png")
        assert resp["status"] == 500

    def test_unknown_path_falls_through_to_404(self, srv_patched: Any) -> None:
        resp = self._serve(srv_patched, "/ext/dsh_adapter/styles/unknown")
        assert resp == {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}


# ── 已装包目录扫描（renderers 聚合来源）──────────────────────────────


class TestInstalledPackageDirs:
    def test_absent_workspace_returns_empty(self, srv: Any, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
        class _M:
            runtime_root = str(tmp_path / "ws")

        monkeypatch.setattr(srv, "_manager", lambda: _M())
        assert srv._installed_package_dirs() == []  # noqa: SLF001

    def test_filters_infra_packages(self, srv: Any, tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
        ws = tmp_path / "ws"
        for rel, name in (("ok-pkg", "ok-pkg"),
                          ("@deepseek-ai/dsh-tools", "@deepseek-ai/dsh-tools")):
            d = ws / "node_modules" / rel
            d.mkdir(parents=True)
            (d / "package.json").write_text(
                json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8")

        class _M:
            runtime_root = str(ws)

        monkeypatch.setattr(srv, "_manager", lambda: _M())
        dirs = srv._installed_package_dirs()  # noqa: SLF001
        assert len(dirs) == 1
        assert dirs[0].replace("\\", "/").endswith("node_modules/ok-pkg")

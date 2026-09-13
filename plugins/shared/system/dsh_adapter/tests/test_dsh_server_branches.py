# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""server.py 分支覆盖补充测试（工具面锚定/皮肤递送/外包装载/生命周期钩子）。

打桩约定：
- 桥（Node runtime）是外部依赖 → 用 FakeBridge 替身，断言锚定后的参数契约；
- 文件系统 IO 故障用 Path.read_text/read_bytes 定点注入 OSError（真实代码路径不变）；
- plugin.json / runtime/extra-tools 的写副作用经 monkeypatch 重定向到 tmp 目录
  （_EXTRA_TOOLS_DIR / 模块 __file__ / AGENTOS_PROJECT_ROOT），代码路径保持真实。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import shutil
import subprocess
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


# ── _make_junction / _sync_extra_package：外包装载区原语 ──────────────


class TestMakeJunction:
    def test_creates_link_to_target(self, srv: Any, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        (target / "m.txt").write_text("x", encoding="utf-8")
        link = tmp_path / "link"
        srv._make_junction(link, target)
        assert link.exists() or link.is_symlink()
        assert (link / "m.txt").is_file()  # 链接可透传访问目标内容

    def test_existing_link_skipped(self, srv: Any, tmp_path: Path) -> None:
        link = tmp_path / "link"
        link.mkdir()
        (link / "keep.txt").write_text("orig", encoding="utf-8")
        other = tmp_path / "other"
        other.mkdir()
        srv._make_junction(link, other)
        # 未被重建为 other 的链接：原目录内容原样保留
        assert (link / "keep.txt").read_text(encoding="utf-8") == "orig"

    def test_symlink_failure_falls_back_to_mklink(
        self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_privilege(src: Any, dst: Any, **kwargs: Any) -> None:
            raise OSError("no privilege")

        recorded: list[tuple[list[str], bool]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            recorded.append((cmd, kwargs.get("check", True)))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(srv.os, "symlink", no_privilege)
        monkeypatch.setattr(srv.subprocess, "run", fake_run)
        link = tmp_path / "link"
        target = tmp_path / "target"
        target.mkdir()
        srv._make_junction(link, target)
        assert recorded == [(["cmd", "/c", "mklink", "/J", str(link), str(target)], False)]


class TestSyncExtraPackage:
    def test_copies_lib_and_manifest(self, srv: Any, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "lib").mkdir(parents=True)
        (src / "lib" / "index.js").write_text("export const a = 1;", encoding="utf-8")
        (src / "package.json").write_text('{"name": "p"}', encoding="utf-8")
        dest = tmp_path / "dest" / "p"
        srv._sync_extra_package(dest, src)
        assert (dest / "lib" / "index.js").read_text(encoding="utf-8") == "export const a = 1;"
        assert json.loads((dest / "package.json").read_text(encoding="utf-8")) == {"name": "p"}

    def test_source_without_lib_copies_manifest_only(self, srv: Any, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "package.json").write_text('{"name": "p"}', encoding="utf-8")
        dest = tmp_path / "dest" / "p"
        srv._sync_extra_package(dest, src)
        assert (dest / "package.json").is_file()
        assert not (dest / "lib").exists()

    def test_resync_overwrites_changed_content(self, srv: Any, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "lib").mkdir(parents=True)
        (src / "lib" / "index.js").write_text("v1", encoding="utf-8")
        dest = tmp_path / "dest" / "p"
        srv._sync_extra_package(dest, src)
        (src / "lib" / "index.js").write_text("v2", encoding="utf-8")
        srv._sync_extra_package(dest, src)
        assert (dest / "lib" / "index.js").read_text(encoding="utf-8") == "v2"


# ── ensure_extra_tools_layout：装载区同步（幂等 + 清理 + 配置过滤） ────


def _setup_layout_env(srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp 包源 + tmp 配置根 + tmp DSH 仓库 + tmp 装载区，全部走真实代码路径。

    返回 extra-tools 装载区目录。
    """
    pkgs = tmp_path / "adapter" / "dsh_plugins"
    for name in ("good-tool", "disabled-tool", "plain-pkg"):
        (pkgs / name).mkdir(parents=True)
        (pkgs / name / "package.json").write_text(
            json.dumps({"name": f"@x/{name}"}), encoding="utf-8"
        )
    for name in ("good-tool", "disabled-tool"):
        (pkgs / name / "lib").mkdir()
        (pkgs / name / "lib" / "index.js").write_text("export {};", encoding="utf-8")

    cfg_root = tmp_path / "root"
    (cfg_root / "config").mkdir(parents=True)
    (cfg_root / "config" / "dsh_adapter.yaml").write_text(
        "plugins:\n  disabled-tool:\n    enabled: false\n", encoding="utf-8"
    )

    repo = tmp_path / "repo"
    for rel in srv._PEER_PKGS.values():  # noqa: SLF001 — 布局契约常量表
        (repo / rel).mkdir(parents=True)

    extra = tmp_path / "extra-tools" / "node_modules" / "@deepseek-ai"
    # discover_dsh_plugins 的缺省基目录取 translator.__file__ 的父目录；
    # 装载区常量重定向到 tmp（真实插件目录不落任何写副作用）
    monkeypatch.setattr(translator_mod, "__file__", str(tmp_path / "adapter" / "translator.py"))
    monkeypatch.setattr(srv, "_EXTRA_TOOLS_DIR", extra)
    monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(cfg_root))
    monkeypatch.setenv("AGENTOS_DSH_REPO_ROOT", str(repo))
    return extra


class TestEnsureExtraToolsLayout:
    def test_syncs_enabled_packages_and_cleans_stale(
        self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra = _setup_layout_env(srv, tmp_path, monkeypatch)
        (extra / "stale-pkg").mkdir(parents=True)  # 预置残留：应被清理

        out = srv.ensure_extra_tools_layout()
        assert out == str(extra)
        # peer 基础包链接就位（无 symlink 特权时经 mklink /J 兜底，均为真实路径）
        for peer in srv._PEER_PKGS:  # noqa: SLF001
            assert (extra / peer).exists(), peer
        # 启用且含 lib/index.js 的包被拷贝；禁用/无 lib 的包不装载
        assert (extra / "good-tool" / "lib" / "index.js").is_file()
        assert json.loads((extra / "good-tool" / "package.json").read_text(encoding="utf-8")) == {
            "name": "@x/good-tool"
        }
        assert not (extra / "disabled-tool").exists()
        assert not (extra / "plain-pkg").exists()
        assert not (extra / "stale-pkg").exists()

        # 幂等：再次装载不报错、结果稳定
        assert srv.ensure_extra_tools_layout() == out
        assert (extra / "good-tool" / "lib" / "index.js").is_file()

    def test_cleanup_is_best_effort_on_locked_entry(
        self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra = _setup_layout_env(srv, tmp_path, monkeypatch)
        locked = extra / "locked"
        locked.mkdir(parents=True)

        def rmtree_raiser(path: Any, **kwargs: Any) -> None:
            raise OSError("file in use")

        monkeypatch.setattr(shutil, "rmtree", rmtree_raiser)
        out = srv.ensure_extra_tools_layout()
        assert out == str(extra)
        assert locked.is_dir()  # 清理失败保留残留，不阻断装载主流程
        assert (extra / "good-tool" / "lib" / "index.js").is_file()


class TestGetBridge:
    def test_first_call_injects_layout_dir_and_memoizes(
        self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected_dir = _setup_layout_env(srv, tmp_path, monkeypatch)
        injected: list[str | None] = []

        class _FakeBridge:
            pass

        singleton = _FakeBridge()

        def fake_factory(extra_plugins_dir: str | None = None) -> Any:
            injected.append(extra_plugins_dir)
            return singleton

        monkeypatch.setattr(srv, "_get_bridge", fake_factory)
        monkeypatch.setattr(srv, "_bridge_ready", False)

        bridge1 = srv.get_bridge()
        bridge2 = srv.get_bridge()
        assert bridge1 is singleton
        assert bridge2 is bridge1  # 同一共享桥实例
        # 仅首次惰性创建时注入装载区，其后为无参取用（memoization 契约）
        assert injected == [str(expected_dir), None, None]


# ── 工作区边界锚定 ────────────────────────────────────────────────────


class TestAnchorToWorkspace:
    def test_no_anchor_rejected(self, srv: Any) -> None:
        assert srv._anchor_to_workspace("a.txt", None, None) is None

    def test_relative_resolved_under_workspace(self, srv: Any, tmp_path: Path) -> None:
        anchored = srv._anchor_to_workspace("sub/a.txt", str(tmp_path), None)
        assert anchored == str((tmp_path / "sub" / "a.txt").resolve())

    def test_project_root_takes_precedence_over_workspace(
        self, srv: Any, tmp_path: Path
    ) -> None:
        root, decoy = tmp_path / "root", tmp_path / "decoy"
        anchored = srv._anchor_to_workspace("a.txt", str(decoy), str(root))
        assert anchored == str((root / "a.txt").resolve())

    def test_absolute_inside_root_kept(self, srv: Any, tmp_path: Path) -> None:
        inside = tmp_path / "x" / "y.txt"
        assert srv._anchor_to_workspace(str(inside), str(tmp_path), None) == str(inside.resolve())

    @pytest.mark.parametrize(
        "path",
        ["../outside.txt", "sub/../../outside.txt", "C:/Windows/win.ini", "/etc/passwd"],
    )
    def test_escape_rejected(self, srv: Any, tmp_path: Path, path: str) -> None:
        assert srv._anchor_to_workspace(path, str(tmp_path), None) is None

    def test_deny_envelope_shape(self, srv: Any) -> None:
        envelope = srv._bridge_deny("a.txt")
        assert envelope["success"] is False
        assert envelope["data"] is None
        assert "a.txt" in envelope["error"]
        assert envelope["duration_ms"] == 0.0


# ── dsh_read / dsh_glob：锚定 → 桥调用参数契约 ─────────────────────────


class _FakeBridge:
    """Node runtime 桥替身（外部依赖）：记录锚定后的调用参数。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        return {
            "success": True,
            "data": {"tool": name, "args": args},
            "error": None,
            "duration_ms": 1.0,
        }


@pytest.fixture
def fake_bridge(srv: Any, monkeypatch: pytest.MonkeyPatch) -> _FakeBridge:
    bridge = _FakeBridge()
    monkeypatch.setattr(srv, "get_bridge", lambda: bridge)
    return bridge


class TestDshRead:
    def test_deny_without_workspace_injection(self, srv: Any, fake_bridge: _FakeBridge) -> None:
        out = asyncio.run(srv.dsh_read(file_path="a.txt"))
        assert out["success"] is False
        assert "超出工作空间边界" in out["error"]
        assert "a.txt" in out["error"]
        assert fake_bridge.calls == []

    def test_deny_on_escape(self, srv: Any, fake_bridge: _FakeBridge, tmp_path: Path) -> None:
        out = asyncio.run(srv.dsh_read(file_path="../x.txt", workspace=str(tmp_path)))
        assert out["success"] is False and fake_bridge.calls == []

    def test_relative_anchored_and_optional_args_forwarded(
        self, srv: Any, fake_bridge: _FakeBridge, tmp_path: Path
    ) -> None:
        out = asyncio.run(
            srv.dsh_read(file_path="a.txt", offset=3, limit=50, workspace=str(tmp_path))
        )
        assert out["success"] is True
        name, args = fake_bridge.calls[0]
        assert name == "read"
        assert Path(args["file_path"]) == (tmp_path / "a.txt").resolve()
        assert args["offset"] == 3
        assert args["limit"] == 50

    def test_optional_args_omitted_when_none(
        self, srv: Any, fake_bridge: _FakeBridge, tmp_path: Path
    ) -> None:
        asyncio.run(srv.dsh_read(file_path="a.txt", workspace=str(tmp_path)))
        args = fake_bridge.calls[0][1]
        assert "offset" not in args and "limit" not in args

    def test_absolute_inside_workspace_forwarded_as_is(
        self, srv: Any, fake_bridge: _FakeBridge, tmp_path: Path
    ) -> None:
        inside = tmp_path / "d" / "f.txt"
        asyncio.run(srv.dsh_read(file_path=str(inside), workspace=str(tmp_path)))
        assert Path(fake_bridge.calls[0][1]["file_path"]) == inside.resolve()


class TestDshGlob:
    def test_deny_without_workspace_mentions_pattern(
        self, srv: Any, fake_bridge: _FakeBridge
    ) -> None:
        out = asyncio.run(srv.dsh_glob(pattern="**/*.ts"))
        assert out["success"] is False
        assert "**/*.ts" in out["error"]
        assert fake_bridge.calls == []

    def test_deny_with_escaping_path_mentions_path(
        self, srv: Any, fake_bridge: _FakeBridge, tmp_path: Path
    ) -> None:
        out = asyncio.run(srv.dsh_glob(pattern="**/*.ts", path="../sub", workspace=str(tmp_path)))
        assert out["success"] is False
        assert "../sub" in out["error"]

    def test_anchored_search_dir_forwarded(
        self, srv: Any, fake_bridge: _FakeBridge, tmp_path: Path
    ) -> None:
        out = asyncio.run(
            srv.dsh_glob(pattern="**/*.ts", path="src", workspace=str(tmp_path))
        )
        assert out["success"] is True
        name, args = fake_bridge.calls[0]
        assert name == "glob"
        assert args["pattern"] == "**/*.ts"
        assert Path(args["path"]) == (tmp_path / "src").resolve()

    def test_missing_path_defaults_to_workspace_root(
        self, srv: Any, fake_bridge: _FakeBridge, tmp_path: Path
    ) -> None:
        asyncio.run(srv.dsh_glob(pattern="*.md", workspace=str(tmp_path)))
        assert Path(fake_bridge.calls[0][1]["path"]) == tmp_path.resolve()


# ── 翻译出口工具面 ────────────────────────────────────────────────────


class TestTranslateManifestTools:
    def test_default_loads_installed_plugins(self, srv: Any) -> None:
        out = asyncio.run(srv.dsh_translate_manifest())
        assert out["count"] >= 1
        assert out["base_dir"].endswith("dsh_plugins")
        assert out["packages"], "仓库自带真实 DSH 插件包应可翻译"

    def test_package_path_translates_single_package(
        self, srv: Any, tmp_path: Path
    ) -> None:
        pkg = tmp_path / "mini"
        pkg.mkdir()
        (pkg / "package.json").write_text(
            json.dumps({"name": "@x/mini", "version": "9.9.9"}), encoding="utf-8"
        )
        out = asyncio.run(srv.dsh_translate_manifest(package_path=str(pkg)))
        assert out["source"]["package"] == "@x/mini"
        assert out["source"]["version"] == "9.9.9"

    def test_list_plugins_reports_inventory(self, srv: Any) -> None:
        out = asyncio.run(srv.dsh_list_plugins())
        assert {"count", "base_dir", "extra_tools_dir", "plugins", "disabled", "errors"} <= set(out)
        assert out["count"] == len(out["plugins"])
        for entry in out["plugins"]:
            assert {
                "package",
                "version",
                "is_client_plugin",
                "renderers",
                "adapter_scope",
                "extra_tools",
            } <= set(entry)
        by_pkg = {e["package"]: e for e in out["plugins"]}
        assert "@deepseek-ai/dsh-tool-time" in by_pkg
        assert by_pkg["@deepseek-ai/dsh-tool-time"]["extra_tools"] is True


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
        assert any("themes auto-sync failed" in r.message for r in caplog.records)

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

# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""translator.py 分支覆盖补充测试（清单/皮肤解析的失败隔离与静态判定边界）。

打桩约定：
- 文件系统 IO 故障用 Path.read_text 定点注入 OSError（真实代码路径不变）；
- 其余一律真实文件 + 纯函数直呼（皮肤/颜色数学为纯静态逻辑）。
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

import translator as tr  # noqa: E402
from translator import (  # noqa: E402
    _brand_text_seed,
    _build_shadcn_bridge,
    _extract_bubble_tokens,
    _face_solid_rgb,
    _hex_to_hsl,
    _hex_to_rgb,
    _luminance,
    _parse_css_color,
    _read_css_file,
    _resolve_bubble_variables,
    _resolve_conversation_bg,
    _resolve_sidebar_fill,
    _wcag_luminance,
    _wcag_ratio,
    classify_dsh_plugin,
    describe_available_skins,
    discover_dsh_plugins,
    dsh_params_to_json_schema,
    list_available_skins,
    resolve_skin_background,
    skin_base_of,
    to_lingxi_tool_entry,
    translate_hooks_config,
    translate_package,
)


def _fail_read_text_for(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """文件系统故障注入：仅对 target 的 read_text 抛 OSError，其余委托真实实现。"""
    original = Path.read_text

    def failing(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            raise OSError(f"injected read failure: {self.name}")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing)


def _write_pkg(
    tmp_path: Path, name: str, files: dict[str, str], package_json: dict[str, Any] | None = None
) -> Path:
    pkg = tmp_path / name
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(
        json.dumps(package_json or {"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    for rel, content in files.items():
        p = pkg / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return pkg


# ── classify_dsh_plugin：dsh.plugin.json 声明面 + 扫描失败隔离 ─────────


class TestClassifyPluginDecls:
    def test_entry_inject_declares_service(self, tmp_path: Path) -> None:
        pkg = _write_pkg(tmp_path, "svc", {
            "src/index.ts": "export class S extends Service { static inject = ['a', 'b'] }\n",
            "dsh.plugin.json": json.dumps({"entry": {"inject": ["bus", "a"]}}),
        })
        kinds = classify_dsh_plugin(pkg)
        # 源码 static inject 与清单 entry.inject 合并去重
        assert kinds["service"]["inject"] == ["a", "b", "bus"]
        assert kinds["service"]["names"] == []

    def test_entry_not_dict_no_service_kind(self, tmp_path: Path) -> None:
        pkg = _write_pkg(tmp_path, "svc", {
            "dsh.plugin.json": json.dumps({"entry": "not-a-dict"}),
        })
        assert "service" not in classify_dsh_plugin(pkg)

    def test_broken_dsh_plugin_json_ignored(self, tmp_path: Path) -> None:
        pkg = _write_pkg(tmp_path, "svc", {"dsh.plugin.json": "{broken"})
        assert classify_dsh_plugin(pkg) == {}

    def test_unreadable_source_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pkg = _write_pkg(tmp_path, "svc", {"src/index.ts": "ctx.on('agent/error', () => {})\n"})
        _fail_read_text_for(monkeypatch, pkg / "src" / "index.ts")
        assert classify_dsh_plugin(pkg) == {}

    def test_output_only_io_role(self, tmp_path: Path) -> None:
        pkg = _write_pkg(tmp_path, "post", {
            "src/index.ts": "ctx.on('tools/result', (r) => r)\n",
        })
        assert classify_dsh_plugin(pkg)["io"]["roles"] == ["output"]

    def test_hook_event_without_command_not_hook(self, tmp_path: Path) -> None:
        pkg = _write_pkg(tmp_path, "listener", {
            "src/index.ts": "ctx.on('agent/error', (e) => console.log(e))\n",
        })
        # 只订阅事件、无 spawn/exec 且无 hooks 词汇 → 不构成钩子形态
        assert "hook" not in classify_dsh_plugin(pkg)


# ── translate_hooks_config：输入形态边界 ──────────────────────────────


class TestTranslateHooksConfigEdges:
    def test_none_returns_empty(self) -> None:
        assert translate_hooks_config(None) == {"triggers": [], "mapped": 0, "unmapped": []}

    def test_invalid_yaml_maps_nothing(self) -> None:
        out = translate_hooks_config("%%%")
        assert out == {"triggers": [], "mapped": 0, "unmapped": []}

    def test_mapping_without_hooks_key_maps_nothing(self) -> None:
        assert translate_hooks_config("a: b")["mapped"] == 0

    def test_non_object_specs_isolated(self) -> None:
        out = translate_hooks_config(["junk", 42, {"on": "turn/start", "run": "x"}])
        assert out["mapped"] == 1
        assert [u["reason"] for u in out["unmapped"]] == ["not-an-object", "not-an-object"]
        assert [u["spec"] for u in out["unmapped"]] == ["junk", 42]

    @pytest.mark.parametrize("when", ["wat", 3.14])
    def test_unknown_turn_end_reason_unmapped(self, when: Any) -> None:
        out = translate_hooks_config([{"on": "turn/end", "when": when, "run": "r"}])
        assert out["mapped"] == 0
        assert out["unmapped"][0]["reason"] == "unknown turn/end reason"
        assert out["unmapped"][0]["when"] == when

    def test_falsy_timeout_falls_back_to_default(self) -> None:
        out = translate_hooks_config(
            [{"on": "turn/end", "when": "completed", "run": "r", "timeoutMs": None}]
        )
        assert out["triggers"][0]["action_params"]["timeout_ms"] == 10000


# ── 工具契约出口：DSL/分类边界 ────────────────────────────────────────


class TestToolContractEdges:
    def test_non_dict_param_spec_skipped(self) -> None:
        schema = dsh_params_to_json_schema({"a": "junk", "b": {"type": "number", "required": True}})
        assert list(schema["properties"]) == ["b"]
        assert schema["required"] == ["b"]

    def test_spec_defaults_to_string_without_description(self) -> None:
        schema = dsh_params_to_json_schema({"x": {}})
        assert schema["properties"]["x"] == {"type": "string"}
        assert "required" not in schema

    def test_category_and_defaults(self) -> None:
        entry = to_lingxi_tool_entry({"name": "t", "category": "tools"})
        assert entry["description"] == ""
        assert entry["input_schema"] == {"type": "object", "properties": {}}
        assert entry["category"] == "tools"
        for absent in ("output_schema", "render"):
            assert absent not in entry


# ── translate_package：源文件读失败隔离 ───────────────────────────────


class TestTranslatePackageUnreadableSource:
    def test_unreadable_client_source_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = _write_pkg(tmp_path, "ui", {
            "src/client/row.tsx": (
                "ctx.slots.register({ name: 'tool.call.toolview', key: 'read' }, Row)\n"
            ),
        }, package_json={"name": "ui", "dsh": {"client": {"platform": "web"}}})
        _fail_read_text_for(monkeypatch, pkg / "src" / "client" / "row.tsx")
        m = translate_package(pkg)
        assert len(m["warnings"]) == 1
        assert m["warnings"][0].startswith("unreadable source row.tsx")
        assert m["client"]["renderers"] == []
        assert m["client"]["is_client_plugin"] is True  # 清单面不受源文件读失败影响


# ── 插件发现与项目根定位 ──────────────────────────────────────────────


class TestDiscoveryAndProjectRoot:
    def test_missing_base_dir_yields_empty(self, tmp_path: Path) -> None:
        assert discover_dsh_plugins(tmp_path / "nope") == []

    def test_discovery_filters_underscore_and_non_package(self, tmp_path: Path) -> None:
        base = tmp_path / "plugins"
        for name in ("_staging", "real-pkg", "no-manifest"):
            (base / name).mkdir(parents=True)
        (base / "_staging" / "package.json").write_text("{}", encoding="utf-8")
        (base / "real-pkg" / "package.json").write_text("{}", encoding="utf-8")
        assert discover_dsh_plugins(base) == [base / "real-pkg"]

    def test_list_skins_missing_base_dir_yields_empty(self, tmp_path: Path) -> None:
        assert list_available_skins(tmp_path / "nope") == []

    def test_project_root_prefers_env_only_when_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_root = tmp_path / "proj"
        (cfg_root / "config").mkdir(parents=True)
        monkeypatch.chdir(cfg_root)
        # env 指向不存在的目录 → 不采纳，继续锚定/cwd 链
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path / "ghost"))
        monkeypatch.setattr(tr, "__file__", str(tmp_path / "a" / "b" / "c" / "d" / "translator.py"))
        assert tr._project_root() == str(cfg_root)

    def test_project_root_anchored_location_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        anchor = tmp_path / "anchor"
        (anchor / "config").mkdir(parents=True)
        monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
        monkeypatch.setattr(
            tr, "__file__", str(anchor / "p1" / "p2" / "p3" / "p4" / "translator.py")
        )
        assert tr._project_root() == str(anchor)

    def test_project_root_cwd_chain_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_root = tmp_path / "proj"
        (cfg_root / "config").mkdir(parents=True)
        workdir = cfg_root / "sub" / "deep"
        workdir.mkdir(parents=True)
        monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
        monkeypatch.setattr(
            tr, "__file__", str(tmp_path / "a" / "b" / "c" / "d" / "translator.py")
        )
        monkeypatch.chdir(workdir)
        # 现状行为（疑似缺陷，见任务报告）：cwd 上溯循环未回写 cur，
        # 只有 cwd 自身含 config/ 才命中；祖先目录的 config/ 不可达。
        assert tr._project_root() == str(workdir)

    def test_project_root_falls_back_to_cwd_when_nothing_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workdir = tmp_path / "w1" / "w2" / "w3" / "w4" / "w5" / "w6"
        workdir.mkdir(parents=True)
        monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
        monkeypatch.setattr(
            tr, "__file__", str(tmp_path / "a" / "b" / "c" / "d" / "translator.py")
        )
        monkeypatch.chdir(workdir)
        assert tr._project_root() == str(workdir)


# ── 皮肤清单 describe：元数据提取与残缺兜底 ───────────────────────────


def _make_skin(tmp_path: Path, skin_id: str, files: dict[str, str]) -> Path:
    root = tmp_path / "skins"
    skin = root / skin_id
    skin.mkdir(parents=True)
    for name, content in files.items():
        (skin / name).write_text(content, encoding="utf-8")
    return root


MIN_SKIN_CSS = ":root{--dsw-alias-bg-base:#111318;color:#e6e6e6;}\n"


class TestDescribeAvailableSkins:
    def test_manifest_meta_extracted(self, tmp_path: Path) -> None:
        _make_skin(tmp_path, "tok", {
            "skin.css": MIN_SKIN_CSS,
            "skin.json": json.dumps({
                "name": "Tok", "tagline": "t", "accent": "#ff0000",
                "tags": ["Dark", 1, None, {"x": 1}],
            }),
        })
        entries = describe_available_skins(tmp_path / "skins")
        assert len(entries) == 1
        e = entries[0]
        assert e["name"] == "Tok" and e["tagline"] == "t" and e["accent"] == "#ff0000"
        # 非 str/int 标签过滤，str/int 归一小写
        assert e["tags"] == ["dark", "1"]
        assert e["colors"]["canvas"] == "#111318"
        assert e["base"] == "dark"

    @pytest.mark.parametrize("manifest", ["{broken", "READ_FAIL"])
    def test_broken_manifest_falls_back_to_skin_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: str
    ) -> None:
        root = _make_skin(tmp_path, "fb", {"skin.css": MIN_SKIN_CSS})
        if manifest == "READ_FAIL":
            _fail_read_text_for(monkeypatch, root / "fb" / "skin.json")
        else:
            (root / "fb" / "skin.json").write_text(manifest, encoding="utf-8")
        entries = describe_available_skins(root)
        assert len(entries) == 1
        e = entries[0]
        assert e["name"] == "fb" and e["tagline"] == "" and e["accent"] == "" and e["tags"] == []

    def test_unreadable_skin_css_keeps_dark_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _make_skin(tmp_path, "dim", {"skin.css": MIN_SKIN_CSS})
        _fail_read_text_for(monkeypatch, root / "dim" / "skin.css")
        e = describe_available_skins(root)[0]
        assert e["base"] == "dark"  # css 读不到 → 无画布色，按暗处理
        assert e["colors"] == {}


class TestResolveSkinBackground:
    @pytest.mark.parametrize("skin_id", ["", "a/b", "a\\b", "..", "x..y"])
    def test_unsafe_skin_id_rejected(self, tmp_path: Path, skin_id: str) -> None:
        assert resolve_skin_background(skin_id, tmp_path) is None

    def test_missing_manifest_none(self, tmp_path: Path) -> None:
        assert resolve_skin_background("ghost", tmp_path) is None

    def test_broken_manifest_none(self, tmp_path: Path) -> None:
        skin = tmp_path / "s"
        skin.mkdir()
        (skin / "skin.json").write_text("{broken", encoding="utf-8")
        assert resolve_skin_background("s", tmp_path) is None

    def test_background_media_not_dict_none(self, tmp_path: Path) -> None:
        skin = tmp_path / "s"
        skin.mkdir()
        (skin / "skin.json").write_text(json.dumps({"contributes": {}}), encoding="utf-8")
        assert resolve_skin_background("s", tmp_path) is None

    @pytest.mark.parametrize(
        "bm",
        [
            {},
            {"dark": {"src": 42}},  # src 非字符串
            {"dark": "a.webp"},  # 态声明非 dict
        ],
    )
    def test_no_valid_mode_none(self, tmp_path: Path, bm: dict) -> None:
        skin = tmp_path / "s"
        skin.mkdir()
        (skin / "skin.json").write_text(
            json.dumps({"contributes": {"backgroundMedia": bm}}), encoding="utf-8"
        )
        assert resolve_skin_background("s", tmp_path) is None

    def test_dark_only_with_default_scrim(self, tmp_path: Path) -> None:
        skin = tmp_path / "s"
        skin.mkdir()
        (skin / "skin.json").write_text(
            json.dumps({"contributes": {"backgroundMedia": {"dark": {"src": "a.webp"}}}}),
            encoding="utf-8",
        )
        out = resolve_skin_background("s", tmp_path)
        assert out == {"skin": "s", "dark": {"src": "a.webp", "scrim": ""}}

    def test_light_only_keeps_declared_scrim(self, tmp_path: Path) -> None:
        skin = tmp_path / "s"
        skin.mkdir()
        (skin / "skin.json").write_text(
            json.dumps({"contributes": {"backgroundMedia": {
                "light": {"src": "b.png", "scrim": "rgba(0,0,0,0.5)"},
            }}}),
            encoding="utf-8",
        )
        out = resolve_skin_background("s", tmp_path)
        assert out is not None
        assert "dark" not in out
        assert out["light"] == {"src": "b.png", "scrim": "rgba(0,0,0,0.5)"}


# ── 颜色数学原语（纯函数，输入可枚举 → parametrize） ──────────────────


class TestColorPrimitives:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("#abc", (170, 187, 204)),
            ("#aabbcc", (170, 187, 204)),  # 3 位与 6 位等价
            ("#ABC", (170, 187, 204)),
            ("nope", None),
            ("#abcd", None),  # 非法长度
            ("", None),
        ],
    )
    def test_hex_to_rgb(self, value: str, expected: tuple[int, int, int] | None) -> None:
        assert _hex_to_rgb(value) == expected

    def test_hex_to_hsl_non_hex_passthrough(self) -> None:
        assert _hex_to_hsl("var(--x)") == "var(--x)"

    @pytest.mark.parametrize(
        "value,expected",
        [("#000000", "0 0% 0%"), ("#ffffff", "0 0% 100%"), ("#ff0000", "0 100% 50%")],
    )
    def test_hex_to_hsl_canonical_values(self, value: str, expected: str) -> None:
        assert _hex_to_hsl(value) == expected

    def test_luminance_non_hex_is_zero(self) -> None:
        assert _luminance("nope") == 0.0
        assert skin_base_of("nope") == "dark"  # 无法解析按暗处理

    @pytest.mark.parametrize(
        "canvas,expected",
        [("#abc", "light"), ("#eef5ff", "light"), ("#111318", "dark"), ("#000000", "dark")],
    )
    def test_skin_base_by_canvas_luminance(self, canvas: str, expected: str) -> None:
        assert skin_base_of(canvas) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("transparent", (0, 0, 0, 0.0)),
            (" #fff ", (255, 255, 255, 1.0)),
            ("#11223344", (17, 34, 51, 68 / 255)),
            ("#abcd", None),
            ("color-mix(in srgb, red 50%)", None),
            ("var(--x)", None),
            ("rgb(1, 2, 3)", (1, 2, 3, 1.0)),
            ("rgba(1, 2, 3, 0.5)", (1, 2, 3, 0.5)),
        ],
    )
    def test_parse_css_color(
        self, value: str, expected: tuple[int, int, int, float] | None
    ) -> None:
        assert _parse_css_color(value) == expected

    def test_wcag_luminance_extremes(self) -> None:
        assert _wcag_luminance((0, 0, 0)) == 0.0
        assert _wcag_luminance((255, 255, 255)) == pytest.approx(1.0)

    def test_wcag_ratio_symmetric(self) -> None:
        black_white = _wcag_ratio((0, 0, 0), (255, 255, 255))
        assert black_white == pytest.approx(21.0)
        assert _wcag_ratio((255, 255, 255), (0, 0, 0)) == black_white  # 对称


# ── 气泡/输入面令牌提取（patches.css，按基准态分侧） ──────────────────

PATCHES_CSS = (
    'body[data-ds-dark-theme] [class*="userRow"] [class*="bubble"]{background:#222222;color:#fff;}'
    '[class*="userRow"] [class*="bubble"]{background:#3366cc;color:#fff;}'
    'body[data-ds-dark-theme] [data-chat-flow-kind="assistant-step"] > * > * > * >div[class*="markdown"]{background:#1a1a2e;}'
    '[data-chat-flow-kind="assistant-step"] > * > * > * >div[class*="markdown"]{background:#f5f5f0;}'
    'body[data-ds-dark-theme] [data-composer-card]{background:linear-gradient(0deg, rgba(255,255,255,0.2), var(--dsw-specific-input-major));}'
    '[data-composer-card]{background:linear-gradient(0deg, rgba(255,255,255,0.2), var(--dsw-specific-input-major));}'
)


class TestExtractBubbleTokens:
    def test_dark_and_light_branches_pick_their_own_rules(self) -> None:
        dark = _extract_bubble_tokens(PATCHES_CSS, dark=True)
        light = _extract_bubble_tokens(PATCHES_CSS, dark=False)
        # 各取基准态分支；渐变含 input-major → 静态已知输入面基底
        assert dark == {
            "--bubble-user-bg": "#222222",
            "--bubble-ai-bg": "#1a1a2e",
            "--chat-input-bg": "#fffdf8d1",
        }
        assert light == {
            "--bubble-user-bg": "#3366cc",
            "--bubble-ai-bg": "#f5f5f0",
            "--chat-input-bg": "#fffdf8d1",
        }

    def test_dark_only_css_gives_light_side_nothing(self) -> None:
        css = 'body[data-ds-dark-theme] [class*="userRow"] [class*="bubble"]{background:#222222;}'
        assert _extract_bubble_tokens(css, dark=False) == {}
        assert _extract_bubble_tokens(css, dark=True)["--bubble-user-bg"] == "#222222"

    def test_rule_without_background_decl_no_token(self) -> None:
        css = '[class*="userRow"] [class*="bubble"]{color:#fff;}'
        assert _extract_bubble_tokens(css, dark=False) == {}

    @pytest.mark.parametrize("decl", ["background:none;", "background: ;"])
    def test_empty_or_none_background_no_token(self, decl: str) -> None:
        css = f'[class*="userRow"] [class*="bubble"]{{{decl}}}'
        assert "--bubble-user-bg" not in _extract_bubble_tokens(css, dark=False)


# ── 气泡面实色判定与品牌文字种子 ──────────────────────────────────────


class TestFaceSolidAndBrandSeed:
    @pytest.mark.parametrize(
        "face,canvas,expected",
        [
            ("#3366cc", None, (51, 102, 204)),  # 不透明直取
            ("rgba(51, 102, 204, 1)", None, (51, 102, 204)),
            ("linear-gradient(x)", None, None),  # 无法静态解析
            ("var(--undef)", None, None),
            ("#00000080", None, None),  # 半透明且无画布 → 放弃
        ],
    )
    def test_face_solid_rgb(
        self,
        face: str,
        canvas: tuple[int, int, int] | None,
        expected: tuple[int, int, int] | None,
    ) -> None:
        assert _face_solid_rgb(face, canvas) == expected

    def test_semi_transparent_composited_over_canvas(self) -> None:
        solid = _face_solid_rgb("rgba(0, 0, 0, 0.5)", (255, 255, 255))
        assert solid is not None
        assert len(set(solid)) == 1  # 黑半透明叠白画布 → 中性灰
        assert 120 <= solid[0] <= 136

    def test_brand_text_seed_valid_color(self) -> None:
        css = ":root{--dsw-alias-brand-text:#123456;}"
        assert _brand_text_seed(css, False, (0, 0, 0)) == (18, 52, 86)

    def test_brand_text_seed_dark_branch(self) -> None:
        css = 'body[data-ds-dark-theme] .x{--dsw-alias-brand-text:#abcdef;}'
        assert _brand_text_seed(css, True, (0, 0, 0)) == (171, 205, 239)

    @pytest.mark.parametrize(
        "face,expected",
        [((10, 10, 10), (255, 255, 255)), ((240, 240, 240), (0, 0, 0))],
    )
    def test_brand_text_seed_fallback_by_face_luminance(
        self, face: tuple[int, int, int], expected: tuple[int, int, int]
    ) -> None:
        assert _brand_text_seed("", False, face) == expected

    def test_brand_text_seed_unresolvable_alias_falls_back(self) -> None:
        css = ":root{--dsw-alias-brand-text:var(--undefined);}"
        assert _brand_text_seed(css, False, (10, 10, 10)) == (255, 255, 255)


# ── 气泡令牌定稿（底色+配对文字成对发射） ─────────────────────────────


class TestResolveBubbleVariables:
    def test_extracted_face_emits_pair_with_contrast(self) -> None:
        out = _resolve_bubble_variables(
            {"--bubble-user-bg": "#3366cc"}, css="", dark=False,
            canvas_str="", canvas_rgb=None,
        )
        assert out["--bubble-user-bg"] == "#3366cc"
        text = _parse_css_color(out["--bubble-user-text"])
        assert text is not None
        assert _wcag_ratio(text[:3], (51, 102, 204)) >= 4.5  # 配对文字强制可读

    def test_link_keeps_accent_when_contrast_sufficient(self) -> None:
        out = _resolve_bubble_variables(
            {"--bubble-user-bg": "#ffffff"}, css="", dark=False,
            canvas_str="", canvas_rgb=None, accent_rgb=(0, 0, 0),
        )
        assert out["--bubble-link"] == "#000000"  # 黑对白面 21:1 ≥ 3.0

    def test_link_falls_back_black_or_white(self) -> None:
        out = _resolve_bubble_variables(
            {"--bubble-user-bg": "#3366cc"}, css="", dark=False,
            canvas_str="", canvas_rgb=None, accent_rgb=(60, 60, 60),
        )
        assert out["--bubble-link"] in ("#000000", "#ffffff")

    def test_unparseable_face_emits_nothing(self) -> None:
        out = _resolve_bubble_variables(
            {"--bubble-user-bg": "linear-gradient(y)"}, css="", dark=False,
            canvas_str="", canvas_rgb=None,
        )
        assert "--bubble-user-bg" not in out
        assert "--bubble-user-text" not in out

    def test_user_face_from_brand_alias(self) -> None:
        css = ":root{--dsw-alias-brand-primary:#3366cc;--dsw-alias-brand-text:#ffffff;}"
        out = _resolve_bubble_variables({}, css=css, dark=False, canvas_str="", canvas_rgb=None)
        assert out["--bubble-user-bg"] == "#3366cc"
        assert out["--bubble-user-text"] == "#ffffff"

    def test_ai_pair_from_canvas_when_no_rule(self) -> None:
        out = _resolve_bubble_variables(
            {}, css="", dark=False, canvas_str="#111318", canvas_rgb=(17, 19, 24),
        )
        assert out["--bubble-ai-bg"] == "color-mix(in srgb, #111318 80%, transparent)"
        text = _parse_css_color(out["--bubble-ai-text"])
        assert text is not None
        assert _wcag_ratio(text[:3], (17, 19, 24)) >= 4.5


# ── 区域背景解析（conversation 表面 / 侧栏 fill） ─────────────────────


class TestConversationBackground:
    def test_no_rule_is_transparent(self) -> None:
        assert _resolve_conversation_bg(":root{color:#fff;}") == "transparent"

    @pytest.mark.parametrize(
        "decl,expected",
        [
            ("background-color:#112233;", "#112233"),
            ("background-color:rgba(10,20,30,0.5);", "rgba(10, 20, 30, 0.5)"),
            ("background-color:rgba(10,20,30,0);", "transparent"),
            ("background-color:var(--x);", "transparent"),
            ("color:#fff;", "transparent"),  # 无背景声明
        ],
    )
    def test_surface_rule_forms(self, decl: str, expected: str) -> None:
        css = f'[data-dsh-surface="conversation"] .inner{{{decl}}}'
        assert _resolve_conversation_bg(css) == expected


class TestSidebarFill:
    def test_no_fill_none(self) -> None:
        assert _resolve_sidebar_fill(":root{color:#fff;}") is None

    @pytest.mark.parametrize(
        "decl,expected",
        [
            ("--dsw-specific-sidebar-fill:#112233;", "#112233"),
            ("--dsw-specific-sidebar-fill:rgba(10,20,30,0.4);", "rgba(10, 20, 30, 0.4)"),
            ("--dsw-specific-sidebar-fill:rgba(0,0,0,0);", "transparent"),
            ("--dsw-specific-sidebar-fill:var(--x);", None),
        ],
    )
    def test_plain_value_forms(self, decl: str, expected: str | None) -> None:
        assert _resolve_sidebar_fill(f":root{{{decl}}}") == expected

    @pytest.mark.parametrize(
        "decl,expected",
        [
            # calc 静态求值（scrim 取缺省位）：1 - 0.35*0.8 = 0.72
            (
                "--dsw-specific-sidebar-fill:rgba(10, 20, 30, calc(1 - var(--dsh-skin-scrim, 0.35) * 0.8));",
                "rgba(10, 20, 30, 0.72)",
            ),
            # 全透明 → transparent
            (
                "--dsw-specific-sidebar-fill:rgba(10, 20, 30, calc(0.5 - var(--dsh-skin-scrim, 1) * 0.5));",
                "transparent",
            ),
            # calc 形态不匹配 → 放弃
            ("--dsw-specific-sidebar-fill:calc(var(--a) * 2);", None),
            # 含 scrim var 但非「常数 - var * 常数」形 → 放弃
            ("--dsw-specific-sidebar-fill:calc(var(--dsh-skin-scrim, 0.3) - 0.2);", None),
            # calc 匹配但无颜色 → 放弃
            ("--dsw-specific-sidebar-fill:calc(1 - var(--dsh-skin-scrim, 0.2) * 0.5);", None),
        ],
    )
    def test_calc_forms(self, decl: str, expected: str | None) -> None:
        assert _resolve_sidebar_fill(f":root{{{decl}}}") == expected


# ── CSS 读取兜底与 shadcn 桥分支 ──────────────────────────────────────


class TestReadCssFileAndShadcnBridge:
    def test_missing_file_empty(self, tmp_path: Path) -> None:
        assert _read_css_file(tmp_path / "nope.css") == ""

    def test_unreadable_file_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "skin.css"
        target.write_text(":root{color:#fff;}", encoding="utf-8")
        _fail_read_text_for(monkeypatch, target)
        assert _read_css_file(target) == ""

    def test_content_roundtrip(self, tmp_path: Path) -> None:
        target = tmp_path / "skin.css"
        target.write_text(":root{--a:1;}", encoding="utf-8")
        assert _read_css_file(target) == ":root{--a:1;}"

    def test_unparseable_canvas_noop(self) -> None:
        variables: dict[str, str] = {}
        _build_shadcn_bridge(variables, canvas="var(--x)", text="#ffffff", accent="", sidebar_bg=None, main_bg="transparent")
        assert variables == {}

    def test_unparseable_text_noop(self) -> None:
        variables: dict[str, str] = {}
        _build_shadcn_bridge(variables, canvas="#111318", text="var(--y)", accent="", sidebar_bg=None, main_bg="transparent")
        assert variables == {}

    def test_region_chat_fg_emitted_when_main_differs(self) -> None:
        variables: dict[str, str] = {}
        _build_shadcn_bridge(
            variables, canvas="#111318", text="#e6e6e6", accent="",
            sidebar_bg=None, main_bg="#223344",
        )
        assert "--region-chat-fg" in variables
        assert variables["--region-chat-fg"] == variables["--region-workspace-fg"]
        assert variables["--region-chat-muted-fg"] == variables["--region-workspace-muted-fg"]
        main_solid = _parse_css_color("#223344")
        fg = _parse_css_color(variables["--region-chat-fg"])
        assert main_solid is not None and fg is not None
        assert _wcag_ratio(fg[:3], main_solid[:3]) >= 4.5  # 区域前景强制可读
        assert "hsl" not in variables["--background"]  # shadcn 桥为 H S% L% 纯串

    def test_transparent_main_keeps_global_foreground(self) -> None:
        variables: dict[str, str] = {}
        _build_shadcn_bridge(
            variables, canvas="#111318", text="#e6e6e6", accent="",
            sidebar_bg=None, main_bg="transparent",
        )
        assert "--region-chat-fg" not in variables

    def test_accent_emits_primary_family(self) -> None:
        variables: dict[str, str] = {}
        _build_shadcn_bridge(
            variables, canvas="#111318", text="#e6e6e6", accent="#ff0000",
            sidebar_bg=None, main_bg="transparent",
        )
        for key in ("--primary", "--primary-foreground", "--accent", "--accent-foreground", "--ring"):
            assert key in variables

    def test_sidebar_face_gets_region_foreground(self) -> None:
        variables: dict[str, str] = {}
        _build_shadcn_bridge(
            variables, canvas="#111318", text="#e6e6e6", accent="",
            sidebar_bg="#ffffff", main_bg="transparent",
        )
        assert "--region-sidebar-fg" in variables
        assert "--region-sidebar-muted-fg" in variables

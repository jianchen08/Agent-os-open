# @feature: FP-0.2.CFG 主题对比度门禁 | @ci: python-test
"""check_theme_contrast.py 单测（2026-11-07 用户裁定：渲染面文字/背景对比度静态核算）。

行为契约（断输入→输出，不钉实现）：
- 颜色数学锚：WCAG 已知值（黑白 21.0、白压 #00a2c7 ≈3.00）与 TS 真实现经
  presets-extract-snapshot.json 双侧锁定（本文件 + frontend 侧 vitest 快照测试）；
- alpha 合成专项：同一半透明前景压深底 vs 浅底合成结果不同、对比度分别
  达标/不达标（用户裁定的「透过背后背景」语义）；
- 面向一：合规主题绿、低对比主题红（含渐变全色标最差判）、皮肤按
  mergeTheme 同式合并后核算；
- 面向二：纯字面量页/主题 token 页逐主题实例化；页面透明背景透出宿主
  主背景；fail-closed（未声明 token / 未知表达式 = 红）；inherit/
  currentColor/transparent 跳过；
- 基线棘轮：新增/恶化红；--baseline-monotonic 待收紧红。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location("check_theme_contrast", SCRIPTS / "check_theme_contrast.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_theme_contrast"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load()


def _write_theme(root: Path, theme_id: str, colors: dict, extra: str = "") -> None:
    """在 tmp 根写一个最小预设主题 TS 文件（数据字面量形态）。"""
    presets = root / "frontend" / "src" / "config" / "themes" / "presets"
    presets.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"id": theme_id, "name": theme_id, "colors": colors}, ensure_ascii=False, indent=2)
    (presets / f"{theme_id}.ts").write_text(f"import type {{ ThemeConfig }} from '@/types/theme'\n\nexport const t: ThemeConfig = {body}\n{extra}", encoding="utf-8", newline="\n")


def _write_asset(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return rel


# ── 颜色数学锚（Python ≡ themeValuePolicy 同式；跨实现漂移任一侧测试即红）──
def test_wcag_known_values(mod) -> None:
    parse_color, ratio = mod.parse_color, mod.contrast_ratio
    assert round(ratio(parse_color("#000000"), parse_color("#ffffff")), 2) == 21.0
    # 白压 #00a2c7（deep-space primary，与资产实例化实测 3.00 同源）
    assert round(ratio(parse_color("#ffffff"), parse_color("#00a2c7")), 2) == 3.0
    assert parse_color("#f00") == (255.0, 0.0, 0.0, 1.0)  # 3 位 hex 展开
    assert parse_color("rgba(255, 255, 255, 0.5)") == (255.0, 255.0, 255.0, 0.5)
    assert parse_color("rgb(50%, 0%, 0%)")[0] == pytest.approx(127.5)
    assert parse_color("hsl(210 40% 98%)") is None  # 不支持的形态 → None（fail-closed 载体）


def test_alpha_compositing_directional(mod) -> None:
    """半透明白压深底 vs 浅底：合成色不同、对比度方向相反（专项锁）。"""
    parse, comp, ratio = mod.parse_color, mod.composite, mod.contrast_ratio
    deep, light = parse("#04060F"), parse("#F4F7FB")
    translucent_white = (255.0, 255.0, 255.0, 0.2)
    on_deep, on_light = comp(translucent_white, deep), comp(translucent_white, light)
    assert on_deep != on_light
    fg = parse("#F1F5F9")  # 深色主题正文
    assert ratio(fg, on_deep) >= 4.5  # 深底上可读（合成后仍是深底）
    assert ratio(fg, on_light) < 2.0  # 浅底上撞色（不可读）


def test_ts_extraction_anchor_snapshot(mod) -> None:
    """活仓预设提取 ≡ 提交快照（TS 真实现由 frontend 侧 vitest 锚同一份快照）。"""
    themes = mod.load_preset_themes()
    snapshot = json.loads(mod.SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert sorted(themes) == sorted(snapshot)
    assert json.loads(json.dumps(themes, ensure_ascii=False, sort_keys=True)) == snapshot


def test_live_repo_gate_green(mod, capsys) -> None:
    """活仓端到端：现行主题 + 资产全量核算绿（存量债全量在基线内）。"""
    assert mod.main(["--baseline-monotonic"]) == 0
    assert "绿" in capsys.readouterr().out


# ── 面向一：主题 token 对 ───────────────────────────────────────────────────
@pytest.fixture()
def theme_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = _load()
    (tmp_path / "frontend" / "src" / "themes").mkdir(parents=True)
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "PRESETS_DIR", tmp_path / "frontend" / "src" / "config" / "themes" / "presets")
    monkeypatch.setattr(mod, "SKINS_DIR", tmp_path / "frontend" / "src" / "themes")
    _write_theme(tmp_path, "ok", COMPLIANT_COLORS)  # main() 需要 ≥1 个可解析预设
    return mod, tmp_path


COMPLIANT_COLORS = {
    "primary": "#0ea5e9",
    "accent": "#3b82f6",
    "background": {"main": "#0f172a", "card": "#1e293b", "input": "#1e293b", "elevated": "#1e293b", "sidebar": "#1e293b"},
    "text": {"primary": "#f8fafc", "secondary": "#e2e8f0", "muted": "#94a3b8", "disabled": "#64748b"},
    "status": {"success": "#34d399", "warning": "#fbbf24", "error": "#f87171"},
    "bubble": {"user_bg": "#22d3ee", "user_text": "#04060f", "ai_bg": "#111c38", "ai_text": "#f1f5f9"},
}


def test_face1_compliant_theme_green(theme_env) -> None:
    mod, root = theme_env
    _write_theme(root, "ok", COMPLIANT_COLORS)
    themes = mod.load_all_themes()
    assert mod.evaluate_theme_pairs("ok", themes["ok"]) == []


def test_face1_low_contrast_text_red(theme_env) -> None:
    mod, root = theme_env
    colors = json.loads(json.dumps(COMPLIANT_COLORS))
    colors["text"]["primary"] = "#334155"  # 压深底 ≈1.6
    _write_theme(root, "bad", colors)
    themes = mod.load_all_themes()
    findings = mod.evaluate_theme_pairs("bad", themes["bad"])
    keys = {f.key for f in findings}
    assert "theme:bad|text.primary→background.main|theme:bad" in keys
    assert all(f.ratio < 4.5 for f in findings)


def test_face1_gradient_bg_worst_stop(theme_env) -> None:
    mod, root = theme_env
    colors = json.loads(json.dumps(COMPLIANT_COLORS))
    # 渐变含一个撞色浅色标：最差色标必须把 text.primary 判红（全色标达标语义）
    colors["background"]["main"] = "linear-gradient(135deg, #0f172a 0%, #f8fafc 100%)"
    _write_theme(root, "grad", colors)
    themes = mod.load_all_themes()
    findings = mod.evaluate_theme_pairs("grad", themes["grad"])
    main_hit = [f for f in findings if f.key.endswith("text.primary→background.main|theme:grad")]
    assert main_hit and main_hit[0].ratio < 1.5  # 白字压白色标


def test_face1_accent_icon_threshold(theme_env) -> None:
    mod, root = theme_env
    colors = json.loads(json.dumps(COMPLIANT_COLORS))
    colors["accent"] = "#1d4ed8"  # 压深底 ≈2.7：图标 3.0 档红
    _write_theme(root, "acc", colors)
    themes = mod.load_all_themes()
    findings = mod.evaluate_theme_pairs("acc", themes["acc"])
    keys = {f.key for f in findings}
    assert "theme:acc|accent→background.main|theme:acc" in keys
    assert all(f.ratio < 3.0 for f in findings)


def test_skin_merge_follows_base(theme_env) -> None:
    """皮肤按 mergeTheme 同式合并：覆盖键胜出、未覆盖键继承 base。"""
    mod, root = theme_env
    _write_theme(root, "base", COMPLIANT_COLORS)
    skin = {
        "id": "sk",
        "basedOn": "base",
        "customizations": {"colors": {"primary": "#2dd4bf", "status": {"running": "#2dd4bf"}}},
    }
    (root / "frontend" / "src" / "themes" / "sk.json").write_text(json.dumps(skin), encoding="utf-8", newline="\n")
    merged = mod.load_all_themes()["sk"]
    assert merged["colors"]["primary"] == "#2dd4bf"  # 覆盖键
    assert merged["colors"]["text"] == COMPLIANT_COLORS["text"]  # 继承键
    assert merged["colors"]["status"]["success"] == "#34d399"  # 子节深合并不丢
    assert merged["colors"]["status"]["running"] == "#2dd4bf"  # 子节新增


def test_theme_parse_failure_fail_closed(theme_env) -> None:
    mod, root = theme_env
    presets = root / "frontend" / "src" / "config" / "themes" / "presets"
    presets.mkdir(parents=True, exist_ok=True)
    (presets / "broken.ts").write_text("export const t: ThemeConfig = { id: broken };\n", encoding="utf-8")
    with pytest.raises(mod.ThemeParseError):
        mod.load_all_themes()


# ── 面向二：插件 web/webview 资产实例化 ─────────────────────────────────────
LITERAL_PAGE = """
<style>
  body { color: #111827; background: #f9fafb; }
  .muted { color: #4b5563; }
  .bad { color: #d1d5db; }
</style>
"""

TOKEN_PAGE = """
<style>
  :root { --ag-bg: #ffffff; --ag-fg: #111111; --muted: #6b7280; }
  html, body { background: transparent; }
  body { color: var(--ag-fg); background: var(--ag-bg); }
  .soft { background: rgba(255,255,255,0.08); color: var(--ag-fg); }
</style>
"""


@pytest.fixture()
def asset_env(theme_env):
    mod, root = theme_env
    themes = {
        "ok": {"id": "ok", "colors": json.loads(json.dumps(COMPLIANT_COLORS))},
    }
    return mod, root, themes


def test_face2_literal_page_pairs(asset_env) -> None:
    mod, root, themes = asset_env
    rel = _write_asset(root, "plugins/shared/demo/webview/p.html", LITERAL_PAGE)
    findings = mod.evaluate_asset(rel, LITERAL_PAGE, themes)
    keys = {f.key for f in findings}
    # .bad #d1d5db 压页面底（body #f9fafb）≈1.4 → 红（身份记声明面「页面透明」，
    # 实际下层背景由页面栈合成）；body/.muted 可读 → 不出现
    assert any("|.bad|#d1d5db→页面透明|" in k for k in keys)
    assert not any("|.muted|" in k for k in keys)
    assert not any("|body|" in k for k in keys)
    # body 底不透明 → 主题无关 → 折叠 theme=*
    bad_keys = [k for k in keys if ".bad" in k]
    assert bad_keys and all(k.endswith("|theme:*") for k in bad_keys)


def test_face2_token_page_per_theme(asset_env) -> None:
    mod, root, _ = asset_env
    rel = _write_asset(root, "plugins/shared/demo/webview/t.html", TOKEN_PAGE)
    dark = {"id": "darky", "colors": json.loads(json.dumps(COMPLIANT_COLORS))}
    dark["colors"]["background"]["main"] = "#000000"
    dark["colors"]["text"]["primary"] = "#ffffff"
    assert mod.evaluate_asset(rel, TOKEN_PAGE, {"darky": dark}) == []
    # 主题 fg 改为与 bg 同色：body 配对（token 引用）必须红（逐主题实例化语义；
    # 双主题入参下另一主题绿 → 身份带 theme:darky 逐主题行）
    broken = json.loads(json.dumps(dark))
    broken["colors"]["text"]["primary"] = "#000000"
    findings = mod.evaluate_asset(rel, TOKEN_PAGE, {"darky": broken, "good": dark})
    body_hits = [f for f in findings if "|body|var(--ag-fg)→var(--ag-bg)|theme:darky" in f.key]
    assert body_hits and body_hits[0].ratio < 1.1


def test_face2_page_root_fallback_when_theme_missing_key(asset_env) -> None:
    """主题缺 --ag-* 键时回退页面 :root 兜底字面量（任务书语义）。"""
    mod, root, _ = asset_env
    rel = _write_asset(root, "plugins/shared/demo/webview/t.html", TOKEN_PAGE)
    minimal = {"id": "m", "colors": {"text": {"primary": "#101010"}}}  # 无 background.main
    findings = mod.evaluate_asset(rel, TOKEN_PAGE, {"m": minimal})
    keys = {f.key for f in findings}
    # --ag-bg 主题缺 → 页面 :root #ffffff 兜底 → #101010 压 #ffffff ≈ 15 可读 → 绿
    assert not any("|body|" in k for k in keys)


def test_face2_semitransparent_bg_composites_over_host(asset_env) -> None:
    mod, root, _ = asset_env
    page = "<style>body { color: var(--ag-fg); background: rgba(255,255,255,0.2); }</style>"
    rel = _write_asset(root, "plugins/shared/demo/webview/a.html", page)
    dark_host = {"id": "x", "colors": json.loads(json.dumps(COMPLIANT_COLORS))}
    dark_host["colors"]["text"]["primary"] = "#ffffff"
    dark_host["colors"]["background"]["main"] = "#04060f"  # 半透明白压深底 → 合成后 fg 可读
    light_host = {"id": "y", "colors": json.loads(json.dumps(COMPLIANT_COLORS))}  # --ag-bg 浅色 → 半透明白压浅底 → 白 fg 撞色
    light_host["colors"]["text"]["primary"] = "#ffffff"
    light_host["colors"]["background"]["main"] = "#f4f7fb"
    assert mod.evaluate_asset(rel, page, {"x": dark_host}) == []
    findings = mod.evaluate_asset(rel, page, {"x": light_host})
    assert len(findings) == 1 and findings[0].ratio < 2.0  # 实测同底异题分叉


def test_face2_fail_closed_unresolved(asset_env) -> None:
    mod, root, themes = asset_env
    page = "<style>.x { color: var(--never-defined); } .y { color: matrix(1,2,3); }</style>"
    rel = _write_asset(root, "plugins/shared/demo/webview/f.html", page)
    findings = mod.evaluate_asset(rel, page, themes)
    assert len(findings) == 2
    assert all(f.ratio == 0.0 for f in findings)  # fail-closed 红（进基线才可豁免）
    keys = {f.key for f in findings}
    assert any("|.x|" in k for k in keys) and any("|.y|" in k for k in keys)


def test_face2_inherit_and_transparent_skipped(asset_env) -> None:
    mod, root, themes = asset_env
    page = "<style>.a { color: inherit; } .b { color: currentColor; } .c { color: transparent; } .d { color: #111827; background: none; }</style>"
    rel = _write_asset(root, "plugins/shared/demo/webview/i.html", page)
    findings = mod.evaluate_asset(rel, page, themes)
    # inherit/currentColor/transparent 跳过；.d 深字透传页面透明→宿主深底 → 撞色红
    assert not any(("|.a|" in f.key or "|.b|" in f.key or "|.c|" in f.key) for f in findings)
    assert any("|.d|" in f.key for f in findings)


def test_face2_large_text_threshold(asset_env) -> None:
    mod, root, themes = asset_env
    page = (
        "<style>"
        ".hero { color: #64748b; background: #0f172a; font-size: 26px; }"
        ".tiny { color: #64748b; background: #0f172a; }"
        "</style>"
    )
    rel = _write_asset(root, "plugins/shared/demo/webview/l.html", page)
    keys = {f.key for f in mod.evaluate_asset(rel, page, themes)}
    assert not any("|.hero|" in k for k in keys)  # 26px 大字 3.0 档过（≈3.8）
    assert any("|.tiny|" in k for k in keys)  # 同色 12px 正文 4.5 档红


# ── 基线棘轮 ───────────────────────────────────────────────────────────────
def _run_main(mod, root: Path, baseline_text: str, *extra: str) -> int:
    baseline = root / "baseline.txt"
    baseline.write_text(baseline_text, encoding="utf-8", newline="\n")
    return mod.main(["--baseline", str(baseline), *extra])


def test_baseline_ratchet_new_pair_red(asset_env, capsys) -> None:
    mod, root, themes = asset_env
    rel = _write_asset(root, "plugins/shared/demo/webview/p.html", LITERAL_PAGE)
    bad = [f for f in mod.evaluate_asset(rel, LITERAL_PAGE, themes) if ".bad" in f.key]
    key = bad[0].key
    recorded = f"{key}={bad[0].ratio + 0.01:.2f}\n"  # 记录比实测好 → 恶化红
    assert _run_main(mod, root, recorded, "--baseline-monotonic") == 1
    assert "恶化" in capsys.readouterr().out


def test_baseline_ratchet_exact_green(asset_env) -> None:
    mod, root, themes = asset_env
    rel = _write_asset(root, "plugins/shared/demo/webview/p.html", LITERAL_PAGE)
    rows = "".join(f"{f.key}={f.ratio:.2f}\n" for f in mod.evaluate_asset(rel, LITERAL_PAGE, themes))
    assert _run_main(mod, root, rows, "--baseline-monotonic") == 0


def test_baseline_ratchet_stale_entry_red(asset_env, capsys) -> None:
    mod, root, themes = asset_env
    _write_asset(root, "plugins/shared/demo/webview/p.html", LITERAL_PAGE)
    assert _run_main(mod, root, "plugins/shared/x|ghost|#000→#000|theme:*=1.00\n", "--baseline-monotonic") == 1
    assert "待收紧" in capsys.readouterr().out


def test_baseline_malformed_row_fails_loud(asset_env) -> None:
    mod, root, _ = asset_env
    with pytest.raises(SystemExit):
        _run_main(mod, root, "not-a-ratio-row\n")

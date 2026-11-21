#!/usr/bin/env python3
"""theme-contrast 门禁：应用渲染面文字/背景对比度静态核算（2026-11-07 用户裁定）。

规则：应用中渲染的所有文字/图标，与其实际渲染背景必须有 WCAG 对比度差别。
静态计算执法（不跑浏览器运行时）：解析颜色表达式，按 alpha over 算子逐层
合成（半透明前景/背景与其下层——含「页面透明背景透出宿主主背景」），对
合成结果算 WCAG 2.1 对比度。阈值：正文 4.5:1；大字（font-size ≥24px，或
≥18.66px 且 font-weight ≥700）3.0:1；辅助可见 token（text.muted/disabled）
与 accent/primary（图标/accent 面）3.0:1（与 presets.contrast.test.ts 的
AA/VISIBLE 阈值同源）。

两个核算面：

- 面向一（主题 token 对）：每个主题配置（frontend/src/config/themes/presets/
  *.ts 预设 + frontend/src/themes/*.json 动态皮肤，皮肤按 themeStorage
  mergeTheme colors 侧同式合并）的 text.* × background.* 面、status.*/accent/
  primary 对背景、bubble 文字对气泡底的对比度；渐变背景取全部色标（最差判），
  半透明表面压 main 首色标合成（与 presets.contrast.test.ts 同模型）。
- 面向二（插件 web/webview 资产实例化）：静态解析资产 HTML/CSS 的规则级
  color/background 声明，逐主题实例化对比：
  * 纯字面量（不接主题的页面）按字面量直接算；
  * var(--ag-*) 按 buildWebviewThemeTokens 同口径 14 键展开（主题声明值
    优先，缺省回退页面 :root 兜底字面量）；
  * 背景链 = 宿主主背景 --ag-bg（终端层，逐主题）→ 页面级（html/body）
    背景按声明序 → 规则自身 background，自底向上 over 合成；任一层为渐变
    则候选链按色标展开（全候选最差判，候选 >64 条 fail-closed）；
  * 未知/解析不了的表达式 fail-closed 算红（倒逼声明或进基线），禁止
    fail-open 假绿。

基线棘轮（.github/theme-contrast-baseline.txt，「key=实测对比度」每条一行，
# 开头注释；key 内含 | 分隔的 主题/资产|选择器|前景→背景 身份）：
现失败对不在基线 = 红（新增）；实测 < 基线记录 - 0.005 = 红（恶化）；
--baseline-monotonic 下基线条目已不再失败 = 红（已清偿须删行，只减不增）。

解析边界（v1，与 fail-closed 互补的已声明语义）：
- color: inherit / currentColor → 无自有前景，跳过（父规则的同底配对已覆盖）；
- color: transparent → 未绘制文字，跳过；
- @media 任意条件内的 :root 重定义 → 不进页面 token 源（theme.sync 下行即被
  整键覆盖；prefers-color-scheme 变体属首帧兜底闪帧）；
- @media 内非 :root 规则照常核算（条件命中即真实渲染）；
- @keyframes/@font-face 块内容跳过（动画帧/字体描述无规则级文字配对）；
- 终端宿主背景按画布根处理（alpha 钳为 1）；
- SVG fill/stroke 图标对比度、JS 运行时设色不在本闸（遗留候选）。

双实现一致性锚：Python 的亮度/对比度/合成数学与 frontend themeValuePolicy
（relativeLuminance/wcagRatio）由两侧测试共同锁定——
tests/gates/test_check_theme_contrast.py（Python 侧）与
frontend/src/config/themes/__tests__/preset-extract-snapshot.test.ts
（TS 侧）比对同一份 presets-extract-snapshot.json；预设主题提取结果与
presetThemes 真实现逐字段一致，任一侧漂移即红。

用法：
    python scripts/check_theme_contrast.py                       # 门禁形态
    python scripts/check_theme_contrast.py --print-report        # 打印全量失败对明细
    python scripts/check_theme_contrast.py --print-extract       # 打印主题提取 JSON（快照再生）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO / ".github" / "theme-contrast-baseline.txt"
PRESETS_DIR = REPO / "frontend" / "src" / "config" / "themes" / "presets"
SKINS_DIR = REPO / "frontend" / "src" / "themes"
SNAPSHOT_PATH = (
    REPO / "frontend" / "src" / "config" / "themes" / "__tests__" / "presets-extract-snapshot.json"
)

# 扫描面：插件 web/webview 资产（与 check_theme_color_hygiene 2026-11-07 扩面同球）。
ASSET_GLOBS = (
    "plugins/shared/*/web/**/*.html",
    "plugins/shared/*/web/**/*.htm",
    "plugins/shared/*/web/**/*.css",
    "plugins/shared/*/web/**/*.js",
    "plugins/shared/*/webview/**/*.html",
    "plugins/shared/*/webview/**/*.htm",
    "plugins/shared/*/webview/**/*.css",
    "plugins/shared/*/webview/**/*.js",
)

AA_TEXT = 4.5
VISIBLE = 3.0
LARGE_TEXT_PX = 24.0
LARGE_BOLD_PX = 18.66
LARGE_BOLD_WEIGHT = 700.0
MAX_VARIANT_CHAINS = 64
MAX_VAR_DEPTH = 8
WORSEN_EPSILON = 0.005

# buildWebviewThemeTokens 同口径：--ag-* 键 → ThemeConfig 取值路径（缺省键不产值，
# 调用方走页面 :root 兜底）；soft 键由源色派生 rgba(a=0.14)，与 TS softToken 同式。
AG_TOKEN_PATHS: dict[str, tuple[str, ...]] = {
    "--ag-bg": ("colors", "background", "main"),
    "--ag-fg": ("colors", "text", "primary"),
    "--ag-muted": ("colors", "text", "secondary"),
    "--ag-border": ("colors", "border", "default"),
    "--ag-card": ("colors", "background", "card"),
    "--ag-accent": ("colors", "accent"),
    "--ag-primary": ("colors", "primary"),
    "--ag-chip": ("components", "badge", "variants", "default", "bg"),
    "--ag-ok": ("colors", "status", "success"),
    "--ag-warn": ("colors", "status", "warning"),
    "--ag-err": ("colors", "status", "error"),
}
AG_SOFT_SOURCES: dict[str, tuple[str, ...]] = {
    "--ag-accent-soft": ("colors", "accent"),
    "--ag-primary-soft": ("colors", "primary"),
}


# ── 颜色数学（与 frontend themeValuePolicy 亮度/对比度同式，锚测试锁定）────
RGBA = tuple[float, float, float, float]  # r,g,b ∈ 0..255, a ∈ 0..1


def parse_color(value: str) -> RGBA | None:
    """解析颜色字面量为 RGBA（hex 3/4/6/8 位 + rgb/rgba 逗号形态）；
    不认识返回 None（fail-closed 语义由调用方落）。"""
    text = value.strip()
    hex_match = re.fullmatch(r"#([0-9a-fA-F]{3,8})", text)
    if hex_match:
        digits = hex_match.group(1)
        if len(digits) in (3, 4):
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) not in (6, 8):
            return None
        r, g, b = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
        a = int(digits[6:8], 16) / 255.0 if len(digits) == 8 else 1.0
        return (float(r), float(g), float(b), a)
    func_match = re.fullmatch(r"rgba?\(\s*([^)]+)\)", text)
    if func_match:
        parts = [p.strip() for p in func_match.group(1).split(",")]
        if len(parts) not in (3, 4) or not all(re.fullmatch(r"[\d.]+%?", p) for p in parts):
            return None
        channels = [float(p[:-1]) * 2.55 if p.endswith("%") else float(p) for p in parts[:3]]
        alpha = 1.0
        if len(parts) == 4:
            alpha = float(parts[3][:-1]) / 100.0 if parts[3].endswith("%") else float(parts[3])
        return (channels[0], channels[1], channels[2], alpha)
    return None


def composite(top: RGBA, bottom: RGBA) -> RGBA:
    """标准 alpha over 合成（top 压在 bottom 上）。"""
    ta = min(1.0, max(0.0, top[3]))
    ba = min(1.0, max(0.0, bottom[3]))
    out_a = ta + ba * (1.0 - ta)
    if out_a <= 0.0:
        return (0.0, 0.0, 0.0, 0.0)
    r = (top[0] * ta + bottom[0] * ba * (1.0 - ta)) / out_a
    g = (top[1] * ta + bottom[1] * ba * (1.0 - ta)) / out_a
    b = (top[2] * ta + bottom[2] * ba * (1.0 - ta)) / out_a
    return (r, g, b, out_a)


def relative_luminance(color: RGBA) -> float:
    """WCAG 2.1 相对亮度（themeValuePolicy.relativeLuminance 同式）。"""
    values = []
    for v in color[:3]:
        n = v / 255.0
        values.append(n / 12.92 if n <= 0.03928 else ((n + 0.055) / 1.055) ** 2.4)
    return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]


def contrast_ratio(a: RGBA, b: RGBA) -> float:
    """两实色 WCAG 对比度（themeValuePolicy.wcagRatio 同式）。"""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


# ── 主题源：TS 预设提取（数据字面量解析，异常即 fail-closed 红）────────────
class ThemeParseError(Exception):
    """预设/皮肤解析失败（fail-closed：门禁红，倒逼修数据形态）。"""


def _strip_js_comments(text: str) -> str:
    """引号感知剥离 // 与 /* */ 注释（字符串字面量保留原样）。"""
    out: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _match_brace(text: str, open_idx: int) -> int:
    """返回与 text[open_idx]=='{' 配对的 '}' 下标（引号感知；不配对抛错）。"""
    depth = 0
    quote: str | None = None
    i, n = open_idx, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ThemeParseError("对象字面量括号不配对")


def _parse_literal(text: str, pos: int = 0) -> tuple[object, int]:
    """递归下降解析 JSON5-ish 字面量（引号串/裸键/数字/true/false/null/对象/数组/尾逗号）。"""
    i, n = pos, len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    if i >= n:
        raise ThemeParseError("字面量意外截断")
    ch = text[i]
    if ch in "{[":
        closer = "}" if ch == "{" else "]"
        container: dict[str, object] | list[object] = {} if ch == "{" else []
        i += 1
        while True:
            while i < n and text[i] in " \t\r\n,":
                i += 1
            if i < n and text[i] == closer:
                return container, i + 1
            if ch == "{":
                key, i = _read_key(text, i)
                while i < n and text[i] in " \t\r\n":
                    i += 1
                if i >= n or text[i] != ":":
                    raise ThemeParseError(f"对象键缺冒号: {key!r}")
                value, i = _parse_literal(text, i + 1)
                container[key] = value
            else:
                value, i = _parse_literal(text, i)
                container.append(value)
    if ch in ("'", '"'):
        return _read_string(text, i)
    match = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text[i:])
    if match:
        raw = match.group(0)
        value: object = float(raw) if any(c in raw for c in ".eE") else int(raw)
        return value, i + len(raw)
    for literal, value in (("true", True), ("false", False), ("null", None)):
        if text.startswith(literal, i):
            return value, i + len(literal)
    raise ThemeParseError(f"无法解析的字面量片段: {text[i : i + 40]!r}")


def _read_key(text: str, pos: int) -> tuple[str, int]:
    i, n = pos, len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    if i < n and text[i] in ("'", '"'):
        return _read_string(text, i)
    match = re.match(r"[A-Za-z_$][\w$]*", text[i:])
    if not match:
        raise ThemeParseError(f"非法对象键: {text[i : i + 20]!r}")
    return match.group(0), i + len(match.group(0))


def _read_string(text: str, pos: int) -> tuple[str, int]:
    quote = text[pos]
    i, n = pos + 1, len(text)
    out: list[str] = []
    while i < n:
        ch = text[i]
        if ch == "\\":
            if i + 1 >= n:
                raise ThemeParseError("字符串转义截断")
            nxt = text[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
            continue
        if ch == quote:
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise ThemeParseError("字符串未闭合")


def extract_preset_object(path: Path) -> dict[str, object]:
    """从 `export const X: ThemeConfig = {...}` 提取完整对象字面量。"""
    text = _strip_js_comments(path.read_text(encoding="utf-8"))
    match = re.search(r"export\s+const\s+\w+\s*:\s*ThemeConfig\s*=\s*", text)
    if not match:
        raise ThemeParseError(f"{path.name}: 未找到 export const X: ThemeConfig = 声明")
    brace = text.index("{", match.end())
    end = _match_brace(text, brace)
    value, _ = _parse_literal(text[brace : end + 1])
    if not isinstance(value, dict):
        raise ThemeParseError(f"{path.name}: 顶层字面量不是对象")
    return value


def load_preset_themes() -> dict[str, dict[str, object]]:
    """全部预设主题：{theme_id: 完整配置对象}（解析失败抛 ThemeParseError → 门禁红）。"""
    themes: dict[str, dict[str, object]] = {}
    for path in sorted(PRESETS_DIR.glob("*.ts")):
        obj = extract_preset_object(path)
        theme_id = obj.get("id")
        if not isinstance(theme_id, str) or not theme_id:
            raise ThemeParseError(f"{path.name}: 缺 id 字段")
        themes[theme_id] = obj
    if not themes:
        raise ThemeParseError("预设主题目录为空（提取面漂移，fail-closed）")
    return themes


def merge_skin(base: dict[str, object], custom: dict[str, object]) -> dict[str, object]:
    """动态皮肤合并（themeStorage.mergeTheme colors 侧同式：色标量浅覆盖、
    background/text/border/status/bubble 五个子节深合并）。"""
    base_colors = dict(base.get("colors") or {})
    custom_colors = dict(custom.get("colors") or {})
    merged_colors: dict[str, object] = {**base_colors, **custom_colors}
    for section in ("background", "text", "border", "status", "bubble"):
        merged_colors[section] = {
            **dict(base_colors.get(section) or {}),
            **dict(custom_colors.get(section) or {}),
        }
    return {**base, "id": custom.get("id") or base.get("id"), "colors": merged_colors}


def load_all_themes() -> dict[str, dict[str, object]]:
    """预设 + 动态皮肤（皮肤 = basedOn 预设按 mergeTheme 同式合并）。"""
    themes = load_preset_themes()
    for path in sorted(SKINS_DIR.glob("*.json")):
        skin = json.loads(path.read_text(encoding="utf-8"))
        base_id = skin.get("basedOn")
        if base_id not in themes:
            raise ThemeParseError(f"{path.name}: basedOn={base_id!r} 无对应预设主题")
        merged = merge_skin(themes[base_id], dict(skin.get("customizations") or {}))
        merged["id"] = skin.get("id") or f"{base_id}:{path.stem}"
        themes[merged["id"]] = merged
    return themes


def theme_value(theme: dict[str, object], path: tuple[str, ...]) -> object:
    node: object = theme
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def build_ag_tokens(theme: dict[str, object]) -> dict[str, str]:
    """buildWebviewThemeTokens 同口径：14 键中的色值键（缺省键不产值 → 调用方
    走页面 :root 兜底）；soft 键由源色派生 rgba(a=0.14)（softToken 同式）。"""
    tokens: dict[str, str] = {}
    for token, path in AG_TOKEN_PATHS.items():
        value = theme_value(theme, path)
        if isinstance(value, str) and value:
            tokens[token] = value
    for token, path in AG_SOFT_SOURCES.items():
        value = theme_value(theme, path)
        if isinstance(value, str) and value:
            rgb = parse_color(value)
            if rgb is not None:
                tokens[token] = f"rgba({int(rgb[0])}, {int(rgb[1])}, {int(rgb[2])}, 0.14)"
    return tokens


# ── 资产解析：CSS 规则级 color/background 提取 ─────────────────────────────
@dataclass(frozen=True)
class Rule:
    selector: str
    decls: dict[str, str] = field(default_factory=dict)
    line: int = 0
    media: str = ""  # 所在 @media 条件原文；顶层规则为空串


def _split_top_level(text: str, sep: str) -> list[str]:
    """按 sep 拆分（括号/引号感知）。"""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _parse_decls(block: str) -> dict[str, str]:
    """声明块 → {属性: 值}（同名后值胜；嵌套块碎片不落声明）。"""
    decls: dict[str, str] = {}
    for raw in _split_top_level(block, ";"):
        if ":" not in raw:
            continue
        name, _, value = raw.partition(":")
        name = name.strip().lower()
        if not name or " " in name or "{" in name:
            continue
        decls[name] = value.strip()
    return decls


def _extract_css_rules(css: str, base_line: int, media: str = "") -> list[Rule]:
    """CSS 文本 → 规则列表。@media/@supports 递归展开；@keyframes/@font-face 跳过。"""
    css = _strip_js_comments(css)
    rules: list[Rule] = []
    prelude_start = 0
    depth = 0
    quote: str | None = None
    block_line = 1
    i, n = 0, len(css)
    while i < n:
        ch = css[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "{":
            if depth == 0:
                prelude = css[prelude_start:i].strip()
                block_line = base_line + css.count("\n", 0, prelude_start)
                low = prelude.lower()
                if low.startswith("@keyframes") or low.startswith("@font-face"):
                    i = _match_brace(css, i) + 1
                    prelude_start = i
                    continue
                depth = 1
            else:
                depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                block = css[prelude_start : i + 1]
                inner_open = block.find("{")
                inner_close = block.rfind("}")
                body = block[inner_open + 1 : inner_close]
                low_prelude = prelude.lower()
                if low_prelude.startswith("@media") or low_prelude.startswith("@supports"):
                    rules.extend(_extract_css_rules(body, block_line, media=prelude))
                elif prelude:
                    decls = _parse_decls(body)
                    if decls:
                        selector = re.sub(r"\s+", " ", prelude).strip()
                        rules.append(Rule(selector=selector, decls=decls, line=block_line, media=media))
                i += 1
                prelude_start = i
                continue
        i += 1
    return rules


STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
INLINE_STYLE_RE = re.compile(r"""style=(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)


def extract_asset_rules(text: str) -> list[Rule]:
    """HTML 资产 → <style> 块规则 + style="" 内联规则；.css 资产直接全文解析。"""
    rules: list[Rule] = []
    consumed: list[tuple[int, int]] = []
    for match in STYLE_BLOCK_RE.finditer(text):
        consumed.append(match.span())
        line = text.count("\n", 0, match.start(1)) + 1
        rules.extend(_extract_css_rules(match.group(1), base_line=line))
    for match in INLINE_STYLE_RE.finditer(text):
        if any(s <= match.start() < e for s, e in consumed):
            continue
        decls = _parse_decls(match.group(2))
        if decls:
            line = text.count("\n", 0, match.start()) + 1
            rules.append(Rule(selector=f"inline@L{line}", decls=decls, line=line))
    return rules


ELEMENT_SELECTOR_PARTS = frozenset({"html", "body", "html body", "*", ":root"})


def _is_page_level(selector: str) -> bool:
    parts = [p.strip().lower() for p in selector.split(",")]
    return bool(parts) and all(p in ELEMENT_SELECTOR_PARTS for p in parts)


# ── 颜色表达式解析 ─────────────────────────────────────────────────────────
GRADIENT_RE = re.compile(r"(?:linear|radial|conic)-gradient\(", re.IGNORECASE)


class Unresolved(Exception):
    """颜色表达式不可静态解析（fail-closed 语义载体）。"""


def _gradient_stop_exprs(text: str) -> list[str]:
    """渐变值 → 色标表达式列表。只收颜色形态片段（#/var(/rgb/rgba 前缀），
    角度/方位/形状关键词（to right, circle at 20% 等）天然排除。"""
    open_idx = text.find("(")
    if open_idx < 0:
        return []
    depth = 0
    close_idx = -1
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx < 0:
        return []
    stops: list[str] = []
    for part in _split_top_level(text[open_idx + 1 : close_idx], ","):
        item = part.strip()
        if not re.match(r"#|var\(|rgba?\(", item, re.IGNORECASE):
            continue
        item = re.sub(r"\s+[\d.]+%$", "", item)  # 色标位置「#fff 45%」→「#fff」
        if item:
            stops.append(item)
    return stops


def resolve_color_layers(
    value: str,
    ag_tokens: dict[str, str],
    page_tokens: dict[str, str],
    depth: int = 0,
) -> list[RGBA]:
    """颜色表达式 → 候选实色列表（渐变取全部色标；var() 链递归）。

    解析序：--ag-* 主题实例值 → 页面 :root 声明 → var() fallback。全部不可达
    即抛 Unresolved（fail-closed）。transparent 返回全零 alpha 色由调用方按
    语义处理（前景跳过 / 背景链透传）。
    """
    if depth > MAX_VAR_DEPTH:
        raise Unresolved(f"var 嵌套过深: {value!r}")
    text = value.strip().rstrip(";").strip()
    if not text:
        raise Unresolved("空颜色表达式")
    low = text.lower()
    if low in ("transparent", "none", "unset", "initial"):
        return [(0.0, 0.0, 0.0, 0.0)]
    if low in ("inherit", "currentcolor"):
        raise Unresolved(f"继承型颜色值: {text!r}")
    if low.startswith("var("):
        if not text.endswith(")"):
            raise Unresolved(f"var() 括号不配对: {text!r}")
        parts = _split_top_level(text[4:-1], ",")
        name = parts[0].strip()
        lookup = ag_tokens.get(name) or page_tokens.get(name)
        if lookup is not None:
            return resolve_color_layers(lookup, ag_tokens, page_tokens, depth + 1)
        if len(parts) > 1:
            fallback = ",".join(parts[1:]).strip()
            return resolve_color_layers(fallback, ag_tokens, page_tokens, depth + 1)
        raise Unresolved(f"未声明 token: {name}")
    if GRADIENT_RE.search(text):
        stops: list[RGBA] = []
        for expr in _gradient_stop_exprs(text):
            stops.extend(resolve_color_layers(expr, ag_tokens, page_tokens, depth + 1))
        if not stops:
            raise Unresolved(f"渐变无可用色标: {text!r}")
        return stops
    color = parse_color(text)
    if color is not None:
        return [color]
    raise Unresolved(f"不可解析表达式: {text!r}")


def _expand_bg_chains(
    bg_value: str | None,
    page_stack: list[str],
    ag_tokens: dict[str, str],
    page_tokens: dict[str, str],
    terminal_stops: list[RGBA],
) -> list[list[RGBA]]:
    """背景链候选展开（每条链 = 自底向上层序）：终端宿主背景 → 页面栈按声明
    序（html 先 body 后）→ 自身 bg。任一层渐变按色标展开候选。"""
    chains: list[list[RGBA]] = [[(t[0], t[1], t[2], 1.0)] for t in terminal_stops]
    for value in page_stack:
        layers = resolve_color_layers(value, ag_tokens, page_tokens)
        chains = [chain + [layer] for chain in chains for layer in layers]
        if len(chains) > MAX_VARIANT_CHAINS:
            raise Unresolved("背景层候选组合爆炸（>64）")
    if bg_value:
        own = resolve_color_layers(bg_value, ag_tokens, page_tokens)
        chains = [chain + [layer] for chain in chains for layer in own]
        if len(chains) > MAX_VARIANT_CHAINS:
            raise Unresolved("背景层候选组合爆炸（>64）")
    return chains


def _chain_rendered(fg: RGBA, chain: list[RGBA]) -> tuple[RGBA, RGBA]:
    """返回 (前景渲染色, 背景有效色)：背景链自底向上合成后压前景。"""
    bg = chain[0]
    for layer in chain[1:]:
        bg = composite(layer, bg)
    fg_final = composite(fg, bg) if fg[3] < 1.0 else fg
    return fg_final, bg


def _threshold_for(rule: Rule) -> float:
    """正文 4.5 / 大字 3.0：按同块 font-size 与 font-weight 判（无声明按正文）。"""
    size = rule.decls.get("font-size", "")
    weight = rule.decls.get("font-weight", "")
    px = re.fullmatch(r"([\d.]+)px", size.strip())
    if weight.strip().lower() == "bold":
        weight_value = 700.0
    elif re.fullmatch(r"[\d.]+", weight.strip()):
        weight_value = float(weight.strip())
    else:
        weight_value = 400.0
    if px:
        size_px = float(px.group(1))
        if size_px >= LARGE_TEXT_PX or (size_px >= LARGE_BOLD_PX and weight_value >= LARGE_BOLD_WEIGHT):
            return VISIBLE
    return AA_TEXT


def _norm_expr(expr: str) -> str:
    return re.sub(r"\s+", "", expr)


# ── 核算结果 ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Finding:
    key: str  # 基线行身份（不含 =ratio 后缀）
    ratio: float  # 最差实测对比度（fail-closed 未解析 = 0.0）
    detail: str  # 人类可读明细（报告/台账用）


def evaluate_asset(
    rel_path: str,
    text: str,
    themes: dict[str, dict[str, object]],
) -> list[Finding]:
    """单资产 × 全主题实例化核算，返回失败对（含 fail-closed 未解析项）。

    两遍法：先收页面 token（顶层 :root）与页面背景栈（顶层 html/body/:root
    的 background 声明，带规则身份）；再逐规则配对——页面级规则以「去掉自身
    的栈」为下层背景（自身 bg 只计一次），内容规则以全栈为下层。同一配对身份
    跨主题失败时按基线粒度折叠：全部主题等值失败 → theme=* 单条；否则逐失败
    主题一条（theme:<id>）。
    """
    rules = extract_asset_rules(text)
    if not rules:
        return []
    page_tokens: dict[str, str] = {}
    for rule in rules:
        if rule.media == "" and rule.selector.strip().lower() == ":root":
            for name, value in rule.decls.items():
                if name.startswith("--"):
                    page_tokens.setdefault(name, value)
    page_stack: list[tuple[int, str]] = []
    for idx, rule in enumerate(rules):
        if rule.media:
            continue  # @media 内页面级重定义不进全局栈（条件面，配对时按自身 bg 计）
        if rule.selector.strip().lower() == ":root" or _is_page_level(rule.selector):
            bg = rule.decls.get("background") or rule.decls.get("background-color")
            if bg:
                page_stack.append((idx, bg))

    # 配对身份（不含 theme 维度）→ {theme_id: 最差实测比}；明细按 (身份, 主题) 分存
    pair_results: dict[str, dict[str, float]] = {}
    pair_details: dict[tuple[str, str], str] = {}

    for theme_id, theme in sorted(themes.items()):
        ag_tokens = build_ag_tokens(theme)
        try:
            terminal_raw = resolve_color_layers(
                ag_tokens.get("--ag-bg") or "var(--ag-bg)", ag_tokens, page_tokens
            )
        except Unresolved as exc:
            identity = f"{rel_path}|<TERMINAL>|UNRESOLVED→--ag-bg"
            pair_results.setdefault(identity, {})[theme_id] = 0.0
            pair_details[(identity, theme_id)] = f"宿主主背景不可解析（theme={theme_id}）：{exc}"
            continue
        for idx, rule in enumerate(rules):
            if rule.selector.strip().lower() == ":root":
                continue  # token 源不做前景配对（@media 内 :root 同此，见 docstring 边界）
            fg_value = rule.decls.get("color")
            bg_value = rule.decls.get("background") or rule.decls.get("background-color")
            if not fg_value:
                continue
            fg_norm = fg_value.strip().lower()
            if fg_norm in ("inherit", "currentcolor", "transparent"):
                continue  # 已声明语义：无自有前景/未绘制文字
            identity = (
                f"{rel_path}|{rule.selector}|{_norm_expr(fg_value)}"
                f"→{_norm_expr(bg_value) if bg_value else '页面透明'}"
            )
            if _is_page_level(rule.selector) and rule.media == "":
                base_stack = [value for j, value in page_stack if j != idx]
            else:
                base_stack = [value for _, value in page_stack]
            try:
                fg_candidates = [
                    c for c in resolve_color_layers(fg_value, ag_tokens, page_tokens) if c[3] > 0.0
                ]
                chains = _expand_bg_chains(bg_value, base_stack, ag_tokens, page_tokens, terminal_raw)
            except Unresolved as exc:
                pair_results.setdefault(identity, {})[theme_id] = 0.0
                pair_details[(identity, theme_id)] = (
                    f"fail-closed（theme={theme_id}）{rule.selector} L{rule.line}: {exc}"
                )
                continue
            if not fg_candidates:
                continue
            worst = min(
                contrast_ratio(*_chain_rendered(fg, chain))
                for fg in fg_candidates
                for chain in chains
            )
            threshold = _threshold_for(rule)
            if worst < threshold:
                pair_results.setdefault(identity, {})[theme_id] = worst
                pair_details[(identity, theme_id)] = (
                    f"{rule.selector} L{rule.line} theme={theme_id}: "
                    f"{_norm_expr(fg_value)} on {_norm_expr(bg_value) if bg_value else '页面透明'}"
                    f" = {worst:.2f} < {threshold}"
                )

    findings: list[Finding] = []
    for identity, by_theme in sorted(pair_results.items()):
        ratios = list(by_theme.values())
        if len(by_theme) == len(themes) and max(ratios) - min(ratios) <= WORSEN_EPSILON:
            # 全主题等值失败（主题无关配对，如自身不透明底）→ 折叠单条 theme=*
            detail = pair_details[(identity, min(by_theme, key=lambda t: by_theme[t]))]
            findings.append(Finding(f"{identity}|theme:*", min(ratios), detail))
        else:
            findings.extend(
                Finding(f"{identity}|theme:{theme_id}", ratio, pair_details[(identity, theme_id)])
                for theme_id, ratio in sorted(by_theme.items())
            )
    return findings


# ── 面向一：主题 token 对核算 ──────────────────────────────────────────────
FACE1_TEXT_PAIRS = (
    ("primary", AA_TEXT),
    ("secondary", AA_TEXT),
    ("muted", VISIBLE),
    ("disabled", VISIBLE),
)
FACE1_SURFACES = ("main", "card", "input", "elevated", "sidebar")


def _color_candidates(value: object, base: RGBA | None) -> list[RGBA]:
    """主题色值 → 实色候选：实色直取；渐变取全部色标；半透明压 base 合成。"""
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    if GRADIENT_RE.search(text):
        candidates: list[RGBA] = []
        for expr in _gradient_stop_exprs(text):
            parsed = parse_color(expr)
            if parsed is not None:
                candidates.append(parsed)
        return candidates
    parsed = parse_color(text)
    if parsed is None:
        return []
    if parsed[3] < 1.0 and base is not None:
        return [composite(parsed, base)]
    return [parsed]


def evaluate_theme_pairs(theme_id: str, theme: dict[str, object]) -> list[Finding]:
    """单主题 token 对核算（渐变全色标最差判；半透明表面压 main 首色标合成）。"""
    colors = theme.get("colors")
    if not isinstance(colors, dict):
        return [
            Finding(
                key=f"theme:{theme_id}|<CONFIG>|UNRESOLVED→colors|theme:{theme_id}",
                ratio=0.0,
                detail="主题缺 colors 节（fail-closed）",
            )
        ]
    bg_section = colors.get("background")
    main_candidates = _color_candidates(bg_section.get("main"), None) if isinstance(bg_section, dict) else []
    main_base = main_candidates[0] if main_candidates else None
    surfaces: list[tuple[str, list[RGBA]]] = []
    if isinstance(bg_section, dict):
        for slot in FACE1_SURFACES:
            candidates = _color_candidates(bg_section.get(slot), main_base)
            if candidates:
                surfaces.append((f"background.{slot}", candidates))
    main_surface = [("background.main", main_candidates)] if main_candidates else []
    card_surface = [s for s in surfaces if s[0] == "background.card"]

    findings: list[Finding] = []
    text_section = colors.get("text")
    if isinstance(text_section, dict):
        for name, threshold in FACE1_TEXT_PAIRS:
            findings.extend(
                _face1_check(theme_id, f"text.{name}", text_section.get(name), surfaces, threshold)
            )
    status_section = colors.get("status")
    if isinstance(status_section, dict):
        for name in sorted(status_section):
            findings.extend(
                _face1_check(theme_id, f"status.{name}", status_section.get(name), main_surface, AA_TEXT)
            )
    findings.extend(
        _face1_check(theme_id, "accent", colors.get("accent"), main_surface + card_surface, VISIBLE)
    )
    findings.extend(
        _face1_check(theme_id, "primary", colors.get("primary"), main_surface + card_surface, VISIBLE)
    )
    bubble = colors.get("bubble")
    if isinstance(bubble, dict):
        for side in ("user", "ai"):
            bubble_bg = _color_candidates(bubble.get(f"{side}_bg"), main_base)
            findings.extend(
                _face1_check(
                    theme_id,
                    f"bubble.{side}_text",
                    bubble.get(f"{side}_text"),
                    [(f"bubble.{side}_bg", bubble_bg)],
                    AA_TEXT,
                )
            )
    return findings


def _face1_check(
    theme_id: str,
    fg_label: str,
    fg_value: object,
    surfaces: list[tuple[str, list[RGBA]]],
    threshold: float,
) -> list[Finding]:
    if not isinstance(fg_value, str) or not fg_value.strip():
        return []  # 主题未声明该 token：无此渲染配对
    fg_stops = _color_candidates(fg_value, None)
    if not fg_stops:
        return [
            Finding(
                key=f"theme:{theme_id}|{fg_label}|UNRESOLVED|theme:{theme_id}",
                ratio=0.0,
                detail=f"{fg_label}={fg_value!r} 不可解析（fail-closed）",
            )
        ]
    out: list[Finding] = []
    for surface_label, bg_candidates in surfaces:
        if not bg_candidates:
            continue
        worst = min(
            contrast_ratio(composite(fg, bg) if fg[3] < 1.0 else fg, bg)
            for fg in fg_stops
            for bg in bg_candidates
        )
        if worst < threshold:
            out.append(
                Finding(
                    key=f"theme:{theme_id}|{fg_label}→{surface_label}|theme:{theme_id}",
                    ratio=worst,
                    detail=f"{fg_label} on {surface_label} = {worst:.2f} < {threshold}",
                )
            )
    return out


# ── 基线棘轮 ───────────────────────────────────────────────────────────────
def load_baseline(path: Path) -> dict[str, float]:
    entries: dict[str, float] = {}
    if not path.is_file():
        return entries
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.rpartition("=")
        if not sep or not key or not re.fullmatch(r"\d+\.\d+", value):
            raise SystemExit(f"基线行非法（应为「key=对比度」）：{raw!r}")
        entries[key] = float(value)
    return entries


def reconcile(
    current: dict[str, float],
    baseline: dict[str, float],
    monotonic: bool,
) -> tuple[list[str], list[str]]:
    """返回 (新增/恶化红行, 待收紧红行)。"""
    reds: list[str] = []
    stale: list[str] = []
    for key, ratio in sorted(current.items()):
        if key not in baseline:
            reds.append(f"RED 新增低对比对（实测 {ratio:.2f}）: {key}")
        elif ratio < baseline[key] - WORSEN_EPSILON:
            reds.append(f"RED 对比度恶化（基线 {baseline[key]:.2f} → 实测 {ratio:.2f}）: {key}")
    if monotonic:
        for key in sorted(baseline):
            if key not in current:
                stale.append(f"RED 基线可收紧（该对已达标或已消失，请删行）: {key}")
    return reds, stale


# ── 入口 ──────────────────────────────────────────────────────────────────
def collect_assets() -> dict[str, str]:
    assets: dict[str, str] = {}
    for pattern in ASSET_GLOBS:
        for path in sorted(REPO.glob(pattern)):
            rel = path.relative_to(REPO).as_posix()
            try:
                assets[rel] = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
    return assets


def compute_findings(themes: dict[str, dict[str, object]]) -> list[Finding]:
    """全量核算（面向一 + 面向二）。"""
    findings: list[Finding] = []
    for theme_id, theme in sorted(themes.items()):
        findings.extend(evaluate_theme_pairs(theme_id, theme))
    for rel, text in collect_assets().items():
        findings.extend(evaluate_asset(rel, text, themes))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="基线文件（默认 .github/theme-contrast-baseline.txt）",
    )
    parser.add_argument(
        "--baseline-monotonic",
        action="store_true",
        help="棘轮收紧强制：基线条目已达标/消失也判红（只减不增）",
    )
    parser.add_argument("--print-report", action="store_true", help="打印全量失败对明细（台账取证用）")
    parser.add_argument("--print-extract", action="store_true", help="打印主题提取 JSON（快照再生）后退出")
    args = parser.parse_args(argv)

    try:
        themes = load_all_themes()
    except (ThemeParseError, json.JSONDecodeError) as exc:
        print(f"theme-contrast：RED 主题源解析失败（fail-closed）：{exc}")
        return 1

    if args.print_extract:
        print(json.dumps(themes, ensure_ascii=False, indent=1, sort_keys=True))
        return 0

    findings = compute_findings(themes)
    failures: dict[str, float] = {}
    detail_by_key: dict[str, Finding] = {}
    for finding in findings:
        failures[finding.key] = min(failures.get(finding.key, float("inf")), finding.ratio)
        detail_by_key[finding.key] = finding

    baseline = load_baseline(args.baseline)
    reds, stale = reconcile(failures, baseline, args.baseline_monotonic)

    for line in reds + stale:
        print(line)
        key = line.rsplit(": ", 1)[-1]
        if key in detail_by_key:
            print(f"    {detail_by_key[key].detail}")
    if args.print_report:
        for key in sorted(failures):
            print(f"LOW {key} = {failures[key]:.2f}")
            print(f"    {detail_by_key[key].detail}")

    if reds or stale:
        print(
            f"\ntheme-contrast：红（{len(reds)} 新增/恶化，{len(stale)} 待收紧；"
            f"现失败对 {len(failures)} 条，基线 {len(baseline)} 条）"
        )
        return 1
    print(
        f"theme-contrast：绿（{len(themes)} 主题 + {len(collect_assets())} 资产全量核算；"
        f"失败对 {len(failures)} 条均在基线内）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

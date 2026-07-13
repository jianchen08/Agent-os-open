"""主题对比度审查脚本。

读取 frontend/src/config/themes/presets/*.ts，对每对「前景 on 背景」计算 WCAG 对比度，
按 AA 阈值（正文 ≥4.5:1，非文字/大号 ≥3:1）判定，输出每个主题的合格/不合格配对。

用法：
    python scripts/check_theme_contrast.py

依赖：标准库。颜色值支持 #rrggbb / #rgb / rgba()/rgb()；渐变背景取所有纯色 stop 的最坏情况。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PRESETS_DIR = Path(__file__).resolve().parent.parent / "frontend" / "src" / "config" / "themes" / "presets"

# AA 阈值：正常文本 ≥4.5，非文本/大文本 ≥3
THRESH_TEXT = 4.5
THRESH_NONTEXT = 3.0


def hex_to_rgb(s: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", s.strip())
    if m:
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.fullmatch(r"#?([0-9a-fA-F]{3})", s.strip())
    if m:
        h = "".join(c * 2 for c in m.group(1))
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return None


def parse_rgba(s: str) -> tuple[int, int, int, float] | None:
    m = re.search(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", s)
    if not m:
        return None
    a = float(m.group(4)) if m.group(4) is not None else 1.0
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), a


def composite(fg: tuple[int, int, int, float], bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """把带 alpha 的前景合成到不透明背景上。"""
    r, g, b, a = fg
    return (
        round(r * a + bg[0] * (1 - a)),
        round(g * a + bg[1] * (1 - a)),
        round(b * a + bg[2] * (1 - a)),
    )


def rel_lum(rgb: tuple[int, int, int]) -> float:
    def chan(c: int) -> float:
        x = c / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1, l2 = rel_lum(fg), rel_lum(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def extract_solid_colors(value: str) -> list[tuple[int, int, int]]:
    """从一条颜色值里提取所有纯色（hex / rgb），渐变取每个 stop。"""
    out: list[tuple[int, int, int]] = []
    for tok in re.findall(r"#[0-9a-fA-F]{3,6}|rgba?\([^)]*\)", value):
        h = hex_to_rgb(tok)
        if h:
            out.append(h)
            continue
        r = parse_rgba(tok)
        if r:
            out.append(r[0:3])
    return out


def representative_bg(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    """取背景的代表性纯色：纯色直接用；渐变取所有 stop 的中位亮度色。"""
    solids = extract_solid_colors(value)
    if not solids:
        return fallback
    solids.sort(key=rel_lum)
    return solids[len(solids) // 2]


def worst_case_bg_stops(value: str, fallback: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    """渐变背景返回所有 stop（用于最坏情况对比）；纯色返回单元素。"""
    solids = extract_solid_colors(value)
    return solids or [fallback]


def find_value(block: str, key: str) -> str | None:
    """在某个已切出的块里，按 'key: "..."' 取值（单引号或双引号）。"""
    m = re.search(rf"\b{re.escape(key)}\s*:\s*('[^']*'|\"[^\"]*\")", block)
    return m.group(1).strip("'\"") if m else None


def extract_block(src: str, key: str) -> str | None:
    """花括号配平提取 `key: { ... }` 的内部文本。"""
    m = re.search(rf"\b{re.escape(key)}\s*:\s*\{{", src)
    if not m:
        return None
    i = m.end() - 1  # 指向起始 {
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    return None


def main() -> int:
    if not PRESETS_DIR.exists():
        print(f"找不到主题目录: {PRESETS_DIR}", file=sys.stderr)
        return 1

    files = sorted(PRESETS_DIR.glob("*.ts"))
    print(f"扫描 {len(files)} 个主题（AA 阈值：正文≥{THRESH_TEXT}，非文字≥{THRESH_NONTEXT}）\n")

    any_fail = False
    for f in files:
        raw = f.read_text(encoding="utf-8")
        colors = extract_block(raw, "colors") or raw
        text_blk = extract_block(colors, "text") or colors
        status_blk = extract_block(colors, "status") or colors
        bubble_blk = extract_block(colors, "bubble") or colors
        bg_blk = extract_block(colors, "background") or colors

        def g(key: str, blk: str) -> str | None:
            return find_value(blk, key)

        bg_main = g("main", bg_blk) or "#ffffff"

        # 主背景的代表性纯色（rgba 合成、渐变取中位 stop），用于把带透明的前景/背景"落地"
        rep_bg = representative_bg(bg_main, (255, 255, 255))

        def resolve_solid(v: str | None) -> tuple[int, int, int] | None:
            """把任意颜色值解析为不透明 RGB（rgba 合成到 rep_bg，渐变取首个 stop）。"""
            if not v:
                return None
            h = hex_to_rgb(v)
            if h:
                return h
            r = parse_rgba(v)
            if r:
                return composite(r, rep_bg)
            for tok in re.findall(r"#[0-9a-fA-F]{3,6}|rgba?\([^)]*\)", v):
                h2 = hex_to_rgb(tok)
                if h2:
                    return h2
                r2 = parse_rgba(tok)
                if r2:
                    return composite(r2, rep_bg)
            return None

        def bg_stops(value: str | None) -> list[tuple[int, int, int]]:
            """背景转 stop 列表：纯色单元素，rgba 合成，渐变每个 stop 分别处理。"""
            if not value:
                return [rep_bg]
            if hex_to_rgb(value):
                return [hex_to_rgb(value)]  # type: ignore[list-item]
            r = parse_rgba(value)
            if r:
                return [composite(r, rep_bg)]
            stops: list[tuple[int, int, int]] = []
            for tok in re.findall(r"#[0-9a-fA-F]{3,6}|rgba?\([^)]*\)", value):
                h2 = hex_to_rgb(tok)
                if h2:
                    stops.append(h2)
                else:
                    r2 = parse_rgba(tok)
                    if r2:
                        stops.append(composite(r2, rep_bg))
            return stops or [rep_bg]

        main_stops = bg_stops(bg_main)
        user_stops = bg_stops(g("user_bg", bubble_blk))
        ai_stops = bg_stops(g("ai_bg", bubble_blk))

        # (label, fg_value, bg_stop_list, threshold)
        pairs: list[tuple[str, str | None, list[tuple[int, int, int]], float]] = [
            ("text.primary   on bg.main ", g("primary", text_blk), main_stops, THRESH_TEXT),
            ("text.secondary on bg.main ", g("secondary", text_blk), main_stops, THRESH_TEXT),
            ("text.muted     on bg.main ", g("muted", text_blk), main_stops, THRESH_NONTEXT),
            ("text.disabled  on bg.main ", g("disabled", text_blk), main_stops, THRESH_NONTEXT),
            ("status.success on bg.main ", g("success", status_blk), main_stops, THRESH_TEXT),
            ("status.warning on bg.main ", g("warning", status_blk), main_stops, THRESH_TEXT),
            ("status.error   on bg.main ", g("error", status_blk), main_stops, THRESH_TEXT),
            ("status.info    on bg.main ", g("info", status_blk), main_stops, THRESH_TEXT),
            ("user_text      on user_bg ", g("user_text", bubble_blk), user_stops, THRESH_TEXT),
            ("ai_text        on ai_bg   ", g("ai_text", bubble_blk), ai_stops, THRESH_TEXT),
        ]

        print(f"══ {f.stem} ══")
        theme_fail = False
        for label, fv, bgs, thr in pairs:
            fg = resolve_solid(fv)
            if fg is None:
                print(f"  ⚠ {label}: 无法解析 {fv!r}")
                continue
            worst = min(contrast(fg, bg) for bg in bgs)
            ok = worst >= thr
            theme_fail = theme_fail or not ok
            print(f"  {'✅' if ok else '❌'} {label}: {worst:.2f}:1  (需≥{thr})")
        print()
        if theme_fail:
            any_fail = True

    # 审查脚本只报告，不改变退出码（即便有不合格也不让 CI 失败）
    print(f"{'⚠ 存在不合格配对' if any_fail else '✅ 全部主题已检查完毕'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

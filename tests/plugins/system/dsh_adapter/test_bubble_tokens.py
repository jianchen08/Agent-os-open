# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
# @feature: 主题气泡与皮肤协调 | @ci: python-coverage
"""dsh_adapter 气泡令牌翻译测试——皮肤下聊天气泡跟皮不脱节、文字恒可读。

撞色根因：翻译器只发底色不发配对文字（文字回落基准预设值，浅玻璃面上
白字不可读）；无原生气泡规则的皮肤连底色也回落内置主题涂料。本车道用
dsh_plugins/ 真实 16 皮语料断言成对发射与对比度性质；对比度判定用测试内
独立实现的 WCAG 相对亮度，不复用被测实现。
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.unit

from translator import (  # noqa: E402,I001 - 裸名导入经 conftest sys.path 注入
    _parse_css_color,
    skins_to_plugin_themes,
)

THEMES = {t["id"]: t for t in skins_to_plugin_themes()}
ALL_SKIN_IDS = sorted(THEMES.keys())

# 内置主题的用户/AI 气泡涂料（深/浅基准），皮肤下出现即为翻译未接管
_BUILTIN_LEAK_BGS = ("#22D3EE", "#111C38", "#0E7490", "#EEF2F8")


def _hex_to_rgba(value: str) -> tuple[int, int, int, float]:
    m = re.fullmatch(r"#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", value.strip())
    assert m, f"非 hex 颜色: {value}"
    h, a = m.group(1), m.group(2)
    return (
        int(h[0:2], 16),
        int(h[2:4], 16),
        int(h[4:6], 16),
        int(a, 16) / 255 if a else 1.0,
    )


def _composite(face_hex: str, canvas_hex: str) -> str:
    """半透明面合成到画布上的实色（测试侧独立实现）。"""
    fr, fg_, fb, fa = _hex_to_rgba(face_hex)
    cr, cg, cb, _ = _hex_to_rgba(canvas_hex)
    return f"#{round(fr * fa + cr * (1 - fa)):02x}{round(fg_ * fa + cg * (1 - fa)):02x}{round(fb * fa + cb * (1 - fa)):02x}"


def _rel_lum(hex6: str) -> float:
    """WCAG 相对亮度（#rrggbb），测试侧独立实现。"""
    h = hex6.strip().lstrip("#")
    vals = []
    for i in (0, 2, 4):
        c = int(h[i : i + 2], 16) / 255
        vals.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_rel_lum(a), _rel_lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_all_skins_present():
    """真实语料完整性：dsh_plugins 下 16 款皮肤全部翻出主题条目。"""
    assert len(ALL_SKIN_IDS) == 16, f"皮肤语料应全量翻译，实际 {len(ALL_SKIN_IDS)} 款"


@pytest.mark.parametrize("tid", ALL_SKIN_IDS)
def test_bubble_tokens_paired_and_readable(tid):
    """每款皮肤发射底色+配对文字成对令牌，且文字对面实色 ≥4.5。"""
    variables = THEMES[tid]["variables"]
    canvas_raw = variables["--ds-bg-canvas"]
    for role in ("user", "ai"):
        bg = variables[f"--bubble-{role}-bg"]
        text = variables[f"--bubble-{role}-text"]
        # 成对性：有底必有字（撞色根因=单发底色、配字回落预设）
        assert bool(bg) == bool(text), f"{tid} --bubble-{role}-* 未成对发射"
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", text), f"{tid} {role} 文字令牌非 hex: {text}"
        if bg.startswith("color-mix("):
            # 画布玻璃缺省：与 MessageItem 平铺态文档化回退同配方（--card 即画布）
            assert bg == f"color-mix(in srgb, {canvas_raw} 80%, transparent)", f"{tid} AI 玻璃面配方漂移: {bg}"
            face_solid = canvas_raw
        else:
            parsed = _parse_css_color(bg)
            face_solid = bg if parsed[3] >= 1 else _composite(bg, canvas_raw)
        assert face_solid is not None
        assert _ratio(text, face_solid) >= 4.5, f"{tid} {role} 气泡文字对面对比度不足"


@pytest.mark.parametrize("tid", ALL_SKIN_IDS)
def test_bubble_link_readable_on_user_face(tid):
    """气泡内链接令牌：对用户面实色 ≥3（保用强调色或黑白择优两通道皆保证）。"""
    variables = THEMES[tid]["variables"]
    link = variables["--bubble-link"]
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", link), f"{tid} 链接色非 hex: {link}"
    face = _face_solid_of(variables["--bubble-user-bg"], variables["--ds-bg-canvas"])
    assert _ratio(link, face) >= 3, f"{tid} 链接 {link} 对面 {face} 不可读"


def _face_solid_of(user_bg: str, canvas: str) -> str:
    """面实色近似（与实现同口径：半透明合成到画布），独立解析。"""
    m = re.fullmatch(r"#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", user_bg.strip())
    if m and not m.group(2):
        return user_bg.strip().lower()
    fr, fg_, fb, fa = _hex_to_rgba(user_bg)
    cr, cg, cb, _ = _hex_to_rgba(canvas)
    return f"#{round(fr * fa + cr * (1 - fa)):02x}{round(fg_ * fa + cg * (1 - fa)):02x}{round(fb * fa + cb * (1 - fa)):02x}"


@pytest.mark.parametrize("tid", ALL_SKIN_IDS)
def test_no_builtin_paint_leak(tid):
    """皮肤下的用户/AI 气泡面不得是内置主题涂料残留。"""
    variables = THEMES[tid]["variables"]
    for key in ("--bubble-user-bg", "--bubble-ai-bg"):
        bg = variables[key]
        head = bg.split(",")[0].strip()
        assert head not in _BUILTIN_LEAK_BGS, f"{tid} {key}={bg} 为内置预设涂料"


@pytest.mark.parametrize(
    ("tid", "field", "expected"),
    [
        # patches.css 原生提取优先：女仆工坊原样保留含透明度声明
        ("dsh-skin-maid-atelier", "--bubble-user-bg", "#e8edf9f2"),
        ("dsh-skin-maid-atelier", "--bubble-ai-bg", "#f8fafff0"),
        # 基准态选段：蓝色幻想 base=light 取 :root 段品牌色（非暗态 #7f96d2）
        ("dsh-skin-blue-fantasy", "--bubble-user-bg", "#4a5fa8"),
        # 赛博夜城 base=dark 取暗态品牌色（非亮态 #00b8d4）
        ("dsh-skin-cyber-night", "--bubble-user-bg", "#00e5ff"),
        # 单值皮肤两态共用
        ("dsh-skin-minecraft", "--bubble-user-bg", "#83c94e"),
        # var() 引用单跳解引用：鲸鱼妈妈 dark 态 var(--dsw-static-blue-450)
        ("dsh-skin-whale-mom", "--bubble-user-bg", "#4ca2e0"),
    ],
)
def test_user_face_source_fidelity(tid, field, expected):
    assert THEMES[tid]["variables"][field] == expected


def test_text_not_same_as_face_seed():
    """防同色互撞：对 brand-text==brand-primary 的皮肤（matrix/minecraft/trading/xp），
    发射文字必须与面实色可区分且达标（对比度性质已在上一条覆盖量值，此处钉差异方向）。"""
    for tid in ("dsh-skin-matrix", "dsh-skin-xp"):
        variables = THEMES[tid]["variables"]
        assert variables["--bubble-user-text"] != variables["--bubble-user-bg"]


def _hsl_to_rgb(value: str) -> tuple[int, int, int]:
    """'H S% L%' 串 → rgb（测试侧独立实现）。"""
    h, s, lig = (float(x.replace("%", "")) for x in value.split())
    h /= 360
    s /= 100
    lig /= 100
    if s == 0:
        v = round(255 * lig)
        return (v, v, v)
    q = lig * (1 + s) if lig < 0.5 else lig + s - lig * s
    p = 2 * lig - q

    def chan(t: float) -> float:
        t %= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    return (
        round(255 * chan(h + 1 / 3)),
        round(255 * chan(h)),
        round(255 * chan(h - 1 / 3)),
    )


@pytest.mark.parametrize("tid", ALL_SKIN_IDS)
def test_primary_accent_readable_on_card_faces(tid):
    """强调色作为文字的配对强制：--primary 压在卡面/画布族上 ≥4.5。

    工具卡标题/文件链接/操作按钮以 text-primary 消费 --primary，底为
    bg-card（=画布）与 bg-muted 派生面——铁律与文本令牌一致：对画布与
    派生面双面达标。
    """
    variables = THEMES[tid]["variables"]
    primary = _hsl_to_rgb(variables["--primary"])
    for face_key in ("--card", "--muted"):
        face = _hsl_to_rgb(variables[face_key])
        face_hex = "#{:02x}{:02x}{:02x}".format(*face)
        primary_hex = "#{:02x}{:02x}{:02x}".format(*primary)
        assert _ratio(primary_hex, face_hex) >= 4.5, (
            f"{tid} --primary={variables['--primary']} 对 {face_key}={variables[face_key]} "
            f"对比度不足（强调色作文字隐形）"
        )


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", value.strip())
    assert m, f"非 #rrggbb 颜色: {value}"
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


@pytest.mark.parametrize(
    "tid",
    ["dsh-skin-cyber-night", "dsh-skin-whale-mom", "dsh-skin-matrix"],
)
def test_primary_brand_preserved_when_already_readable(tid):
    """达标皮肤的 --primary 与皮肤原 accent（--ds-accent-primary）等值——
    enforce 只动不达标色，品牌识别不受扰（hsl 串往返允许 ±1 量化漂移）。"""
    variables = THEMES[tid]["variables"]
    got = _hsl_to_rgb(variables["--primary"])
    want = _hex_to_rgb(variables["--ds-accent-primary"])
    assert all(abs(a - b) <= 1 for a, b in zip(got, want, strict=True)), (
        f"{tid} --primary={variables['--primary']} 偏离原 accent {want}"
    )


class TestPickSkinAliasBranches:
    """段归属与回退分支（合成语料覆盖真实皮肤未触达的路径）。"""

    def test_light_block_wins_for_light_base(self):
        from translator import _pick_skin_alias

        css = ":root { --brand: #light; }\nbody[data-ds-dark-theme] { --brand: #dark; }"
        assert _pick_skin_alias(css, "--brand", dark=False) == "#light"

    def test_dark_block_wins_for_dark_base(self):
        from translator import _pick_skin_alias

        css = ":root { --brand: #light; }\nbody[data-ds-dark-theme] { --brand: #dark; }"
        assert _pick_skin_alias(css, "--brand", dark=True) == "#dark"

    def test_missing_state_falls_back_to_other(self):
        """目标段未重涂（单值皮肤）回退另一段——head 区状态切换块先于别名
        定义出现的结构不得误判段归属。"""
        from translator import _pick_skin_alias

        css = (
            ":root { color: #000 } body[data-ds-dark-theme] { color: #fff } "
            ":root { --dsw-alias-brand-primary: #shared; }"
        )
        assert _pick_skin_alias(css, "--dsw-alias-brand-primary", dark=True) == "#shared"
        assert _pick_skin_alias(css, "--dsw-alias-brand-primary", dark=False) == "#shared"

    def test_absent_declaration_returns_none(self):
        from translator import _pick_skin_alias

        css = ":root { --other: #123 }"
        assert _pick_skin_alias(css, "--dsw-alias-brand-primary", dark=False) is None

"""
ASCII 艺术字生成器 单元测试

按 TDD Red 阶段编写，验证：
1. 至少存在 3 种字体
2. 生成结果为多行字符串（高度 = 字体的行数）
3. 颜色着色通过 ANSI 转义序列包裹
4. 文件输出功能正确
5. 未知字符使用空白占位（不报错）
6. 字体高度一致
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

# 允许从同目录及上级导入被测模块
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from ascii_art_generator import (  # noqa: E402
    FONTS,
    COLOR_CODES,
    DEFAULT_FONT,
    get_font,
    render_char,
    render_text,
    colorize,
    main,
)


class TestFontRegistry(unittest.TestCase):
    """字体注册表约束"""

    def test_has_at_least_three_fonts(self) -> None:
        self.assertGreaterEqual(len(FONTS), 3, "至少需要 3 种字体")

    def test_required_fonts_exist(self) -> None:
        for name in ("standard", "banner", "block"):
            self.assertIn(name, FONTS, f"缺少必需字体: {name}")

    def test_default_font_exists(self) -> None:
        self.assertIn(DEFAULT_FONT, FONTS)

    def test_every_font_is_multiline(self) -> None:
        """每种字体的字符模板必须至少 2 行（否则不算 ASCII 艺术字）"""
        for font_name, font_data in FONTS.items():
            for ch, template in font_data.items():
                self.assertGreaterEqual(
                    len(template), 2,
                    f"字体 {font_name} 字符 {ch!r} 行数不足 2",
                )

    def test_every_font_height_consistent(self) -> None:
        """同一种字体的所有字符行数必须一致"""
        for font_name, font_data in FONTS.items():
            heights = {len(template) for template in font_data.values()}
            self.assertEqual(
                len(heights), 1,
                f"字体 {font_name} 字符高度不一致: {heights}",
            )


class TestColorRegistry(unittest.TestCase):
    """颜色方案约束"""

    def test_required_colors(self) -> None:
        for c in ("red", "green", "blue", "yellow"):
            self.assertIn(c, COLOR_CODES, f"缺少颜色: {c}")

    def test_color_code_is_ansi_escape(self) -> None:
        """每个颜色码必须以 \\x1b[ 开头、m 结尾（ANSI 转义序列）"""
        for name, code in COLOR_CODES.items():
            self.assertTrue(
                code.startswith("\x1b[") and code.endswith("m"),
                f"颜色 {name} 的码 {code!r} 不是合法 ANSI 转义序列",
            )


class TestRenderChar(unittest.TestCase):
    """单字符渲染"""

    def test_known_char_returns_template(self) -> None:
        template = get_font("standard")["A"]
        self.assertEqual(render_char("A", "standard"), template)

    def test_unknown_char_returns_blank_lines(self) -> None:
        result = render_char("?", "standard")
        height = len(get_font("standard")["A"])
        self.assertEqual(len(result), height)
        for line in result:
            self.assertEqual(line.strip(), "")

    def test_lowercase_normalized_to_upper(self) -> None:
        upper = render_char("a", "standard")
        self.assertEqual(upper, render_char("A", "standard"))


class TestRenderText(unittest.TestCase):
    """整段文字渲染"""

    def test_output_height_matches_font(self) -> None:
        text = "HI"
        height = len(get_font("banner")["A"])
        lines = render_text(text, "banner")
        self.assertEqual(len(lines), height)

    def test_output_width_scales_with_input(self) -> None:
        """输出宽度应随输入字符数线性增长（每字符宽度=模板首行宽度）"""
        font_data = get_font("standard")
        char_width = len(font_data["A"][0])
        single = render_text("A", "standard")
        triple = render_text("AAA", "standard")
        self.assertEqual(len(single[0]), char_width)
        self.assertEqual(len(triple[0]), char_width * 3)

    def test_empty_text_returns_empty(self) -> None:
        self.assertEqual(render_text("", "standard"), [])

    def test_includes_space(self) -> None:
        """空格也必须有模板（用于字符间留白）"""
        self.assertIn(" ", get_font("standard"))
        self.assertIn(" ", get_font("banner"))
        self.assertIn(" ", get_font("block"))


class TestColorize(unittest.TestCase):
    """颜色着色"""

    def test_colorize_wraps_each_line(self) -> None:
        lines = ["AB", "CD"]
        out = colorize(lines, "green")
        self.assertEqual(len(out), len(lines))
        for original, wrapped in zip(lines, out):
            self.assertIn(COLOR_CODES["green"], wrapped)
            self.assertIn(original, wrapped)
            # 必须以 RESET 结尾
            self.assertTrue(wrapped.endswith("\x1b[0m"))

    def test_colorize_unknown_color_returns_plain(self) -> None:
        """未知颜色应安全降级为原样输出，不抛异常"""
        lines = ["X"]
        out = colorize(lines, "nonexistent_color")
        self.assertEqual(out, lines)


class TestFileOutput(unittest.TestCase):
    """文件输出"""

    def test_output_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "art.txt")
            with patch_argv(["ascii_art_generator.py", "--text", "AB", "--output", path]):
                main()
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertGreater(len(content), 0)
            # 默认无颜色时，文件输出不应带 ANSI 码
            self.assertNotIn("\x1b[", content)


def patch_argv(argv: list[str]) -> object:
    """替换 sys.argv 的上下文管理器（仅供测试使用）"""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        original = sys.argv[:]
        sys.argv = argv
        try:
            yield
        finally:
            sys.argv = original

    return _ctx()


if __name__ == "__main__":
    unittest.main()

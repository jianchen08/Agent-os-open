# @feature: FP-0.2.〇 TB参赛适配 R20 | @ci: none-local
"""图像原生分辨率网格切分测试（R20 定稿：ceil 等分网格、零缩放、拼接覆盖=原图）。

断行为不断实现：网格几何用性质断言（每片 ≤ 上限、并集恰等于原图、相邻片
共享端点）；像素面用合成图像拼接还原（ImageChops 差集 bbox 为空 = 逐像素
相等）。≥2 组有区分度输入覆盖 1×1/2×1/1×2/2×2/非整数倍/护栏收缩。
"""

from __future__ import annotations

import base64
import io

import pytest
from tiling import (
    MAX_TILES_PER_IMAGE,
    compute_grid,
    equal_splits,
    split_image_tiling,
)

pytestmark = pytest.mark.unit


class TestEqualSplits:
    def test_single_part_returns_whole_axis(self) -> None:
        assert equal_splits(1000, 1) == [(0, 1000)]

    def test_exact_multiple_splits_evenly(self) -> None:
        assert equal_splits(2000, 2) == [(0, 1000), (1000, 2000)]

    def test_non_integer_ceil_front_loads_remainder(self) -> None:
        # 1500/2：每片 750；1501/2：前片 751 承接余数，并集不变。
        assert equal_splits(1500, 2) == [(0, 750), (750, 1500)]
        assert equal_splits(1501, 2) == [(0, 751), (751, 1501)]

    def test_coverage_property_any_parts(self) -> None:
        for length in (1, 7, 1568, 1569, 3001, 10000):
            for parts in (1, 2, 3, 5):
                splits = equal_splits(length, parts)
                assert splits[0][0] == 0
                assert splits[-1][1] == length
                for (_, end), (start, _) in zip(splits, splits[1:], strict=False):
                    assert end == start, "相邻片共享端点（无重叠无缝隙）"
                assert all(b - a <= -(-length // parts) for a, b in splits)


class TestComputeGrid:
    def test_within_spec_no_split(self) -> None:
        g = compute_grid(800, 600, 2458624)  # isqrt=1568，与旧单边 1568 形状等价
        assert (g["rows"], g["cols"]) == (1, 1)
        assert g["window"] == (0, 0, 800, 600)
        assert g["capped"] is False

    def test_width_two_times_two_cols(self) -> None:
        g = compute_grid(3136, 800, 2458624)
        assert (g["rows"], g["cols"]) == (1, 2)

    def test_height_two_times_two_rows(self) -> None:
        g = compute_grid(800, 3136, 2458624)
        assert (g["rows"], g["cols"]) == (2, 1)

    def test_both_axes_two_by_two(self) -> None:
        g = compute_grid(3136, 3136, 2458624)
        assert (g["rows"], g["cols"]) == (2, 2)

    def test_non_integer_ceil_goes_up(self) -> None:
        # 1.5 倍 → 2 份；2.1 倍 → 3 份（ceil 语义）。
        assert compute_grid(int(1568 * 1.5), 800, 2458624)["cols"] == 2
        assert compute_grid(int(1568 * 2.1), 800, 2458624)["cols"] == 3

    def test_deepseek_total_pixel_threshold_catches_poster(self) -> None:
        # R21 回归锚（总像素口径）：TB 真题海报 816×1056 = 861,696 总像素 >
        # DeepSeek 不缩放安全区 544² = 295,936 → 必须切分（R20 误用单边
        # 1300 阈值漏判，API 端照旧缩放）。每块总像素 ≤ 295,936。
        g = compute_grid(816, 1056, 295936)
        assert (g["rows"], g["cols"]) == (2, 2)
        assert not g["capped"]
        x0, y0, x1, y1 = g["window"]
        for xa, xb in [(x0, (x0 + x1) // 2), ((x0 + x1) // 2, x1)]:
            for ya, yb in [(y0, (y0 + y1) // 2), ((y0 + y1) // 2, y1)]:
                assert (xb - xa) * (yb - ya) <= 295936

    def test_tile_cap_shrinks_to_centered_window(self) -> None:
        mp = 1000 * 1000  # 轴预算 isqrt = 1000
        g = compute_grid(4 * 1000, 4 * 1000, mp)  # 4×4=16 块 > 护栏 6
        assert g["capped"] is True
        assert g["rows"] * g["cols"] <= MAX_TILES_PER_IMAGE
        x0, y0, x1, y1 = g["window"]
        assert x0 >= 0
        assert x1 <= 4 * 1000
        assert y0 >= 0
        assert y1 <= 4 * 1000
        # 中心锚定：窗口四周留白对称。
        assert x0 == 4 * 1000 - x1
        assert y0 == 4 * 1000 - y1
        # 窗口内每片 ≤ 轴预算（窗口边 ≤ 份数 × 轴预算）。
        assert (x1 - x0) <= g["cols"] * 1000
        assert (y1 - y0) <= g["rows"] * 1000


def _make_png(width: int, height: int) -> str:
    """合成区分度图像：四象限异色 + 象限交界处细线（拼接错位即可检出）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    cx, cy = width // 2, height // 2
    draw.rectangle([0, 0, cx, cy], fill=(200, 30, 30))
    draw.rectangle([cx, 0, width, cy], fill=(30, 200, 30))
    draw.rectangle([0, cy, cx, height], fill=(30, 30, 200))
    draw.rectangle([cx, cy, width, height], fill=(240, 240, 60))
    draw.line([cx, 0, cx, height], fill=(0, 0, 0), width=3)
    draw.line([0, cy, width, cy], fill=(0, 0, 0), width=3)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class TestSplitImageTiling:
    def test_small_image_not_tiled(self) -> None:
        out = split_image_tiling(_make_png(800, 600), "image/png", 2458624)
        assert out["tiled"] is False
        assert out["tiles"] == []
        assert (out["width"], out["height"]) == (800, 600)

    def test_two_by_one_tiles_native_resolution_and_stitch_exact(self) -> None:
        from PIL import Image, ImageChops

        w, h = 2000, 1000
        out = split_image_tiling(_make_png(w, h), "image/png", 2458624)
        assert out["tiled"] is True
        assert out["grid"] == {"rows": 1, "cols": 2}
        assert len(out["tiles"]) == 2
        canvas = Image.new("RGB", (w, h))
        for tile in out["tiles"]:
            raw = base64.b64decode(tile["base64_data"])
            piece = Image.open(io.BytesIO(raw))
            # 原生分辨率零缩放：每块尺寸 = 坐标差，且 ≤ 规格上限。
            assert piece.size == (tile["x1"] - tile["x0"], tile["y1"] - tile["y0"])
            assert tile["x1"] - tile["x0"] <= 1568  # 轴预算 isqrt(2458624)
            assert tile["y1"] - tile["y0"] <= 1568
            assert tile["mime_type"] == "image/png"
            canvas.paste(piece, (tile["x0"], tile["y0"]))
        # 拼接覆盖 = 原图（逐像素相等）。
        original = Image.open(io.BytesIO(base64.b64decode(_make_png(w, h))))
        assert ImageChops.difference(canvas, original).getbbox() is None

    def test_two_by_two_grid_rows_cols_annotation(self) -> None:
        w, h = 3136, 3136
        out = split_image_tiling(_make_png(w, h), "image/png", 2458624)
        assert out["grid"] == {"rows": 2, "cols": 2}
        assert len(out["tiles"]) == 4
        assert [(t["row"], t["col"]) for t in out["tiles"]] == [
            (1, 1),
            (1, 2),
            (2, 1),
            (2, 2),
        ]
        text = out["annotation"]
        assert "2 行 × 2 列" in text
        assert "无缩放" in text
        assert "块(行1,列1)" in text
        assert "块(行2,列2)" in text
        assert f"{w}×{h}" in text

    def test_cap_path_annotation_declares_center_window(self) -> None:
        mp = 500 * 500  # 轴预算 isqrt = 500
        w, h = 4 * 500, 4 * 500
        out = split_image_tiling(_make_png(w, h), "image/png", mp)
        assert out["tiled"] is True
        assert len(out["tiles"]) <= MAX_TILES_PER_IMAGE
        assert "中心区域窗口" in out["annotation"]
        for tile in out["tiles"]:
            assert tile["width"] <= 500
            assert tile["height"] <= 500

    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(ValueError, match="base64"):
            split_image_tiling("not-base64!!!", "image/png", 2458624)

    def test_invalid_max_pixels_raises(self) -> None:
        with pytest.raises(ValueError, match="max_pixels"):
            split_image_tiling(_make_png(10, 10), "image/png", 0)

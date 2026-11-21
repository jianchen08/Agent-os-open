# @feature: FP-0.2.〇 TB参赛适配 R20/R21 | @ci: none-local
"""图像原生分辨率网格切分（R20 定稿：原图+分块一次性全注入，零缩放）。

实证背景：发给模型 API 的大图会被 API 端自己的视觉编码器缩放（插件层无缩放
也发生，API 行为不可控），文字边缘细节丢失。解法：注入前按当前模型视觉输入
上限做**原生分辨率等分网格切分**——原图 1 张（全局）+ 分块 N 张（细节）同时
注入；分块尺寸 ≤ 上限 → 不触发 API 端缩放，文字以原始分辨率进模型。

规格口径（R21 纠偏，总像素）：API 端缩放判定是**总像素（面积）口径**——
DeepSeek 官方：总像素小于约 544×544 等比放大、更大则缩到总像素约 1300×1300
（R20 误取目标值 1300 做单边阈值，861K 像素的海报被漏判"未超规格"，API 端
照旧缩放——TB 真题 R21 实证 57.42% 低于上轮 66.64%）。规格键改 max_pixels
（总像素上限），轴长上限 = isqrt(max_pixels)。

算法（R20 定稿保留）：图像长、宽各自与轴长上限求比，ceil(轴长/上限) = 该轴
份数（比值 ≤1 该轴不切）；两轴各自等分 → 网格，原生分辨率等分，相邻块共享
边（无重叠、零缩放、拼接覆盖恰等于原图）；等分前段多 1 承接余数，每块
≤ ceil(轴长/份数) ≤ sqrt(max_pixels)，总像素 ≤ max_pixels → 不触发缩放。

成本护栏：单图分块数上限 6（超限按比例收缩网格，仅注入以图像中心为锚的
规格窗口，窗口外边缘区域不注入——取舍记录于 TB参赛适配_台账.md R20）。

规格来源：llm.yaml models.<id>.multimodal.max_pixels（总像素上限），由
llm_core 解析写入 state.llm_vision_max_pixels，经 tool_args_inject 注入
file_read 后传入本服务——本模块不读任何配置文件（纯函数 + 像素操作）。
"""

from __future__ import annotations

import base64
import io
import math
from typing import Any

# 单图分块数护栏（R20 定稿保留项）：网格 rows*cols 超过该值时收缩为
# 中心窗口覆盖。6 = 2×3/3×2/1×6/6×1 的最大可用粒度（2×3 覆盖常见横图）。
MAX_TILES_PER_IMAGE = 6

# 分块统一重编码为 PNG（无损，保文字细节；分块尺寸 ≤ 上限不触发 API 缩放）。
_TILE_MIME = "image/png"


def axis_budget(max_pixels: int) -> int:
    """由总像素上限求单轴预算（isqrt）：每块 ≤ 轴预算² ≤ max_pixels。"""
    return max(1, math.isqrt(max(1, max_pixels)))


def ceil_div(a: int, b: int) -> int:
    """正整数向上取整除法。"""
    return -(-a // b)


def equal_splits(length: int, parts: int) -> list[tuple[int, int]]:
    """轴长等分切片（原生分辨率，前段多 1 承接余数）：[(start, end), ...]。

    parts=1 → 全轴一块。任意 parts 下每片 ≤ ceil(length/parts)，并集恰为
    [0, length)（相邻片共享端点、无重叠无缝隙）。
    """
    if parts <= 1:
        return [(0, length)]
    base, rem = divmod(length, parts)
    splits: list[tuple[int, int]] = []
    start = 0
    for i in range(parts):
        size = base + (1 if i < rem else 0)
        splits.append((start, start + size))
        start += size
    return splits


def compute_grid(
    width: int, height: int, max_pixels: int, max_tiles: int = MAX_TILES_PER_IMAGE
) -> dict[str, Any]:
    """按定稿算法求切分网格与覆盖窗口（纯几何，不碰像素）。

    轴长上限 = isqrt(max_pixels)（总像素口径 → 单轴预算）。

    Returns:
        {
          "rows": 行数, "cols": 列数,
          "window": (x0, y0, x1, y1) 覆盖窗口（原生分辨率）,
          "capped": 是否触发分块护栏（中心窗口收缩）,
        }
    网格 rows*cols ≤ max_tiles 时窗口 = 全图；超限时按比例收缩网格份数，
    窗口收缩为以图像中心为锚的 r_keep*axis × c_keep*axis 窗口（每片总像素
    仍 ≤ max_pixels）。
    """
    axis = axis_budget(max_pixels)
    rows = max(1, ceil_div(height, axis))
    cols = max(1, ceil_div(width, axis))
    if rows * cols <= max_tiles:
        return {"rows": rows, "cols": cols, "window": (0, 0, width, height), "capped": False}
    # 护栏收缩：网格份数按 sqrt(护栏/所需) 等比缩（保长宽比），再钳回合法区间；
    # 收缩后仍超限则逐次减大轴（确定性收敛到 ≤ max_tiles）。
    factor = math.sqrt(max_tiles / (rows * cols))
    r_keep = min(rows, max(1, round(rows * factor)))
    c_keep = min(cols, max(1, round(cols * factor)))
    while r_keep * c_keep > max_tiles:
        if r_keep >= c_keep and r_keep > 1:
            r_keep -= 1
        elif c_keep > 1:
            c_keep -= 1
        else:
            break
    win_h = min(height, r_keep * axis)
    win_w = min(width, c_keep * axis)
    x0 = (width - win_w) // 2
    y0 = (height - win_h) // 2
    return {
        "rows": r_keep,
        "cols": c_keep,
        "window": (x0, y0, x0 + win_w, y0 + win_h),
        "capped": True,
    }


def split_image_tiling(
    base64_data: str, mime_type: str, max_pixels: int
) -> dict[str, Any]:
    """解码图像并按网格切分（PIL 像素面；纯函数入口供 server 装配）。

    Returns:
        {
          "tiled": bool,                  # False = 尺寸未超规格（零切分）
          "width", "height": 原图尺寸,
          "grid": {"rows", "cols"},
          "tiles": [每块 {base64_data, mime_type, width, height, row, col,
                           x0, y0, x1, y1}]（行优先序）,
          "annotation": 现成标注文本（调用方直通工具结果 content 键）,
        }
    Raises:
        ValueError：base64 非法 / PIL 无法解码 / 参数非法（调用方降级路径自判）。
    """
    if not isinstance(max_pixels, int) or max_pixels <= 0:
        raise ValueError(f"max_pixels 必须为正整数，收到 {max_pixels!r}")
    from PIL import Image  # noqa: PLC0415 — 惰性：仅在真实切分请求时导入

    try:
        raw = base64.b64decode(base64_data, validate=True)
    except Exception as exc:
        raise ValueError(f"base64 解码失败: {exc}") from exc
    try:
        img: Image.Image = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:
        raise ValueError(f"图像解码失败（PIL）: {exc}") from exc
    width, height = img.size
    grid = compute_grid(width, height, max_pixels)
    if grid["rows"] == 1 and grid["cols"] == 1:
        return {
            "tiled": False,
            "width": width,
            "height": height,
            "grid": {"rows": 1, "cols": 1},
            "tiles": [],
            "annotation": "",
        }
    # 非 RGB(A) 模式（P/CMYK/1 等）转 RGB 再切（PNG 编码安全；RGBA 保留透明）。
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    x0w, y0w, x1w, y1w = grid["window"]
    xs = equal_splits(x1w - x0w, grid["cols"])
    ys = equal_splits(y1w - y0w, grid["rows"])
    tiles: list[dict[str, Any]] = []
    for r, (ya, yb) in enumerate(ys):
        for c, (xa, xb) in enumerate(xs):
            tile = img.crop((x0w + xa, y0w + ya, x0w + xb, y0w + yb))
            buf = io.BytesIO()
            tile.save(buf, format="PNG")
            tiles.append(
                {
                    "base64_data": base64.b64encode(buf.getvalue()).decode("ascii"),
                    "mime_type": _TILE_MIME,
                    "width": xb - xa,
                    "height": yb - ya,
                    "row": r + 1,
                    "col": c + 1,
                    "x0": x0w + xa,
                    "y0": y0w + ya,
                    "x1": x0w + xb,
                    "y1": y0w + yb,
                }
            )
    return {
        "tiled": True,
        "width": width,
        "height": height,
        "grid": {"rows": grid["rows"], "cols": grid["cols"]},
        "tiles": tiles,
        "annotation": _annotation_text(width, height, max_pixels, grid, tiles),
    }


def _annotation_text(
    width: int, height: int, max_pixels: int, grid: dict[str, Any], tiles: list[dict[str, Any]]
) -> str:
    """拼合关系标注（模型据此知道各块位置；行优先序与注入顺序一致）。"""
    rows, cols = grid["rows"], grid["cols"]
    lines = [
        f"[图像切分] 原图 {width}×{height}（总像素 {width * height}）超出当前模型"
        f"视觉输入不缩放上限（总像素 {max_pixels}），"
        f"已按原生分辨率等分切分（无缩放）：网格 {rows} 行 × {cols} 列，"
        "块序=行优先（从左到右、从上到下）。"
    ]
    if grid.get("capped"):
        wx0, wy0, wx1, wy1 = grid["window"]
        lines.append(
            f"原图超出单图分块护栏（{MAX_TILES_PER_IMAGE} 块），仅注入中心区域窗口 "
            f"x={wx0}..{wx1}, y={wy0}..{wy1}（窗口外边缘区域未注入）。"
        )
    spans = "；".join(
        f"块(行{t['row']},列{t['col']}) 覆盖 x={t['x0']}..{t['x1']}, y={t['y0']}..{t['y1']}"
        for t in tiles
    )
    lines.append(f"{spans}。原图+分块已一并注入：原图看全局，分块看细节（文字细节以分块为准）。")
    return "\n".join(lines)

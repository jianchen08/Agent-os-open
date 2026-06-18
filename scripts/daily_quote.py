#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日励志语录 — 每次运行随机输出一条温暖励志的语录。

独立运行：
    python scripts/daily_quote.py
"""

import random
from datetime import datetime


QUOTES: list[str] = [
    "每一个不曾起舞的日子，都是对生命的辜负。",
    "你今天受的苦，会照亮你未来的路。",
    "慢慢来，比较快。",
    "星光不问赶路人，时光不负有心人。",
    "所谓优秀，就是不断地把平凡的事做到不平凡。",
    "把每一个平凡的日子都过成诗。",
    "你的努力，时间终会给你答案。",
    "愿你眼里有光，心中有爱，脚下有力。",
    "生活不会亏待一个用力奔跑的人。",
    "愿你成为自己的太阳，无需凭借谁的光。",
    "所有的好运，都来自过往的坚持和努力。",
    "认真的人改变自己，执着的人改变命运。",
    "心若有所向往，何惧道阻且长。",
    "你必须非常努力，才能看起来毫不费力。",
    "今天比昨天好，就是最大的进步。",
    "愿你成为自己喜欢的样子，不畏将来，不念过往。",
    "与其感慨路难行，不如马上出发。",
    "努力不一定成功，但放弃一定失败。",
    "温柔要有，但不是妥协；安静要有，但不是沉默。",
    "愿你出走半生，归来仍是少年。",
    "低谷期是为了铺垫更好的未来，请保持热爱。",
    "把今天过好，就是在给明天铺路。",
    "你的坚持，终将美好。",
    "别让平凡的生活，磨灭你眼里的光。",
    "愿所有付出都不被辜负，所有等待都有回响。",
]


def get_daily_quote() -> str:
    """从语录库中随机选择一条。

    Returns:
        随机选中的语录文本。
    """
    return random.choice(QUOTES)


def format_quote(quote: str, emoji: str = "🌟") -> str:
    """将语录格式化为带 emoji 和分隔线的展示文本。

    Args:
        quote: 语录内容。
        emoji: 装饰 emoji。

    Returns:
        格式化后的多行字符串。
    """
    line = "─" * 48
    today = datetime.now().strftime("%Y-%m-%d")
    return (
        f"{line}\n"
        f"  {emoji}  今日语录  {emoji}\n"
        f"  📅 {today}\n"
        f"{line}\n"
        f"\n"
        f"  「{quote}」\n"
        f"\n"
        f"{line}\n"
        f"  ✨ 愿这句话给你一整天的力量 ✨\n"
        f"{line}"
    )


def main() -> None:
    """脚本入口：随机选一条语录并打印。"""
    quote = get_daily_quote()
    print(format_quote(quote))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抛硬币概率模拟脚本

模拟抛硬币实验，统计正反面出现次数和实际概率，
并使用 matplotlib 绘制概率收敛曲线图。

Usage:
    python scripts/coin_flip.py            # 默认 10000 次
    python scripts/coin_flip.py --flips 50000
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path
from typing import List, Tuple

import matplotlib

# 使用非交互后端，避免在无显示器的环境下报错
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="抛硬币概率模拟器")
    parser.add_argument(
        "--flips",
        type=int,
        default=10000,
        help="抛硬币总次数（默认 10000）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="随机种子（可选，便于复现结果）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="docs/coin_flip_chart.png",
        help="图表输出路径（默认 docs/coin_flip_chart.png）",
    )
    return parser.parse_args()


def simulate_coin_flips(n: int, rng: random.Random) -> List[int]:
    """
    模拟 n 次抛硬币，返回每次结果序列。

    使用 0 表示反面（Tail），1 表示正面（Head）。
    """
    return [rng.randint(0, 1) for _ in range(n)]


def compute_running_probability(results: List[int]) -> List[float]:
    """
    计算累计正面出现概率（收敛序列）。

    第 i 个元素表示前 i+1 次抛掷中正面出现的累计频率。
    """
    running_prob: List[float] = []
    heads_so_far = 0
    for i, value in enumerate(results, start=1):
        if value == 1:
            heads_so_far += 1
        running_prob.append(heads_so_far / i)
    return running_prob


def count_outcomes(results: List[int]) -> Tuple[int, int]:
    """统计正反面次数，返回 (heads, tails)。"""
    heads = sum(1 for v in results if v == 1)
    tails = len(results) - heads
    return heads, tails


def print_statistics(flips: int, heads: int, tails: int) -> None:
    """打印统计信息到标准输出。"""
    print("=" * 50)
    print("抛硬币模拟结果")
    print("=" * 50)
    print(f"总次数:    {flips}")
    print(f"正面(Head): {heads}  实际概率: {heads / flips:.4%}")
    print(f"反面(Tail): {tails}  实际概率: {tails / flips:.4%}")
    print(f"理论概率:  0.5000")
    print("=" * 50)


def plot_convergence(
    running_prob: List[float],
    flips: int,
    output_path: Path,
) -> None:
    """绘制概率收敛曲线并保存为 PNG。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(
        range(1, flips + 1),
        running_prob,
        color="#1f77b4",
        linewidth=1.2,
        label="实际正面概率（累计频率）",
    )
    ax.axhline(
        y=0.5,
        color="red",
        linestyle="--",
        linewidth=1.5,
        label="理论概率 0.5",
    )

    ax.set_title(f"Coin Flip Probability Convergence (N = {flips})", fontsize=14)
    ax.set_xlabel("Number of Flips", fontsize=12)
    ax.set_ylabel("Running Frequency of Heads", fontsize=12)
    ax.set_ylim(0.0, 1.0)
    ax.set_xlim(1, flips)
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(loc="upper right", fontsize=11)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    """脚本主入口。"""
    args = parse_args()

    if args.flips <= 0:
        raise ValueError(f"抛硬币次数必须为正整数，得到: {args.flips}")

    rng = random.Random(args.seed)
    results = simulate_coin_flips(args.flips, rng)
    heads, tails = count_outcomes(results)

    print_statistics(args.flips, heads, tails)

    running_prob = compute_running_probability(results)
    output_path = Path(args.output)
    plot_convergence(running_prob, args.flips, output_path)

    print(f"收敛曲线已保存至: {output_path}")


if __name__ == "__main__":
    main()

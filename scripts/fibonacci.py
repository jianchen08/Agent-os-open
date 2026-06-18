"""生成并打印斐波那契数列前 N 项。

脚本可独立运行：python scripts/fibonacci.py
也可作为模块导入使用 generate_fibonacci(n)。
"""

from __future__ import annotations


DEFAULT_COUNT: int = 20


def generate_fibonacci(n: int) -> list[int]:
    """生成斐波那契数列前 n 项。

    数列定义：F(0)=0, F(1)=1, F(k)=F(k-1)+F(k-2)。
    使用迭代算法，时间复杂度 O(n)，空间复杂度 O(n)。

    Args:
        n: 要生成的项数，必须 >= 1。

    Returns:
        包含 n 个斐波那契数的列表，按索引顺序排列。

    Raises:
        ValueError: 当 n < 1 时。
    """
    if n < 1:
        raise ValueError(f"n 必须 >= 1，当前值: {n}")

    if n == 1:
        return [0]

    sequence: list[int] = [0, 1]
    for _ in range(2, n):
        sequence.append(sequence[-1] + sequence[-2])
    return sequence


def format_output(sequence: list[int]) -> str:
    """将斐波那契数列格式化为可读的多行字符串。

    Args:
        sequence: 斐波那契数列列表。

    Returns:
        包含表头、各项及统计信息的格式化字符串。
    """
    if not sequence:
        return "空数列"

    width: int = max(len(str(v)) for v in sequence)
    index_width: int = max(2, len(str(len(sequence) - 1)))
    line: str = "-" * (index_width + width + 9)

    lines: list[str] = [
        f"斐波那契数列前 {len(sequence)} 项：",
        line,
    ]
    for index, value in enumerate(sequence):
        lines.append(f"F({index:>{index_width}}) = {value:>{width}}")
    lines.append(line)
    lines.append(f"总和 : {sum(sequence)}")
    lines.append(f"最大 : {max(sequence)}")
    return "\n".join(lines)


def main(count: int = DEFAULT_COUNT) -> None:
    """脚本入口：生成数列并打印。

    Args:
        count: 要生成的项数，默认 20。
    """
    sequence: list[int] = generate_fibonacci(count)
    print(format_output(sequence))


if __name__ == "__main__":
    main()

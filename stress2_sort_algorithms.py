"""排序算法集合与性能对比测试。

实现冒泡排序、快速排序、归并排序、插入排序、堆排序五种算法，
并提供性能对比测试代码。
"""

from __future__ import annotations

import time
import random
from typing import Callable


def bubble_sort(arr: list[int]) -> list[int]:
    """冒泡排序。

    相邻元素两两比较，将较大的元素逐步"冒泡"到数组末尾。

    Args:
        arr: 待排序的整数列表。

    Returns:
        排序后的新列表（升序）。
    """
    result = arr.copy()
    n = len(result)
    for i in range(n):
        swapped = False
        for j in range(0, n - i - 1):
            if result[j] > result[j + 1]:
                result[j], result[j + 1] = result[j + 1], result[j]
                swapped = True
        if not swapped:
            break
    return result


def quick_sort(arr: list[int]) -> list[int]:
    """快速排序。

    选取基准元素，将数组分为小于和大于基准的两部分，递归排序。

    Args:
        arr: 待排序的整数列表。

    Returns:
        排序后的新列表（升序）。
    """
    if len(arr) <= 1:
        return arr.copy()
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return quick_sort(left) + middle + quick_sort(right)


def merge_sort(arr: list[int]) -> list[int]:
    """归并排序。

    将数组递归拆分为两半，分别排序后合并。

    Args:
        arr: 待排序的整数列表。

    Returns:
        排序后的新列表（升序）。
    """
    if len(arr) <= 1:
        return arr.copy()
    mid = len(arr) // 2
    left = merge_sort(arr[:mid])
    right = merge_sort(arr[mid:])

    # 合并两个有序列表
    result: list[int] = []
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1
    result.extend(left[i:])
    result.extend(right[j:])
    return result


def insertion_sort(arr: list[int]) -> list[int]:
    """插入排序。

    逐个将元素插入到已排序部分的正确位置。

    Args:
        arr: 待排序的整数列表。

    Returns:
        排序后的新列表（升序）。
    """
    result = arr.copy()
    for i in range(1, len(result)):
        key = result[i]
        j = i - 1
        while j >= 0 and result[j] > key:
            result[j + 1] = result[j]
            j -= 1
        result[j + 1] = key
    return result


def heap_sort(arr: list[int]) -> list[int]:
    """堆排序。

    利用最大堆性质，反复取出堆顶最大元素完成排序。

    Args:
        arr: 待排序的整数列表。

    Returns:
        排序后的新列表（升序）。
    """
    result = arr.copy()
    n = len(result)

    def _heapify(heap: list[int], size: int, root: int) -> None:
        """对以 root 为根的子树执行堆化。"""
        largest = root
        left = 2 * root + 1
        right = 2 * root + 2

        if left < size and heap[left] > heap[largest]:
            largest = left
        if right < size and heap[right] > heap[largest]:
            largest = right
        if largest != root:
            heap[root], heap[largest] = heap[largest], heap[root]
            _heapify(heap, size, largest)

    # 建最大堆
    for i in range(n // 2 - 1, -1, -1):
        _heapify(result, n, i)

    # 逐个提取堆顶元素
    for i in range(n - 1, 0, -1):
        result[0], result[i] = result[i], result[0]
        _heapify(result, i, 0)

    return result


def run_performance_comparison(sizes: list[int] | None = None) -> None:
    """运行性能对比测试。

    对每种排序算法在不同数据规模下计时，并打印对比结果。

    Args:
        sizes: 测试数据规模列表，默认 [100, 1000, 5000]。
    """
    if sizes is None:
        sizes = [100, 1000, 5000]

    algorithms: list[tuple[str, Callable[[list[int]], list[int]]]] = [
        ("冒泡排序", bubble_sort),
        ("快速排序", quick_sort),
        ("归并排序", merge_sort),
        ("插入排序", insertion_sort),
        ("堆排序", heap_sort),
    ]

    print("=" * 60)
    print("排序算法性能对比测试")
    print("=" * 60)

    for size in sizes:
        print(f"\n数据规模: {size} 个随机整数")
        print("-" * 40)

        # 生成随机数据，所有算法使用同一份数据
        data = [random.randint(0, size * 10) for _ in range(size)]

        for name, sort_func in algorithms:
            test_data = data.copy()
            start = time.perf_counter()
            sorted_result = sort_func(test_data)
            elapsed = time.perf_counter() - start

            # 验证排序正确性
            assert sorted_result == sorted(data), f"{name} 排序结果不正确"

            print(f"  {name:8s}: {elapsed:.6f} 秒")

    print("\n" + "=" * 60)
    print("性能对比测试完成")


if __name__ == "__main__":
    run_performance_comparison()

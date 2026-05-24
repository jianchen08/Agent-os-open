"""压力测试工具模块，提供简单的 Calculator 类及单元测试。"""

from __future__ import annotations


class Calculator:
    """简易计算器，支持加减乘除运算。"""

    def add(self, a: float, b: float) -> float:
        """加法。

        Args:
            a: 被加数
            b: 加数

        Returns:
            两数之和
        """
        return a + b

    def subtract(self, a: float, b: float) -> float:
        """减法。

        Args:
            a: 被减数
            b: 减数

        Returns:
            两数之差
        """
        return a - b

    def multiply(self, a: float, b: float) -> float:
        """乘法。

        Args:
            a: 被乘数
            b: 乘数

        Returns:
            两数之积
        """
        return a * b

    def divide(self, a: float, b: float) -> float:
        """除法。

        Args:
            a: 被除数
            b: 除数

        Returns:
            两数之商

        Raises:
            ValueError: 除数为零时抛出
        """
        if b == 0:
            raise ValueError("除数不能为零")
        return a / b


# ===== 单元测试 =====

import unittest


class TestCalculator(unittest.TestCase):
    """Calculator 类的单元测试。"""

    def setUp(self) -> None:
        self.calc = Calculator()

    # --- 加法 ---
    def test_add_positive(self) -> None:
        self.assertEqual(self.calc.add(2, 3), 5)

    def test_add_negative(self) -> None:
        self.assertEqual(self.calc.add(-1, -2), -3)

    def test_add_zero(self) -> None:
        self.assertEqual(self.calc.add(0, 5), 5)

    # --- 减法 ---
    def test_subtract_positive(self) -> None:
        self.assertEqual(self.calc.subtract(10, 4), 6)

    def test_subtract_negative(self) -> None:
        self.assertEqual(self.calc.subtract(3, 7), -4)

    # --- 乘法 ---
    def test_multiply_positive(self) -> None:
        self.assertEqual(self.calc.multiply(3, 4), 12)

    def test_multiply_zero(self) -> None:
        self.assertEqual(self.calc.multiply(5, 0), 0)

    def test_multiply_negative(self) -> None:
        self.assertEqual(self.calc.multiply(-2, 3), -6)

    # --- 除法 ---
    def test_divide_positive(self) -> None:
        self.assertEqual(self.calc.divide(10, 2), 5)

    def test_divide_float_result(self) -> None:
        self.assertAlmostEqual(self.calc.divide(7, 2), 3.5)

    def test_divide_by_zero_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.calc.divide(1, 0)


if __name__ == "__main__":
    unittest.main()

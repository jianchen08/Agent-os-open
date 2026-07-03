#!/usr/bin/env python3
"""
单位换算工具
支持长度、重量、温度等常见单位的相互转换
"""


class UnitConverter:
    """单位换算器类"""

    # 长度单位转换（以米为基准）
    LENGTH_TO_METER = {
        "m": 1.0,  # 米
        "km": 1000.0,  # 千米
        "cm": 0.01,  # 厘米
        "mm": 0.001,  # 毫米
        "mi": 1609.344,  # 英里
        "yd": 0.9144,  # 码
        "ft": 0.3048,  # 英尺
        "in": 0.0254,  # 英寸
    }

    # 重量单位转换（以千克为基准）
    WEIGHT_TO_KG = {
        "kg": 1.0,  # 千克
        "g": 0.001,  # 克
        "mg": 0.000001,  # 毫克
        "lb": 0.453592,  # 磅
        "oz": 0.0283495,  # 盎司
        "t": 1000.0,  # 吨
    }

    @staticmethod
    def convert_length(value: float, from_unit: str, to_unit: str) -> float:
        """长度单位转换"""
        from_unit = from_unit.lower()
        to_unit = to_unit.lower()

        if from_unit not in UnitConverter.LENGTH_TO_METER:
            raise ValueError(f"不支持的长度单位: {from_unit}")
        if to_unit not in UnitConverter.LENGTH_TO_METER:
            raise ValueError(f"不支持的长度单位: {to_unit}")

        # 转换为米，再转换为目标单位
        meters = value * UnitConverter.LENGTH_TO_METER[from_unit]
        return meters / UnitConverter.LENGTH_TO_METER[to_unit]

    @staticmethod
    def convert_weight(value: float, from_unit: str, to_unit: str) -> float:
        """重量单位转换"""
        from_unit = from_unit.lower()
        to_unit = to_unit.lower()

        if from_unit not in UnitConverter.WEIGHT_TO_KG:
            raise ValueError(f"不支持的重量单位: {from_unit}")
        if to_unit not in UnitConverter.WEIGHT_TO_KG:
            raise ValueError(f"不支持的重量单位: {to_unit}")

        # 转换为千克，再转换为目标单位
        kg = value * UnitConverter.WEIGHT_TO_KG[from_unit]
        return kg / UnitConverter.WEIGHT_TO_KG[to_unit]

    @staticmethod
    def convert_temperature(value: float, from_unit: str, to_unit: str) -> float:
        """温度单位转换"""
        from_unit = from_unit.upper()
        to_unit = to_unit.upper()

        # 先转换为摄氏度
        if from_unit == "C":
            celsius = value
        elif from_unit == "F":
            celsius = (value - 32) * 5 / 9
        elif from_unit == "K":
            celsius = value - 273.15
        else:
            raise ValueError(f"不支持的温度单位: {from_unit}")

        # 从摄氏度转换到目标单位
        if to_unit == "C":
            return celsius
        if to_unit == "F":
            return celsius * 9 / 5 + 32
        if to_unit == "K":
            return celsius + 273.15
        raise ValueError(f"不支持的温度单位: {to_unit}")

    @classmethod
    def convert(cls, value: float, from_unit: str, to_unit: str, category: str = "length") -> float:
        """通用转换接口"""
        if category == "length":
            return cls.convert_length(value, from_unit, to_unit)
        if category == "weight":
            return cls.convert_weight(value, from_unit, to_unit)
        if category == "temperature":
            return cls.convert_temperature(value, from_unit, to_unit)
        raise ValueError(f"不支持的类别: {category}")


def main():
    """命令行交互"""
    print("=" * 50)
    print("           单位换算工具")
    print("=" * 50)
    print("\n支持的单位类型:")
    print("  长度: m, km, cm, mm, mi, yd, ft, in")
    print("  重量: kg, g, mg, lb, oz, t")
    print("  温度: C, F, K")
    print("\n示例:")

    # 示例使用
    converter = UnitConverter()

    # 长度转换示例
    result = converter.convert_length(100, "m", "km")
    print(f"  100米 = {result}千米")

    # 重量转换示例
    result = converter.convert_weight(1, "kg", "lb")
    print(f"  1千克 = {result:.4f}磅")

    # 温度转换示例
    result = converter.convert_temperature(100, "C", "F")
    print(f"  100摄氏度 = {result}华氏度")

    print("\n" + "=" * 50)


if __name__ == "__main__":
    main()

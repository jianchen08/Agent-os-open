"""变量优先级定义。

从旧代码 src/memory/variable_priority.py 搬迁，逻辑不变。

暴露接口：
- VariablePriority: 变量优先级枚举
"""

from __future__ import annotations

from enum import IntEnum


class VariablePriority(IntEnum):
    """变量优先级枚举。

    优先级顺序（从高到低）：
    1. TAR (1): 模板化组合占位符（最高优先级，可嵌套其他变量）
    2. SAR (2): 模型条件注入（针对特定 AI 模型）
    3. VAR (3): 全局变量（通用配置）
    4. SYS (4): 系统内置（日期、时间等）
    """

    TAR = 1  # 模板化组合占位符（最高）
    SAR = 2  # 模型条件注入
    VAR = 3  # 全局变量
    SYS = 4  # 系统内置（日期、时间等）

    def __str__(self) -> str:
        """字符串表示。"""
        return self.name

    @classmethod
    def from_prefix(cls, prefix: str) -> VariablePriority:
        """从变量前缀解析优先级。

        Args:
            prefix: 变量前缀（Tar/Sar/Var/Sys）

        Returns:
            对应的优先级枚举值

        Raises:
            ValueError: 无效的前缀
        """
        prefix_map = {
            "Tar": cls.TAR,
            "Sar": cls.SAR,
            "Var": cls.VAR,
            "Sys": cls.SYS,
        }

        prefix_normalized = prefix.capitalize()
        if prefix_normalized not in prefix_map:
            raise ValueError(
                f"Invalid variable prefix: {prefix}. "
                f"Must be one of: {list(prefix_map.keys())}"
            )

        return prefix_map[prefix_normalized]

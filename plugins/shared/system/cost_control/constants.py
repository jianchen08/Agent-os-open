"""
成本控制相关常量

从 0.1 src/core/constants.py 提取，仅保留 CostControl 部分。
"""


class CostControl:
    """成本控制相关常量"""

    WARNING_THRESHOLD = 0.80
    CRITICAL_THRESHOLD = 0.90
    EXHAUSTED_THRESHOLD = 1.0
    DAILY_TOKEN_LIMIT = 10**12
    MONTHLY_TOKEN_LIMIT = 10**15

"""
成本控制默认表

单点默认（B2 收敛）：与设置页真值 yaml（manifest ``config_files[id=cost]``
声明，server.py ``_COST_CONTROL_YAML`` 同源文件）及其缺省回落
（server.py ``_DEFAULT_COST_CONTROL``）同值同语义——限额为 token 数、
告警阈值为百分刻度(0-100)。执法面模型（config.py AlertThresholds）
持有小数刻度(0-1)，百分→小数折算在该模型单点完成。
"""


class CostControl:
    """成本控制相关常量"""

    WARNING_THRESHOLD_PERCENT = 70
    CRITICAL_THRESHOLD_PERCENT = 90
    EXHAUSTED_THRESHOLD_PERCENT = 100
    DAILY_TOKEN_LIMIT = 1_000_000
    MONTHLY_TOKEN_LIMIT = 30_000_000
    PER_TASK_TOKEN_LIMIT = 200_000
    PER_SESSION_TOKEN_LIMIT = 500_000

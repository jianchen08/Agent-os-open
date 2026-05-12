"""
进度计算器

提供统一的进度计算逻辑，确保所有服务使用相同的计算公式。

核心功能：
1. 统一进度计算入口
2. 标准化进度数据结构
3. 支持从 acceptance_criteria 或 Task 对象计算

使用方式：
    >>> calculator = ProgressCalculator()
    >>> progress = calculator.calculate(acceptance_criteria)
    >>> print(progress.progress_percent)
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ProgressInfo:
    """
    进度信息数据类

    Attributes:
        total_criteria: 总标准数
        passed_criteria: 通过数
        failed_criteria: 失败数
        pending_criteria: 待评估数
        progress_percent: 进度百分比
    """

    total_criteria: int
    passed_criteria: int
    failed_criteria: int
    pending_criteria: int
    progress_percent: float

    def to_dict(self) -> dict[str, Any]:
        """
        转换为字典格式

        Returns:
            进度信息字典
        """
        return {
            "total_criteria": self.total_criteria,
            "passed_criteria": self.passed_criteria,
            "failed_criteria": self.failed_criteria,
            "pending_criteria": self.pending_criteria,
            "progress_percent": self.progress_percent,
        }


class ProgressCalculator:
    """
    进度计算器 - 唯一计算入口

    统一所有进度计算逻辑，确保公式一致性。

    计算公式：
        - total = len(acceptance_criteria)
        - passed = count(status == 'passed')
        - failed = count(status == 'failed')
        - pending = total - passed - failed
        - percent = (passed / total * 100) if total > 0 else 0

    Example:
        >>> calculator = ProgressCalculator()
        >>> criteria = [
        ...     {"status": "passed"},
        ...     {"status": "failed"},
        ...     {"status": "pending"},
        ... ]
        >>> progress = calculator.calculate(criteria)
        >>> print(progress.progress_percent)  # 33.33
    """

    def calculate(self, acceptance_criteria: list[dict[str, Any]]) -> ProgressInfo:
        """
        统一进度计算

        从 acceptance_criteria 数组中计算进度信息。

        Args:
            acceptance_criteria: 验收标准列表，每个元素包含：
                - status: 状态（pending/passed/failed）
                - retry_count: 重试次数（可选）
                - metric_id: 指标 ID（可选）

        Returns:
            ProgressInfo 进度信息对象
        """
        if not acceptance_criteria:
            return ProgressInfo(
                total_criteria=0,
                passed_criteria=0,
                failed_criteria=0,
                pending_criteria=0,
                progress_percent=0.0,
            )

        total = len(acceptance_criteria)
        passed = sum(1 for ac in acceptance_criteria if ac.get("status") == "passed")
        failed = sum(1 for ac in acceptance_criteria if ac.get("status") == "failed")

        return ProgressInfo(
            total_criteria=total,
            passed_criteria=passed,
            failed_criteria=failed,
            pending_criteria=total - passed - failed,
            progress_percent=round((passed / total * 100) if total > 0 else 0, 2),
        )

    def calculate_from_task(self, task: Any) -> ProgressInfo:
        """
        从 Task 对象计算进度

        Args:
            task: 任务对象，需包含 acceptance_criteria 属性

        Returns:
            ProgressInfo 进度信息对象
        """
        acceptance_criteria = getattr(task, "acceptance_criteria", None) or []
        return self.calculate(acceptance_criteria)


# 全局单例实例
_progress_calculator: ProgressCalculator | None = None


def get_progress_calculator() -> ProgressCalculator:
    """
    获取进度计算器单例

    Returns:
        ProgressCalculator 实例
    """
    global _progress_calculator
    if _progress_calculator is None:
        _progress_calculator = ProgressCalculator()
    return _progress_calculator

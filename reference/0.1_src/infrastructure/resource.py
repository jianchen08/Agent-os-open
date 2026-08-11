"""资源管理器 — 简化版配额检查。

精简原则：
- 保留 L1/L2/L3 配额检查（确实需要限制各级别管道数量）
- 去掉 psutil CPU/内存检查
- 去掉数据库查询
"""

from __future__ import annotations


class ResourceQuota:
    """资源配额。

    定义单个管道类型的最大实例数和最大迭代次数。

    Attributes:
        max_pipelines: 该类型允许的最大管道实例数
        max_iterations: 单个管道的最大迭代次数
    """

    def __init__(self, max_pipelines: int = 10, max_iterations: int = 500) -> None:
        """初始化资源配额。

        Args:
            max_pipelines: 最大管道实例数，默认 10
            max_iterations: 最大迭代次数，默认 500
        """
        self.max_pipelines = max_pipelines
        self.max_iterations = max_iterations


class ResourceManager:
    """资源管理器 — 简化版配额检查。

    管理不同类型管道的配额和活跃实例数，
    通过 can_create 检查是否允许创建新管道，
    通过 register/release 维护活跃计数。

    Attributes:
        _quotas: 管道类型到配额的映射
        _active_counts: 管道类型到当前活跃数的映射
    """

    def __init__(self, quotas: dict[str, ResourceQuota] | None = None) -> None:
        """初始化资源管理器。

        Args:
            quotas: 管道类型到配额的映射，未指定时使用默认配额
        """
        self._quotas: dict[str, ResourceQuota] = quotas or {"default": ResourceQuota()}
        self._active_counts: dict[str, int] = {}

    def can_create(self, pipeline_type: str = "default") -> bool:
        """检查是否可以创建新管道。

        Args:
            pipeline_type: 管道类型，未找到时使用 "default" 配额

        Returns:
            当前活跃数未超过配额时返回 True
        """
        quota = self._quotas.get(pipeline_type, self._quotas["default"])
        current = self._active_counts.get(pipeline_type, 0)
        return current < quota.max_pipelines

    def register(self, pipeline_type: str = "default") -> None:
        """注册新管道，递增活跃计数。

        Args:
            pipeline_type: 管道类型
        """
        self._active_counts[pipeline_type] = self._active_counts.get(pipeline_type, 0) + 1

    def release(self, pipeline_type: str = "default") -> None:
        """释放管道，递减活跃计数（不低于 0）。

        Args:
            pipeline_type: 管道类型
        """
        if pipeline_type in self._active_counts:
            self._active_counts[pipeline_type] = max(0, self._active_counts[pipeline_type] - 1)

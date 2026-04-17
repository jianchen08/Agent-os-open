"""任务依赖验证器

在任务提交前验证依赖关系的合法性，包括：
- 自我依赖检测
- 依赖任务存在性检查
- 循环依赖检测
- 跨父任务依赖检查

暴露接口：
- DependencyValidator：依赖验证器类
- validate_task_dependencies()：便捷验证函数
- ValidationResult：验证结果数据类
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from tasks.service import TaskService

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """验证结果。

    Attributes:
        is_valid: 是否验证通过
        errors: 错误信息列表
        warnings: 警告信息列表
    """

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.is_valid

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class DependencyValidator:
    """任务依赖验证器。

    通过注入 TaskService 替代数据库查询，
    不再依赖 ORM 和 async_session。

    Args:
        task_service: 任务服务实例，用于查询任务数据
    """

    def __init__(self, task_service: TaskService) -> None:
        """初始化验证器。

        Args:
            task_service: 任务服务实例
        """
        self._task_service = task_service

    async def validate(
        self,
        task_id: str,
        dependencies: list[str],
        parent_task_id: str | None = None,
    ) -> ValidationResult:
        """验证任务的依赖关系。

        依次检查：自我依赖、重复依赖、依赖存在性、循环依赖、父任务依赖。

        Args:
            task_id: 当前任务ID
            dependencies: 依赖的任务ID列表
            parent_task_id: 父任务ID

        Returns:
            验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []

        if task_id in dependencies:
            errors.append(f"任务不能依赖自己: task_id='{task_id}'")
            return ValidationResult(is_valid=False, errors=errors)

        unique_deps = list(set(dependencies))
        if len(unique_deps) != len(dependencies):
            duplicates = [dep for dep in unique_deps if dependencies.count(dep) > 1]
            warnings.append(f"检测到重复依赖，已自动去重: {duplicates}")
            dependencies = unique_deps

        if dependencies:
            missing_tasks = self._check_existence(dependencies)
            if missing_tasks:
                errors.append(f"依赖的任务不存在: {missing_tasks}")

        if errors:
            return ValidationResult(is_valid=False, errors=errors, warnings=warnings)

        has_cycle = self._check_cycle(task_id, dependencies, parent_task_id)
        if has_cycle:
            errors.append("检测到循环依赖")

        if parent_task_id and parent_task_id in dependencies:
            errors.append(f"任务不能依赖其父任务: parent_task_id='{parent_task_id}'")

        is_valid = len(errors) == 0
        return ValidationResult(is_valid=is_valid, errors=errors, warnings=warnings)

    def _check_existence(self, task_ids: list[str]) -> list[str]:
        """检查依赖任务是否存在。

        通过 TaskService 查询替代数据库 SQL 查询。

        Args:
            task_ids: 要检查的任务ID列表

        Returns:
            不存在的任务ID列表
        """
        if not task_ids:
            return []

        missing = []
        for tid in task_ids:
            task = self._task_service.get_task(tid)
            if task is None:
                missing.append(tid)
        return missing

    def _check_cycle(
        self,
        task_id: str,
        dependencies: list[str],
        parent_task_id: str | None = None,
    ) -> bool:
        """检查是否存在循环依赖。

        通过 TaskService 获取任务的依赖列表构建依赖图，
        使用 DFS 检测循环。

        Args:
            task_id: 当前任务ID
            dependencies: 当前任务的依赖列表
            parent_task_id: 父任务ID

        Returns:
            是否存在循环依赖
        """
        if not dependencies:
            return False

        all_task_ids = set([task_id] + dependencies)
        if parent_task_id:
            all_task_ids.add(parent_task_id)

        task_deps: dict[str, list[str]] = {}
        for tid in all_task_ids:
            task = self._task_service.get_task(tid)
            task_deps[tid] = task.dependencies if task and task.dependencies else []

        task_deps[task_id] = dependencies

        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(all_task_ids, WHITE)

        def dfs(node: str) -> bool:
            """深度优先搜索检测回边"""
            color[node] = GRAY
            for dep in task_deps.get(node, []):
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    return True
                if color[dep] == WHITE and dfs(dep):
                    return True
            color[node] = BLACK
            return False

        if color[task_id] == WHITE:
            if dfs(task_id):
                return True

        return False

    async def validate_dependencies_in_family(
        self,
        task_id: str,
        dependencies: list[str],
        parent_task_id: str | None = None,
    ) -> ValidationResult:
        """验证依赖关系是否在同一个任务家族中。

        检查所有依赖任务是否都有相同的父任务。

        Args:
            task_id: 当前任务ID
            dependencies: 依赖的任务ID列表
            parent_task_id: 父任务ID

        Returns:
            验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []

        if not dependencies or not parent_task_id:
            return ValidationResult(is_valid=True, errors=errors, warnings=warnings)

        for dep_id in dependencies:
            dep_task = self._task_service.get_task(dep_id)
            if dep_task and dep_task.parent_task_id != parent_task_id:
                warnings.append(
                    f"依赖任务 '{dep_id}' 的父任务与当前任务不同: "
                    f"'{dep_task.parent_task_id}' != '{parent_task_id}'"
                )

        return ValidationResult(is_valid=True, errors=errors, warnings=warnings)


async def validate_task_dependencies(
    task_service: TaskService,
    task_id: str,
    dependencies: list[str],
    parent_task_id: str | None = None,
) -> ValidationResult:
    """便捷函数：验证任务依赖关系。

    Args:
        task_service: 任务服务实例
        task_id: 当前任务ID
        dependencies: 依赖的任务ID列表
        parent_task_id: 父任务ID

    Returns:
        验证结果
    """
    validator = DependencyValidator(task_service)
    return await validator.validate(task_id, dependencies, parent_task_id)

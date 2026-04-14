"""
任务依赖验证器

暴露接口：
- to_dict(self) -> dict[str, Any]：to_dict功能
- dfs(node: str) -> bool：dfs功能
- ValidationResult：ValidationResult类
- DependencyValidator：DependencyValidator类
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Task

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """
    验证结果

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
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class DependencyValidator:
    """
    任务依赖验证器

    在任务提交前验证依赖关系的合法性
    """

    def __init__(self, session: AsyncSession):
        """初始化验证器"""
        self.session = session

    async def validate(
        self,
        task_id: str,
        dependencies: list[str],
        parent_task_id: str | None = None,
    ) -> ValidationResult:
        """验证任务的依赖关系"""
        errors = []
        warnings = []

        # 1. 检查自我依赖
        if task_id in dependencies:
            errors.append(f"任务不能依赖自己: task_id='{task_id}'")
            return ValidationResult(is_valid=False, errors=errors)

        # 2. 检查重复依赖
        unique_deps = list(set(dependencies))
        if len(unique_deps) != len(dependencies):
            duplicates = [dep for dep in unique_deps if dependencies.count(dep) > 1]
            warnings.append(f"检测到重复依赖，已自动去重: {duplicates}")
            dependencies = unique_deps

        # 3. 检查依赖任务是否存在
        if dependencies:
            missing_tasks = await self._check_existence(dependencies)
            if missing_tasks:
                errors.append(f"依赖的任务不存在: {missing_tasks}")

        # 4. 检查循环依赖（如果有数据库中的任务）
        if errors:
            # 如果有错误，直接返回
            return ValidationResult(is_valid=False, errors=errors, warnings=warnings)

        # 循环依赖检测需要检查整个依赖链
        has_cycle = await self._check_cycle(task_id, dependencies, parent_task_id)
        if has_cycle:
            errors.append("检测到循环依赖")

        # 5. 检查是否依赖父任务
        if parent_task_id and parent_task_id in dependencies:
            errors.append(f"任务不能依赖其父任务: parent_task_id='{parent_task_id}'")

        is_valid = len(errors) == 0
        return ValidationResult(is_valid=is_valid, errors=errors, warnings=warnings)

    async def _check_existence(self, task_ids: list[str]) -> list[str]:
        """检查任务是否存在"""
        if not task_ids:
            return []

        # 查询数据库中的任务
        result = await self.session.execute(
            select(Task.id).where(Task.id.in_(task_ids))
        )
        existing_ids = {row[0] for row in result.fetchall()}

        # 找出不存在的任务
        missing = [task_id for task_id in task_ids if task_id not in existing_ids]
        return missing

    async def _check_cycle(
        self,
        task_id: str,
        dependencies: list[str],
        parent_task_id: str | None = None,
    ) -> bool:
        """检查是否存在循环依赖"""
        if not dependencies:
            return False

        # 构建依赖图
        # 包含：当前任务 + 依赖任务 + 它们的依赖
        all_task_ids = set([task_id] + dependencies)
        if parent_task_id:
            all_task_ids.add(parent_task_id)

        # 从数据库获取所有相关任务的依赖
        result = await self.session.execute(
            select(Task.id, Task.dependencies).where(Task.id.in_(all_task_ids))
        )
        task_deps = {row[0]: (row[1] or []) for row in result.fetchall()}

        # 添加当前任务（尚未保存到数据库）
        task_deps[task_id] = dependencies

        # DFS 检测循环
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(all_task_ids, WHITE)

        def dfs(node: str) -> bool:
            color[node] = GRAY
            for dep in task_deps.get(node, []):
                # 跳过不在图中的依赖
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    return True  # 发现回边，存在循环
                if color[dep] == WHITE and dfs(dep):
                    return True
            color[node] = BLACK
            return False

        # 从当前任务开始检测
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
        """验证依赖关系是否在同一个任务家族中"""
        errors = []
        warnings = []

        if not dependencies or not parent_task_id:
            return ValidationResult(is_valid=True, errors=errors, warnings=warnings)

        # 检查所有依赖任务是否都有相同的父任务
        result = await self.session.execute(
            select(Task.id, Task.parent_task_id).where(Task.id.in_(dependencies))
        )

        for dep_id, dep_parent_id in result.fetchall():
            if dep_parent_id != parent_task_id:
                warnings.append(
                    f"依赖任务 '{dep_id}' 的父任务与当前任务不同: "
                    f"'{dep_parent_id}' != '{parent_task_id}'"
                )

        return ValidationResult(is_valid=True, errors=errors, warnings=warnings)


async def validate_task_dependencies(
    session: AsyncSession,
    task_id: str,
    dependencies: list[str],
    parent_task_id: str | None = None,
) -> ValidationResult:
    """便捷函数：验证任务依赖关系"""
    validator = DependencyValidator(session)
    return await validator.validate(task_id, dependencies, parent_task_id)

"""
权限检查器

暴露接口：
- check_write_permission(path: str, workspace: str | None, policy: WorkspacePermissionPolicy | dict[str, Any], project_root: str, operation: str) -> tuple[bool, str]：check_write_permission功能
- check_read_permission(self, path: str, workspace: str | None, policy: WorkspacePermissionPolicy) -> tuple[bool, str]：check_read_permission功能
- check_write_permission(self, path: str, workspace: str | None, policy: WorkspacePermissionPolicy, operation: str) -> tuple[bool, str]：check_write_permission功能
- is_path_in_workspace(self, path: str, workspace: str) -> tuple[bool, str | None]：is_path_in_workspace功能
- get_project_root(self) -> str：get_project_root功能
- resolve_path(self, path: str) -> str：resolve_path功能
- PermissionChecker：PermissionChecker类
"""

import logging
import os
from pathlib import Path
from typing import Any

from isolation.permission_policy import (
    PermissionScope,
    WorkspacePermissionPolicy,
)

logger = logging.getLogger(__name__)


class PermissionChecker:
    """权限检查器

    检查文件操作是否符合权限策略，提供：
    - 读取权限检查
    - 写入权限检查
    - 路径标准化处理
    - 工作目录边界检查

    使用场景：
    - 文件操作前的权限验证
    - 命令执行的工作目录检查
    - 安全审计日志记录
    """

    def __init__(self, project_root: str = "."):
        """初始化权限检查器"""
        self._project_root = Path(project_root).resolve()

    def check_read_permission(  # noqa: PLR0911
        self,
        path: str,
        workspace: str | None,
        policy: WorkspacePermissionPolicy,
    ) -> tuple[bool, str]:
        """检查读取权限"""
        read_perm = policy.read

        # 检查权限范围
        if read_perm.scope == PermissionScope.NONE:
            return False, "当前策略禁止所有读取操作"

        if read_perm.scope == PermissionScope.PROJECT:
            # 允许读取整个项目
            return True, ""

        if read_perm.scope == PermissionScope.WORKSPACE:
            if not workspace:
                return False, "未指定工作目录，无法执行读取操作"

            # 检查路径是否在工作目录内
            is_inside, error = self.is_path_in_workspace(path, workspace)
            if not is_inside:
                return False, f"权限拒绝：路径 '{path}' 不在工作目录 '{workspace}' 内"
            return True, ""

        if read_perm.scope == PermissionScope.CUSTOM:
            # 检查自定义路径
            if read_perm.custom_paths:
                normalized_path = self._normalize_path(path)
                for custom_path in read_perm.custom_paths:
                    custom_full = self._normalize_path(custom_path)
                    if normalized_path.startswith(custom_full):
                        return True, ""
            return False, f"路径 '{path}' 不在允许的自定义路径列表中"

        return True, ""

    def check_write_permission(  # noqa: PLR0911
        self,
        path: str,
        workspace: str | None,
        policy: WorkspacePermissionPolicy,
        operation: str = "write",
    ) -> tuple[bool, str]:
        """检查写入权限"""
        write_perm = policy.write

        # 1. 检查权限范围
        if write_perm.scope == PermissionScope.NONE:
            return False, "当前策略禁止所有写入操作"

        if write_perm.scope == PermissionScope.PROJECT:
            # 允许写入整个项目
            if write_perm.require_confirmation:
                logger.info(f"[PermissionChecker] 写入操作需要用户确认 | path={path}")
            return True, ""

        if write_perm.scope == PermissionScope.WORKSPACE:
            if not workspace:
                return False, "未指定工作目录，无法执行写入操作"

            # 2. 标准化路径
            normalized_path = self._normalize_path(path)
            workspace_path = self._normalize_workspace(workspace)

            # 3. 检查是否在 workspace 内
            is_inside = self._is_path_inside(normalized_path, workspace_path)

            if not is_inside and not write_perm.allow_outside:
                error_msg = f"权限拒绝：无法在工作目录 '{workspace}' 之外执行写入操作。当前路径: '{path}'"
                logger.warning(f"[PermissionChecker] 写入权限检查失败 | path={path} | workspace={workspace}")
                return False, error_msg

            # 4. 检查是否需要检查点
            if write_perm.require_checkpoint:
                logger.info(f"[PermissionChecker] 写入操作需要创建检查点 | path={path} | workspace={workspace}")

            # 5. 检查允许的操作类型
            if write_perm.allowed_operations and operation not in write_perm.allowed_operations:
                return False, f"当前策略不允许执行 '{operation}' 操作"

            return True, ""

        if write_perm.scope == PermissionScope.CUSTOM:
            # 检查自定义路径
            if write_perm.custom_paths:
                normalized_path = self._normalize_path(path)
                for custom_path in write_perm.custom_paths:
                    custom_full = self._normalize_path(custom_path)
                    if normalized_path.startswith(custom_full):
                        return True, ""
            return False, f"路径 '{path}' 不在允许的自定义路径列表中"

        return False, "未知的权限范围"

    def is_path_in_workspace(
        self,
        path: str,
        workspace: str,
    ) -> tuple[bool, str | None]:
        """检查路径是否在工作目录内"""
        try:
            normalized_path = self._normalize_path(path)
            workspace_path = self._normalize_workspace(workspace)

            is_inside = self._is_path_inside(normalized_path, workspace_path)

            if is_inside:
                return True, None
            return False, f"路径 '{path}' 不在工作目录 '{workspace}' 内"

        except Exception as e:
            error_msg = f"路径检查失败: {str(e)}"
            logger.error(f"[PermissionChecker] 路径检查异常 | error={e}")
            return False, error_msg

    def _normalize_path(self, path: str) -> str:
        """标准化路径"""
        # 如果是相对路径，转换为绝对路径
        abs_path = (self._project_root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()  # noqa: PTH117

        # 使用 normpath 处理路径分隔符
        normalized = os.path.normpath(str(abs_path))

        # Windows 下统一使用小写比较
        if os.name == "nt":
            return normalized.lower()

        return normalized

    def _normalize_workspace(self, workspace: str) -> str:
        """标准化工作目录路径"""
        workspace_path = (self._project_root / workspace).resolve()
        normalized = os.path.normpath(str(workspace_path))

        # Windows 下统一使用小写比较
        if os.name == "nt":
            return normalized.lower()

        return normalized

    def _is_path_inside(self, path: str, workspace_path: str) -> bool:
        """检查路径是否在工作目录内"""
        # 路径完全相同
        if path == workspace_path:
            return True

        # 路径以工作目录开头（需要路径分隔符）
        # 例如: workspace_path = "C:\\project\\src\\auth"
        #       path = "C:\\project\\src\\auth\\file.py"
        return bool(path.startswith(workspace_path + os.sep))

    def get_project_root(self) -> str:
        """获取项目根目录"""
        return str(self._project_root)

    def resolve_path(self, path: str) -> str:
        """解析路径为绝对路径"""
        if os.path.isabs(path):  # noqa: PTH117
            return os.path.normpath(path)
        return str((self._project_root / path).resolve())


def check_write_permission(
    path: str,
    workspace: str | None,
    policy: WorkspacePermissionPolicy | dict[str, Any],
    project_root: str = ".",
    operation: str = "write",
) -> tuple[bool, str]:
    """检查写入权限（便捷函数）"""
    checker = PermissionChecker(project_root)

    # 如果传入的是字典，转换为策略对象
    if isinstance(policy, dict):
        from isolation.permission_policy import (  # noqa: PLC0415
            PermissionPolicyType,
            ReadPermission,
            WritePermission,
        )

        read_config = policy.get("read", {})
        write_config = policy.get("write", {})

        policy_obj = WorkspacePermissionPolicy(
            name=policy.get("name", "custom"),
            policy_type=PermissionPolicyType(policy.get("policy_type", "default")),
            read=ReadPermission(
                scope=PermissionScope(read_config.get("scope", "project")),
                allow_all=read_config.get("allow_all", True),
            ),
            write=WritePermission(
                scope=PermissionScope(write_config.get("scope", "workspace")),
                allow_outside=write_config.get("allow_outside", False),
                require_checkpoint=write_config.get("require_checkpoint", False),
            ),
            description=policy.get("description", ""),
        )
        return checker.check_write_permission(path, workspace, policy_obj, operation)

    return checker.check_write_permission(path, workspace, policy, operation)

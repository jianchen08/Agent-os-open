"""
工具权限管理服务

提供工具权限管理功能，包括用户权限、角色权限和权限模板管理。
"""

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class ToolPermissionManager:
    """工具权限管理器"""

    def __init__(self):
        self.user_permissions: dict[str, set[str]] = {}
        self.role_permissions: dict[str, set[str]] = {}
        self.tool_permissions: dict[str, set[str]] = {}
        self.permission_templates: dict[str, set[str]] = {}
        self.audit_log: list[dict[str, Any]] = []

    def add_user_permission(self, user_id: str, permission: str):
        """添加用户权限"""
        if user_id not in self.user_permissions:
            self.user_permissions[user_id] = set()
        self.user_permissions[user_id].add(permission)

        # 记录审计日志
        self._log_permission_change("add_user_permission", user_id, permission)

    def remove_user_permission(self, user_id: str, permission: str):
        """移除用户权限"""
        if user_id in self.user_permissions:
            self.user_permissions[user_id].discard(permission)
            self._log_permission_change("remove_user_permission", user_id, permission)

    def add_role_permission(self, role: str, permission: str):
        """添加角色权限"""
        if role not in self.role_permissions:
            self.role_permissions[role] = set()
        self.role_permissions[role].add(permission)

        # 记录审计日志
        self._log_permission_change("add_role_permission", role, permission)

    def remove_role_permission(self, role: str, permission: str):
        """移除角色权限"""
        if role in self.role_permissions:
            self.role_permissions[role].discard(permission)
            self._log_permission_change("remove_role_permission", role, permission)

    def set_tool_permissions(self, tool_name: str, permissions: list[str]):
        """设置工具所需权限"""
        self.tool_permissions[tool_name] = set(permissions)

    def create_permission_template(self, template_name: str, permissions: list[str]):
        """创建权限模板"""
        self.permission_templates[template_name] = set(permissions)

    def apply_permission_template(self, user_id: str, template_name: str):
        """应用权限模板到用户"""
        if template_name in self.permission_templates:
            if user_id not in self.user_permissions:
                self.user_permissions[user_id] = set()
            self.user_permissions[user_id].update(self.permission_templates[template_name])
            self._log_permission_change("apply_template", user_id, template_name)

    def check_permission(self, user_id: str, tool_name: str, user_roles: list[str] = None) -> bool:
        """检查用户是否有权限使用工具"""
        if user_roles is None:
            user_roles = []

        # 获取工具所需权限
        required_permissions = self.tool_permissions.get(tool_name, set())
        if not required_permissions:
            return True  # 无权限要求的工具默认允许

        # 检查用户直接权限
        user_perms = self.user_permissions.get(user_id, set())
        if required_permissions.issubset(user_perms):
            return True

        # 检查角色权限
        for role in user_roles:
            role_perms = self.role_permissions.get(role, set())
            if required_permissions.issubset(role_perms):
                return True

        return False

    def get_user_permissions(self, user_id: str) -> set[str]:
        """获取用户所有权限"""
        return self.user_permissions.get(user_id, set()).copy()

    def get_role_permissions(self, role: str) -> set[str]:
        """获取角色权限"""
        return self.role_permissions.get(role, set()).copy()

    def get_effective_permissions(self, user_id: str, user_roles: list[str] = None) -> set[str]:
        """获取用户有效权限（包括角色继承）"""
        if user_roles is None:
            user_roles = []

        effective_perms = self.user_permissions.get(user_id, set()).copy()

        # 添加角色权限
        for role in user_roles:
            role_perms = self.role_permissions.get(role, set())
            effective_perms.update(role_perms)

        return effective_perms

    def get_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取权限审计日志"""
        return self.audit_log[-limit:]

    def _log_permission_change(self, action: str, target: str, permission: str):
        """记录权限变更日志"""
        self.audit_log.append(
            {
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "target": target,
                "permission": permission,
            }
        )

        # 保持日志在合理范围内
        if len(self.audit_log) > 1000:
            self.audit_log = self.audit_log[-500:]

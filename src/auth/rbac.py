"""
RBAC 权限控制

基于角色的访问控制实现
"""

from enum import Enum

from src.core.exceptions import PermissionDeniedError


class Permission(str, Enum):
    """权限枚举"""

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"


class Role(str, Enum):
    """角色枚举"""

    GUEST = "guest"
    USER = "user"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


# 角色权限定义
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.GUEST: {Permission.READ},
    Role.USER: {Permission.READ, Permission.WRITE},
    Role.ADMIN: {Permission.READ, Permission.WRITE, Permission.DELETE},
    Role.SUPER_ADMIN: {
        Permission.READ,
        Permission.WRITE,
        Permission.DELETE,
        Permission.ADMIN,
    },
}

# 角色继承关系
ROLE_INHERITANCE: dict[Role, list[Role]] = {
    Role.GUEST: [],
    Role.USER: [Role.GUEST],
    Role.ADMIN: [Role.USER, Role.GUEST],
    Role.SUPER_ADMIN: [Role.ADMIN, Role.USER, Role.GUEST],
}


class RBACManager:
    """RBAC 权限管理器"""

    def __init__(self):
        """初始化权限管理器"""
        # 资源级别权限: {resource: {role: {permissions}}}
        self._resource_permissions: dict[str, dict[Role, set[Permission]]] = {}

    def get_role_permissions(self, role: Role | str) -> set[Permission]:
        """
        获取角色的权限集合

        Args:
            role: 角色

        Returns:
            权限集合
        """
        role = self._normalize_role(role)
        permissions = set()

        # 收集角色自身及其继承的所有权限
        roles_to_check = [role] + ROLE_INHERITANCE.get(role, [])
        for r in roles_to_check:
            permissions.update(ROLE_PERMISSIONS.get(r, set()))

        return permissions

    def has_permission(
        self,
        role: Role | str,
        permission: Permission,
    ) -> bool:
        """
        检查角色是否有指定权限

        Args:
            role: 角色
            permission: 权限

        Returns:
            是否有权限
        """
        permissions = self.get_role_permissions(role)
        return permission in permissions

    def check_permission(
        self,
        role: Role | str,
        permission: Permission,
    ) -> None:
        """
        检查权限，无权限时抛出异常

        Args:
            role: 角色
            permission: 权限

        Raises:
            PermissionDeniedError: 权限不足
        """
        if not self.has_permission(role, permission):
            role = self._normalize_role(role)
            raise PermissionDeniedError(f"角色 '{role}' 没有 '{permission.value}' 权限")

    def add_resource_permission(
        self,
        resource: str,
        role: Role | str,
        permissions: list[Permission],
    ) -> None:
        """
        添加资源级别权限

        Args:
            resource: 资源名称
            role: 角色
            permissions: 权限列表
        """
        role = self._normalize_role(role)

        if resource not in self._resource_permissions:
            self._resource_permissions[resource] = {}

        if role not in self._resource_permissions[resource]:
            self._resource_permissions[resource][role] = set()

        self._resource_permissions[resource][role].update(permissions)

    def has_resource_permission(
        self,
        role: Role | str,
        resource: str,
        permission: Permission,
    ) -> bool:
        """
        检查角色是否有资源级别权限

        Args:
            role: 角色
            resource: 资源名称
            permission: 权限

        Returns:
            是否有权限
        """
        role = self._normalize_role(role)

        # 检查资源是否有权限配置
        if resource not in self._resource_permissions:
            return False

        # 检查角色自身及其继承角色的资源权限
        roles_to_check = [role] + ROLE_INHERITANCE.get(role, [])
        for r in roles_to_check:
            if r in self._resource_permissions[resource]:
                if permission in self._resource_permissions[resource][r]:
                    return True

        return False

    def check_resource_permission(
        self,
        role: Role | str,
        resource: str,
        permission: Permission,
    ) -> None:
        """
        检查资源权限，无权限时抛出异常

        Args:
            role: 角色
            resource: 资源名称
            permission: 权限

        Raises:
            PermissionDeniedError: 权限不足
        """
        if not self.has_resource_permission(role, resource, permission):
            role = self._normalize_role(role)
            raise PermissionDeniedError(
                f"角色 '{role}' 没有资源 '{resource}' 的 '{permission.value}' 权限"
            )

    def _normalize_role(self, role: Role | str) -> Role:
        """
        标准化角色

        Args:
            role: 角色（字符串或枚举）

        Returns:
            Role 枚举

        Raises:
            ValueError: 无效的角色
        """
        if isinstance(role, Role):
            return role

        try:
            return Role(role)
        except ValueError:
            raise ValueError(f"无效的角色: '{role}'")

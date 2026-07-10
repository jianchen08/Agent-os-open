"""资源×操作权限矩阵。

从 3 个粗粒度角色（admin/user/viewer）细化为资源级权限控制，
定义 资源×操作 的权限矩阵。

权限矩阵定义了每个资源上、每个角色允许执行的操作集合。
设计原则::

    GUEST       → 只读（部分资源无权限）
    USER        → 读 + 创建 + 更新（部分资源可执行）
    ADMIN       → 读 + 创建 + 更新 + 删除 + 管理
    SUPER_ADMIN → 全部操作

使用方式::

    from auth.permission_matrix import has_resource_action_permission, Resource, Action

    if not has_resource_action_permission(user.role, Resource.TASKS, Action.DELETE):
        raise PermissionDeniedError("权限不足")

也可通过 RBACManager 使用::

    manager.has_resource_action_permission(role, resource, action)
    manager.check_resource_action_permission(role, resource, action)
"""

from __future__ import annotations

from enum import Enum

from src.auth.rbac import Role

__all__ = [
    "Resource",
    "Action",
    "RESOURCE_PERMISSION_MATRIX",
    "has_resource_action_permission",
]


class Resource(str, Enum):
    """受控资源枚举——用于资源级细粒度权限控制。

    每个值对应一类 API 资源。
    """

    THREADS = "threads"
    TASKS = "tasks"
    AGENTS = "agents"
    TOOLS = "tools"
    CONFIG = "config"
    MEMORY = "memory"
    TRIGGERS = "triggers"
    EVALUATION = "evaluation"
    USERS = "users"
    WORKSPACES = "workspaces"
    PLUGINS = "plugins"
    REVIEWS = "reviews"
    MAINTENANCE = "maintenance"
    ARTIFACTS = "artifacts"


class Action(str, Enum):
    """操作枚举——用于资源级细粒度权限控制。"""

    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    MANAGE = "manage"
    EXECUTE = "execute"


# ── 权限快捷集合 ──────────────────────────────────────────

_ALL_ACTIONS: set[Action] = set(Action)
_READ: set[Action] = {Action.READ}
_RCU: set[Action] = {Action.READ, Action.CREATE, Action.UPDATE}
_RCUX: set[Action] = _RCU | {Action.EXECUTE}
_RCUDM: set[Action] = {
    Action.READ,
    Action.CREATE,
    Action.UPDATE,
    Action.DELETE,
    Action.MANAGE,
}
_RCUDMX: set[Action] = _RCUDM | {Action.EXECUTE}


# ── 资源×操作权限矩阵 ────────────────────────────────────

RESOURCE_PERMISSION_MATRIX: dict[Resource, dict[Role, set[Action]]] = {
    Resource.THREADS: {
        Role.GUEST: _READ,
        Role.USER: _RCU,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.TASKS: {
        Role.GUEST: _READ,
        Role.USER: _RCUX,
        Role.ADMIN: _RCUDMX,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.AGENTS: {
        Role.GUEST: _READ,
        Role.USER: _READ,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.TOOLS: {
        Role.GUEST: _READ,
        Role.USER: _RCUX,
        Role.ADMIN: _RCUDMX,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.CONFIG: {
        Role.GUEST: _READ,
        Role.USER: _READ,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.MEMORY: {
        Role.GUEST: _READ,
        Role.USER: _RCU,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.TRIGGERS: {
        Role.GUEST: _READ,
        Role.USER: _RCU,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.EVALUATION: {
        Role.GUEST: _READ,
        Role.USER: _READ,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.USERS: {
        Role.GUEST: set(),
        Role.USER: _READ,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.WORKSPACES: {
        Role.GUEST: _READ,
        Role.USER: _RCU,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.PLUGINS: {
        Role.GUEST: _READ,
        Role.USER: _READ,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.REVIEWS: {
        Role.GUEST: _READ,
        Role.USER: _RCU,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.MAINTENANCE: {
        Role.GUEST: set(),
        Role.USER: set(),
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
    Resource.ARTIFACTS: {
        Role.GUEST: _READ,
        Role.USER: _RCU,
        Role.ADMIN: _RCUDM,
        Role.SUPER_ADMIN: _ALL_ACTIONS,
    },
}


def has_resource_action_permission(
    role: Role | str,
    resource: Resource,
    action: Action | str,
) -> bool:
    """检查角色是否对指定资源拥有指定操作权限。

    基于 RESOURCE_PERMISSION_MATRIX 判断。权限矩阵在设计时已确保
    高级角色的权限是低级角色的超集，因此只需检查角色自身的权限即可。

    Args:
        role: 用户角色（枚举或字符串）
        resource: 目标资源
        action: 目标操作（枚举或字符串）

    Returns:
        是否有权限
    """
    if isinstance(role, str):
        role = Role(role)
    if isinstance(action, str):
        action = Action(action)
    perms = RESOURCE_PERMISSION_MATRIX.get(resource, {}).get(role, set())
    return action in perms

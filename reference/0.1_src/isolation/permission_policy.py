"""
权限策略管理器

暴露接口：
- get_policy(self, policy_type: PermissionPolicyType | str) -> WorkspacePermissionPolicy：get_policy功能
- get_default_policy(self) -> WorkspacePermissionPolicy：get_default_policy功能
- get_readonly_policy(self) -> WorkspacePermissionPolicy：get_readonly_policy功能
- list_policies(self) -> list[str]：list_policies功能
- has_policy(self, policy_name: str) -> bool：has_policy功能
- PermissionScope：PermissionScope类
- PermissionPolicyType：PermissionPolicyType类
- ReadPermission：ReadPermission类
- WritePermission：WritePermission类
- WorkspacePermissionPolicy：WorkspacePermissionPolicy类
- PermissionPolicyManager：PermissionPolicyManager类
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class PermissionScope(str, Enum):
    """权限范围枚举

    定义文件操作的权限范围：
    - PROJECT: 整个项目目录
    - WORKSPACE: 仅指定的工作目录
    - CUSTOM: 自定义路径列表
    - NONE: 无权限
    """

    PROJECT = "project"
    WORKSPACE = "workspace"
    CUSTOM = "custom"
    NONE = "none"


class PermissionPolicyType(str, Enum):
    """权限策略类型枚举

    定义不同场景下的权限策略：
    - DEFAULT: 默认策略，读取整个项目，写入仅限工作目录
    - SUBTASK: 子任务策略，同 DEFAULT
    - SYSTEM_CONFIG: 系统配置策略，修改前需要检查点
    - READONLY: 只读策略，禁止所有写入
    """

    DEFAULT = "default"
    SUBTASK = "subtask"
    ROOT_TASK = "root_task"
    SYSTEM_CONFIG = "system_config"
    READONLY = "readonly"


@dataclass
class ReadPermission:
    """读取权限配置

    定义读取操作的权限规则

    Attributes:
        scope: 权限范围
        allow_all: 是否允许读取整个项目
        custom_paths: 自定义路径列表（当 scope=CUSTOM 时使用）
    """

    scope: PermissionScope
    allow_all: bool = True
    custom_paths: list[str] | None = None


@dataclass
class WritePermission:
    """写入权限配置

    定义写入操作的权限规则

    Attributes:
        scope: 权限范围
        allow_outside: 是否允许在工作目录外写入
        require_checkpoint: 是否需要创建检查点
        require_confirmation: 是否需要用户确认
        allowed_operations: 允许的操作类型列表（create/modify/delete）
        custom_paths: 自定义路径列表（当 scope=CUSTOM 时使用）
    """

    scope: PermissionScope
    allow_outside: bool = False
    require_checkpoint: bool = False
    require_confirmation: bool = False
    allowed_operations: list[str] | None = None
    custom_paths: list[str] | None = None


@dataclass
class WorkspacePermissionPolicy:
    """工作区权限策略

    定义完整的读写权限规则

    Attributes:
        name: 策略名称
        policy_type: 策略类型
        read: 读取权限配置
        write: 写入权限配置
        description: 策略描述
    """

    name: str
    policy_type: PermissionPolicyType
    read: ReadPermission
    write: WritePermission
    description: str = ""


class PermissionPolicyManager:
    """权限策略管理器

    管理不同场景下的权限策略，提供：
    - 默认策略定义
    - 策略获取接口
    - 策略配置加载（可选）

    使用场景：
    - 获取任务的权限策略
    - 检查文件操作权限
    - 配置特殊目录的权限规则
    """

    # 默认权限策略定义
    # 与 config/isolation/isolation_config.yaml 中的 permission_policies 保持同步
    DEFAULT_POLICIES: dict[str, dict[str, Any]] = {
        "default": {
            "read": {"scope": "project", "allow_all": True},
            "write": {"scope": "workspace", "allow_outside": False},
            "description": "默认策略：读取整个项目，写入仅限工作目录",
        },
        "subtask": {
            "read": {"scope": "project", "allow_all": True},
            "write": {"scope": "workspace", "allow_outside": False},
            "description": "子任务策略：同默认策略",
        },
        "root_task": {
            "read": {"scope": "project", "allow_all": True},
            "write": {
                "scope": "project",
                "allow_outside": True,
                "require_confirmation": True,
            },
            "description": "根任务策略：可读写整个项目，需用户确认",
        },
        "system_config": {
            "read": {"scope": "project", "allow_all": True},
            "write": {
                "scope": "workspace",
                "allow_outside": False,
                "require_checkpoint": True,
                "allowed_operations": ["create", "modify"],
            },
            "description": "系统配置策略：修改前需要创建检查点",
        },
        "readonly": {
            "read": {"scope": "project", "allow_all": True},
            "write": {"scope": "none", "allow_outside": False},
            "description": "只读策略：禁止所有写入操作",
        },
    }

    def __init__(self, custom_policies: dict[str, dict[str, Any]] | None = None):
        """初始化权限策略管理器。

        策略加载优先级：isolation_config.yaml 的 permission_policies 段 >
        custom_policies 参数 > 代码内 DEFAULT_POLICIES 兜底。

        Args:
            custom_policies: 可选的自定义策略字典，优先级高于代码默认值但低于配置文件。
        """
        self._policies: dict[str, WorkspacePermissionPolicy] = {}
        self._load_default_policies()

        # 尝试从 isolation_config.yaml 加载，覆盖代码默认值
        self._load_from_config_file()

        # 加载自定义策略（最高优先级的代码传入值）
        if custom_policies:
            self._load_custom_policies(custom_policies)

    def _load_default_policies(self) -> None:
        """加载默认策略"""
        for policy_name, policy_config in self.DEFAULT_POLICIES.items():
            policy = self._create_policy_from_config(policy_name, policy_config, PermissionPolicyType(policy_name))
            self._policies[policy_name] = policy

        logger.info(f"[PermissionPolicyManager] 默认策略已加载 | count={len(self._policies)}")

    def _load_custom_policies(self, custom_policies: dict[str, dict[str, Any]]) -> None:
        """加载自定义策略"""
        for policy_name, policy_config in custom_policies.items():
            # 确定策略类型
            policy_type_str = policy_config.get("policy_type", policy_name)
            try:
                policy_type = PermissionPolicyType(policy_type_str)
            except ValueError:
                policy_type = PermissionPolicyType.DEFAULT

            policy = self._create_policy_from_config(policy_name, policy_config, policy_type)
            self._policies[policy_name] = policy

        logger.info(f"[PermissionPolicyManager] 自定义策略已加载 | count={len(custom_policies)}")

    def _load_from_config_file(self) -> None:
        """从 isolation_config.yaml 的 permission_policies 段加载策略。

        配置中的策略会覆盖同名代码默认策略。加载失败时静默回退到默认值。
        yaml 中策略键名（如 root_task_policy）会自动去掉 _policy 后缀
        作为策略名称（如 "root_task"）。
        """
        try:
            from config.config_center import get_config_center  # noqa: PLC0415

            config = get_config_center().get("isolation/isolation_config.yaml") or {}
            policies_section = config.get("permission_policies", {})
            if not policies_section:
                logger.debug("[PermissionPolicyManager] 配置文件中未找到 permission_policies，使用代码默认值")
                return

            loaded = 0
            for config_key, policy_config in policies_section.items():
                if not isinstance(policy_config, dict):
                    continue
                # 将 isolation_config.yaml 的键名（如 "root_task_policy"）
                # 映射为策略名称（"root_task"）
                name = config_key.replace("_policy", "")
                if name == config_key:
                    continue  # 跳过非策略段（如 special_directories）

                self._policies[name] = self._create_policy_from_config(
                    name,
                    policy_config,
                    PermissionPolicyType(policy_config.get("policy_type", "default")),
                )
                loaded += 1

            logger.info(
                f"[PermissionPolicyManager] 从配置文件加载策略完成 | loaded={loaded} | total={len(self._policies)}"
            )
        except Exception as e:
            logger.warning(f"[PermissionPolicyManager] 配置文件加载失败，使用代码默认策略 | error={e}")

    def _create_policy_from_config(
        self,
        name: str,
        config: dict[str, Any],
        policy_type: PermissionPolicyType,
    ) -> WorkspacePermissionPolicy:
        """从配置创建策略对象"""
        read_config = config.get("read", {})
        write_config = config.get("write", {})

        # 创建读取权限
        read_scope = PermissionScope(read_config.get("scope", "project"))
        read_permission = ReadPermission(
            scope=read_scope,
            allow_all=read_config.get("allow_all", True),
            custom_paths=read_config.get("custom_paths"),
        )

        # 创建写入权限
        write_scope = PermissionScope(write_config.get("scope", "workspace"))
        write_permission = WritePermission(
            scope=write_scope,
            allow_outside=write_config.get("allow_outside", False),
            require_checkpoint=write_config.get("require_checkpoint", False),
            require_confirmation=write_config.get("require_confirmation", False),
            allowed_operations=write_config.get("allowed_operations"),
            custom_paths=write_config.get("custom_paths"),
        )

        return WorkspacePermissionPolicy(
            name=name,
            policy_type=policy_type,
            read=read_permission,
            write=write_permission,
            description=config.get("description", ""),
        )

    def get_policy(self, policy_type: PermissionPolicyType | str) -> WorkspacePermissionPolicy:
        """获取指定类型的权限策略"""
        # 支持字符串或枚举
        policy_name = policy_type.value if isinstance(policy_type, PermissionPolicyType) else policy_type

        policy = self._policies.get(policy_name)
        if not policy:
            logger.warning(f"[PermissionPolicyManager] 策略不存在，返回默认策略 | requested={policy_name}")
            return self.get_default_policy()

        return policy

    def get_default_policy(self) -> WorkspacePermissionPolicy:
        """获取默认权限策略"""
        return self._policies.get(
            "default",
            self._create_policy_from_config(
                "default",
                self.DEFAULT_POLICIES["default"],
                PermissionPolicyType.DEFAULT,
            ),
        )

    def get_readonly_policy(self) -> WorkspacePermissionPolicy:
        """获取只读权限策略"""
        return self._policies.get(
            "readonly",
            self._create_policy_from_config(
                "readonly",
                self.DEFAULT_POLICIES["readonly"],
                PermissionPolicyType.READONLY,
            ),
        )

    def list_policies(self) -> list[str]:
        """列出所有可用策略"""
        return list(self._policies.keys())

    def has_policy(self, policy_name: str) -> bool:
        """检查策略是否存在"""
        return policy_name in self._policies

    @staticmethod
    def get_policy_name_for_agent_level(agent_level: int | str | None) -> str:
        """根据 agent 层级返回对应的权限策略名称。

        L1 / 缺省 → root_task（按 root_task_policy: allow_outside=true, read.scope=project）
        L2+ → subtask（按 subtask_policy: allow_outside=false, write restrict workspace）

        Args:
            agent_level: parent_agent_level 值（1=主agent, 2+=子任务, None=缺省按L1处理）

        Returns:
            策略名称字符串（"root_task" / "subtask"）
        """
        if agent_level is None:
            return "root_task"
        try:
            level = int(str(agent_level).upper().lstrip("L"))
        except (ValueError, TypeError):
            return "root_task"
        return "root_task" if level <= 1 else "subtask"


def get_policy_name_for_agent_level(agent_level: int | str | None) -> str:
    """便捷函数：根据 agent 层级返回策略名称。"""
    return PermissionPolicyManager.get_policy_name_for_agent_level(agent_level)

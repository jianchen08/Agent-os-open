"""Agent 层级控制器

三层 Agent 双层指挥系统：
- L1 主 Agent: 全局调度者，管理 Project 和 Task
- L2 Sub Agent: 任务调度者，分解任务并调度 L3
- L3 执行 Agent: 纯粹执行者，不调度、不澄清
"""

import logging
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path  # noqa: F401
from typing import Any, TypedDict

import yaml

logger = logging.getLogger(__name__)


class AgentLevel(IntEnum):
    """Agent 层级"""

    L1 = 1  # 主 Agent
    L2 = 2  # Sub Agent
    L3 = 3  # 执行 Agent


class ValidationError(str, Enum):
    """验证错误类型"""

    INVALID_LEVEL = "INVALID_LEVEL"
    MAX_DEPTH_EXCEEDED = "MAX_DEPTH_EXCEEDED"
    CANNOT_SUBMIT_TASK = "CANNOT_SUBMIT_TASK"
    INVALID_TARGET_LEVEL = "INVALID_TARGET_LEVEL"


class LevelConfigError(Exception):
    """层级配置错误"""


class ToolPermissionError(Exception):
    """工具权限错误"""


@dataclass
class ValidationResult:
    """验证结果"""

    passed: bool
    error_code: str | None = None
    error_message: str | None = None


class LevelConfig(TypedDict):
    """层级配置类型"""

    name: str
    max_depth: int
    can_submit: bool
    can_submit_to: list[AgentLevel]
    can_create_task: bool
    can_manage_project: bool


class LevelController:
    """
    Agent 层级控制器

    控制三层 Agent 的工具访问权限和任务提交权限
    """

    # 层级配置（精简版）
    LEVEL_CONFIGS: dict[AgentLevel, LevelConfig] = {
        AgentLevel.L1: {
            "name": "主 Agent",
            "max_depth": 3,
            "can_submit": True,
            "can_submit_to": [AgentLevel.L2, AgentLevel.L3],  # L1 可以提交给 L2 和 L3
            "can_create_task": True,
            "can_manage_project": True,
        },
        AgentLevel.L2: {
            "name": "Sub Agent",
            "max_depth": 2,
            "can_submit": True,
            "can_submit_to": [AgentLevel.L3],  # L2 只能提交给 L3
            "can_create_task": True,
            "can_manage_project": False,
        },
        AgentLevel.L3: {
            "name": "执行 Agent",
            "max_depth": 1,
            "can_submit": False,
            "can_submit_to": [],  # L3 不能提交任务
            "can_create_task": False,
            "can_manage_project": False,
        },
    }

    # 默认 L3 不能使用的工具（可被配置文件覆盖）
    DEFAULT_RESTRICTED_TOOLS = {"task_submit", "task_evaluate"}

    # 工具权限配置文件路径
    TOOL_PERMISSIONS_CONFIG = "config/tool_permissions.yaml"

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        self._tool_permissions = self._load_tool_permissions()

    def _load_tool_permissions(self) -> dict[str, Any]:
        """
        从配置文件加载工具权限配置

        Returns:
            工具权限配置字典
        """
        try:
            from config.config_center import get_config_center  # noqa: PLC0415

            config = get_config_center().get("tool_permissions.yaml") or {}
            if not config:
                self.logger.info("工具权限配置文件不存在，使用默认配置")
                return self._get_default_permissions()
            return config.get("tool_permissions", {})
        except ImportError as e:
            self.logger.info("config_center 未安装，使用默认工具权限: %s", e)
            return self._get_default_permissions()
        except yaml.YAMLError as e:
            self.logger.warning("工具权限配置文件解析失败: %s", e)
            return self._get_default_permissions()
        except OSError as e:
            self.logger.warning("工具权限配置文件读取失败: %s", e)
            return self._get_default_permissions()

    def _get_default_permissions(self) -> dict[str, Any]:
        """获取默认工具权限配置"""
        return {
            "L1": {"allowed": ["*"]},
            "L2": {"allowed": ["*"]},
            "L3": {"denied": list(self.DEFAULT_RESTRICTED_TOOLS)},
        }

    def can_submit_task(self, agent_level: int) -> bool:
        """判断是否可以提交子任务"""
        try:
            level = AgentLevel(agent_level)
            config = self.LEVEL_CONFIGS.get(level)
            return config["can_submit"] if config else False
        except ValueError:
            return False

    def get_allowed_targets(self, agent_level: int) -> list[int]:
        """获取允许提交的目标层级"""
        try:
            level = AgentLevel(agent_level)
            config = self.LEVEL_CONFIGS.get(level)
            if config:
                return [t.value for t in config["can_submit_to"]]
            return []
        except ValueError:
            return []

    def get_max_depth(self, agent_level: int) -> int:
        """获取最大嵌套深度"""
        try:
            level = AgentLevel(agent_level)
            config = self.LEVEL_CONFIGS.get(level)
            return config["max_depth"] if config else 0
        except ValueError:
            return 0

    def can_create_task(self, agent_level: int) -> bool:
        """判断是否可以创建任务"""
        try:
            level = AgentLevel(agent_level)
            config = self.LEVEL_CONFIGS.get(level)
            return config["can_create_task"] if config else False
        except ValueError:
            return False

    def can_manage_project(self, agent_level: int) -> bool:
        """判断是否可以管理 Project"""
        try:
            level = AgentLevel(agent_level)
            config = self.LEVEL_CONFIGS.get(level)
            return config["can_manage_project"] if config else False
        except ValueError:
            return False

    def get_level_name(self, agent_level: int) -> str:
        """获取层级名称"""
        try:
            level = AgentLevel(agent_level)
            config = self.LEVEL_CONFIGS.get(level)
            return config["name"] if config else "未知层级"
        except ValueError:
            return "未知层级"

    def is_valid_level(self, agent_level: int) -> bool:
        """判断层级是否有效"""
        try:
            AgentLevel(agent_level)
            return True
        except ValueError:
            return False

    def get_level_info(self, agent_level: int) -> dict[str, Any] | None:
        """
        获取层级详细信息

        Args:
            agent_level: Agent 层级

        Returns:
            层级信息字典，无效层级返回 None
        """
        try:
            level = AgentLevel(agent_level)
            config = self.LEVEL_CONFIGS.get(level)
            if config:
                submit_to = [t.value for t in config["can_submit_to"]]
                return {
                    "name": config["name"],
                    "max_depth": config["max_depth"],
                    "can_submit": config["can_submit"],
                    "can_submit_to": submit_to,
                    "can_create_task": config["can_create_task"],
                    "can_manage_project": config["can_manage_project"],
                }
            return None
        except ValueError:
            return None

    def calculate_current_depth(self, parent_task_id: str | None, task_depth_map: dict[str, int]) -> int:
        """
        计算当前任务深度

        Args:
            parent_task_id: 父任务 ID，None 表示根任务
            task_depth_map: 任务 ID 到深度的映射

        Returns:
            当前任务深度
        """
        if parent_task_id is None:
            return 1
        parent_depth = task_depth_map.get(parent_task_id, 0)
        return parent_depth + 1

    def get_basic_tools(self) -> set:
        """
        获取基础工具集合（所有层级都可用的工具）

        Returns:
            基础工具名称集合
        """
        return {"file_read", "file_write", "bash_execute", "web_search"}

    def validate_task_submission(
        self,
        parent_level: int,
        current_depth: int | None = None,
    ) -> ValidationResult:
        """
        验证任务提交的合法性

        Args:
            parent_level: 父 Agent 层级
            current_depth: 当前嵌套深度

        Returns:
            验证结果
        """
        # 验证层级有效性
        try:
            AgentLevel(parent_level)
        except ValueError:
            return ValidationResult(
                passed=False,
                error_code=ValidationError.INVALID_LEVEL,
                error_message=f"无效的 Agent 层级: {parent_level}",
            )

        # 检查提交权限
        if not self.can_submit_task(parent_level):
            return ValidationResult(
                passed=False,
                error_code=ValidationError.CANNOT_SUBMIT_TASK,
                error_message=f"层级 L{parent_level} 不能提交子任务",
            )

        # 检查嵌套深度
        if current_depth is not None:
            max_depth = self.get_max_depth(parent_level)
            if current_depth > max_depth:
                msg = f"超过最大嵌套深度: {current_depth} > {max_depth}"
                return ValidationResult(
                    passed=False,
                    error_code=ValidationError.MAX_DEPTH_EXCEEDED,
                    error_message=msg,
                )

        return ValidationResult(passed=True)

    def validate_transition(self, from_level: int, to_level: int) -> ValidationResult:
        """验证层级转换的合法性"""
        try:
            AgentLevel(from_level)
            AgentLevel(to_level)
        except ValueError as e:
            return ValidationResult(
                passed=False,
                error_code=ValidationError.INVALID_LEVEL,
                error_message=f"无效的层级: {e}",
            )

        allowed = self.get_allowed_targets(from_level)
        if to_level not in allowed:
            return ValidationResult(
                passed=False,
                error_code=ValidationError.INVALID_TARGET_LEVEL,
                error_message=(f"L{from_level} 不能提交任务给 L{to_level}，允许: {allowed}"),
            )

        return ValidationResult(passed=True)

"""
审批决策模块

提供审批相关的类型定义和决策引擎，包括：
- ApprovalContext: 审批上下文
- ApprovalDecision: 审批决策结果
- DangerChecker: 危险操作检测器
- ApprovalDecisionEngine: 审批决策引擎

审批决策逻辑（可配置）：
- 默认规则：HOST模式 + 危险操作 → 需要审批
- 其他情况 → 自动批准
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from isolation.policy import ToolIsolationPolicy
from isolation.types import IsolationLevel

if TYPE_CHECKING:
    from src.tools.types import Tool

logger = logging.getLogger(__name__)


# 不同工具对应的命令输入字段
TOOL_COMMAND_FIELDS = {
    "bash_execute": "command",
    "shell_execute": "command",
    "python_execute": "code",
    "file_write": "content",
    "rollback": "operation",
}


@dataclass
class ApprovalContext:
    """审批上下文

    包含审批决策所需的所有信息
    """

    tool_name: str
    tool_definition: "Tool | None" = None
    inputs: dict[str, Any] = field(default_factory=dict)
    isolation_level: IsolationLevel = IsolationLevel.CONTAINER
    policy: ToolIsolationPolicy | None = None
    user_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "tool_name": self.tool_name,
            "tool_definition": (
                self.tool_definition.name if self.tool_definition else None
            ),
            "inputs": self.inputs,
            "isolation_level": self.isolation_level.value,
            "policy_approval": self.policy.approval if self.policy else None,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
        }


@dataclass
class ApprovalDecision:
    """审批决策结果

    包含审批决策的所有信息，用于决定是否需要用户审批
    """

    requires_approval: bool
    decision_type: str  # "AUTO_APPROVED", "NEEDS_APPROVAL", "AUTO_DENIED"
    reason: str
    risk_score: float = 0.0  # 0.0 - 1.0
    risk_factors: list[str] = field(default_factory=list)  # ["HOST_MODE", "DANGEROUS_OPERATION"]
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "requires_approval": self.requires_approval,
            "decision_type": self.decision_type,
            "reason": self.reason,
            "risk_score": self.risk_score,
            "risk_factors": self.risk_factors,
            "details": self.details,
        }


class DangerChecker:
    """危险操作检测器

    只检测工具定义中声明的 dangerous_operations，不使用全局硬编码模式。
    危险操作由工具开发者在 Tool 定义中声明。
    """

    def check(
        self,
        tool_name: str,
        tool_definition: "Tool | None",
        inputs: dict[str, Any],
    ) -> str | None:
        """检测危险操作

        Args:
            tool_name: 工具名称
            tool_definition: 工具定义
            inputs: 工具输入参数

        Returns:
            匹配到的危险操作名称，未检测到返回 None
        """
        # 1. 从工具定义获取声明的危险操作
        dangerous_ops = []
        if tool_definition is not None:
            dangerous_ops = getattr(tool_definition, "dangerous_operations", []) or []

        if not dangerous_ops:
            return None

        # 2. 获取需要检测的输入字段
        command_input = self._get_command_input(tool_name, inputs)

        # 3. 如果无法获取命令输入，尝试检查字段名是否匹配危险操作
        if not command_input:
            for key in inputs.keys():
                key_lower = key.lower()
                for op in dangerous_ops:
                    op_lower = op.lower()
                    # 检查 key 是否以危险操作开头（如 "write:/etc/"）
                    if key_lower.startswith(op_lower.split(":")[0].lower()):
                        logger.info(f"[DangerChecker] 检测到危险操作字段: {key}")
                        return op
            return None

        # 4. 匹配工具声明的危险操作
        command_lower = command_input.lower()
        for op in dangerous_ops:
            op_lower = op.lower()
            # 支持前缀匹配（如 "rm -rf" 匹配 "rm -rf /tmp"）
            if op_lower in command_lower:
                logger.info(f"[DangerChecker] 检测到工具声明的危险操作: {op}")
                return op

        return None

    def _get_command_input(self, tool_name: str, inputs: dict[str, Any]) -> str | None:
        """获取需要检测的命令输入

        Args:
            tool_name: 工具名称
            inputs: 工具输入参数

        Returns:
            命令输入字符串，如果无法获取返回 None
        """
        field = TOOL_COMMAND_FIELDS.get(tool_name)
        if field and field in inputs:
            value = inputs[field]
            if isinstance(value, str):
                return value
        return None


class ApprovalDecisionEngine:
    """审批决策引擎

    默认决策规则：
    - HOST模式 + 危险操作 → 需要审批
    - 其他情况 → 自动批准

    通过配置可以自定义审批规则。
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
    ):
        """初始化决策引擎

        Args:
            config: 审批配置，如果为 None 则使用默认配置
        """
        self.config = config or self._load_default_config()
        self._danger_checker = DangerChecker()

    def _load_default_config(self) -> dict[str, Any]:
        """加载默认配置"""
        return {
            "enabled": True,
            "policies": {
                "host_dangerous": {"enabled": True, "action": "NEEDS_APPROVAL"},
            },
        }

    async def decide(self, context: ApprovalContext) -> ApprovalDecision:
        """决策是否需要审批

        Args:
            context: 审批上下文

        Returns:
            审批决策结果
        """
        logger.debug(f"[ApprovalDecisionEngine] 开始审批决策 | tool={context.tool_name}")

        # 1. 策略级审批：策略配置要求审批时直接返回
        if context.policy and context.policy.approval:
            decision = ApprovalDecision(
                requires_approval=True,
                decision_type="NEEDS_APPROVAL",
                reason=f"工具 {context.tool_name} 配置为需要审批",
                risk_score=0.6,
                risk_factors=["POLICY_APPROVAL"],
                details={"policy_approval": True},
            )
            logger.info(
                f"[ApprovalDecisionEngine] 策略要求审批 | "
                f"tool={context.tool_name}"
            )
            return decision

        # 2. 检测危险操作
        has_dangerous_op = self._danger_checker.check(
            tool_name=context.tool_name,
            tool_definition=context.tool_definition,
            inputs=context.inputs,
        )

        # 3. 构建风险因子
        risk_factors = []
        risk_score = 0.0

        if context.isolation_level == IsolationLevel.HOST:
            risk_factors.append("HOST_MODE")
            risk_score += 0.5

        if has_dangerous_op:
            risk_factors.append("DANGEROUS_OPERATION")
            risk_score += 0.4

        # 4. 决策：只有 HOST + 危险操作 才需要审批
        if context.isolation_level == IsolationLevel.HOST and has_dangerous_op:
            decision = ApprovalDecision(
                requires_approval=True,
                decision_type="NEEDS_APPROVAL",
                reason=f"HOST模式危险操作: {has_dangerous_op}",
                risk_score=min(risk_score, 1.0),
                risk_factors=risk_factors,
                details={"dangerous_operation": has_dangerous_op},
            )
            logger.info(
                f"[ApprovalDecisionEngine] HOST模式危险操作需要审批 | "
                f"tool={context.tool_name}, op={has_dangerous_op}, "
                f"risk_score={decision.risk_score}"
            )
            return decision

        # 其他情况自动批准
        decision = ApprovalDecision(
            requires_approval=False,
            decision_type="AUTO_APPROVED",
            reason="沙盒模式或无危险操作",
            risk_score=risk_score,
            risk_factors=risk_factors,
            details={},
        )
        logger.debug(
            f"[ApprovalDecisionEngine] 自动批准 | "
            f"tool={context.tool_name}, isolation={context.isolation_level.value}, "
            f"has_dangerous_op={has_dangerous_op}"
        )
        return decision

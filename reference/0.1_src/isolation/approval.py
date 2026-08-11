"""
审批决策模块

提供审批相关的类型定义和决策引擎，包括：
- ApprovalContext: 审批上下文
- ApprovalDecision: 审批决策结果
- DangerChecker: 危险操作检测器
- ApprovalDecisionEngine: 审批决策引擎

Host 模式安全审批策略（单一事实源：isolation_policy.yaml 的 policy.execution）：
- execution="host_direct"（内部 API 工具，如 task_submit/memory）→ 免审批放行
- execution="command_in_container"（命令执行类，如 bash_execute）→ HOST 降级时需审批
- SAFE_TOOLS / DANGEROUS_TOOLS 硬编码集合仅作兜底参考，不再是决策主路径
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

# ── Host 模式工具安全分类 ──────────────────────────────────────
# 只读 / 查询类工具：HOST 模式下免审批白名单
SAFE_TOOLS: set[str] = {
    # 文件读取
    "file_read",
    "read_file",
    "list_directory",
    # 搜索
    "enhanced_search",
    "code_search",
    "search",
    "grep",
    # 资源查询
    "resource_search",
    # 记忆/知识
    "memory",
    "retrieve_memory",
    # 任务管理（只读操作为主）
    "task_manage",
    "task_evaluate",
    # 工具自身信息
    "tool_info",
    # 评估
    "evaluate",
}

# 危险操作类工具：HOST 模式下必须审批
DANGEROUS_TOOLS: set[str] = {
    # 文件写入
    "file_write",
    "write_file",
    "file_edit",
    "file_delete",
    # 命令执行
    "bash_execute",
    "shell_execute",
    "python_execute",
    "command_execute",
    # 系统操作
    "rollback",
    "checkpoint",
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
            "tool_definition": (self.tool_definition.name if self.tool_definition else None),
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
            for key in inputs:
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


def classify_tool_safety(tool_name: str) -> str:
    """分类工具的安全级别（兜底用途，非决策主路径）。

    .. deprecated:: 决策主路径已改用 isolation_policy.yaml 的 policy.execution
        字段（见 ApprovalDecisionEngine.decide）。本函数仅在缺少 policy 上下文
        的场景作兜底参考，不应作为审批的唯一依据。

    Args:
        tool_name: 工具名称

    Returns:
        "safe" / "dangerous" / "unknown"
    """
    if tool_name in SAFE_TOOLS:
        return "safe"
    if tool_name in DANGEROUS_TOOLS:
        return "dangerous"
    return "unknown"


class ApprovalDecisionEngine:
    """审批决策引擎

    HOST 模式安全审批策略（单一事实源：isolation_policy.yaml）：

    1. 策略级审批：策略配置 (policy.approval=True) 要求审批时直接返回
    2. HOST 模式 + policy.execution 分类：
       - execution="host_direct"（内部 API 工具）→ 自动批准
       - execution="command_in_container"（命令执行类降级）→ 需要审批，
         并叠加 DangerChecker 危险操作检测提升风险分
    3. 非 HOST 模式 → 自动批准（容器内执行无需审批）
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

        决策优先级：
        1. 策略级审批 (policy.approval=True)
        2. HOST 模式 + policy.execution 分类
        3. 非 HOST 模式 → 自动批准

        Args:
            context: 审批上下文（context.policy 应由调用方传入）

        Returns:
            审批决策结果
        """
        logger.debug(f"[ApprovalDecisionEngine] 开始审批决策 | tool={context.tool_name}")

        # ── 第 1 层：策略级审批 ──
        if context.policy and context.policy.approval:
            decision = ApprovalDecision(
                requires_approval=True,
                decision_type="NEEDS_APPROVAL",
                reason=f"工具 {context.tool_name} 配置为需要审批",
                risk_score=0.6,
                risk_factors=["POLICY_APPROVAL"],
                details={"policy_approval": True},
            )
            logger.info(f"[ApprovalDecisionEngine] 策略要求审批 | tool={context.tool_name}")
            return decision

        # ── 第 2 层：HOST 模式工具级分类 ──
        # 以 isolation_policy.yaml 为单一事实源：policy.execution 区分两类工具
        #   - host_direct          : 宿主机内部 API 工具（task_submit/memory 等）→ 免审批
        #   - command_in_container : 命令执行类（bash_execute 等）→ 降级到 HOST 时需审批
        if context.isolation_level == IsolationLevel.HOST:
            execution = context.policy.execution if context.policy else "command_in_container"

            if execution == "host_direct":
                # 内部 API 工具：直接操作内存/数据库/状态，免审批放行
                decision = ApprovalDecision(
                    requires_approval=False,
                    decision_type="AUTO_APPROVED",
                    reason=f"HOST 模式内部工具免审批 (host_direct): {context.tool_name}",
                    risk_score=0.1,
                    risk_factors=["HOST_MODE", "HOST_DIRECT_TOOL"],
                    details={"execution": "host_direct"},
                )
                logger.debug(f"[ApprovalDecisionEngine] HOST 内部工具免审批 | tool={context.tool_name}")
                return decision

            # execution == "command_in_container"：命令执行类降级到 HOST，必须审批
            # 先用 DangerChecker 叠加危险操作检测以提升风险分
            has_dangerous_op = self._danger_checker.check(
                tool_name=context.tool_name,
                tool_definition=context.tool_definition,
                inputs=context.inputs,
            )

            if has_dangerous_op:
                risk_factors = ["HOST_MODE", "DANGEROUS_OPERATION"]
                risk_score = min(0.9 + 0.1, 1.0)
                reason = f"HOST 模式命令执行工具检测到危险操作: {has_dangerous_op}"
            else:
                risk_factors = ["HOST_MODE", "COMMAND_EXECUTION"]
                risk_score = 0.9
                reason = f"HOST 模式命令执行工具需要审批 (command_in_container): {context.tool_name}"

            decision = ApprovalDecision(
                requires_approval=True,
                decision_type="NEEDS_APPROVAL",
                reason=reason,
                risk_score=risk_score,
                risk_factors=risk_factors,
                details={
                    "execution": "command_in_container",
                    "dangerous_operation": has_dangerous_op,
                },
            )
            logger.info(
                f"[ApprovalDecisionEngine] HOST 命令执行工具需要审批 | tool={context.tool_name}, op={has_dangerous_op}"
            )
            return decision

        # ── 第 3 层：非 HOST 模式，检测危险操作 ──
        has_dangerous_op = self._danger_checker.check(
            tool_name=context.tool_name,
            tool_definition=context.tool_definition,
            inputs=context.inputs,
        )

        risk_factors = []
        risk_score = 0.0

        if has_dangerous_op:
            risk_factors.append("DANGEROUS_OPERATION")
            risk_score += 0.4

        # 非 HOST 模式自动批准
        decision = ApprovalDecision(
            requires_approval=False,
            decision_type="AUTO_APPROVED",
            reason="非 HOST 模式，自动批准",
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

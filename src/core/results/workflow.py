"""
工作流执行结果

提供工作流执行结果的类型定义，继承自 ExecutionResult 基类。

注意：NodeExecutionRecord 定义在 src/workflows/types.py 中，
此处通过 TYPE_CHECKING 导入以避免循环导入。
"""

from typing import TYPE_CHECKING, Any

from pydantic import Field

from src.core.results.base import ExecutionResult
from src.core.states import ExecutionStatus

if TYPE_CHECKING:
    pass


# 使用字符串类型避免循环导入
# NodeExecutionRecord 在运行时通过 workflows.types 导入
def _get_node_execution_record_type():
    """延迟导入 NodeExecutionRecord 类型"""
    from src.workflows.types import NodeExecutionRecord
    return NodeExecutionRecord


class WorkflowExecutionResult(ExecutionResult[dict[str, Any]]):
    """工作流执行结果

    继承自 ExecutionResult 基类，添加工作流特有字段。

    特有字段：
    - workflow_id: 工作流 ID
    - workflow_version: 工作流版本
    - progress: 执行进度
    - inputs: 输入参数
    - node_executions: 节点执行记录

    Attributes:
        workflow_id: 工作流 ID
        workflow_version: 工作流版本
        progress: 执行进度 (0.0-1.0)
        inputs: 输入参数
        node_executions: 节点执行记录列表
    """

    # 工作流标识
    workflow_id: str = Field(..., description="工作流 ID")
    workflow_version: str = Field(default="1.0.0", description="工作流版本")

    # 执行进度
    progress: float = Field(default=0.0, ge=0.0, le=1.0, description="执行进度")

    # 输入参数
    inputs: dict[str, Any] = Field(default_factory=dict, description="输入参数")

    # 节点执行记录（使用 Any 类型避免循环导入）
    node_executions: list[Any] = Field(
        default_factory=list,
        description="节点执行记录"
    )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典

        Returns:
            包含工作流执行结果的字典
        """
        result = super().to_dict()

        result["workflow_id"] = self.workflow_id
        result["workflow_version"] = self.workflow_version
        result["progress"] = self.progress

        if self.inputs:
            result["inputs"] = self.inputs

        if self.node_executions:
            result["node_executions"] = [
                ne.model_dump() if hasattr(ne, "model_dump") else ne
                for ne in self.node_executions
            ]

        return result

    def to_summary(self) -> dict[str, Any]:
        """转换为摘要信息（用于列表显示）

        Returns:
            包含摘要信息的字典
        """
        # 处理 status：由于 use_enum_values=True，status 可能已经是字符串
        status_value = self.status.value if hasattr(self.status, 'value') else self.status

        return {
            "workflow_id": self.workflow_id,
            "status": status_value,
            "progress": self.progress,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "duration_ms": self.duration_ms,
            "node_count": len(self.node_executions),
            "success_count": sum(
                1 for ne in self.node_executions
                if hasattr(ne, "status") and ne.status == ExecutionStatus.COMPLETED
            ),
            "failed_count": sum(
                1 for ne in self.node_executions
                if hasattr(ne, "status") and ne.status == ExecutionStatus.FAILED
            ),
        }

    def update_progress(self) -> None:
        """更新执行进度

        根据节点执行记录计算当前进度。
        """
        if not self.node_executions:
            self.progress = 0.0
            return

        completed = sum(
            1 for ne in self.node_executions
            if hasattr(ne, "status") and ne.status == ExecutionStatus.COMPLETED
        )
        self.progress = completed / len(self.node_executions)

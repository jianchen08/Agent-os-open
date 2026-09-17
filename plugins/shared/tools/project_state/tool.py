"""project_state 工具——项目方案工作流状态查询与迁移（ADR 2026-09-17）。

状态机与合法边唯一真值 = plugins/shared/project_registry.py（WORKFLOW_TRANSITIONS）。
plan→running（定稿过门）与 running→plan（修订回门）是人审门控迁移：本工具发起
迁移即触发审批（security_rules needs_approval 按 action=transition 命中），审批
通过前登记行不变——工具面只认合法边与登记写入，不做审批判定（插件判定/内核落库
同构：审批在人，账在登记行）。done 由用户界面管理，不在工具可迁集合内。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

# 共享层自举（plugins/shared/ —— project_registry 所在，与 project_create 同模式）
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from agentos_plugin_sdk.builtin_tool import BuiltinTool  # noqa: E402
from agentos_plugin_sdk.results import ToolExecutionResult  # noqa: E402
from agentos_plugin_sdk.tool_types import (  # noqa: E402
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_id": {
            "type": "string",
            "minLength": 1,
            "description": "项目 id（12hex 登记键）",
        },
        "action": {
            "type": "string",
            "enum": ["query", "transition"],
            "default": "query",
            "description": "query=读当前工作流状态；transition=申请状态迁移（触发用户审批）",
        },
        "target_state": {
            "type": "string",
            "enum": ["plan", "running"],
            "description": (
                "transition 的目标状态（合法边：plan→running 定稿过门；"
                "running→plan 修订回门）。done 由用户界面管理，不在工具可迁集合内。"
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "transition 必填：迁移事由（定稿说明 / 修订申请：改动点+断链影响面+"
                "在途任务清单），进审批卡与日志审计"
            ),
        },
    },
    "required": ["project_id"],
}

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["project_id", "workflow_state"],
    "properties": {
        "project_id": {"type": "string"},
        "workflow_state": {"type": "string", "description": "plan | running | done"},
        "title": {"type": "string"},
        "path": {"type": "string"},
        "transitioned": {"type": "boolean", "description": "本次是否发生迁移（query 恒 false）"},
    },
}


def _registry() -> Any:
    from project_registry import ProjectRegistry  # noqa: PLC0415

    return ProjectRegistry()


class ProjectStateTool(BuiltinTool):
    """项目状态工具（查询 + 门控迁移）。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="project_state",
            description=(
                "查询或迁移项目方案工作流状态（plan | running | done）。"
                "query：读登记行工作流状态；transition：申请状态迁移"
                "（plan→running 定稿过门 / running→plan 修订回门），"
                "迁移触发用户审批（安全规则 needs_approval），审批通过前状态不变。"
                "plan 态内 task_submit 仅允许派发调研/环境准备类任务，"
                "执行类任务会被状态闸拒绝。done 由用户在界面管理。"
            ),
            input_schema=_INPUT_SCHEMA,
            output_schema=_OUTPUT_SCHEMA,
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.L1_L2_ONLY,
            tags=["project", "state", "workflow"],
            injected_params=["user_id", "session_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        project_id = str(inputs.get("project_id") or "").strip()
        if not project_id:
            return create_failure_result(
                error="必须指定 project_id", error_code="MISSING_PROJECT_ID"
            )
        action = str(inputs.get("action") or "query").strip() or "query"
        registry = _registry()
        project = registry.get(project_id)
        if project is None:
            return create_failure_result(
                error=f"项目 {project_id} 不存在（登记中无此 id）",
                error_code="PROJECT_NOT_FOUND",
            )

        if action == "query":
            return self._snapshot(project, transitioned=False)

        if action != "transition":
            return create_failure_result(
                error=f"未知 action: {action}（query | transition）",
                error_code="UNKNOWN_ACTION",
            )

        target = str(inputs.get("target_state") or "").strip()
        reason = str(inputs.get("reason") or "").strip()
        if target not in ("plan", "running"):
            return create_failure_result(
                error="transition 必须提供 target_state（plan | running）；done 由用户界面管理",
                error_code="MISSING_TARGET_STATE",
            )
        if not reason:
            return create_failure_result(
                error="transition 必须提供 reason（定稿说明 / 修订申请），进审批与审计",
                error_code="MISSING_REASON",
            )
        if project.workflow_state == target:
            return create_failure_result(
                error=f"项目已处于 {target} 态，无需迁移",
                error_code="ALREADY_IN_TARGET_STATE",
            )
        from project_registry import transition_workflow_state  # noqa: PLC0415

        try:
            transition_workflow_state(project, target)
        except ValueError as exc:
            return create_failure_result(error=str(exc), error_code="ILLEGAL_TRANSITION")
        registry.save(project)
        logger.info(
            "[ProjectState] 状态迁移 | project_id=%s | %s | by=%s | reason=%s",
            project_id,
            target,
            inputs.get("user_id") or "-",
            reason[:200],
        )
        return self._snapshot(project, transitioned=True)

    @staticmethod
    def _snapshot(project: Any, transitioned: bool) -> ToolExecutionResult:
        return create_success_result(
            data={
                "project_id": project.id,
                "workflow_state": project.workflow_state,
                "title": project.title,
                "path": project.path,
                "transitioned": transitioned,
            }
        )

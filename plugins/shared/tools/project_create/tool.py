"""project_create 工具——创建项目（= 真实文件夹 + 登记行）。

项目与任务解耦（2026-08-30 用户裁定）：项目是任务树分组锚点，不执行、
无执行者；创建走独立入口（本工具 / 会话目录登记 / 前端表单），任务经
task_submit 的 project_id 挂靠后在其文件夹下执行（默认 worktree 分叉）。
同目录已登记时幂等复用既有项目（不重复建文件夹/登记行）。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

# 共享层自举（plugins/shared/ —— project_registry 所在，与 service_access.py 同模式）
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

_PROJECT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "minLength": 1,
            "description": (
                "项目标题（必填）。项目 = 真实文件夹 + 登记行（任务树分组锚点），"
                "不执行、无执行者；任务经 task_submit 的 project_id 挂靠后"
                "在项目文件夹下执行（默认 worktree 从项目仓库分叉）。"
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "项目文件夹（可选）。显式指定目录；缺省自动生成"
                "{工作空间根}/projects/<标题>。已存在目录非 git 仓库会自动"
                "git init（不删改现有文件）；同目录已登记时复用既有项目。"
            ),
        },
    },
    "required": ["goal"],
}

_PROJECT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["project_id", "title", "path", "status", "created"],
    "properties": {
        "project_id": {"type": "string", "description": "项目 id（12hex；复用既有时同值）"},
        "title": {"type": "string", "description": "项目标题"},
        "path": {"type": "string", "description": "项目文件夹宿主绝对路径"},
        "status": {"type": "string", "description": "active | paused"},
        "created": {"type": "boolean", "description": "true=新建登记；false=复用既有"},
        "session_id": {"type": "string", "description": "创建时关联的会话（可选）"},
    },
}


class ProjectCreateTool(BuiltinTool):
    """项目创建工具。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="project_create",
            description=(
                "创建项目（= 真实文件夹 + 登记行）。项目是任务树分组锚点，"
                "不执行、无执行者；创建后把 project_id 传给 task_submit 挂靠，"
                "任务即在项目文件夹下执行（默认 worktree 分叉）。"
                "同目录已登记时复用既有项目（created=false）。"
            ),
            input_schema=_PROJECT_INPUT_SCHEMA,
            output_schema=_PROJECT_OUTPUT_SCHEMA,
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.L1_L2_ONLY,
            tags=["project", "create"],
            injected_params=["user_id", "session_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """创建项目：建文件夹（显式路径优先，缺省 {ws_base}/projects/<slug>）+
        非 git 自动 git init + 登记；同路径已登记时幂等复用。"""
        goal = str(inputs.get("goal") or "").strip()
        if not goal:
            return create_failure_result(
                error="必须指定项目标题（goal）——项目 = 文件夹 + 登记，标题是文件夹命名与登记标题的来源",
                error_code="MISSING_GOAL",
            )
        explicit_path = str(inputs.get("path") or "").strip()

        from project_registry import ensure_project_registered  # noqa: PLC0415

        try:
            project, created = ensure_project_registered(
                title=goal,
                explicit_path=explicit_path,
                session_id=str(inputs.get("session_id") or ""),
                submitted_by=str(inputs.get("user_id") or ""),
            )
        except (ValueError, RuntimeError) as exc:
            logger.error("[ProjectCreate] 项目创建失败 | goal=%s | err=%s", goal, exc)
            return create_failure_result(
                error=f"项目创建失败: {exc}",
                error_code="PROJECT_CREATE_FAILED",
            )

        logger.info(
            "[ProjectCreate] %s项目 | project_id=%s | title=%s | path=%s | user=%s",
            "复用已登记" if not created else "创建",
            project.id,
            project.title,
            project.path,
            project.submitted_by or "-",
        )
        return create_success_result(
            data={
                "project_id": project.id,
                "title": project.title,
                "path": project.path,
                "status": project.status,
                "created": created,
                "session_id": project.session_id or "",
            }
        )

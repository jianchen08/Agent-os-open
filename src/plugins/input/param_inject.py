"""参数注入 Input 插件。

负责在管道循环的输入阶段为工具调用注入运行时参数，
包括会话 ID、用户信息、时间戳等上下文参数，
以及工具特定的默认参数填充。

M6b 阶段：从旧代码 isolation/tools.py 的参数预处理逻辑迁移。

State 命名空间：
    - tool.params_injected : 本插件标记参数已注入
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


def _resolve_project_root() -> Path | None:
    """推导 Agent OS 项目根目录。

    从本文件（param_inject.py）向上逐级查找，找到同时包含
    config/ 和 src/ 目录的祖先目录即为项目根目录。
    结果会被缓存到模块级变量以避免重复计算。

    Returns:
        项目根目录的 Path 对象，未找到时返回 None
    """
    if _resolve_project_root._cached is not None:
        return _resolve_project_root._cached

    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "config").is_dir() and (parent / "src").is_dir():
            _resolve_project_root._cached = parent
            return parent

    return None


_resolve_project_root._cached: Path | None = None


class ParamInjectPlugin(IInputPlugin):
    """参数注入 Input 插件。

    在工具执行前为工具调用参数注入运行时上下文信息，
    例如会话 ID、用户 ID、时间戳等。同时支持为特定工具
    填充默认参数值。

    优先级：20（准备级，在安全检查之前完成参数注入）
    错误策略：ABORT（参数注入失败工具无法执行）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化参数注入插件。

        Args:
            config: 插件配置字典，支持以下键：
                - inject_session_id: 是否注入会话 ID（默认 True）
                - inject_user_id: 是否注入用户 ID（默认 True）
                - inject_timestamp: 是否注入时间戳（默认 True）
                - default_params: 工具默认参数映射 {tool_name: {param: value}}
        """
        self._config = config or {}
        self._inject_session_id = self._config.get("inject_session_id", True)
        self._inject_user_id = self._config.get("inject_user_id", True)
        self._inject_timestamp = self._config.get("inject_timestamp", True)
        self._default_params = self._config.get("default_params", {})

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "param_inject"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 20)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行参数注入。

        为 state 中的工具调用参数注入运行时上下文信息。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含注入参数状态更新的插件执行结果
        """
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行参数注入逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            更新后的工具调用参数字典
        """
        updates: dict[str, Any] = {}

        core_type = ctx.state.get(StateKeys.CORE_TYPE, "llm_call")
        if core_type != "tool_execute":
            return {"tool.params_injected": False}

        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return {"tool.params_injected": False}

        # 注入上下文参数到每个工具调用
        injected_calls = []
        for tc in tool_calls:
            injected_tc = dict(tc)
            raw_args = injected_tc.get("args", injected_tc.get("arguments", {}))
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    raw_args = {}
            if not isinstance(raw_args, dict):
                raw_args = {}
            args = dict(raw_args)

            # 注入运行时参数（仅当参数不存在时才注入）
            if self._inject_session_id and "session_id" not in args:
                session_id = ctx.state.get(StateKeys.SESSION_ID, "")
                if session_id:
                    args["session_id"] = session_id

            if self._inject_user_id and "user_id" not in args:
                user_id = ctx.state.get("user_id", "")
                if user_id:
                    args["user_id"] = user_id

            if self._inject_timestamp and "timestamp" not in args:
                args["timestamp"] = datetime.now(UTC).isoformat()

            # BUG-FIX-fix_20260418_task_inject: 注入 task_id
            # 问题根因: task_evaluate 声明 injected_params=["task_id"] 但实际注入链断裂
            # 修复方案: 在 ParamInjectPlugin 中补充 task_id 注入，从 state 获取
            # 影响范围: 所有声明 injected_params 含 task_id 的工具
            if "task_id" not in args:
                task_id = ctx.state.get(StateKeys.TASK_ID, "")
                if task_id:
                    args["task_id"] = task_id

            # 注入 pipeline_id（仅当参数不存在且 state 中有值时才注入）
            if "pipeline_id" not in args:
                pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
                if pipeline_id:
                    args["pipeline_id"] = pipeline_id

            if "workspace" not in args:
                workspace = ctx.state.get("workspace", "")
                if workspace:
                    args["workspace"] = workspace

            # 注入 project_root：从 state 获取 Agent OS 项目根目录
            # 供 workspace_aware 等工具使用，与 workspace 注入同源
            if "project_root" not in args:
                project_root = ctx.state.get("project_root", "")
                if project_root:
                    args["project_root"] = project_root

            # 注入 parent_agent_level：从 state 中获取当前 Agent 层级
            # 供 task_submit / task_manage 等工具判断权限和设置子任务层级
            if "parent_agent_level" not in args:
                raw_level = (
                    ctx.state.get(StateKeys.AGENT_LEVEL)
                    or ctx.state.get("context.agent_level", "")
                )
                if raw_level:
                    level_str = str(raw_level).upper().lstrip("L")
                    try:
                        args["parent_agent_level"] = int(level_str)
                    except (ValueError, TypeError):
                        pass

            # 注入工具默认参数
            tool_name = injected_tc.get("name", "")
            if tool_name in self._default_params:
                for param, value in self._default_params[tool_name].items():
                    if param not in args:
                        args[param] = value

            # 替换 {{project_root}} 模板变量
            # 将 args 中所有字符串值里的 {{project_root}} 替换为 Agent OS 实际项目根路径
            _project_root_path = _resolve_project_root()
            if _project_root_path is not None:
                _pr_str = str(_project_root_path)
                for key, val in args.items():
                    if isinstance(val, str) and "{{project_root}}" in val:
                        args[key] = val.replace("{{project_root}}", _pr_str)

            injected_tc["args"] = args
            injected_calls.append(injected_tc)

        updates[StateKeys.RAW_TOOL_CALLS] = injected_calls
        updates["tool.params_injected"] = True

        logger.debug(
            "[%s] Parameters injected | count=%d",
            self.name, len(injected_calls),
        )

        return updates

"""参数注入 Input 插件。"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


def _resolve_project_root() -> Path | None:
    """推导 Agent OS 项目根目录。"""
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
    """参数注入 Input 插件。"""

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化参数注入插件。"""
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
        """执行参数注入。"""
        result = await self._do_work(ctx)
        return PluginResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0912,PLR0915
        """执行参数注入逻辑。"""
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
                    tool_name = injected_tc.get("name", "?")
                    logger.warning(
                        "[%s] 工具 %s 的 arguments JSON 解析失败（疑似输出被 max_tokens 截断），长度=%d，前200字符: %s",
                        self.name,
                        tool_name,
                        len(raw_args),
                        raw_args[:200],
                    )
                    # 截断修复：用 repair_json_string 尽量保住完整字段（含半截 content），
                    # 避免直接 raw_args={} 把半截内容全部丢失，导致下游验证器/tool_core
                    # 拿不到任何内容，只能返回模糊的 "不支持的操作: None"。
                    from plugins.core.llm_core._message_normalizer import (  # noqa: PLC0415
                        repair_json_string,
                    )

                    repaired = repair_json_string(raw_args)
                    if repaired is not None:
                        try:
                            raw_args = json.loads(repaired)
                        except (json.JSONDecodeError, TypeError):
                            raw_args = {}
                        # 打结构性截断标记：不依赖代理返回 finish_reason，
                        # 供 tool_schema_validator 识别并提示「文件太大请分块」
                        injected_tc["_args_truncated"] = True
                        logger.info(
                            "[%s] 工具 %s 截断修复成功，已保住可用字段 %s",
                            self.name,
                            tool_name,
                            list(raw_args.keys()) if isinstance(raw_args, dict) else [],
                        )
                    else:
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

            # 注入 task_id
            # （task_id=null/""）时仍判定为「已存在」而跳过注入，
            # 导致 L2 task_submit 拿不到 parent_task_id，报
            # L2_REQUIRES_PARENT_TASK。注入参数是系统权威值，
            # 只要 args 中没有有效值就注入。
            if not args.get("task_id"):
                task_id = ctx.state.get(StateKeys.TASK_ID, "")
                if task_id:
                    args["task_id"] = task_id
                else:
                    # 诊断：state 中无 task_id，说明引擎 state 未携带本任务 ID。
                    # task_submit/task_evaluate 等依赖该注入的工具将无法确定父任务。
                    _tool_name = injected_tc.get("name", "?")
                    if _tool_name in ("task_submit", "task_evaluate", "task_manage"):
                        logger.warning(
                            "[param_inject] task_id 注入失败 | tool=%s | state[TASK_ID]=%r | pipeline_id=%s",
                            _tool_name,
                            ctx.state.get(StateKeys.TASK_ID),
                            ctx.state.get(StateKeys.PIPELINE_ID, "")[:12],
                        )

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
                raw_level = ctx.state.get(StateKeys.AGENT_LEVEL) or ctx.state.get("context.agent_level", "")
                if raw_level:
                    level_str = str(raw_level).upper().lstrip("L")
                    with contextlib.suppress(ValueError, TypeError):
                        args["parent_agent_level"] = int(level_str)

            # 注入 agent_config_id：从 state 中获取当前 Agent 的 config_id
            # 供 memory 等工具自动标记记忆来源（谁写的就将谁作为标签）
            if "agent_config_id" not in args:
                agent_config_id = ctx.state.get("agent_config_id", "")
                if agent_config_id:
                    args["agent_config_id"] = agent_config_id

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
            self.name,
            len(injected_calls),
        )

        return updates

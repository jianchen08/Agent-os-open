"""参数注入 Input 插件。"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


def classify_args_parse_failure(raw: str) -> str:
    """对 json.loads 失败的原始串做诚实分类，避免一律归因为 max_tokens 截断。

    返回稳定标识符，供日志/诊断使用：
    - ``empty``: 空串或纯空白
    - ``markdown_wrapped``: 被 markdown 代码块（```...```）包裹
    - ``leading_noise``: 前导非 JSON 文本（不以 ``{`` 开头，且非 markdown）
    - ``truncated``: 结构性截断——出现 ``{`` 但无匹配的 ``}``（末尾残缺）
    - ``malformed``: 其它语法错误（如未转义字符、尾逗号等结构完整但非法）

    背景：原日志固定打印「疑似输出被 max_tokens 截断」，但生产误报案例中
    arguments 仅 283 字符，根本不可能触达 max_tokens。真实原因多为 markdown
    包裹或前导自然语言，应如实标注。
    """
    if not raw or not raw.strip():
        return "empty"

    s = raw.strip()
    if s.startswith("```"):
        return "markdown_wrapped"
    if not s.startswith("{"):
        return "leading_noise"

    # 判定结构性截断：花括号不配对（{ 比 } 多，且字符串外未闭合）。
    # 用与 repair_json_string 一致的字符串状态机，避免被字符串内的括号误导。
    depth = 0
    in_string = False
    escape_next = False
    for c in s:
        if escape_next:
            escape_next = False
            continue
        if c == "\\":
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
    # depth > 0 → 有未闭合的 { → 末尾残缺
    return "truncated" if depth > 0 else "malformed"


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


_resolve_project_root._cached: Path | None = None  # type: ignore[misc]


class ParamInjectPlugin(IInputPlugin):
    """参数注入 Input 插件。"""

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
                    # 诚实分类失败原因：不一律归因为 max_tokens 截断
                    # （短 arguments 也可能解析失败，如 markdown 包裹）。
                    reason = classify_args_parse_failure(raw_args)
                    logger.warning(
                        "[%s] 工具 %s 的 arguments JSON 解析失败 | reason=%s | 长度=%d | 前200字符: %s",
                        self.name,
                        tool_name,
                        reason,
                        len(raw_args),
                        raw_args[:200],
                    )
                    # 兜底修复：用 repair_json_string 尽量保住完整字段（含半截 content），
                    # 避免直接 raw_args={} 把半截内容全部丢失，导致下游验证器/tool_core
                    # 拿不到任何内容，只能返回模糊的 "不支持的操作: None"。
                    # 经 pipeline 命名空间包解析（plugins/shared 在 sys.path）；
                    # 原 ``plugins.core...`` 路径不存在。
                    from pipeline.core.llm_core._message_normalizer import (  # noqa: PLC0415
                        repair_json_string,
                    )

                    repaired = repair_json_string(raw_args)
                    if repaired is not None:
                        try:
                            raw_args = json.loads(repaired)
                        except (json.JSONDecodeError, TypeError):
                            raw_args = {}
                        # 仅当真实分类为截断时才打结构性截断标记，
                        # 供 tool_schema_validator 识别并提示「文件太大请分块」。
                        # 其它原因（markdown 包裹/前导噪声）修复成功不算截断，
                        # 无差别打标会误导下游给出错误的「请分块」提示。
                        if reason == "truncated":
                            injected_tc["_args_truncated"] = True
                        logger.info(
                            "[%s] 工具 %s arguments 兜底修复成功 | reason=%s | 已保住可用字段 %s",
                            self.name,
                            tool_name,
                            reason,
                            list(raw_args.keys()) if isinstance(raw_args, dict) else [],
                        )
                    else:
                        raw_args = {}
            if not isinstance(raw_args, dict):
                raw_args = {}
            args = dict(raw_args)

            # 剥离 LLM 夹带的 `_` 前缀内部键（安全边界）：
            # `_owner/_call_context/_container_id/_isolation_provider` 等下划线键是
            # 内核（dispatch 期）与管道插件（isolation_guard，位于本插件之后的
            # 服务端注入通道——isolation_guard 无条件覆盖注入 _container_id，故
            # 此处先剥不影响合法注入；内核注入的 _owner/_log_ctx 发生在管道之后，
            # 亦不受影响）。任何工具 schema 均未声明下划线参数，出现即视为
            # 提示注入伪造（下划线参数可被用于绕过危险命令黑名单），先剥后
            # 注入，服务端值权威。
            for forged_key in [k for k in args if k.startswith("_")]:
                del args[forged_key]

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

            # 注入 workspace / isolation_level / project_root：服务端权威值
            # **无条件覆盖**（仅 task_submit 例外——其 workspace/isolation 是
            # agent 显式选择项）。"参数不存在才注入"会允许 LLM 在工具
            # 参数里夹带伪造 workspace/project_root，使 fs_tools 等把校验锚点
            # 定到任意路径（安全边界），故必须覆盖式注入。
            _tool_name = injected_tc.get("name", "")
            _skip_task_ctx = _tool_name == "task_submit"

            if not _skip_task_ctx:
                workspace = ctx.state.get("workspace", "")
                if workspace:
                    args["workspace"] = workspace

                # 会话级隔离模式注入（task_submit 跳过：隔离由 agent 显式选择或
                # 任务类型默认，不再经 state 继承）
                isolation_level = ctx.state.get("isolation_level", "")
                if isolation_level:
                    args["isolation_level"] = isolation_level

                # 注入 project_root：从 state 获取 Agent OS 项目根目录
                # 供 workspace_aware 等工具使用，与 workspace 注入同源
                project_root = ctx.state.get("project_root", "")
                if project_root:
                    args["project_root"] = project_root

            # 注入 parent_agent_level：从 state 中获取当前 Agent 层级
            # 供 task_submit / task_manage 等工具判断权限和设置子任务层级
            if "parent_agent_level" not in args:
                raw_level = ctx.state.get(StateKeys.AGENT_LEVEL, "")
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

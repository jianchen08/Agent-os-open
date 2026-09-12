"""参数注入 Input 插件。"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)

# 能力调用器（server.py on_load 经 wiring.make_capability_caller 注入）：
# JSON 兜底修复走 llm_service 的 llm.repair_json 能力——repair_json_string
# 单一真值源在 llm_service._message_normalizer，跨插件共享经能力面收敛
# （2026-09-06 T7 裁定），不再 import 他插件模块。未注入（单元测试/能力
# 缺失）→ 修复不可用，走既有"修复失败"路径（arguments 置空 dict，不阻塞注入）。
CapabilityCaller = Callable[[str, dict[str, Any], float | None], Any]

_capability_caller: CapabilityCaller | None = None


def set_capability_caller(caller: CapabilityCaller | None) -> None:
    """注入能力调用句柄（async fn `(method, params, timeout) -> Any`）。

    Args:
        caller: 能力调用 async 函数；传 None 清空
    """
    global _capability_caller
    _capability_caller = caller


async def _repair_json_string(text: str) -> str | None:
    """经 llm_service 的 llm.repair_json 能力修复残缺 JSON。

    返回修复后字符串；调用器未注入 / 能力调用失败 / 不可修复 → None
    （与修复函数返回 None 的既有语义一致：调用方降级，不阻塞注入）。
    """
    caller = _capability_caller
    if caller is None:
        return None
    params = {
        "tool_name": "llm.repair_json",
        "plugin_id": "llm_service",
        "args": {"text": text},
    }
    try:
        envelope = await caller("tool-executor.invoke", params, None)
    except Exception as exc:
        logger.warning("[param_inject] llm.repair_json 调用失败（按不可修复降级）: %s", exc)
        return None
    if not isinstance(envelope, dict) or not envelope.get("success"):
        logger.warning(
            "[param_inject] llm.repair_json 信封异常（按不可修复降级）: %r",
            envelope,
        )
        return None
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    repaired = data.get("repaired")
    return repaired if isinstance(repaired, str) else None


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
    """推导 Agent OS 项目根目录（解析实现统一在 plugins/shared/repo_anchor.py）。

    AGENTOS_CONFIG_ROOT（内核启动发布）优先、config/isolation 标记法回退的
    语义与缓存由 repo_anchor.resolve_repo_root 单点承担；返回 None 时
    {{project_root}} 模板替换与 project_root 回退注入不生效（约定不变）。
    """
    import repo_anchor  # noqa: PLC0415

    return repo_anchor.resolve_repo_root()


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

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
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
            args = await self._normalize_args(injected_tc)
            self._strip_forged_keys(args)
            tool_name = injected_tc.get("name", "")
            self._inject_identity_params(ctx, args, tool_name)
            self._inject_workspace_ctx(ctx, args, tool_name)
            self._inject_parent_agent_level(ctx, args)
            self._inject_agent_config_id(ctx, args)
            self._apply_default_params(args, injected_tc.get("name", ""))
            self._expand_project_root_template(args)

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

    async def _normalize_args(self, injected_tc: dict[str, Any]) -> dict[str, Any]:
        """把工具调用的原始 arguments 归一化为 dict。

        字符串 JSON 先解析；解析失败做诚实分类（classify_args_parse_failure）
        并经 llm.repair_json 能力兜底修复——修复成功且真实分类为结构性截断时
        打 ``_args_truncated`` 标记（供 tool_schema_validator 提示「请分块」）；
        修复不可用（能力未注入/调用失败）与无法修复同语义降级为空 dict。

        Args:
            injected_tc: 工具调用副本（读取 args/arguments）

        Returns:
            归一化后的参数 dict（独立副本，后续注入不污染原调用）
        """
        raw_args = injected_tc.get("args", injected_tc.get("arguments", {}))
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                tool_name = injected_tc.get("name", "?")
                reason = classify_args_parse_failure(raw_args)
                logger.warning(
                    "[%s] 工具 %s 的 arguments JSON 解析失败 | reason=%s | 长度=%d | 前200字符: %s",
                    self.name,
                    tool_name,
                    reason,
                    len(raw_args),
                    raw_args[:200],
                )
                # 兜底修复：经 llm.repair_json 能力尽量保住完整字段（含半截
                # content），避免直接 raw_args={} 把半截内容全部丢失，导致下游
                # 验证器/tool_core 拿不到任何内容，只能返回模糊的
                # "不支持的操作: None"。
                repaired = await _repair_json_string(raw_args)
                if repaired is not None:
                    try:
                        raw_args = json.loads(repaired)
                    except (json.JSONDecodeError, TypeError):
                        raw_args = {}
                    # 仅当真实分类为截断时才打结构性截断标记；其它原因
                    # （markdown 包裹/前导噪声）修复成功不算截断，无差别打标
                    # 会误导下游给出错误的「请分块」提示。
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
            return {}
        return dict(raw_args)

    @staticmethod
    def _strip_forged_keys(args: dict[str, Any]) -> None:
        """剥离 LLM 夹带的 ``_`` 前缀内部键（安全边界）。

        `_owner/_call_context/_container_id/_isolation_provider` 等下划线键是
        内核（dispatch 期）与管道插件（isolation_guard，位于本插件之后的
        服务端注入通道——isolation_guard 无条件覆盖注入 _container_id，故
        此处先剥不影响合法注入；内核注入的 _owner/_log_ctx 发生在管道之后，
        亦不受影响）。任何工具 schema 均未声明下划线参数，出现即视为
        提示注入伪造（下划线参数可被用于绕过危险命令黑名单），先剥后
        注入，服务端值权威。
        """
        for forged_key in [k for k in args if k.startswith("_")]:
            del args[forged_key]

    def _inject_identity_params(self, ctx: PluginContext, args: dict[str, Any], tool_name: str) -> None:
        """注入会话/用户/时间戳/任务/管道身份参数（仅当参数不存在时）。

        task_id 例外：注入参数是系统权威值，args 中任何空值占位
        （None/""）都不算"已存在"——只要没有有效值就必须注入，否则
        下游 task_submit/task_evaluate 拿不到父任务身份无法定位任务。
        """
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

        if not args.get("task_id"):
            # 任务身份权威键是 task.id（点号键，内核 chat_send_handler
            # 创建管道时注入，值 = pipeline_id，引擎注入不可伪造）。
            task_id = ctx.state.get(StateKeys.TASK_ID, "")
            if task_id:
                args["task_id"] = task_id
            # 诊断：state 中无任务身份，说明引擎 state 未携带本任务 ID。
            # task_submit/task_evaluate 等依赖该注入的工具将无法确定父任务。
            elif tool_name in ("task_submit", "task_evaluate", "task_manage"):
                logger.warning(
                    "[param_inject] task_id 注入失败 | tool=%s | state[task.id]=%r | pipeline_id=%s",
                    tool_name,
                    ctx.state.get(StateKeys.TASK_ID),
                    ctx.state.get(StateKeys.PIPELINE_ID, "")[:12],
                )

        if "pipeline_id" not in args:
            pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
            if pipeline_id:
                args["pipeline_id"] = pipeline_id

    def _inject_workspace_ctx(self, ctx: PluginContext, args: dict[str, Any], tool_name: str) -> None:
        """注入工作空间坐标（服务端权威值，无条件覆盖）。

        仅 task_submit 例外——其 workspace/isolation 是 agent 显式选择项，
        覆盖会吞掉合法选择；task_submit 专属 parent_ws_meta（提交者工作空间
        坐标，覆盖式注入防 LLM 伪造，父无工作空间时以 None 覆盖）。
        """
        if tool_name != "task_submit":
            workspace = ctx.state.get("workspace", "")
            if workspace:
                args["workspace"] = workspace

            # 会话级隔离模式注入（task_submit 跳过：隔离由 agent 显式选择或
            # 任务类型默认，不再经 state 继承）
            isolation_level = ctx.state.get("isolation_level", "")
            if isolation_level:
                args["isolation_level"] = isolation_level

            # 注入 project_root：state 权威值（任务管道 = 工作空间，
            # 主会话 = 配置的工作空间根，均由 workspace_lifecycle 写入）。
            # 不做仓库根回退——state 缺锚点时文件工具 fail-closed 报错，
            # 不得把项目源码树变成 agent 读写面。
            project_root = ctx.state.get("project_root", "")
            if project_root:
                args["project_root"] = project_root
        else:
            # parent_ws_meta：子任务出生契约经它携带父工作空间坐标，使
            # workspace_lifecycle 的共享决策不依赖发起瞬间的聚合读可见性
            # （父管道运行中 registry 行可能尚未建立）。
            parent_ws_meta = ctx.state.get("ws_meta")
            if isinstance(parent_ws_meta, str):
                try:
                    parent_ws_meta = json.loads(parent_ws_meta)
                except ValueError:
                    parent_ws_meta = None
            if not isinstance(parent_ws_meta, dict) or not parent_ws_meta.get("path"):
                parent_ws_meta = None
            args["parent_ws_meta"] = parent_ws_meta

    def _inject_parent_agent_level(self, ctx: PluginContext, args: dict[str, Any]) -> None:
        """注入 parent_agent_level：当前 Agent 层级（task_submit/task_manage
        等工具判断权限和设置子任务层级用）。"""
        if "parent_agent_level" in args:
            return
        raw_level = ctx.state.get(StateKeys.AGENT_LEVEL, "")
        if raw_level:
            level_str = str(raw_level).upper().lstrip("L")
            with contextlib.suppress(ValueError, TypeError):
                args["parent_agent_level"] = int(level_str)

    def _inject_agent_config_id(self, ctx: PluginContext, args: dict[str, Any]) -> None:
        """注入 agent_config_id：当前 Agent 的 config_id（memory 等工具自动
        标记记忆来源用）。"""
        if "agent_config_id" in args:
            return
        agent_config_id = ctx.state.get("agent_config_id", "")
        if agent_config_id:
            args["agent_config_id"] = agent_config_id

    def _apply_default_params(self, args: dict[str, Any], tool_name: str) -> None:
        """注入工具默认参数（仅补缺，不覆盖已有值）。"""
        if tool_name in self._default_params:
            for param, value in self._default_params[tool_name].items():
                if param not in args:
                    args[param] = value

    @staticmethod
    def _expand_project_root_template(args: dict[str, Any]) -> None:
        """把 args 字符串值中的 {{project_root}} 替换为实际项目根路径。"""
        project_root = _resolve_project_root()
        if project_root is None:
            return
        pr_str = str(project_root)
        for key, val in args.items():
            if isinstance(val, str) and "{{project_root}}" in val:
                args[key] = val.replace("{{project_root}}", pr_str)

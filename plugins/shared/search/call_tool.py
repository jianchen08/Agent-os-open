"""动态工具薄转发壳 ``call_tool``（与 ``resource_search`` 同插件，复用其执行面接线）。

职责只有一件：**读动态定义（准入/否决）→ 解析参数 → 转发给既有执行面 → 归一信封**。

- **不新增 capability、不新增契约、不碰注册表**：转发走内核既有
  ``tool-executor.invoke``（``mcp/src/capability.rs`` 的 ``STANDARD_CAPABILITIES``
  已列，处理器 ``capability_router/tool_executor.rs``）；同插件 ``server.py`` 的
  ``_agent_service`` 早已用同一条通道调 ``tool-executor.invoke``，本工具复用同一
  接线，不新开通道。
- **动态定义真值单源**：``state["dynamic_tools"]``（由 ``tool_schema`` 插件从
  ``resource_search`` 的 ``tool_defs`` 抽取落库），**不信模型回传的定义**：模型
  只提供「指名道姓」的目标工具名 + 参数。
- **工具调用路径的读取来源**：主来源是同轮入参快照 ``inputs["_dynamic_tools"]``
  （``tool_args_inject`` 从活 state 注入）。工具路径不投喂 ``state.reads``、不注入
  ``_log_ctx``，故既读不到 state 也没有日志上下文。快照缺席才回落既有
  ``pipeline-state.list`` 读面，并按内核 dispatch 期回填的
  ``_call_context.pipeline_id`` 过滤（聚合行是当前租户全部管道，不过滤会读到他管道定义）。
- 读面回落是**兜底**而非主路：``pipeline-state.list`` 读内存 registry，而 registry
  只在 run 收尾刷新（``stage_exec.rs`` 的 ``reg.update_state``），run 进行中读到的是
  本轮之前的旧快照。
- 权限判定**按目标工具名**展开（``security_check`` / ``level_guard`` 在判定入口
  展开本工具的调用），本工具不做也不得做权限判定——判定单源在既有安全插件。
- 参数注入**不在本壳**：注入由管道注入级 ``tool_args_inject`` 前瞻完成（与原生
  工具调用共用同一份注入代码：身份/工作区/默认参数/vision/父级/agent_config/
  模板展开），壳不持有第二份注入键表（2026-11-18 用户裁定：注入只留注入方一处）。
- 参数**校验也不在本壳**：``arguments`` 解析为对象后直接透传，参数合法性终审归
  目标工具自身 handler/schema；壳只保留黑名单门禁（``tool_deny`` 否决集 +
  ``security_check``/``level_guard`` 按目标名展开的既有判定）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agentos_plugin_sdk import (
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

DYNAMIC_TOOL_STATE_KEY = "dynamic_tools"

#: 注入方（tool_args_inject）写进工具入参的同名快照键——工具调用路径的可靠来源。
#: 带 ``_`` 前缀：模型产出的下划线键已先被 ``_strip_forged_keys`` 剥除，
#: 故该键只可能是服务端权威注入值。
DYNAMIC_TOOLS_INPUT_KEY = "_dynamic_tools"

#: 目标工具的调用名 → 「读 state 取动态定义」「转发既有执行面」两个接线缝。
StateReader = Callable[[], Awaitable[Any]]
ToolInvoker = Callable[[str, dict[str, Any], str], Awaitable[Any]]

_state_reader: StateReader | None = None
_tool_invoker: ToolInvoker | None = None


class DynamicToolsUnavailable(RuntimeError):
    """动态定义读取通道不可用/返回无效（fail-closed，不猜、不放行）。"""


class ToolExecutorUnavailable(RuntimeError):
    """既有执行面（tool-executor）调用通道不可用（fail-closed）。"""


def set_state_reader(reader: StateReader | None) -> None:
    """注入/清除动态定义读取通道（server.py on_load 调用）。"""
    global _state_reader
    _state_reader = reader


def set_tool_invoker(invoker: ToolInvoker | None) -> None:
    """注入/清除既有执行面转发通道（server.py on_load 调用）。"""
    global _tool_invoker
    _tool_invoker = invoker


def _current_pipeline_id(inputs: dict[str, Any] | None = None) -> str:
    """取本调用所属管道的 pipeline_id；取不到返回空串（调用方 fail-closed）。

    两个来源，按可用性取首个非空：

    1. ``inputs["_call_context"]["pipeline_id"]`` —— **工具调用路径的唯一可用来源**。
       该键由内核 dispatch 期按管道 ``state.pipeline_id`` 构造并写入工具入参
       （``invoker/tool_pre_dispatch.rs::invoke_params`` /
       ``tool_core::build_invoke_params``）。模型不可伪造：``tool_args_inject``
       先剥掉模型产出的全部下划线键（``_strip_forged_keys``），内核
       ``tool-executor`` 再以自身 params 权威值回填（``tool_executor.rs`` 的
       ``internal_keys`` 剥离 + params 回填）。
    2. ``LogContext.pipeline_id`` —— pipeline 插件调用路径的 ``_log_ctx`` 绑定
       （``invoke_paths.rs::attempt_sidecar_pipeline``）。工具调用路径不注入该键，
       故此来源在工具面上通常为空，仅作兼容兜底。

    绝不猜管道、绝不退化为「全部管道」。
    """
    if inputs:
        ctx = inputs.get("_call_context")
        if isinstance(ctx, dict):
            value = str(ctx.get("pipeline_id") or "").strip()
            if value and value != "-":
                return value
    try:
        from agentos_plugin_sdk.logging import LogContext  # noqa: PLC0415
    except ImportError:  # 无身份来源 = 无法定位本管道定义，fail-closed
        return ""
    value = str(LogContext.get("pipeline_id") or "")
    return "" if value == "-" else value


def _require_state_reader() -> StateReader:
    if _state_reader is None:
        raise DynamicToolsUnavailable("动态定义读取通道未注入（on_load 未接线）")
    return _state_reader


def _require_tool_invoker() -> ToolInvoker:
    if _tool_invoker is None:
        raise ToolExecutorUnavailable("tool-executor 转发通道未注入（on_load 未接线）")
    return _tool_invoker


def _parse_arguments(raw: Any) -> dict[str, Any] | None:
    """解析目标参数：dict 直接透传，JSON 字符串解析为 dict。

    只做**线形解析**，不做任何 schema 校验（参数合法性终审归目标工具自身
    handler/schema；转发壳重造校验必漂移——2026-11-18 用户裁定）。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _normalize_envelope(name: str, payload: Any) -> dict[str, Any]:
    """目标调用结果 → 本节点的统一信封（成功透传，失败不吞不改写）。

    目标工具失败**不吞、不编造**：``tool-executor.invoke`` 返回什么就上行什么——
    不改写错误文本、不补字段（改了就不是目标的真实死因）。``tool_name`` 只在成功
    路径补齐，便于模型归因。

    **两处形状事实（别当成承诺）**：

    1. **本壳自身成功/目标失败可判别**：目标失败时壳**自己仍算执行成功**（转发动作
       完成了），失败信息在 ``data`` 内层（``data["success"] is False``）。下游判
       「目标失败」须读 ``data.success``，只看顶层 ``success`` 会把目标失败误判成
       成功、连败计数永不累积。
    2. **错误码分类由内核决定，不由本壳保证**：内核
       ``ToolExecutionResult``（``kernel/crates/core/src/types.rs``）只有
       ``success/data/error/duration_ms/metadata``，**没有 ``error_code`` 字段**；
       其 ``Err`` 分支（``capability_router/tool_executor.rs``）会把插件侧错误码
       统一降级成 ``{"success": false, "error": "tool execution failed: <msg>"}``。
       故 ``PLUGIN_CRASHED`` / ``MCP_TOOL_CALL_FAILED`` 这类分类**只在信封本来就带着
       它时才出现**，不能假定必然存在——消费方须按「有则用、无则读 error 文本」处理。
    """
    if isinstance(payload, dict):
        if payload.get("success") is False:
            return payload
        data = payload.get("output", payload.get("data"))
        if data is not None:
            merged = dict(data) if isinstance(data, dict) else {"content": data}
            merged.setdefault("tool_name", name)
            return merged
        merged = dict(payload)
        merged.setdefault("tool_name", name)
        return merged
    return {"tool_name": name, "content": payload}


class CallTool:
    """动态工具薄转发壳（tool 类插件工具，LLM 可见面单入口）。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义（LLM function-calling 声明）。"""
        return Tool(
            name="call_tool",
            description=(
                "调用资源搜索引入的动态工具。先用 resource_search(mode='detailed') 拿到 "
                "tool_defs 定义，再按该定义的 input_schema 传工具名与参数。"
                "（动态工具不进你的原生工具列表；每次调用按目标工具走权限判定。"
                "定义长期有效：引入一次即可反复调用（跨轮与跨 run 持久），无需重复搜索。）"
            ),
            when_to_use=[
                "resource_search 返回了 tool_defs，需要实际执行其中某个工具时",
            ],
            when_not_to_use=[
                "工具已在你自己的工具列表里 → 直接调用，无需经本工具",
                "只是想知道有哪些工具 → 用 resource_search，不要用本工具试探",
            ],
            caveats=[
                "只接受已由 resource_search 引入的动态工具名；未引入的名字会显式失败。"
                "已引入的定义长期有效（跨工具迭代与跨 run 持久保留，不会每轮重置），"
                "同一工具可直接反复调用，不必在调用前重做 resource_search",
                "参数必须符合该工具 input_schema（必填项/类型不符会直接报错，不会执行）",
                "目标工具失败时原样返回其错误，不会伪造成功",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "description": (
                            "目标工具名，须是已由 resource_search(mode='detailed') "
                            "引入过的动态工具之一（引入一次长期有效，跨轮与跨 run 持久，不必重搜）"
                        ),
                    },
                    "arguments": {
                        "type": "object",
                        "description": "目标工具参数对象，结构遵循该工具 tool_defs 里的 input_schema",
                    },
                },
                "required": ["tool", "arguments"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "description": (
                    "目标工具的执行结果（成功时透传其输出，补 tool_name）；失败时为 "
                    "{success: false, error, error_code} 显式失败信封"
                ),
                "properties": {
                    "success": {"type": "boolean"},
                    "error": {"type": "string"},
                    "error_code": {"type": "string"},
                    "tool_name": {"type": "string"},
                },
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.SYSTEM,
            tags=["tool", "dynamic", "system"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """按动态定义转发一次目标工具调用（参数解析后直接透传）。"""
        name = str(inputs.get("tool") or "").strip()
        if not name:
            return create_failure_result(error="缺少 tool（目标工具名）", error_code="INVALID_ARGUMENTS")
        arguments = _parse_arguments(inputs.get("arguments"))
        if arguments is None:
            return create_failure_result(
                error="arguments 必须是 JSON 对象（或可解析为对象的 JSON 字符串）",
                error_code="INVALID_ARGUMENTS",
            )

        # 动态面否决门禁（ADR 2026-11-18-agent-dynamic-tool-deny-list）：目标命中
        # 本 agent tool_deny 即拒——置于定义读取之前，fail-closed（定义通道异常
        # 也不放行被否决目标）；与 resource_search 搜索过滤同集双层。
        raw_deny = inputs.get("_tool_deny")
        if isinstance(raw_deny, list) and name in {str(d) for d in raw_deny if isinstance(d, str)}:
            logger.warning("[call_tool] 目标工具被 tool_deny 否决 | tool=%s", name)
            return create_failure_result(
                error=f"目标工具 '{name}' 在本 agent 的 tool_deny 否决集内，动态面不可达",
                error_code="TOOL_DENIED",
            )

        try:
            defs = await self._dynamic_defs(inputs)
        except DynamicToolsUnavailable as exc:
            logger.warning("[call_tool] 动态定义读取失败: %s", exc)
            return create_failure_result(
                error=f"动态工具面不可用: {exc}", error_code="DYNAMIC_TOOLS_UNAVAILABLE"
            )

        definition = next((d for d in defs if str(d.get("name") or "") == name), None)
        if definition is None:
            # 未引入即不可调用：动态面只列活注册表工具，名字不在其中 = 不可达，
            # 与「注册表里没有的工具」同语义（TOOL_NOT_FOUND）。
            names = sorted(str(d.get("name") or "") for d in defs)
            logger.warning("[call_tool] 目标工具未引入 | tool=%s | introduced=%s", name, names)
            return create_failure_result(
                error=(
                    f"目标工具 '{name}' 未由 resource_search 引入过（引入一次即长期有效，"
                    f"跨轮与跨 run 持续有效，不需重搜；未引入的名字不可调用）。"
                    f"已引入: {names or '无'}"
                ),
                error_code="TOOL_NOT_FOUND",
            )

        # 参数直接透传：不做 input_schema 校验（2026-11-18 用户裁定：转发壳只解析
        # 参数并转发；参数合法性的终审归目标工具自身 handler/schema）。注入仍由
        # 管道注入级 tool_args_inject 前瞻完成（与原生调用同一份代码），本壳零参与。
        # 本壳只保留黑名单门禁：上面的 tool_deny 否决集；权限/危险命令黑名单由
        # security_check / level_guard 按目标工具名+目标参数展开判定（判定单源）。
        plugin_id = str(definition.get("plugin_id") or "")
        try:
            payload = await _require_tool_invoker()(name, arguments, plugin_id)
        except ToolExecutorUnavailable as exc:
            logger.warning("[call_tool] 执行面通道不可用: %s", exc)
            return create_failure_result(error=f"执行面不可用: {exc}", error_code="TOOL_EXECUTOR_UNAVAILABLE")

        logger.info("[call_tool] 转发 | tool=%s | plugin_id=%s", name, plugin_id)
        # metadata.target_tool = **真实可达的目标名**：工具调用路径的 tool_results 条目
        # 是 ToolResult 七键封闭结构（不含 arguments），只有 metadata 能把目标名带到
        # 下游——duplicate_check 的连败归属按它落动态桶；缺它则回落壳名，会把常规桶
        # 污染成「动态面误用一次就摘掉常驻工具」。
        return create_success_result(
            data=_normalize_envelope(name, payload),
            metadata={"target_tool": name},
        )

    @staticmethod
    async def _dynamic_defs(inputs: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """取**本管道**的动态定义集：优先入参快照，回落 ``pipeline-state`` 读面。

        来源优先级：

        1. ``inputs["_dynamic_tools"]`` —— 同管道 ``tool_args_inject`` 在**同一轮**
           从活 state 注入的服务端权威快照（键带 ``_`` 前缀，模型已无法伪造）。
           这是**工具调用路径的唯一可靠来源**：``pipeline-state.list`` 读到的是
           run 收尾才刷新的 registry 常驻状态（``stage_exec`` 的
           ``reg.update_state``），run 进行中写入的 ``dynamic_tools`` 不在其中，
           本轮读取必然拿到空快照（活体验证实证 ``introduced=[]`` 恒现）。
        2. ``pipeline-state.list`` 聚合行 —— 兜底（快照缺席时，如未接线注入方）。
           聚合行是当前租户**全部管道**，必须按本管道 ``pipeline_id`` 过滤——
           不过滤会读到他管道引入的定义（跨会话越权面）。行内 ``state`` 是摘要
           对象，``dynamic_tools`` 需在 manifest 的 ``export_fields`` 声明才会出口。

        两路都取不到即 fail-closed（不猜管道、不放行）。
        """
        if inputs:
            injected = inputs.get(DYNAMIC_TOOLS_INPUT_KEY)
            if isinstance(injected, list):
                return [d for d in injected if isinstance(d, dict)]

        pipeline_id = _current_pipeline_id(inputs)
        if not pipeline_id:
            raise DynamicToolsUnavailable(
                "当前管道身份未知（工具入参无 _call_context.pipeline_id，日志上下文亦无）"
            )

        rows = await _require_state_reader()()
        if not isinstance(rows, list):
            raise DynamicToolsUnavailable(f"管道聚合行非列表: {type(rows).__name__}")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("pipeline_id") or "") != pipeline_id:
                continue
            # 行形态：内核 ``summarize_state`` 把 export_fields 声明键**平铺在行顶层**
            # （routes.rs 的 ``out.insert(k, ...)``），再补 pipeline_id/thread_id/agent_id。
            # 故 ``dynamic_tools`` 在 ``row`` 本身，不在 ``row["state"]`` 里。
            defs = row.get(DYNAMIC_TOOL_STATE_KEY)
            if isinstance(defs, list):
                return [d for d in defs if isinstance(d, dict)]
            state = row.get("state")
            if isinstance(state, dict) and isinstance(state.get(DYNAMIC_TOOL_STATE_KEY), list):
                return [d for d in state[DYNAMIC_TOOL_STATE_KEY] if isinstance(d, dict)]
            return []
        return []

"""Tool Core 插件 —— 单调用契约的工具执行核（并行 for-each 循环体唯一步骤）。

引擎 for-each 循环对 ``state.raw_tool_calls`` 逐元素执行，把当前调用注入迭代
局部 state 顶层键 ``current_call``（形状 ``{name, args|arguments, id}``，与
raw_tool_calls 数组元素同构）；本插件契约是**单调用**：

读 current_call → 预填短路（pre_decided_results 按 call_id/工具名命中即出
预填结果，不执行）→ 经 tool-executor capability 调内核唯一执行漏斗 →
输出契约校验（tool_output_contracts，违规 fail-closed）→ 截断
（config ``tool_output`` 命名空间，缺省 16384/12288/2048）→ 发
tool_start/tool_result 事件（event-bus.emit）→ 产单条 tool 消息 op（SDK
tool_result_protocol，契约夹具双车道）→ collect 键各写单元素数组
（tool_results/_full_tool_results/_executed_tool_calls）与
submitted_task_ids 增量（collect_append 键，父侧追加）。

行为蓝本 = 同目录 Rust 实现（src/，休眠后备，manifest 不再引用）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

# 共享根解析（plugins/shared）：本文件位于 plugins/shared/pipeline/core/
# tool_core/，上溯 3 级；`pipeline` 包经它解析（llm_core 同款）。必须在
# 首方 import 之前注入。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from agentos_plugin_sdk.tool_result_protocol import (  # noqa: E402
    build_tool_result_ops,
    serialize_for_content,
)
from pipeline.plugin import ICorePlugin, PluginContext  # noqa: E402

logger = logging.getLogger(__name__)

# 能力委托（server.py on_load 注入；测试注入伪实现）：
# - tool 委托 = tool-executor.invoke（内核唯一工具执行漏斗）；
# - event 委托 = event-bus.emit（fire-and-forget，失败不阻断主流程）。
ToolDelegate = Callable[[dict[str, Any], float | None], Awaitable[Any]]
EventDelegate = Callable[[dict[str, Any]], Awaitable[Any]]

# capability call 超时（长任务工具语义，对齐 llm_core 的长等待理由）：
# 缺省 600s，config.tool_output.invoke_timeout_s 可覆盖。
DEFAULT_INVOKE_TIMEOUT_S = 600.0

# 工具输出超长截断契约（2026-09-09 用户裁定）：>max 触发，头 head 尾 tail +
# 中部省略标记。全文经 _full_tool_results 交 tool_cache_writer 写缓存；LLM 要
# 全文重发同一调用即可命中缓存取回。
TOOL_OUTPUT_MAX_CHARS = 16 * 1024
TOOL_OUTPUT_HEAD_CHARS = 12 * 1024
TOOL_OUTPUT_TAIL_CHARS = 2 * 1024

# pre_decided_results entry 的协议已知键（七键）；其余键 = 扩展位随结果透传
# （如 retry_allowed/arguments，duplicate_check 按键认读），不进 messages
# envelope。
_PRE_DECIDED_KNOWN_KEYS = frozenset(
    {"call_id", "tool_name", "success", "error", "data", "metadata", "duration_ms"}
)


def _failed_result(tool_name: str, error: str, duration_ms: float = 0.0) -> dict[str, Any]:
    """规范失败结果（data 归 None，对齐 Rust ToolResult::failed）。"""
    return {
        "tool_name": tool_name,
        "success": False,
        "error": error,
        "data": None,
        "metadata": None,
        "duration_ms": round(duration_ms, 1),
    }


def _args_parse_failed(tool_name: str) -> dict[str, Any]:
    """args JSON 解析失败的失败结果（文案对齐 Rust types.rs args_parse_failed）。"""
    return {
        "tool_name": tool_name,
        "success": False,
        "error": (
            f"工具 {tool_name} 的调用参数 JSON 格式无效（可能参数内容过长导致被截断）。"
            "请将操作拆分为多个小步骤：\n"
            "1. 如果是 file_write：请分多次写入，每次写入一个章节或部分内容\n"
            "2. 如果是其他工具：请减少参数中的文本量\n"
            "3. 不要一次性传入大量文本作为参数"
        ),
        "data": None,
        "metadata": None,
        "duration_ms": 0.0,
    }


def truncate_tool_output(text: str, limits: dict[str, int]) -> str:
    """超长文本头尾保留 + 中部省略标记（文案对齐 Rust truncate_tool_output）。"""
    total = len(text)
    if total <= limits["max_chars"]:
        return text
    head = text[: limits["head_chars"]]
    tail = text[total - limits["tail_chars"]:] if limits["tail_chars"] > 0 else ""
    omitted = total - limits["head_chars"] - limits["tail_chars"]
    return (
        f"{head}\n…[工具输出已截断：省略 {omitted} 字符；"
        f"完整结果已缓存，重发同一工具调用即可取回]…\n{tail}"
    )


def truncate_value_strings(value: Any, limits: dict[str, int]) -> Any:
    """深拷贝并截断 data 中超长字符串（dict/list 递归，标量原样）。"""
    if isinstance(value, str):
        if len(value) > limits["max_chars"]:
            return truncate_tool_output(value, limits)
        return value
    if isinstance(value, list):
        return [truncate_value_strings(v, limits) for v in value]
    if isinstance(value, dict):
        return {k: truncate_value_strings(v, limits) for k, v in value.items()}
    return value


def error_to_contract_string(err: Any) -> str:
    """错误 → string（流式契约要求）：对象取 message 字段否则整体 JSON 串化。"""
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        message = err.get("message")
        if isinstance(message, str):
            return message
    return json.dumps(err, ensure_ascii=False, default=str)


class ToolCore(ICorePlugin):
    """工具执行核 —— 单调用契约：执行 state.current_call 指向的一个工具调用。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._tool_delegate: ToolDelegate | None = None
        self._event_delegate: EventDelegate | None = None

    @property
    def name(self) -> str:
        return "tool_core"

    @property
    def priority(self) -> int:
        return 50

    def set_tool_delegate(self, delegate: ToolDelegate | None) -> None:
        """注入 tool-executor 委托（async ``(params, timeout) -> Any``）。"""
        self._tool_delegate = delegate

    def set_event_delegate(self, delegate: EventDelegate | None) -> None:
        """注入 event-bus 委托（async ``(params) -> Any``，fire-and-forget）。"""
        self._event_delegate = delegate

    # ── 配置 ─────────────────────────────────────────────

    @staticmethod
    def _tool_output_namespace(config: dict[str, Any]) -> dict[str, Any]:
        ns = config.get("tool_output")
        return ns if isinstance(ns, dict) else {}

    def _limits_from_config(self) -> dict[str, int]:
        """截断参数（manifest config_files 的 ``tool_output`` 命名空间）。"""
        ns = self._tool_output_namespace(self._config)
        return {
            "max_chars": int(ns.get("max_chars", TOOL_OUTPUT_MAX_CHARS)),
            "head_chars": int(ns.get("head_chars", TOOL_OUTPUT_HEAD_CHARS)),
            "tail_chars": int(ns.get("tail_chars", TOOL_OUTPUT_TAIL_CHARS)),
        }

    def _invoke_timeout_s(self) -> float:
        ns = self._tool_output_namespace(self._config)
        return float(ns.get("invoke_timeout_s", DEFAULT_INVOKE_TIMEOUT_S))

    # ── 主流程 ───────────────────────────────────────────

    # 设计意图：core 插件返回 state_updates dict（与 IPlugin.execute 的
    # PluginResult 不同，由引擎包装层转换）；协变契约类型系统无法表达，
    # 豁免 override 检查（与 SDK ICorePlugin/llm_core 同款豁免）。
    async def execute(self, ctx: PluginContext) -> dict[str, Any]:  # type: ignore[override]
        """执行单个工具调用，返回 state_updates（collect 键各为单元素数组）。"""
        state = ctx.state
        raw_call = state.get("current_call")
        if not isinstance(raw_call, dict) or not raw_call:
            # 循环体不会以空调用触发本步骤；直接调用（step context 注入）缺失
            # 迭代变量时同样零动作。
            return {}

        limits = self._limits_from_config()
        name, args, call_id, fail_result = self._parse_call(raw_call)
        if fail_result is not None:  # args JSON 无效：失败结果直出（不发事件）
            result = fail_result
        else:
            hit = self._match_pre_decided(state, name, call_id)
            if hit is not None:
                # 预填命中：跳过执行（不调 tool-executor），事件照发——执行
                # 下沉唯一入口，预填结果不进内核执行面。
                result = self._result_from_pre_decided_entry(name, hit)
                await self._emit_tool_event(
                    state, "tool_start", name, args, call_id, None, limits
                )
                await self._emit_tool_event(
                    state, "tool_result", name, args, call_id, result, limits
                )
            else:
                result = await self._execute_single_tool(state, name, args, call_id, limits)

        display = dict(result)
        display["data"] = truncate_value_strings(result.get("data"), limits)
        return await self._collect_side_effects(state, raw_call, result, display, limits)

    # ── 解析 ─────────────────────────────────────────────

    def _parse_call(
        self, raw: dict[str, Any]
    ) -> tuple[str, dict[str, Any], str | None, dict[str, Any] | None]:
        """解析 current_call（对齐 Rust ToolCall::parse）。

        Returns:
            ``(name, args, call_id, fail_result)``；解析失败时 fail_result 携带
            失败结果（错误文案对齐 Rust types.rs args_parse_failed）。
        """
        raw_name = raw.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
        raw_args = raw.get("args")
        if raw_args is None:
            raw_args = raw.get("arguments")
        if raw_args is None:
            raw_args = {}
        raw_id = raw.get("id")
        call_id = raw_id if isinstance(raw_id, str) else None

        if isinstance(raw_args, str):
            try:
                args: Any = json.loads(raw_args)
            except json.JSONDecodeError:
                return name, {}, call_id, _args_parse_failed(name)
        else:
            args = raw_args
        if not isinstance(args, dict):
            args = {}
        return name, args, call_id, None

    # ── 预填短路（ADR 2026-09-28 结果预填）────────────────

    @staticmethod
    def _match_pre_decided(
        state: dict[str, Any], tool_name: str, call_id: str | None
    ) -> dict[str, Any] | None:
        """命中预填结果：有 id 按 call_id，无 id（或 id 未命中）按工具名兜底
        （名字匹配只对没有 call_id 的 entry 生效）。"""
        entries = state.get("pre_decided_results")
        if not isinstance(entries, list):
            return None
        if call_id:
            for entry in entries:
                if isinstance(entry, dict) and entry.get("call_id") == call_id:
                    return entry
        for entry in entries:
            if (
                isinstance(entry, dict)
                and not entry.get("call_id")
                and entry.get("tool_name") == tool_name
            ):
                return entry
        return None

    @staticmethod
    def _result_from_pre_decided_entry(
        tool_name: str, entry: dict[str, Any]
    ) -> dict[str, Any]:
        """entry → 结果 dict：七键直达（success 缺省 False fail-closed），其余
        键收入 extra 透传（不进 envelope）。"""
        raw_error = entry.get("error")
        raw_metadata = entry.get("metadata")
        result: dict[str, Any] = {
            "tool_name": entry.get("tool_name") or tool_name,
            "success": bool(entry.get("success", False)),
            "error": raw_error if isinstance(raw_error, str) else None,
            "data": entry.get("data"),
            "metadata": raw_metadata,
            "duration_ms": round(float(entry.get("duration_ms") or 0.0), 1),
        }
        for key, value in entry.items():
            if key not in _PRE_DECIDED_KNOWN_KEYS:
                result[key] = value
        return result

    # ── 执行 ─────────────────────────────────────────────

    def _build_invoke_params(
        self, name: str, args: dict[str, Any], call_id: str | None, state: dict[str, Any]
    ) -> dict[str, Any]:
        """tool-executor.invoke 参数（_call_context 为前端路由键，内核透传给
        工具 sidecar 供执行中推 tool_progress）。"""
        thread_id = state.get("session_id") or state.get("thread_id") or ""
        return {
            "tool_name": name,
            "args": args,
            "_call_context": {
                "call_id": call_id or "",
                "pipeline_id": state.get("pipeline_id") or "",
                "message_id": state.get("message_id") or "",
                "thread_id": thread_id,
            },
        }

    async def _execute_single_tool(
        self,
        state: dict[str, Any],
        name: str,
        args: dict[str, Any],
        call_id: str | None,
        limits: dict[str, int],
    ) -> dict[str, Any]:
        """执行单个工具：tool_start 事件 → capability 调用 → 契约校验 →
        tool_result 事件。"""
        await self._emit_tool_event(state, "tool_start", name, args, call_id, None, limits)

        start = time.monotonic()
        if self._tool_delegate is None:
            result = _failed_result(name, "host capability call unavailable")
        else:
            params = self._build_invoke_params(name, args, call_id, state)
            try:
                resp = await self._tool_delegate(params, self._invoke_timeout_s())
                result = self._parse_executor_response(name, resp)
            except Exception as exc:  # noqa: BLE001 —— 执行通道故障转失败结果带回 LLM
                logger.warning("[%s] tool-executor invoke failed: %s", self.name, exc)
                result = _failed_result(
                    name, f"host capability call failed: {exc}", (time.monotonic() - start) * 1000
                )
            result["duration_ms"] = round((time.monotonic() - start) * 1000, 1)

        # output_schema 消费端：成功结果按 tool_output_contracts 校验，违规
        # fail-closed 转失败——错误带回 LLM 自我修正。放在 tool_result 事件前，
        # 事件与持久化结果的 success/error 同源（冷热一致）。
        if result["success"] and self._validation_enabled(state):
            err = self._validate_output_contract(state, name, result.get("data"))
            if err is not None:
                result = _failed_result(
                    name,
                    f"output_schema validation failed: {err}",
                    float(result.get("duration_ms") or 0.0),
                )

        await self._emit_tool_event(state, "tool_result", name, args, call_id, result, limits)
        return result

    @staticmethod
    def _parse_executor_response(tool_name: str, resp: Any) -> dict[str, Any]:
        """解析 tool-executor.invoke 信封（对齐 Rust parse_tool_executor_response；
        duration_ms 由调用方本地计时回填）。"""
        if not isinstance(resp, dict):
            resp = {}
        if resp.get("success"):
            return {
                "tool_name": tool_name,
                "success": True,
                "error": None,
                "data": resp.get("data"),
                "metadata": resp.get("metadata"),
                "duration_ms": 0.0,
            }
        error = resp.get("error")
        return _failed_result(
            tool_name,
            error if isinstance(error, str) and error else "tool execution failed",
        )

    # ── 输出契约校验（常用子集，对齐 Rust output_validate.rs）──

    @staticmethod
    def _validation_enabled(state: dict[str, Any]) -> bool:
        """tool_output_validation == "off" 时整体跳过（缺省开启）。"""
        return state.get("tool_output_validation") != "off"

    def _validate_output_contract(
        self, state: dict[str, Any], tool_name: str, data: Any
    ) -> str | None:
        """按 tool_output_contracts[tool_name]["schema"] 校验 data；无契约/无
        schema 返回 None（通过），违例返回首个违规点描述。"""
        contracts = state.get("tool_output_contracts")
        if not isinstance(contracts, dict):
            return None
        contract = contracts.get(tool_name)
        if not isinstance(contract, dict):
            return None
        schema = contract.get("schema")
        if not isinstance(schema, dict):
            return None
        return self._validate_at(schema, data, "")

    def _validate_at(self, schema: dict[str, Any], data: Any, path: str) -> str | None:
        type_decl = schema.get("type")
        if type_decl is not None and (err := self._check_type(type_decl, data, path)):
            return err
        enum_variants = schema.get("enum")
        if isinstance(enum_variants, list) and not any(
            self._json_eq(v, data) for v in enum_variants
        ):
            return (
                f"{path}: value {self._truncate_repr(data)} not in enum "
                f"[{', '.join(self._truncate_repr(v) for v in enum_variants)}]"
            )
        required = schema.get("required")
        if isinstance(required, list) and isinstance(data, dict):
            for key in required:
                if isinstance(key, str) and key not in data:
                    return f"{path}: missing required field `{key}`"
        properties = schema.get("properties")
        if isinstance(properties, dict) and isinstance(data, dict):
            for key, prop_schema in properties.items():
                if key in data and isinstance(prop_schema, dict):
                    if err := self._validate_at(prop_schema, data[key], f"{path}.{key}"):
                        return err
        items_schema = schema.get("items")
        if isinstance(items_schema, dict) and isinstance(data, list):
            for i, item in enumerate(data):
                if err := self._validate_at(items_schema, item, f"{path}[{i}]"):
                    return err
        return None

    def _check_type(self, type_decl: Any, data: Any, path: str) -> str | None:
        def matches(t: str) -> bool:
            if t == "object":
                return isinstance(data, dict)
            if t == "array":
                return isinstance(data, list)
            if t == "string":
                return isinstance(data, str)
            if t == "boolean":
                return isinstance(data, bool)
            if t == "null":
                return data is None
            if t == "number":
                return isinstance(data, (int, float)) and not isinstance(data, bool)
            if t == "integer":
                # 整值浮点（1.0）按 integer 兼容（对齐 Rust 宽松收窄）。
                if isinstance(data, bool):
                    return False
                return isinstance(data, int) or (
                    isinstance(data, float) and data.is_integer()
                )
            return True  # 未知类型名不约束（宽松）

        if isinstance(type_decl, str):
            ok = matches(type_decl)
        elif isinstance(type_decl, list):
            ok = any(isinstance(t, str) and matches(t) for t in type_decl)
        else:
            ok = True
        if ok:
            return None
        return (
            f"{path}: expected type {self._truncate_repr(type_decl)}, "
            f"got {self._json_type_name(data)} ({self._truncate_repr(data)})"
        )

    @staticmethod
    def _json_eq(a: Any, b: Any) -> bool:
        if isinstance(a, bool) or isinstance(b, bool):
            return a is b
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return float(a) == float(b)
        return a == b

    @staticmethod
    def _json_type_name(data: Any) -> str:
        if data is None:
            return "null"
        if isinstance(data, bool):
            return "boolean"
        if isinstance(data, (int, float)):
            return "number"
        if isinstance(data, str):
            return "string"
        if isinstance(data, list):
            return "array"
        if isinstance(data, dict):
            return "object"
        return "unknown"

    @staticmethod
    def _truncate_repr(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)[:80]

    # ── 事件 ─────────────────────────────────────────────

    async def _emit_tool_event(
        self,
        state: dict[str, Any],
        kind: str,
        name: str,
        args: dict[str, Any],
        call_id: str | None,
        result: dict[str, Any] | None,
        limits: dict[str, int],
    ) -> None:
        """发 tool_start / tool_result 事件（event-bus.emit，fire-and-forget）。

        委托未注入或发射失败只记日志不阻断——事件是观测面，主流程失败面必须
        隔离（对齐 Rust 忽略返回的 fire-and-forget 语义）。
        """
        if self._event_delegate is None:
            return
        payload: dict[str, Any] = {
            "thread_id": state.get("session_id") or state.get("thread_id") or "",
            "pipeline_id": state.get("pipeline_id") or "",
            "message_id": state.get("message_id") or "",
            "call_id": call_id or "",
            "tool_name": name,
        }
        if kind == "tool_start":
            payload["args"] = args
        elif result is not None:
            data = result.get("data")
            success = bool(result.get("success"))
            if success:
                # 结果文本 = serialize（全文）后按限长截断（对齐 Rust emit_tool_event）。
                payload["result"] = truncate_tool_output(serialize_for_content(data), limits)
            else:
                payload["result"] = f"Error: {result.get('error') or 'unknown'}"
            # 契约 result_data 要 object：失败路径 data 为 None、工具也可返回
            # 标量/数组——非对象省略该字段（结果全文仍在 result）。
            if isinstance(data, dict):
                payload["result_data"] = truncate_value_strings(data, limits)
            payload["success"] = success
            payload["duration_ms"] = round(float(result.get("duration_ms") or 0.0), 1)
            if not success:
                # 统一错误信封（单一真值源 config/kernel/error_codes.json）。
                # 流式契约 error 要 string，信封对象会被契约网关 fail-closed
                # 整事件丢弃——降级为 string 进载荷（message 优先）。
                envelope = {
                    "code": "TOOL_EXEC_FAILED",
                    "message": result.get("error") or "unknown",
                    "source": "plugin",
                    "retryable": False,
                    "details": None,
                    "request_id": None,
                }
                payload["error"] = error_to_contract_string(envelope)
        params = {"event": kind, "payload": payload}
        try:
            await self._event_delegate(params)
        except Exception as exc:  # noqa: BLE001 —— fire-and-forget：观测面失败不阻断
            logger.warning("[%s] emit %s event failed: %s", self.name, kind, exc)

    # ── 消息 op ──────────────────────────────────────────

    async def _build_message_ops(
        self,
        state: dict[str, Any],
        raw_call: dict[str, Any],
        result: dict[str, Any],
        display: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """构造本调用的消息增量 ops：assistant 补造（快照缺配对消息时）+ tool
        结果 op（SDK build_tool_result_ops）+ 多模态 user 消息 op。"""
        ops: list[dict[str, Any]] = []
        messages = state.get("messages")
        msg_list = messages if isinstance(messages, list) else []
        has_tool_call_msg = any(
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and m.get("tool_calls") is not None
            for m in msg_list
        )
        if not has_tool_call_msg:
            ops.append({"op": "set", "msg": self._build_assistant_message(state, raw_call)})

        tool_ops: list[dict[str, Any]] = list(build_tool_result_ops([raw_call], [display])["_ops"])
        if state.get("output_truncated") and display.get("success") and tool_ops:
            msg = tool_ops[0].get("msg")
            if isinstance(msg, dict):
                msg["content"] = str(msg.get("content", "")) + self._truncation_note(
                    display.get("data"), display.get("tool_name", "")
                )
        ops.extend(tool_ops)

        pending_images = self._collect_pending_images(result)
        if pending_images:
            await self._emit_multimodal_event(state, pending_images)
            ops.append(
                {"op": "set", "msg": self._build_multimodal_message(state, pending_images)}
            )
        return ops

    def _build_assistant_message(
        self, state: dict[str, Any], raw_call: dict[str, Any]
    ) -> dict[str, Any]:
        """补造 assistant tool_calls 消息（OpenAI 规范，对齐 Rust messages::rebuild）。"""
        raw_name = raw_call.get("name")
        name = raw_name if isinstance(raw_name, str) else ""
        arguments = raw_call.get("args")
        if arguments is None:
            arguments = raw_call.get("arguments")
        if arguments is None:
            arguments = {}
        arguments_str = (
            arguments
            if isinstance(arguments, str)
            else json.dumps(arguments, ensure_ascii=False)
        )
        raw_id = raw_call.get("id")
        call_id = raw_id if isinstance(raw_id, str) else None
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id or "call_0",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments_str},
                }
            ],
        }
        thinking = state.get("raw_thinking")
        if thinking is not None:
            msg["reasoning_content"] = thinking
        return msg

    @staticmethod
    def _truncation_note(data: Any, tool_name: str) -> str:
        """max_tokens 截断注记（对齐 Rust messages.rs rebuild 的 output_truncated
        分支）：file_write/file_append 追加续写引导。"""
        lines = data.get("lines") if isinstance(data, dict) else None
        if lines is not None:
            note = (
                f"\n\n⚠️ 本次输出因达到 max_tokens 被截断，结果可能基于不完整参数。"
                f" 已写入 {lines} 行。"
            )
        else:
            note = "\n\n⚠️ 本次输出因达到 max_tokens 被截断，结果可能基于不完整参数。"
        if tool_name in ("file_write", "file_append"):
            note += " 如内容未写完，请用 file_write(action=append) 追加续写，勿用 write 覆盖。"
        return note

    # ── 多模态（对齐 Rust messages::inject_multimodal）────

    @staticmethod
    def _collect_pending_images(result: dict[str, Any]) -> list[dict[str, str]]:
        """从全文结果收集图片（base64_data+mime_type / images[] / metadata
        multimodal_content data URL）。"""
        pending: list[dict[str, str]] = []
        data = result.get("data")
        if isinstance(data, dict):
            b64 = data.get("base64_data")
            mime = data.get("mime_type")
            if isinstance(b64, str) and isinstance(mime, str):
                raw_path = data.get("path")
                pending.append(
                    {
                        "base64": b64,
                        "mime_type": mime,
                        "path": raw_path if isinstance(raw_path, str) else "",
                    }
                )
            images = data.get("images")
            if isinstance(images, list):
                for img in images:
                    if not isinstance(img, dict) or not isinstance(img.get("base64"), str):
                        continue
                    mime_type = img.get("mime_type")
                    raw_path = img.get("path")
                    pending.append(
                        {
                            "base64": img["base64"],
                            "mime_type": mime_type if isinstance(mime_type, str) else "image/png",
                            "path": raw_path if isinstance(raw_path, str) else "",
                        }
                    )
        metadata = result.get("metadata")
        if isinstance(metadata, dict):
            blocks = metadata.get("multimodal_content")
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "image_url":
                        continue
                    image_url = block.get("image_url")
                    url = image_url.get("url") if isinstance(image_url, dict) else None
                    if not isinstance(url, str) or not url.startswith("data:"):
                        continue
                    rest = url[len("data:"):]
                    if ";base64," not in rest:
                        continue
                    mime, b64 = rest.split(";base64,", 1)
                    pending.append({"base64": b64, "mime_type": mime, "path": ""})
        return pending

    async def _emit_multimodal_event(
        self, state: dict[str, Any], pending: list[dict[str, str]]
    ) -> None:
        """发 tool_multimedia_result 事件（仅 mime_type + path，不含 base64）。"""
        if self._event_delegate is None:
            return
        params = {
            "event": "tool_multimedia_result",
            "payload": {
                "thread_id": state.get("session_id") or state.get("thread_id") or "",
                "pipeline_id": state.get("pipeline_id") or "",
                "message_id": state.get("message_id") or "",
                "count": len(pending),
                "multimedia": [
                    {"mime_type": img["mime_type"], "path": img["path"]} for img in pending
                ],
            },
        }
        try:
            await self._event_delegate(params)
        except Exception as exc:  # noqa: BLE001 —— fire-and-forget：观测面失败不阻断
            logger.warning("[%s] emit tool_multimedia_result failed: %s", self.name, exc)

    def _build_multimodal_message(
        self, state: dict[str, Any], pending: list[dict[str, str]]
    ) -> dict[str, Any]:
        """视觉可用 → 多模态 user 消息；不可用 → 文本引导（文案对齐 Rust）。"""
        if state.get("llm_supports_vision"):
            blocks: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": f"[工具截图] 共 {len(pending)} 张图片，请分析截图内容：",
                }
            ]
            for img in pending:
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{img['mime_type']};base64,{img['base64']}"
                        },
                    }
                )
            return {"role": "user", "name": "tool_images", "content": blocks}
        paths = [img["path"] for img in pending if img["path"]]
        paths_str = ", ".join(paths) if paths else "见工具返回"
        content = (
            f"[工具截图] 已保存 {len(pending)} 张截图（{paths_str}）。"
            "当前模型不支持图片分析，请使用 mcp__4_5v_mcp__analyze_image 工具分析截图内容，"
            "获取文本描述后继续验证。"
        )
        return {"role": "user", "name": "tool_images", "content": content}

    # ── state_updates 组装 ───────────────────────────────

    async def _collect_side_effects(
        self,
        state: dict[str, Any],
        raw_call: dict[str, Any],
        result: dict[str, Any],
        display: dict[str, Any],
        limits: dict[str, int],
    ) -> dict[str, Any]:
        """组装 state_updates（并行归并契约：collect 键单元素数组、
        submitted_task_ids 写增量、raw_error 仅 task_failed 级才置）。"""
        data = result.get("data")
        success = bool(result.get("success"))
        if success:
            if isinstance(data, dict):
                serialized = serialize_for_content(data) or json.dumps(
                    data, ensure_ascii=False, default=str
                )
                preview = truncate_tool_output(serialized, limits)[:200]
            else:
                preview = json.dumps(data, ensure_ascii=False, default=str)[:200]
        else:
            preview = truncate_tool_output(f"Error: {result.get('error') or 'unknown'}", limits)

        updates: dict[str, Any] = {
            "tool_results": [display],
            "_full_tool_results": [dict(result)],
            "_executed_tool_calls": [raw_call],
            "raw_result": preview,
            "messages": {
                "_ops": await self._build_message_ops(state, raw_call, result, display)
            },
        }

        data_metadata = data.get("metadata") if isinstance(data, dict) else None
        # 任务系统级失败（对齐 Rust collect_side_effects 的 has_task_failed 分支）：
        # 置 ended + raw_error。平时不写 raw_error——并行归并 last-wins，写 None
        # 会抹掉其他迭代的错误。
        if isinstance(data_metadata, dict) and data_metadata.get("task_failed"):
            error = data.get("error") if isinstance(data, dict) else None
            updates["raw_error"] = (
                error if isinstance(error, str) and error else "任务系统级失败"
            )
            updates["ended"] = True

        meta = data_metadata if isinstance(data_metadata, dict) else None
        if meta is None and isinstance(result.get("metadata"), dict):
            meta = result["metadata"]

        if success:
            action = meta.get("action") if isinstance(meta, dict) else None
            if action in ("task_submit", "task_submit_container") and isinstance(data, dict):
                task_id = data.get("task_id")
                if isinstance(task_id, str) and task_id:
                    snapshot_ids = state.get("submitted_task_ids")
                    snapshot = snapshot_ids if isinstance(snapshot_ids, list) else []
                    if task_id not in snapshot:
                        # 增量语义（collect_append 键）：只写本调用新 id，父侧追加。
                        updates["submitted_task_ids"] = [task_id]
                    # latest_task 落键仅当快照缺失（并行归并 last-wins，缺省才写）。
                    if "task_id" not in state:
                        updates["task_id"] = task_id
                    workspace = data.get("resolved_workspace") or data.get("workspace")
                    if isinstance(workspace, str) and workspace and "workspace" not in state:
                        updates["workspace"] = workspace

            tool_name = result.get("tool_name")
            if tool_name == "task_evaluate" and isinstance(meta, dict):
                if meta.get("result") == "completed":
                    updates["task_evaluation_completed"] = True

            if tool_name == "human_interaction":
                conv_flag = isinstance(data, dict) and "conversation_mode" in data
                if not conv_flag and isinstance(data, dict):
                    for key in ("output", "data"):
                        inner = data.get(key)
                        if isinstance(inner, dict) and "conversation_mode" in inner:
                            conv_flag = True
                            break
                if conv_flag:
                    updates["_pending_route_signal"] = {
                        "route_type": "wait",
                        "reason": "human_interaction: user arrived, entering conversation",
                    }

        logger.info(
            "[%s] call executed | tool=%s | success=%s | duration_ms=%s",
            self.name,
            result.get("tool_name"),
            success,
            result.get("duration_ms"),
        )
        logger.debug(
            "[%s] state_updates keys=%s | raw_result=%.80s",
            self.name,
            sorted(updates),
            preview,
        )
        return updates

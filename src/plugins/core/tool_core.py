"""Tool Core 插件 — 工具执行核心（M3 完善）。

负责执行 LLM 返回的工具调用：
- 从 state["raw_tool_calls"] 读取工具调用列表
- 逐个执行已注册的工具函数
- 使用 asyncio.wait_for 设置超时保护
- 收集执行结果并写入 state
- 支持 IsolationExecutor 在 Docker 容器中执行
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from typing import Any, Callable, TYPE_CHECKING

from pipeline.plugin import ICorePlugin, PluginContext
from pipeline.types import ErrorPolicy, StateKeys
from tools.registry import ToolRegistry

if TYPE_CHECKING:
    from isolation.executor import IsolationExecutor

logger = logging.getLogger(__name__)


class ToolCore(ICorePlugin):
    """工具执行 Core — 从 raw_tool_calls 读取并执行工具调用。

    执行流程：
    1. 从 ctx.state["raw_tool_calls"] 获取待执行的工具调用列表
    2. 逐个查找已注册的工具函数并执行
    3. 使用 asyncio.wait_for 为每个调用设置超时保护
    4. 收集所有结果（含成功/失败状态和耗时）
    5. 将结果写入 state

    Class Attributes:
        error_policy: 错误策略为 RETRY（工具执行可重试）

    Attributes:
        _config: 插件配置字典
        _tools: 已注册的工具函数映射，键为工具名，值为可调用对象
        _tool_registry: 外部工具注册表引用（可选，用于批量注册）
        _default_timeout: 工具执行默认超时时间（秒）
        _isolation_executor: 隔离执行器实例（可选，用于 Docker 容器执行）
    """

    error_policy = ErrorPolicy.RETRY

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化 Tool Core 插件。

        Args:
            config: 插件配置字典，支持以下键：
                - timeout: 工具执行超时时间（秒），默认 30
        """
        self._config = config or {}
        self._tools: dict[str, Callable[..., Any]] = {}
        self._tool_registry: ToolRegistry | None = None
        self._default_timeout: float = self._config.get("timeout", 30.0)
        self._isolation_executor: IsolationExecutor | None = None

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_core"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 50

    def register_tool(self, name: str, func: Callable[..., Any]) -> None:
        """注册一个工具函数。

        Args:
            name: 工具名称，需与 LLM tool_calls 中的 name 匹配
            func: 工具执行函数（同步或异步）
        """
        self._tools[name] = func
        logger.debug("[%s] Tool registered: %s", self.name, name)

    def register_tools_from_registry(self, registry: ToolRegistry) -> None:
        """从 ToolRegistry 批量注册工具。

        将 ToolRegistry 中所有已注册工具的处理函数导入到 ToolCore 中，
        使 ToolCore 能够执行这些工具。

        Args:
            registry: 工具注册表实例
        """
        self._tool_registry = registry
        for tool_def in registry.list_all():
            handler = registry.get_handler(tool_def.name)
            if handler is not None:
                self._tools[tool_def.name] = handler
                logger.debug(
                    "[%s] Tool imported from registry: %s",
                    self.name, tool_def.name,
                )

    def set_isolation_executor(self, executor: IsolationExecutor) -> None:
        """设置隔离执行器。

        注入 IsolationExecutor 实例后，ToolCore 将优先通过
        执行器执行工具，根据 state["execution_contexts"] 中的
        隔离决策在 Docker 或宿主机中执行。

        Args:
            executor: IsolationExecutor 实例
        """

    def _get_schema_timeout_default(self, tool_name: str) -> float | None:
        """从工具 schema 中获取 timeout_seconds 的默认值。

        当 LLM 调用工具时未传递 timeout_seconds 参数，
        从工具定义的 input_schema 中读取默认值作为 ToolCore 超时。

        Args:
            tool_name: 工具名称

        Returns:
            schema 中的默认超时秒数，无则返回 None
        """
        if self._tool_registry is None:
            return None
        tool_def = self._tool_registry.get_tool(tool_name)
        if tool_def is None:
            return None
        schema = getattr(tool_def, "input_schema", None)
        if not isinstance(schema, dict):
            return None
        timeout_prop = schema.get("properties", {}).get("timeout_seconds")
        if not isinstance(timeout_prop, dict):
            return None
        default = timeout_prop.get("default")
        if default is not None and float(default) > 0:
            return float(default)
        return None
        self._isolation_executor = executor
        logger.info("[%s] IsolationExecutor 已注入", self.name)

    def _get_tool(self, name: str) -> Callable[..., Any] | None:
        """获取工具函数。

        优先从本地注册表查找，然后从外部 ToolRegistry 查找。

        Args:
            name: 工具名称

        Returns:
            工具函数，未找到时返回 None
        """
        if name in self._tools:
            return self._tools[name]
        if self._tool_registry and self._tool_registry.has(name):
            return self._tool_registry.get_handler(name)
        return None

    # 服务注入映射：tool_args 中的下划线前缀参数名 → ctx.get_service() 的 key
    _SERVICE_INJECT_MAP: dict[str, str] = {
        "_task_service": "task_service",
        "_message_queue": "message_queue",
        "_tool_registry": "tool_registry",
        "_session": "db_session",
        "_memory_service": "memory_service",
    }

    @staticmethod
    def _normalize_tool_result(result: Any) -> Any:
        """将工具返回值标准化为可 JSON 序列化的对象。

        如果工具返回 ToolExecutionResult 实例，提取其 output/data 字段；
        如果是 dict 或基础类型，直接返回；否则转为字符串。

        Args:
            result: 工具函数的原始返回值

        Returns:
            可 JSON 序列化的对象
        """
        if result is None:
            return None

        if hasattr(result, "to_dict"):
            return result.to_dict()

        if hasattr(result, "output"):
            return result.output

        if hasattr(result, "data"):
            return result.data

        if isinstance(result, (dict, list, str, int, float, bool)):
            return result

        return str(result)

    async def _execute_single_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        timeout: float,
        services: dict[str, Any] | None = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """执行单个工具调用。

        执行前自动将 services 中的依赖注入到 tool_args（仅注入
        工具尚未提供的下划线前缀参数），使工具函数能获取
        TaskService、MessageQueue 等运行时依赖，无需 CLI 闭包包装。

        执行完成后通过 on_chunk 发射 tool_result 事件，供 CLI 实时显示。

        Args:
            tool_name: 工具名称
            tool_args: 工具调用参数
            timeout: 执行超时时间（秒）
            services: 管道共享服务字典（来自 ctx._services）
            on_chunk: 流式事件回调（来自 CLI 的 on_chunk）

        Returns:
            工具执行结果字典，包含 tool_name、success、data/error、duration_ms
        """
        if on_chunk:
            on_chunk({"type": "tool_start", "tool_name": tool_name})

        if services:
            for param_key, service_key in self._SERVICE_INJECT_MAP.items():
                if param_key not in tool_args and service_key in services:
                    tool_args[param_key] = services[service_key]
                    logger.debug(
                        "[%s] Injected service '%s' to tool '%s'",
                        self.name, service_key, tool_name,
                    )

        func = self._get_tool(tool_name)
        if func is None:
            logger.warning("[%s] Tool not found: %s", self.name, tool_name)
            result = {
                "tool_name": tool_name,
                "success": False,
                "error": f"Tool '{tool_name}' not found",
                "duration_ms": 0,
            }
            if on_chunk:
                on_chunk({
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "result": f"Tool '{tool_name}' not found",
                    "success": False,
                    "duration_ms": 0,
                })
            return result

        start = time.monotonic()
        try:
            if inspect.iscoroutinefunction(func):
                raw_result = await asyncio.wait_for(
                    func(tool_args),
                    timeout=timeout,
                )
            else:
                raw_result = await asyncio.wait_for(
                    asyncio.to_thread(func, tool_args),
                    timeout=timeout,
                )

            normalized = self._normalize_tool_result(raw_result)

            duration_ms = (time.monotonic() - start) * 1000
            logger.debug(
                "[%s] Tool executed: %s (%.1fms)",
                self.name, tool_name, duration_ms,
            )
            result = {
                "tool_name": tool_name,
                "success": True,
                "data": normalized,
                "duration_ms": round(duration_ms, 1),
            }
            if on_chunk:
                display_result = str(normalized)[:200] if normalized else ""
                on_chunk({
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "result": display_result,
                    "success": True,
                    "duration_ms": round(duration_ms, 1),
                })
            return result
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - start) * 1000
            logger.warning(
                "[%s] Tool timeout: %s (%.1fms, limit=%.1fs)",
                self.name, tool_name, duration_ms, timeout,
            )
            error_msg = f"Tool '{tool_name}' timed out after {timeout}s"
            result = {
                "tool_name": tool_name,
                "success": False,
                "error": error_msg,
                "duration_ms": round(duration_ms, 1),
            }
            if on_chunk:
                on_chunk({
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "result": error_msg,
                    "success": False,
                    "duration_ms": round(duration_ms, 1),
                })
            return result
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            logger.error(
                "[%s] Tool error: %s (%.1fms) — %s",
                self.name, tool_name, duration_ms, exc,
            )
            error_msg = str(exc)
            result = {
                "tool_name": tool_name,
                "success": False,
                "error": error_msg,
                "duration_ms": round(duration_ms, 1),
            }
            if on_chunk:
                on_chunk({
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "result": error_msg,
                    "success": False,
                    "duration_ms": round(duration_ms, 1),
                })
            return result

    async def execute(self, ctx: PluginContext) -> dict[str, Any]:
        """执行工具调用。

        从 state["raw_tool_calls"] 读取工具调用列表，逐个执行，
        收集结果后写入 state。

        Args:
            ctx: 插件执行上下文

        Returns:
            核心执行结果字典，将合并到管道状态中，包含：
            - tool_results: 工具执行结果列表
            - raw_result: 最后一个工具的结果文本
            - raw_error: 始终为 None（错误由各工具结果中的 error 字段表达）
            - raw_tool_calls: 清空为空列表（已处理完毕）
        """
        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])

        if not tool_calls:
            return {
                StateKeys.RAW_RESULT: "No tool calls to execute",
                StateKeys.RAW_ERROR: None,
                StateKeys.RAW_TOOL_CALLS: [],
                StateKeys.TOOL_RESULTS: [],
            }

        results: list[dict[str, Any]] = []
        last_result_text = ""
        on_chunk = ctx.state.get("on_chunk")

        for tc in tool_calls:
            tool_name = tc.get("name", "unknown")
            tool_args = tc.get("args", tc.get("arguments", {}))

            # LLM 返回的 arguments 可能是 JSON 字符串，需要解析
            args_parse_failed = False
            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except (json.JSONDecodeError, TypeError):
                    args_parse_failed = True
                    logger.warning(
                        "[%s] 工具 %s 的 arguments JSON 解析失败（可能过长被截断），"
                        "长度=%d，前200字符: %s",
                        self.name, tool_name, len(tool_args), tool_args[:200],
                    )
                    result = {
                        "tool_name": tool_name,
                        "success": False,
                        "error": (
                            f"工具 {tool_name} 的调用参数 JSON 格式无效（可能参数内容过长导致被截断）。"
                            f"请将操作拆分为多个小步骤：\n"
                            f"1. 如果是 file_write：请分多次写入，每次写入一个章节或部分内容\n"
                            f"2. 如果是其他工具：请减少参数中的文本量\n"
                            f"3. 不要一次性传入大量文本作为参数"
                        ),
                    }
                    results.append(result)
                    last_result_text = f"Error: {result['error']}"
                    continue

            if not isinstance(tool_args, dict):
                tool_args = {}

            # 允许工具通过 timeout_seconds 参数覆盖默认超时
            timeout = self._default_timeout
            if isinstance(tool_args, dict) and "timeout_seconds" in tool_args:
                try:
                    requested = float(tool_args["timeout_seconds"])
                    if requested > 0:
                        timeout = requested
                except (ValueError, TypeError):
                    pass
            elif self._tool_registry is not None:
                schema_default = self._get_schema_timeout_default(tool_name)
                if schema_default is not None:
                    timeout = schema_default

            # 优先通过隔离执行器执行
            if self._isolation_executor is not None:
                func = self._get_tool(tool_name)
                result = await self._isolation_executor.execute_tool(
                    state=ctx.state,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_func=func,  # type: ignore[arg-type]
                    timeout=timeout,
                )
                if on_chunk:
                    display_data = result.get("data", result.get("error", ""))
                    on_chunk({
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "result": str(display_data)[:200] if display_data else "",
                        "success": result.get("success", True),
                        "duration_ms": result.get("duration_ms", 0),
                    })
            else:
                result = await self._execute_single_tool(
                    tool_name, tool_args, timeout,
                    services=ctx._services,
                    on_chunk=on_chunk,
                )
            results.append(result)

            # 最后一个工具的结果用于 LLM 上下文
            if result["success"]:
                last_result_text = str(result["data"])
            else:
                last_result_text = f"Error: {result['error']}"

        logger.info(
            "[%s] Executed %d tool(s): %s",
            self.name,
            len(results),
            [r["tool_name"] for r in results],
        )

        # 更新 messages：追加工具结果消息，供下一轮 LLMCore 读取
        current_messages: list[dict[str, Any]] = list(ctx.state.get("messages", []))
        # 如果 messages 中已有 assistant 的 tool_calls 消息，只需追加 tool 结果
        has_tool_call_msg = any(
            m.get("role") == "assistant" and m.get("tool_calls")
            for m in current_messages
        )
        # 如果没有 assistant tool_calls 消息，先构建 assistant tool_calls 消息
        if not has_tool_call_msg and tool_calls:
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tc.get("id", f"call_{i}"),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": tc.get("args", tc.get("arguments", "")),
                        },
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            }
            current_messages.append(assistant_msg)

        # 追加 tool 结果消息
        for i, result in enumerate(results):
            tc_id = tool_calls[i].get("id", f"call_{i}") if i < len(tool_calls) else f"call_{i}"
            result_data = result.get("data", result.get("error", ""))
            try:
                content_str = json.dumps(result_data, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                content_str = str(result_data)
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": content_str if result.get("success") else f"Error: {result.get('error', 'unknown')}",
            }
            current_messages.append(tool_msg)

        # BUG-FIX-fix_20260418_all_tools_failed
        # 问题根因: 工具全部失败时 raw_error 始终为 None，导致 error_check 插件
        #           无法识别，Output 插件链无 route_signal，管道异常退出
        # 修复方案: 检测所有工具失败的情况，设置 raw_error 让 error_check 能处理
        # 影响范围: 所有工具执行流程
        all_failed = results and all(not r.get("success") for r in results)
        raw_error = None
        if all_failed:
            error_summary = "; ".join(
                f"{r.get('tool_name', 'unknown')}: {r.get('error', 'unknown')}"
                for r in results
            )
            raw_error = f"所有工具执行失败: {error_summary}"

        # BUG-FIX-fix_20260418_task_inject: 检测 task_failed 标记，直接结束管道
        has_task_failed = False
        for r in results:
            tool_data = r.get("data", {})
            if isinstance(tool_data, dict):
                meta = tool_data.get("metadata", {})
                if isinstance(meta, dict) and meta.get("task_failed"):
                    has_task_failed = True
                    if not raw_error:
                        raw_error = tool_data.get("error", "任务系统级失败")
                    break

        submitted_task_ids = list(ctx.state.get("submitted_task_ids", []))
        evaluation_completed = False
        for r in results:
            if not r.get("success"):
                continue
            tool_data = r.get("data", {})
            if not isinstance(tool_data, dict):
                continue
            meta = tool_data.get("metadata", {})
            if isinstance(meta, dict) and meta.get("action") == "task_submit":
                tid = tool_data.get("task_id")
                if tid and tid not in submitted_task_ids:
                    submitted_task_ids.append(tid)
            tool_name = r.get("tool_name", "")
            if tool_name == "task_evaluate" and tool_data.get("overall_passed") is True:
                evaluation_completed = True

        return_dict = {
            StateKeys.TOOL_RESULTS: results,
            StateKeys.RAW_RESULT: last_result_text,
            StateKeys.RAW_ERROR: raw_error,
            StateKeys.RAW_TOOL_CALLS: [],
            "_executed_tool_calls": tool_calls,
            "messages": current_messages,
        }
        if submitted_task_ids:
            return_dict["submitted_task_ids"] = submitted_task_ids
        if evaluation_completed:
            return_dict["task_evaluation_completed"] = True
        if has_task_failed:
            return_dict[StateKeys.ENDED] = True

        return return_dict

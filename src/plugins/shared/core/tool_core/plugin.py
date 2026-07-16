"""Tool Core 插件 — 工具执行核心（M3 完善）。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any  # noqa: F401

from pipeline.plugin import ICorePlugin, PluginContext
from pipeline.types import ErrorPolicy, StateKeys
from tools.format_manager import get_format_manager
from tools.registry import ToolRegistry

# asyncio 工具执行器：在线程中独立事件循环运行异步工具。


def _asyncio_tool_runner(func: Callable, tool_args: dict[str, Any]) -> Any:
    """在线程中运行异步工具函数，使用独立事件循环。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(func(tool_args))
    finally:
        # 刻意不调用 _cancel_all_tasks()：工具返回后，嵌套管道引擎应继续独立运行，
        # 它们有自己的生命周期管理（通过 asyncio.to_thread 独立事件循环）。
        loop.close()


def _recover_workspace_from_task(state: dict[str, Any], task_id: str) -> str | None:
    """state 中 workspace 缺失时，从任务数据反查恢复。"""
    if not task_id or task_id == "unknown":
        return None
    try:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415
        from tasks.workspace import resolve_task_workspace  # noqa: PLC0415

        provider = get_service_provider()
        task_service = provider.get("task_service") if provider else None
        if not task_service:
            return None
        task = task_service.get_task(task_id)  # noqa: SLF001
        if task is None:
            return None
        return resolve_task_workspace(task)
    except Exception:
        return None


logger = logging.getLogger(__name__)


class ToolCore(ICorePlugin):
    """工具执行 Core — 从 raw_tool_calls 读取并执行工具调用。"""

    error_policy = ErrorPolicy.RETRY

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化 Tool Core 插件。"""
        self._config = config or {}
        self._tools: dict[str, Callable[..., Any]] = {}
        self._tool_registry: ToolRegistry | None = None
        self._default_timeout: float = self._config.get("timeout", 30.0)
        self._tool_timeouts: dict[str, float] = self._config.get("tool_timeouts", {})

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_core"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 50

    def register_tool(self, name: str, func: Callable[..., Any]) -> None:
        """注册一个工具函数。"""
        self._tools[name] = func
        logger.debug("[%s] Tool registered: %s", self.name, name)

    def register_tools_from_registry(self, registry: ToolRegistry) -> None:
        """从 ToolRegistry 批量注册工具。"""
        self._tool_registry = registry
        for tool_def in registry.list_all():
            handler = registry.get_handler(tool_def.name)
            if handler is not None:
                self._tools[tool_def.name] = handler
                logger.debug(
                    "[%s] Tool imported from registry: %s",
                    self.name,
                    tool_def.name,
                )

    def _get_schema_timeout_default(self, tool_name: str) -> float | None:
        """从工具 schema 中获取 timeout_seconds 的默认值。"""
        if self._tool_registry is None:
            return None
        tool_def = self._tool_registry.get_optional(tool_name)
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

    def _get_tool(self, name: str) -> Callable[..., Any] | None:
        """获取工具函数。"""
        if name in self._tools:
            return self._tools[name]
        if self._tool_registry and self._tool_registry.has(name):
            return self._tool_registry.get_handler(name)
        return None

    # 服务注入映射：tool_args 中的下划线前缀参数名 → ctx.get_service() 的 key
    _SERVICE_INJECT_MAP: dict[str, str] = {
        "_task_service": "task_service",
        "_tool_registry": "tool_registry",
        "_session": "db_session",
        "_memory_service": "memory_service",
        "_retriever": "retriever",
        "_storage": "execution_record_storage",
    }

    @staticmethod
    def _normalize_tool_result(result: Any, slim: bool = False) -> Any:
        """将工具返回值标准化为可 JSON 序列化的对象。"""
        if result is None:
            return None

        if hasattr(result, "to_dict"):
            return result.to_dict(slim=slim)

        if hasattr(result, "output"):
            return result.output

        if hasattr(result, "data"):
            return result.output

        if isinstance(result, (dict, list, str, int, float, bool)):
            return result

        return str(result)

    def _check_tool_blocked(
        self,
        tool_name: str,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """统一工具拦截检查：被策略拦截的工具返回失败结果，否则返回 None。"""
        # level_guard 越权拦截
        level_decision = state.get("security.level_decision")
        if isinstance(level_decision, dict) and level_decision.get("allowed") is False:
            blocked = level_decision.get("blocked_tools") or []
            if tool_name in blocked or not blocked:
                reason = level_decision.get("reason", "权限不足")
                logger.warning("[tool_core] 工具 %s 被 level_guard 拦截: %s", tool_name, reason)
                return {
                    "tool_name": tool_name,
                    "success": False,
                    "error": f"工具被权限策略拦截: {reason}",
                    "duration_ms": 0,
                }

        # isolation_guard 拦截：execution_contexts 中标记 blocked 的工具
        for ctx_entry in state.get("execution_contexts", []):
            if ctx_entry.get("tool_name") == tool_name and ctx_entry.get("blocked"):
                reason = ctx_entry.get("reason", "隔离策略阻止")
                logger.warning("[tool_core] 工具 %s 被 isolation_guard 拦截: %s", tool_name, reason)
                return {
                    "tool_name": tool_name,
                    "success": False,
                    "error": f"工具被隔离策略拦截: {reason}",
                    "duration_ms": 0,
                }

        # security_check 拦截
        sec_decision = state.get("security.decision")
        if isinstance(sec_decision, dict) and sec_decision.get("allowed") is False:
            reason = sec_decision.get("reason", "安全检查拦截")
            logger.warning("[tool_core] 工具 %s 被 security_check 拦截: %s", tool_name, reason)
            return {
                "tool_name": tool_name,
                "success": False,
                "error": f"工具被安全检查拦截: {reason}",
                "duration_ms": 0,
            }

        return None

    async def _execute_in_isolated_container(
        self,
        state: dict[str, Any],
        tool_args: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """在 IsolationManager 管理的 Docker 容器中执行 bash_execute。"""
        import time as _time  # noqa: PLC0415

        from isolation.manager import get_isolation_manager  # noqa: PLC0415
        from isolation.types import TaskType  # noqa: PLC0415

        task_id = state.get("task_id", "unknown")
        workspace = state.get("workspace")

        # 容器隔离必须挂载工作空间：workspace 只认任务数据解析出来的路径，
        # 取不到即为功能错误——绝不静默创建无挂载容器去执行。否则命令会落到
        # 空/不存在的 /workspace 目录且 exit 0，表现为“目录看不到”却无任何报错，
        # 极难排查。
        if not workspace:
            workspace = _recover_workspace_from_task(state, task_id)
            if workspace:
                logger.warning(
                    "[tool_core] bash_execute workspace 在运行中丢失，已从任务数据恢复 | "
                    "task=%s | pipeline_id=%s | ws=%s",
                    task_id,
                    state.get("pipeline_id", "?"),
                    workspace,
                )

        if not workspace:
            # 诊断：打出报错现场全貌，定位 workspace 是在哪条链路丢失的。
            # use_docker 由 execution_contexts 决定（isolation_guard 写入）；
            # workspace 应经 engine.run(workspace=...) → extra_state → state。
            _exec_ctxs = state.get("execution_contexts", [])
            _bash_ctx = next((c for c in _exec_ctxs if c.get("tool_name") == "bash_execute"), None)
            logger.error(
                "[tool_core] bash_execute 容器隔离被拒绝：state 中无 workspace | "
                "task=%s | has_task_id=%s | pipeline_id=%s | "
                "ws_meta_keys=%s | has_session_id=%s | provider=%s | "
                "state_top_keys=%s",
                task_id,
                bool(state.get("task_id")),
                state.get("pipeline_id", "?"),
                list((state.get("ws_meta") or {}).keys())
                if isinstance(state.get("ws_meta"), dict)
                else state.get("ws_meta"),
                bool(state.get("session_id")),
                _bash_ctx.get("provider") if _bash_ctx else "no_ctx",
                sorted(k for k in state if not k.startswith("_"))[:15],
            )
            return {
                "tool_name": "bash_execute",
                "success": False,
                "error": (
                    "工作空间未解析（state 中无 workspace），拒绝在容器中执行命令。"
                    "工作空间只能来自任务数据，请检查任务工作空间初始化链路"
                    "（task_executor → ws_meta → state）。"
                ),
                "duration_ms": 0,
            }

        operation = {
            "type": "command",
            "command": tool_args.get("command", ""),
            "timeout": timeout,
            "working_dir": "/workspace",
        }

        manager = await get_isolation_manager()
        _start = _time.monotonic()
        exec_result = await manager.execute_in_isolation(
            task_id=task_id,
            task_type=TaskType.ATOMIC,
            operation=operation,
            workspace=workspace,
            tool_name="bash_execute",
        )
        duration_ms = (_time.monotonic() - _start) * 1000

        if exec_result.success:
            output = exec_result.output
            data = output.get("stdout", "") if isinstance(output, dict) else str(output or "")
            return {
                "tool_name": "bash_execute",
                "success": True,
                "data": data,
                "duration_ms": round(duration_ms, 1),
            }
        return {
            "tool_name": "bash_execute",
            "success": False,
            "error": exec_result.error or "容器执行失败",
            "duration_ms": round(duration_ms, 1),
        }

    async def _execute_single_tool(  # noqa: PLR0912,PLR0915
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        timeout: float,
        services: dict[str, Any] | None = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        """执行单个工具调用。"""
        _wrapped_chunk = on_chunk
        if on_chunk and call_id:

            def _wrap(chunk: dict[str, Any]) -> Any:
                chunk.setdefault("call_id", call_id)
                return on_chunk(chunk)

            _wrapped_chunk = _wrap

        if _wrapped_chunk:
            _wrapped_chunk({"type": "tool_start", "tool_name": tool_name, "args": tool_args, "call_id": call_id})

        if services:
            tool_args = dict(tool_args)
            for param_key, service_key in self._SERVICE_INJECT_MAP.items():
                if param_key not in tool_args and service_key in services:
                    tool_args[param_key] = services[service_key]
                    logger.debug(
                        "[%s] Injected service '%s' to tool '%s'",
                        self.name,
                        service_key,
                        tool_name,
                    )

        func = self._get_tool(tool_name)
        if func is None:
            func = await self._try_auto_load_tool(tool_name)

        if func is None:
            logger.warning("[%s] Tool not found: %s", self.name, tool_name)
            result = {
                "tool_name": tool_name,
                "success": False,
                "error": f"Tool '{tool_name}' not found",
                "duration_ms": 0,
            }
            if _wrapped_chunk:
                _wrapped_chunk(
                    {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "result": f"Tool '{tool_name}' not found",
                        "success": False,
                        "duration_ms": 0,
                    }
                )
            return result

        start = time.monotonic()

        try:
            handler_for_check = self._tool_registry.get_handler(tool_name) if self._tool_registry else None
            tool_self = (
                handler_for_check.__self__ if handler_for_check and hasattr(handler_for_check, "__self__") else None
            )
            _is_main_loop = tool_name in ("human_interaction", "bash_execute") or (
                tool_self is not None and getattr(tool_self, "run_on_main_loop", False)
            )

            if inspect.iscoroutinefunction(func) and not _is_main_loop:
                raw_result = await asyncio.wait_for(
                    asyncio.to_thread(
                        _asyncio_tool_runner,
                        func,
                        tool_args,
                    ),
                    timeout=timeout,
                )
            elif inspect.iscoroutinefunction(func) and _is_main_loop:
                raw_result = await asyncio.wait_for(func(tool_args), timeout=timeout)
            else:
                raw_result = await asyncio.wait_for(
                    asyncio.to_thread(func, tool_args),
                    timeout=timeout,
                )

            normalized = self._normalize_tool_result(raw_result, slim=True)

            duration_ms = (time.monotonic() - start) * 1000
            _result_preview = str(normalized)[:200] if normalized else ""
            logger.info(
                "[%s] Tool executed: %s (%.1fms) → %s",
                self.name,
                tool_name,
                duration_ms,
                _result_preview,
            )
            result = {
                "tool_name": tool_name,
                "success": True,
                "data": normalized,
                "duration_ms": round(duration_ms, 1),
            }
            if hasattr(raw_result, "metadata") and isinstance(raw_result.metadata, dict) and raw_result.metadata:
                result["metadata"] = raw_result.metadata
            if _wrapped_chunk:
                display_result = str(normalized)[:200] if normalized else ""
                # result_data 供前端工具卡片渲染（含 diff 正文 old_content/new_content），
                # 必须用 full 版（slim=False）：slim 版已剔除这些大体积字段（只给 LLM）。
                # data（回 LLM 上下文的那份，见下方 result["data"]）保持 slim，避免写入
                # 原文整段回灌进模型上下文。
                display_data = self._normalize_tool_result(raw_result, slim=False)
                _wrapped_chunk(
                    {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "result": display_result,
                        # 结构化完整数据（含 diff 的 added/removed/old_content/new_content），
                        # 流式 result 为截断字符串仅供预览；result_data 供前端工具卡片渲染。
                        "result_data": display_data,
                        "success": True,
                        "duration_ms": round(duration_ms, 1),
                    }
                )
            return result
        except asyncio.CancelledError:
            duration_ms = (time.monotonic() - start) * 1000
            logger.info(
                "[%s] Tool cancelled: %s (%.1fms)",
                self.name,
                tool_name,
                duration_ms,
            )
            result = {
                "tool_name": tool_name,
                "success": False,
                "error": (f"Tool '{tool_name}' cancelled (pipeline stopped)"),
                "duration_ms": round(duration_ms, 1),
            }
            if _wrapped_chunk:
                _wrapped_chunk(
                    {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "result": "cancelled",
                        "success": False,
                        "duration_ms": round(duration_ms, 1),
                    }
                )
            raise
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - start) * 1000
            logger.warning(
                "[%s] Tool timeout: %s (%.1fms, limit=%.1fs)",
                self.name,
                tool_name,
                duration_ms,
                timeout,
            )
            if tool_name == "human_interaction":
                error_msg = (
                    f"人类交互超时（等待了{timeout:.0f}秒），"
                    "用户未在规定时间内响应。"
                    "你不应假设用户同意或默认通过，"
                    "应根据当前任务上下文决定下一步操作。"
                )
            else:
                error_msg = f"Tool '{tool_name}' timed out after {timeout}s"
            result = {
                "tool_name": tool_name,
                "success": False,
                "error": error_msg,
                "duration_ms": round(duration_ms, 1),
            }
            if _wrapped_chunk:
                _wrapped_chunk(
                    {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "result": error_msg,
                        "success": False,
                        "duration_ms": round(duration_ms, 1),
                    }
                )
            return result
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            logger.error(
                "[%s] Tool error: %s (%.1fms) — %s",
                self.name,
                tool_name,
                duration_ms,
                exc,
            )
            error_msg = str(exc)
            result = {
                "tool_name": tool_name,
                "success": False,
                "error": error_msg,
                "duration_ms": round(duration_ms, 1),
            }
            if _wrapped_chunk:
                _wrapped_chunk(
                    {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "result": error_msg,
                        "success": False,
                        "duration_ms": round(duration_ms, 1),
                    }
                )
            return result

    async def _try_auto_load_tool(self, tool_name: str) -> Callable[..., Any] | None:
        """尝试自动加载未注册的工具。"""
        try:
            from tools.auto_loader import get_tool_auto_loader  # noqa: PLC0415

            auto_loader = get_tool_auto_loader()
            if auto_loader is None:
                logger.debug("[%s] ToolAutoLoader 不可用，无法自动加载: %s", self.name, tool_name)
                return None

            tool = await auto_loader.auto_load_tool(tool_name)
            if tool is None:
                return None

            # 从全局注册表获取 handler
            if self._tool_registry and self._tool_registry.has(tool_name):
                handler = self._tool_registry.get_handler(tool_name)
                if handler:
                    self._tools[tool_name] = handler
                    logger.info("[%s] 自动加载工具成功: %s", self.name, tool_name)
                    return handler

            return None

        except Exception as e:
            logger.warning("[%s] 自动加载工具失败: %s — %s", self.name, tool_name, e)
            return None

    async def execute(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0912,PLR0915
        """执行工具调用。"""
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
            tc_call_id = tc.get("id")

            # LLM 返回的 arguments 可能是 JSON 字符串，需要解析
            args_parse_failed = False
            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except (json.JSONDecodeError, TypeError):
                    # 尝试容错修复 JSON（MiniMax 等模型返回格式不稳定）
                    from plugins.core.llm_core import _repair_json_string  # noqa: PLC0415

                    repaired = _repair_json_string(tool_args)
                    if repaired is not None:
                        logger.info(
                            "[%s] 工具 %s 的 arguments JSON 修复成功: %s -> %s",
                            self.name,
                            tool_name,
                            tool_args[:200],
                            repaired[:200],
                        )
                        try:
                            tool_args = json.loads(repaired)
                        except (json.JSONDecodeError, TypeError):
                            args_parse_failed = True
                    else:
                        args_parse_failed = True

                    if args_parse_failed:
                        logger.warning(
                            "[%s] 工具 %s 的 arguments JSON 解析失败（可能过长被截断），长度=%d，前200字符: %s",
                            self.name,
                            tool_name,
                            len(tool_args),
                            tool_args[:200],
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

            timeout = self._default_timeout
            if tool_name in self._tool_timeouts:
                timeout = self._tool_timeouts[tool_name]
            if tool_name != "human_interaction" and isinstance(tool_args, dict) and "timeout_seconds" in tool_args:
                try:
                    requested = float(tool_args["timeout_seconds"])
                    if requested > 0:
                        timeout = requested
                except (ValueError, TypeError):
                    pass
            elif tool_name not in self._tool_timeouts and self._tool_registry is not None:
                schema_default = self._get_schema_timeout_default(tool_name)
                if schema_default is not None:
                    timeout = schema_default

            # 统一工具拦截检查：被权限/隔离/安全策略拦截的工具转为失败结果，
            # 让 LLM 自行调整，而不是终结整个管道。
            _blocked_result = self._check_tool_blocked(tool_name, ctx.state)
            if _blocked_result is not None:
                if on_chunk:
                    on_chunk(
                        {
                            "type": "tool_start",
                            "tool_name": tool_name,
                            "args": tool_args,
                            "call_id": tc_call_id,
                        }
                    )
                    on_chunk(
                        {
                            "type": "tool_result",
                            "tool_name": tool_name,
                            "result": _blocked_result.get("error", ""),
                            "success": False,
                            "duration_ms": 0,
                            "call_id": tc_call_id,
                        }
                    )
                results.append(_blocked_result)
                last_result_text = f"Error: {_blocked_result['error']}"
                continue

            # 根据 execution_contexts 决定执行路径
            # provider=docker → 容器执行；provider=host → 宿主机执行
            execution_contexts = ctx.state.get("execution_contexts", [])
            ctx_entry = next((c for c in execution_contexts if c.get("tool_name") == tool_name), None)
            use_docker = (
                ctx_entry is not None and ctx_entry.get("provider") == "docker" and not ctx_entry.get("blocked", False)
            )

            logger.debug("[tool_core] tool=%s use_docker=%s", tool_name, use_docker)
            if use_docker and tool_name == "bash_execute":
                if on_chunk:
                    on_chunk({"type": "tool_start", "tool_name": tool_name, "args": tool_args, "call_id": tc_call_id})
                result = await self._execute_in_isolated_container(
                    state=ctx.state,
                    tool_args=tool_args,
                    timeout=timeout,
                )
                if on_chunk:
                    display_data = result.get("data", result.get("error", ""))
                    on_chunk(
                        {
                            "type": "tool_result",
                            "tool_name": tool_name,
                            "result": str(display_data)[:200] if display_data else "",
                            "result_data": display_data,
                            "success": result.get("success", True),
                            "duration_ms": result.get("duration_ms", 0),
                            "call_id": tc_call_id,
                        }
                    )
            else:
                result = await self._execute_single_tool(
                    tool_name,
                    tool_args,
                    timeout,
                    services=ctx._services,
                    on_chunk=on_chunk,
                    call_id=tc_call_id,
                )
            results.append(result)

            # 最后一个工具的结果用于 LLM 上下文
            last_result_text = str(result["data"]) if result["success"] else f"Error: {result['error']}"

        logger.info(
            "[%s] Executed %d tool(s): %s",
            self.name,
            len(results),
            [r["tool_name"] for r in results],
        )

        # 更新 messages：追加工具结果消息，供下一轮 LLMCore 读取
        current_messages: list[dict[str, Any]] = list(ctx.state.get("messages", []))
        # 如果 messages 中已有 assistant 的 tool_calls 消息，只需追加 tool 结果
        has_tool_call_msg = any(m.get("role") == "assistant" and m.get("tool_calls") for m in current_messages)
        # 如果没有 assistant tool_calls 消息，先构建 assistant tool_calls 消息
        # 预先解析 tool_call_id 列表，确保 assistant 消息和 tool 结果使用一致的 id
        tc_ids: list[str] = []
        for i, tc in enumerate(tool_calls):  # noqa: B007
            tc_ids.append(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}")

        if not has_tool_call_msg and tool_calls:
            # 从 state 取 reasoning_content（与 LLMCore 保持一致，统一存）
            _rc_for_rebuild = ctx.state.get(StateKeys.RAW_THINKING)
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tc_ids[i],
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            # arguments 必须是 JSON 字符串（OpenAI API 规范），
                            # 与 LLMCore 保持一致：直接透传原始值，不做 dict 转换。
                            "arguments": tc.get("args", tc.get("arguments", "")),
                        },
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            }
            if _rc_for_rebuild:
                assistant_msg["reasoning_content"] = _rc_for_rebuild
            current_messages.append(assistant_msg)

        # 追加 tool 结果消息
        # 输出被 max_tokens 截断时（finish_reason=length）：已执行的工具（如 file_write）
        # 其参数可能不完整，成功写入的文件实际是「半截内容」——在结果里如实标注，
        # 并引导模型用 append 续写而非 write 覆盖，避免覆盖丢失已写入部分。
        output_truncated = bool(ctx.state.get("output_truncated", False))
        for i, result in enumerate(results):
            tc_id = tc_ids[i] if i < len(tc_ids) else f"call_{uuid.uuid4().hex[:8]}"
            result_data = result.get("data", result.get("error", ""))
            try:
                content_str = get_format_manager().serialize(result_data)
            except (TypeError, ValueError):
                content_str = str(result_data)
            if result.get("success"):
                content = content_str
                if output_truncated:
                    tool_name = result.get("tool_name", "")
                    written_lines = None
                    if isinstance(result_data, dict):
                        written_lines = result_data.get("lines")
                    note = (
                        f"⚠️ 本次输出因达到 max_tokens 被截断，结果可能基于不完整参数。 已写入 {written_lines} 行。"
                        if written_lines is not None
                        else "⚠️ 本次输出因达到 max_tokens 被截断，结果可能基于不完整参数。"
                    )
                    if tool_name in ("file_write", "file_append"):
                        note += " 如内容未写完，请用 file_write(action=append) 追加续写，勿用 write 覆盖。"
                    content = f"{content}\n\n{note}"
            else:
                content = f"Error: {result.get('error', 'unknown')}"
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": content,
            }
            current_messages.append(tool_msg)

        # === 多模态图片处理（双保险） ===
        # 收集工具返回的图片数据，根据模型能力选择注入方式
        pending_images: list[dict] = []
        for _r in results:
            _data = _r.get("data", {})
            if not isinstance(_data, dict):
                continue
            if _data.get("base64_data") and _data.get("mime_type"):
                pending_images.append(
                    {
                        "base64": _data["base64_data"],
                        "mime_type": _data["mime_type"],
                        "path": _data.get("path", ""),
                    }
                )
            for _img in _data.get("images", []):
                if isinstance(_img, dict) and _img.get("base64"):
                    pending_images.append(_img)

        # MM-3/MM-5: 从工具返回的 metadata.multimodal_content 收集多模态内容
        for _r in results:
            _meta = _r.get("metadata", {})
            if not isinstance(_meta, dict):
                continue
            _mm_content = _meta.get("multimodal_content")
            if not isinstance(_mm_content, list):
                continue
            for _block in _mm_content:
                if not isinstance(_block, dict):
                    continue
                if _block.get("type") == "image_url":
                    _url = (_block.get("image_url") or {}).get("url", "")
                    if _url.startswith("data:") and ";base64," in _url:
                        _mime_part, _b64_part = _url[5:].split(";base64,", 1)
                        pending_images.append(
                            {
                                "base64": _b64_part,
                                "mime_type": _mime_part,
                                "path": "",
                            }
                        )

        # MM-4b: 工具产生多模态结果时推送 tool_multimedia_result WS 事件
        if pending_images and on_chunk:
            on_chunk(
                {
                    "type": "tool_multimedia_result",
                    "count": len(pending_images),
                    "multimedia": [
                        {
                            "mime_type": _img.get("mime_type", "image/png"),
                            "path": _img.get("path", ""),
                        }
                        for _img in pending_images
                    ],
                }
            )

        if pending_images:
            from multimodal.capabilities import ModelCapabilityRegistry  # noqa: PLC0415

            _model_name = ctx.state.get("llm_model", "")
            _supports_vision = ModelCapabilityRegistry.is_multimodal_supported(_model_name)

            if _supports_vision:
                # 路径 A：模型支持视觉 → 注入多模态 user 消息
                _content_blocks: list[dict] = [
                    {
                        "type": "text",
                        "text": (f"[工具截图] 共 {len(pending_images)} 张图片，请分析截图内容："),
                    }
                ]
                for _img in pending_images:
                    _data_url = f"data:{_img['mime_type']};base64,{_img['base64']}"
                    _content_blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": _data_url},
                        }
                    )
                current_messages.append(
                    {
                        "role": "user",
                        "name": "tool_images",
                        "content": _content_blocks,
                    }
                )
            else:
                # 路径 B：模型不支持视觉 → 提示 agent 调 MCP 分析
                _paths = [_i.get("path", "") for _i in pending_images if _i.get("path")]
                _paths_str = ", ".join(_paths) if _paths else "见工具返回"
                current_messages.append(
                    {
                        "role": "user",
                        "name": "tool_images",
                        "content": (
                            f"[工具截图] 已保存 {len(pending_images)} 张截图"
                            f"（{_paths_str}）。当前模型不支持图片分析，"
                            "请使用 mcp__4_5v_mcp__analyze_image 工具分析"
                            "截图内容，获取文本描述后继续验证。"
                        ),
                    }
                )

        all_failed = results and all(not r.get("success") for r in results)
        raw_error = None
        if all_failed:
            error_summary = "; ".join(f"{r.get('tool_name', 'unknown')}: {r.get('error', 'unknown')}" for r in results)
            raw_error = f"所有工具执行失败: {error_summary}"

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
        conversation_activated = False
        for r in results:
            if not r.get("success"):
                continue
            tool_data = r.get("data", {})
            if not isinstance(tool_data, dict):
                continue
            meta = tool_data.get("metadata") or r.get("metadata", {})
            if isinstance(meta, dict) and meta.get("action") == "task_submit":
                tid = tool_data.get("task_id")
                if tid and tid not in submitted_task_ids:
                    submitted_task_ids.append(tid)
            tool_name = r.get("tool_name", "")
            if tool_name == "task_evaluate" and isinstance(meta, dict) and meta.get("result") == "completed":
                evaluation_completed = True
            if tool_name == "human_interaction":
                conv_flag = tool_data.get("conversation_mode")
                if not conv_flag:
                    for _key in ("output", "data"):
                        _inner = tool_data.get(_key)
                        if isinstance(_inner, dict) and _inner.get("conversation_mode"):
                            conv_flag = True
                            break
                if conv_flag:
                    conversation_activated = True

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
        if conversation_activated:
            # 直接路由信号：告诉管道仲裁器"立即 wait"。
            # _execute_core_and_route 取出此信号与 Output 插件信号一起仲裁，
            # wait(priority=10) 优先于 next_llm(priority=50)，管道立即挂起。
            # 用户消息到达后 inject_message 唤醒 → 工具结果 + 用户消息一起发 LLM。
            return_dict["_pending_route_signal"] = {
                "route_type": "wait",
                "reason": "human_interaction: user arrived, entering conversation",
            }

        return return_dict

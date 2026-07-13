"""
工具执行器

暴露接口：
- load_from_file(cls, path: str)：load_from_file功能
- is_cacheable(self, tool_name: str) -> bool：is_cacheable功能
- get_ttl(self, tool_name: str) -> int：get_ttl功能
- get_cache_stats(self) -> dict[str, Any]：get_cache_stats功能
- set_progress_callback(self, callback: ProgressCallback | None) -> None：set_progress_callback功能
- register_handler(self, tool_name: str, handler: ToolHandler) -> None：register_handler功能
- unregister_handler(self, tool_name: str) -> None：unregister_handler功能
- has_handler(self, tool_name: str) -> bool：has_handler功能
- set_runnable_first(self, enabled: bool) -> None：set_runnable_first功能
- check_sensitive(data)：check_sensitive功能
- ToolCacheConfig：ToolCacheConfig类
- ToolProgress：ToolProgress类
- ExecutionContext：ExecutionContext类
- ToolExecutor：ToolExecutor类
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

import jsonschema
from pydantic import BaseModel, Field

from core.exceptions import (
    ToolExecutionError,
    ToolNotFoundError,
    ToolValidationError,
)
from core.results import ToolExecutionResult
from tools.input_normalizer import (
    fix_task_submit_inputs,
    normalize_input_types,
)
from tools.interfaces import IToolExecutor, IToolRegistry
from tools.nested_record_manager import NestedRecordManager
from tools.tool_cache import ToolCache, ToolCacheConfig
from tools.types import Tool, create_failure_result, create_success_result

if TYPE_CHECKING:
    from core.runnable import ToolRunnable


from tools.interfaces import ProgressCallback

# 工具处理函数类型
ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, ToolExecutionResult]]

# 日志
logger = logging.getLogger(__name__)


class ToolProgress(BaseModel):
    """工具执行进度"""

    tool_call_id: str = Field(..., description="工具调用 ID")
    progress: float = Field(..., description="进度百分比 (0-100)")
    current_step: str | None = Field(None, description="当前步骤描述")
    estimated_remaining_ms: int | None = Field(None, description="预计剩余时间（毫秒）")


class ExecutionContext(BaseModel):
    """执行上下文"""

    session_id: str = Field(..., description="会话 ID")
    task_id: str = Field(default="", description="任务 ID")
    user_id: str | None = Field(None, description="用户 ID")
    agent_level: int = Field(default=3, description="Agent 层级（1=L1, 2=L2, 3=L3）")
    db_session: Any | None = Field(None, description="数据库会话（用于需要数据库的工具）")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")


class ToolExecutor(IToolExecutor):
    """
    工具执行器

    负责工具的执行和验证，支持：
    - 传统 handler 执行模式
    - Runnable 执行模式
    - 混合模式（优先使用 Runnable）
    - 进度回调机制
    - 工具结果缓存
    """

    def __init__(
        self,
        registry: IToolRegistry,
        cache_config: ToolCacheConfig | None = None,
        db_session: Any | None = None,
    ):
        """初始化执行器"""
        self._registry = registry
        self._handlers: dict[str, ToolHandler] = {}
        self._use_runnable_first: bool = True  # 优先使用 Runnable
        self._progress_callback: ProgressCallback | None = None
        self._db_session = db_session  # 数据库会话

        # 缓存（组合 ToolCache）
        self._cache_config = cache_config or ToolCacheConfig.load_from_file()
        self._tool_cache = ToolCache(self._cache_config)

        # 嵌套执行记录管理（组合 NestedRecordManager）
        self._nested_record_mgr = NestedRecordManager(db_session)

        # 性能监控器
        try:
            from monitoring import get_performance_monitor  # noqa: PLC0415

            self._performance_monitor = get_performance_monitor()
        except ImportError:
            self._performance_monitor = None

    # ------------------------------------------------------------------
    # 缓存相关 — 委托给 ToolCache
    # ------------------------------------------------------------------

    def get_cache_stats(self) -> dict[str, Any]:
        """获取缓存统计"""
        return self._tool_cache.get_cache_stats()

    async def clear_tool_cache(self, tool_name: str | None = None) -> int:
        """清除工具缓存"""
        return await self._tool_cache.clear_tool_cache(tool_name)

    # ------------------------------------------------------------------
    # 进度回调
    # ------------------------------------------------------------------

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """设置进度回调函数"""
        self._progress_callback = callback

    async def _notify_progress(
        self,
        tool_call_id: str,
        progress: float,
        current_step: str | None = None,
    ) -> None:
        """通知进度更新"""
        if self._progress_callback:
            try:
                await self._progress_callback(tool_call_id, progress, current_step)
            except Exception as e:
                # 进度回调失败不应影响工具执行
                logger = logging.getLogger(__name__)
                logger.warning("进度回调失败: %s", e)

    # ------------------------------------------------------------------
    # Handler 注册
    # ------------------------------------------------------------------

    def register_handler(self, tool_name: str, handler: ToolHandler) -> None:
        """注册工具处理函数"""
        self._handlers[tool_name] = handler

    def unregister_handler(self, tool_name: str) -> None:
        """注销工具处理函数"""
        if tool_name in self._handlers:
            del self._handlers[tool_name]

    def has_handler(self, tool_name: str) -> bool:
        """检查是否有处理函数"""
        return tool_name in self._handlers

    # ------------------------------------------------------------------
    # 核心执行
    # ------------------------------------------------------------------

    async def execute(  # noqa: PLR0912,PLR0915
        self,
        tool_name: str,
        inputs: dict[str, Any],
        context: ExecutionContext,
        timeout: float | None = None,
        use_runnable: bool | None = None,
        tool_call_id: str | None = None,
        use_cache: bool = True,
    ) -> ToolExecutionResult:
        """执行工具"""
        start_time = time.time()

        # 生成工具调用 ID（如果未提供）
        if tool_call_id is None:
            import uuid  # noqa: PLC0415

            tool_call_id = str(uuid.uuid4())

        # 检查是否在评估器执行上下文中（需要创建嵌套执行记录）
        is_evaluation_context = context.metadata.get("evaluation", False)
        evaluation_record_id = context.metadata.get("evaluation_record_id")

        # 如果是评估上下文，必须提供 evaluation_record_id
        if is_evaluation_context and not evaluation_record_id:
            raise ValueError(
                f"评估上下文中必须提供 evaluation_record_id | tool_name={tool_name} | session_id={context.session_id}"
            )

        # 创建嵌套的评估器执行记录
        if evaluation_record_id:
            nested_record_id = await self._nested_record_mgr.create_nested_execution_record(
                parent_record_id=evaluation_record_id,
                session_id=context.session_id,
                tool_name=tool_name,
                tool_args=inputs,
                tool_call_id=tool_call_id,
            )
        else:
            nested_record_id = None

        logger.info(
            f"[ToolExecutor] 开始执行工具 | "
            f"tool_name={tool_name} | "
            f"tool_call_id={tool_call_id} | "
            f"session_id={context.session_id} | "
            f"user_id={context.user_id} | "
            f"evaluation_record_id={evaluation_record_id}"
        )
        logger.debug(
            f"[ToolExecutor] 工具输入参数 | "
            f"tool_name={tool_name} | "
            f"inputs={json.dumps(inputs, ensure_ascii=False, default=str)[:500]}"
        )
        logger.debug(
            f"[ToolExecutor] 执行配置 | timeout={timeout} | use_runnable={use_runnable} | use_cache={use_cache}"
        )

        # 获取工具定义（支持动态加载）
        tool = self._registry.get_optional(tool_name)

        # 如果工具未注册，尝试动态加载
        if tool is None:
            from tools.loader import get_dynamic_tool_loader  # noqa: PLC0415

            loader = get_dynamic_tool_loader()
            if loader is not None:
                try:
                    logger.info(f"[ToolExecutor] 工具未注册，尝试动态加载 | tool_name={tool_name}")
                    await loader.load_tool(tool_name)
                    # 重新获取工具
                    tool = self._registry.get(tool_name)
                except Exception as e:
                    logger.warning(f"[ToolExecutor] 动态加载失败 | tool_name={tool_name} | error={e}")

        if tool is None:
            raise ToolNotFoundError(tool_name)

        logger.debug(f"[ToolExecutor] 工具定义 | name={tool.name} | category={tool.category}")

        # 工具层级权限检查由 level_guard 插件统一处理（基于 tool_ids SSOT），
        # executor 不再重复校验

        inputs = self._validate_inputs(tool, inputs)
        logger.debug(f"[ToolExecutor] 输入验证通过 | tool_name={tool_name}")

        # 智能判断是否应该缓存
        should_cache = use_cache and self._tool_cache.should_cache(tool_name, inputs)

        # 尝试从缓存获取结果
        if should_cache:
            cached_result = await self._tool_cache.get_cached_result(tool_name, inputs)
            if cached_result is not None:
                logger.info(f"[ToolExecutor] 缓存命中 | tool_name={tool_name} | tool_call_id={tool_call_id}")
                # 添加缓存标记（在 metadata 和 data 中都标记）
                if cached_result.metadata is None:
                    cached_result.metadata = {}
                cached_result.metadata["from_cache"] = True
                cached_result.metadata["duration_ms"] = 0

                # 在 data 中添加明显的缓存提示
                if isinstance(cached_result.output, dict):
                    cached_result.output["_cache_info"] = "⚠️ 此结果来自缓存，操作已在之前执行过，无需重复操作"
                elif isinstance(cached_result.output, str):
                    cached_result.output = f"⚠️ [缓存结果] {cached_result.output}\n\n注意：此结果来自缓存，操作已在之前执行过，无需重复操作。"

                # 记录缓存命中的工具执行指标
                if self._performance_monitor:
                    try:
                        self._performance_monitor.record_tool_execution(
                            execution_time=0,  # 缓存命中，执行时间为0
                            cache_hit=True,
                            error=not cached_result.success,
                        )
                    except Exception as e:
                        logger.warning(f"记录缓存命中指标失败: {e}")

                return cached_result
            logger.debug(f"[ToolExecutor] 缓存未命中 | tool_name={tool_name} | tool_call_id={tool_call_id}")
        else:
            logger.debug(f"[ToolExecutor] 缓存已禁用 | tool_name={tool_name} | tool_call_id={tool_call_id}")

        # 发送开始进度
        await self._notify_progress(tool_call_id, 0.0, f"开始执行工具 {tool_name}")

        # 决定执行模式
        should_use_runnable = use_runnable if use_runnable is not None else self._use_runnable_first
        logger.debug(
            f"[ToolExecutor] 执行模式选择 | "
            f"tool_name={tool_name} | "
            f"mode={'Runnable' if should_use_runnable else 'Handler'}"
        )

        try:
            # 尝试使用 Runnable 执行
            if should_use_runnable:
                runnable = self._registry.get_runnable(tool_name)
                if runnable is not None:
                    logger.info(f"[ToolExecutor] 使用 Runnable 模式 | tool_name={tool_name}")
                    await self._notify_progress(tool_call_id, 10.0, "使用 Runnable 模式执行")
                    result = await self._execute_runnable(runnable, inputs, timeout, tool_call_id)
                    await self._notify_progress(tool_call_id, 100.0, "执行完成")
                    result = self._finalize_result(result, start_time, tool_name, cache_hit=False, tool=tool)

                    # 更新嵌套执行记录
                    if nested_record_id:
                        await self._nested_record_mgr.update_nested_execution_record(
                            record_id=nested_record_id,
                            success=result.success,
                            output=result.output,
                            error=result.error,
                            duration_ms=(result.metadata.get("duration_ms") if result.metadata else None),
                        )

                    logger.info(
                        f"[ToolExecutor] Runnable 执行完成 | "
                        f"tool_name={tool_name} | "
                        f"success={result.success} | "
                        f"duration_ms={result.metadata.get('duration_ms', 0) if result.metadata else 0}"
                    )

                    # 缓存结果
                    if should_cache and result.success:
                        await self._tool_cache.set_cached_result(tool_name, inputs, result)

                    return result

            # 回退到 handler 执行
            handler = self._handlers.get(tool_name)
            if handler is None:
                logger.error(f"[ToolExecutor] 未找到处理函数 | tool_name={tool_name}")
                raise ToolExecutionError(tool_name, f"未找到工具 '{tool_name}' 的处理函数或 Runnable")

            logger.info(f"[ToolExecutor] 使用 Handler 模式 | tool_name={tool_name}")
            await self._notify_progress(tool_call_id, 10.0, "使用 Handler 模式执行")
            result = await self._execute_handler(handler, tool_name, inputs, timeout, tool_call_id)
            await self._notify_progress(tool_call_id, 100.0, "执行完成")
            result = self._finalize_result(result, start_time, tool_name, cache_hit=False, tool=tool)

            # 更新嵌套执行记录
            if nested_record_id:
                await self._nested_record_mgr.update_nested_execution_record(
                    record_id=nested_record_id,
                    success=result.success,
                    output=result.output,
                    error=result.error,
                    duration_ms=(result.metadata.get("duration_ms") if result.metadata else None),
                )

            logger.info(
                f"[ToolExecutor] Handler 执行完成 | "
                f"tool_name={tool_name} | "
                f"success={result.success} | "
                f"duration_ms={result.metadata.get('duration_ms', 0) if result.metadata else 0}"
            )

            # 缓存结果
            if should_cache and result.success:
                await self._tool_cache.set_cached_result(tool_name, inputs, result)

            return result

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(
                f"[ToolExecutor] 执行失败 | tool_name={tool_name} | error={str(e)}",
                exc_info=True,
            )
            await self._notify_progress(tool_call_id, 100.0, f"执行失败: {str(e)}")

            # 更新嵌套执行记录（失败情况）
            if nested_record_id:
                await self._nested_record_mgr.update_nested_execution_record(
                    record_id=nested_record_id,
                    success=False,
                    error=str(e),
                    duration_ms=duration_ms,
                )

            raise

    async def execute_runnable(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        context: ExecutionContext,
        timeout: float | None = None,
    ) -> ToolExecutionResult:
        """强制使用 Runnable 模式执行"""
        return await self.execute(
            tool_name=tool_name,
            inputs=inputs,
            context=context,
            timeout=timeout,
            use_runnable=True,
        )

    async def _execute_runnable(
        self,
        runnable: "ToolRunnable",
        inputs: dict[str, Any],
        timeout: float | None,
        tool_call_id: str,
    ) -> ToolExecutionResult:
        """使用 Runnable 执行"""
        runnable_start = time.time()
        logger.debug(f"[ToolExecutor._execute_runnable] 开始 | tool_call_id={tool_call_id} | timeout={timeout}")

        try:
            await self._notify_progress(tool_call_id, 30.0, "准备执行 Runnable")

            if timeout:
                logger.debug(f"[ToolExecutor._execute_runnable] 带超时执行 | timeout={timeout}")
                await self._notify_progress(tool_call_id, 50.0, "执行中...")
                raw_result = await asyncio.wait_for(
                    runnable.ainvoke(inputs),
                    timeout=timeout,
                )
            else:
                await self._notify_progress(tool_call_id, 50.0, "执行中...")
                raw_result = await runnable.ainvoke(inputs)

            runnable_duration_ms = int((time.time() - runnable_start) * 1000)
            logger.debug(f"[ToolExecutor._execute_runnable] Runnable.ainvoke 完成 | duration_ms={runnable_duration_ms}")

            await self._notify_progress(tool_call_id, 90.0, "处理执行结果")

            # 将原始结果包装为 ToolExecutionResult
            if isinstance(raw_result, ToolExecutionResult):
                logger.debug(
                    f"[ToolExecutor._execute_runnable] 返回 ToolExecutionResult | success={raw_result.success}"
                )
                return raw_result
            logger.debug(
                f"[ToolExecutor._execute_runnable] 包装为 ToolExecutionResult | result_type={type(raw_result).__name__}"
            )
            return create_success_result(data=raw_result)

        except TimeoutError:
            logger.warning(
                f"[ToolExecutor._execute_runnable] 执行超时 | tool_call_id={tool_call_id} | timeout={timeout}"
            )
            return create_failure_result(
                error=f"执行超时（{timeout}秒）",
                error_code="TIMEOUT",
            )
        except Exception as e:
            logger.error(
                f"[ToolExecutor._execute_runnable] 执行异常 | tool_call_id={tool_call_id} | error={str(e)}",
                exc_info=True,
            )
            return create_failure_result(
                error=str(e),
                error_code="EXECUTION_ERROR",
            )

    async def _execute_handler(
        self,
        handler: ToolHandler,
        tool_name: str,
        inputs: dict[str, Any],
        timeout: float | None,
        tool_call_id: str,
    ) -> ToolExecutionResult:
        """使用 handler 执行"""
        handler_start = time.time()
        logger.debug(
            f"[ToolExecutor._execute_handler] 开始 | "
            f"tool_name={tool_name} | "
            f"tool_call_id={tool_call_id} | "
            f"timeout={timeout}"
        )

        try:
            await self._notify_progress(tool_call_id, 30.0, "准备执行 Handler")

            if timeout:
                logger.debug(f"[ToolExecutor._execute_handler] 带超时执行 | timeout={timeout}")
                await self._notify_progress(tool_call_id, 50.0, "执行中...")
                result = await asyncio.wait_for(
                    handler(inputs),
                    timeout=timeout,
                )
            else:
                await self._notify_progress(tool_call_id, 50.0, "执行中...")
                result = await handler(inputs)

            handler_duration_ms = int((time.time() - handler_start) * 1000)
            logger.debug(
                f"[ToolExecutor._execute_handler] Handler 执行完成 | "
                f"tool_name={tool_name} | "
                f"duration_ms={handler_duration_ms}"
            )

            await self._notify_progress(tool_call_id, 90.0, "处理执行结果")
            return result
        except TimeoutError:
            logger.warning(f"[ToolExecutor._execute_handler] 执行超时 | tool_name={tool_name} | timeout={timeout}")
            raise ToolExecutionError(tool_name, f"执行超时（{timeout}秒）") from None
        except Exception as e:
            logger.error(
                f"[ToolExecutor._execute_handler] 执行异常 | tool_name={tool_name} | error={str(e)}",
                exc_info=True,
            )
            raise ToolExecutionError(tool_name, str(e), cause=e) from e

    MAX_TOOL_OUTPUT_LENGTH = 100000  # 100K 字符

    def _finalize_result(
        self,
        result: ToolExecutionResult,
        start_time: float,
        tool_name: str,
        cache_hit: bool = False,
        tool: "Tool | None" = None,
    ) -> ToolExecutionResult:
        """完成结果处理，添加执行时间，验证输出结构，截断过大输出"""
        duration_ms = int((time.time() - start_time) * 1000)
        duration_seconds = duration_ms / 1000.0

        if result.metadata is None:
            result.metadata = {}
        result.metadata["duration_ms"] = duration_ms

        result.output = self._truncate_output(result.output)

        # 输出结构验证：如果工具定义了 output_schema，验证输出是否符合
        if tool and tool.output_schema and result.success:
            try:
                import jsonschema as _js  # noqa: PLC0415

                _js.validate(instance=result.output, schema=tool.output_schema)
            except Exception as schema_err:
                logger.warning(f"[ToolExecutor] 输出不符合 output_schema | tool_name={tool_name} | error={schema_err}")
                result = ToolExecutionResult.create_failed(
                    error=f"工具输出不符合预期结构: {schema_err}",
                    error_code="OUTPUT_SCHEMA_MISMATCH",
                )
                if result.metadata is None:
                    result.metadata = {}
                result.metadata["duration_ms"] = duration_ms

        # 记录工具执行指标
        if self._performance_monitor:
            try:
                self._performance_monitor.record_tool_execution(
                    execution_time=duration_seconds,
                    cache_hit=cache_hit,
                    error=not result.success,
                )
            except Exception as e:
                logger.warning(f"记录工具执行指标失败: {e}")

        return result

    def _truncate_output(self, output: Any) -> Any:
        """截断过大的工具输出，防止上下文窗口溢出

        当工具输出为字符串且超过阈值时，截断并添加提示信息。
        当输出为字典且包含大型字符串值时，对最长值进行截断。

        Args:
            output: 原始工具输出

        Returns:
            截断后的输出
        """
        if isinstance(output, str) and len(output) > self.MAX_TOOL_OUTPUT_LENGTH:
            truncated = output[: self.MAX_TOOL_OUTPUT_LENGTH]
            total_len = len(output)
            logger.warning(
                f"[ToolExecutor] 工具输出已截断 | "
                f"original_length={total_len} | max_length={self.MAX_TOOL_OUTPUT_LENGTH}"
            )
            return truncated + (f"\n\n[输出已截断，共 {total_len} 字符，仅显示前 {self.MAX_TOOL_OUTPUT_LENGTH} 字符]")
        return output

    async def batch_execute(
        self,
        calls: list[dict[str, Any]],
        context: ExecutionContext,
    ) -> list[ToolExecutionResult]:
        """批量执行工具"""
        tasks = [
            self.execute(
                tool_name=call["tool_name"],
                inputs=call.get("inputs", {}),
                context=context,
            )
            for call in calls
        ]

        return await asyncio.gather(*tasks, return_exceptions=False)

    async def execute_pipeline(
        self,
        tool_names: list[str],
        initial_input: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolExecutionResult:
        """执行工具管道（顺序执行，前一个输出作为后一个输入）"""
        current_input = initial_input

        for tool_name in tool_names:
            result = await self.execute(
                tool_name=tool_name,
                inputs=current_input,
                context=context,
            )

            if not result.success:
                return result

            # 使用当前结果作为下一个工具的输入
            current_input = result.output if isinstance(result.output, dict) else {"data": result.output}

        return result

    # ------------------------------------------------------------------
    # 输入验证 — 委托给 input_normalizer
    # ------------------------------------------------------------------

    def _validate_inputs(self, tool: Tool, inputs: dict[str, Any]) -> dict[str, Any]:
        """验证输入参数"""

        if tool.name == "task_submit":
            fix_task_submit_inputs(inputs)

        self._fill_schema_defaults(tool, inputs)

        normalized_inputs = normalize_input_types(inputs, tool.input_schema)

        try:
            jsonschema.validate(instance=normalized_inputs, schema=tool.input_schema)
        except jsonschema.ValidationError as e:
            raise ToolValidationError(
                f"工具 '{tool.name}' 输入验证失败: {e.message}",
                errors=[e.message],
            ) from e

        return normalized_inputs

    def _fill_schema_defaults(self, tool: Tool, inputs: dict[str, Any]) -> None:
        """填充 schema 中定义的默认值，解决 LLM 未传有默认值的必填参数的问题"""
        properties = tool.input_schema.get("properties", {})
        for field_name, field_def in properties.items():
            if field_name not in inputs and "default" in field_def:
                inputs[field_name] = field_def["default"]
                logger.debug(
                    f"[_fill_schema_defaults] 工具 '{tool.name}' 字段 '{field_name}' 使用默认值: {field_def['default']}"
                )

    # ------------------------------------------------------------------
    # 权限检查（已移除，由 level_guard 插件统一处理）
    # ------------------------------------------------------------------

    def set_runnable_first(self, enabled: bool) -> None:
        """设置是否优先使用 Runnable 模式"""
        self._use_runnable_first = enabled

    async def execute_task(self, task: Any) -> dict[str, Any]:
        """统一任务执行接口"""
        tool_name = task.config.get("tool_name")
        inputs = task.config.get("inputs", {})
        if tool_name is None:
            return {
                "success": False,
                "error": "工具执行缺少 tool_name 参数",
            }

        context = ExecutionContext(
            session_id=task.session_id or "",
            task_id=task.config.get("task_id", ""),
            user_id=task.config.get("user_id"),
            metadata=task.config.get("metadata", {}),
        )

        result = await self.execute(tool_name, inputs, context)
        return {
            "success": result.success,
            "output": result.output,
            "error": result.error,
        }

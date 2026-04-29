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
import hashlib
import json
import logging
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema
import yaml
from pydantic import BaseModel, Field

from core.exceptions import (
    ToolExecutionError,
    ToolNotFoundError,
    ToolValidationError,
)
from core.results import ToolExecutionResult
from tools.interfaces import IToolExecutor, IToolRegistry
from tools.types import Tool, create_failure_result, create_success_result

if TYPE_CHECKING:
    from core.runnable import ToolRunnable


# 工具处理函数类型
ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, ToolExecutionResult]]

# 进度回调函数类型
ProgressCallback = Callable[[str, float, str | None], Coroutine[Any, Any, None]]

# 日志
logger = logging.getLogger(__name__)


class ToolCacheConfig(BaseModel):
    """工具缓存配置"""

    enabled: bool = Field(default=True, description="是否启用缓存")
    default_ttl: int = Field(default=300, description="默认 TTL（秒）")
    tools: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="按工具配置"
    )

    @classmethod
    def load_from_file(cls, path: str = "config/builtin_tools_config.yaml"):
        """从配置文件加载"""
        config_path = Path(path)
        if not config_path.exists():
            return cls()

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        # 处理不同的配置文件格式
        # 1. 期望格式: {tool_cache: {enabled: bool, default_ttl: int, tools: {}}}
        # 2. 列表格式: [{tool_id: ..., enabled: ...}, ...]
        if isinstance(data, list):
            # 列表格式，返回默认配置（此文件用于工具注册，不是缓存配置）
            return cls()

        # 字典格式，提取 tool_cache 配置
        cache_config = data.get("tool_cache", {})
        return cls(
            enabled=cache_config.get("enabled", True),
            default_ttl=cache_config.get("default_ttl", 300),
            tools=cache_config.get("tools", {}),
        )

    def is_cacheable(self, tool_name: str) -> bool:
        """检查工具是否可缓存"""
        if not self.enabled:
            return False

        tool_config = self.tools.get(tool_name, {})
        return tool_config.get("enabled", False)

    def get_ttl(self, tool_name: str) -> int:
        """获取工具的 TTL"""
        tool_config = self.tools.get(tool_name, {})
        return tool_config.get("ttl", self.default_ttl)


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
    db_session: Any | None = Field(
        None, description="数据库会话（用于需要数据库的工具）"
    )
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

        # 缓存配置
        self._cache_config = cache_config or ToolCacheConfig.load_from_file()
        self._cache: Any | None = None  # 延迟初始化

        # 缓存统计
        self._cache_hits: int = 0
        self._cache_misses: int = 0

        # 性能监控器
        try:
            from monitoring import get_performance_monitor

            self._performance_monitor = get_performance_monitor()
        except ImportError:
            self._performance_monitor = None

    def _get_cache(self):
        """获取缓存实例（延迟初始化）"""
        if self._cache is None and self._cache_config.enabled:
            try:
                from cache.multi_level_cache import get_global_cache

                self._cache = get_global_cache()
            except ImportError:
                logger.warning("缓存模块不可用，禁用工具缓存")
                self._cache_config.enabled = False
        return self._cache

    def _generate_cache_key(self, tool_name: str, inputs: dict[str, Any]) -> str:
        """生成缓存键"""
        # 优化：快速生成缓存键
        try:
            # 对输入进行规范化处理，移除无关字段
            normalized_inputs = self._normalize_inputs(inputs)
            # 将输入序列化并哈希
            inputs_str = json.dumps(normalized_inputs, sort_keys=True, default=str)
            inputs_hash = hashlib.sha256(inputs_str.encode()).hexdigest()[:16]
            return f"tool:{tool_name}:{inputs_hash}"
        except Exception:
            # 异常时使用简单方式，确保缓存键生成不会失败
            return f"tool:{tool_name}:{hash(str(inputs))}"

    def _normalize_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """规范化输入参数，移除无关字段，提高缓存命中率"""
        normalized = {}
        for key, value in inputs.items():
            # 跳过可能变化但不影响结果的字段
            if key in [
                "timestamp",
                "request_id",
                "session_id",
                "user_id",
                "tool_call_id",
                "execution_id",
            ]:
                continue
            # 递归处理嵌套字典
            if isinstance(value, dict):
                normalized_value = self._normalize_inputs(value)
                # 只保留非空的嵌套字典
                if normalized_value:
                    normalized[key] = normalized_value
            elif value is not None and value != "":
                # 只保留非空值
                normalized[key] = value
        return normalized

    def _should_cache(self, tool_name: str, inputs: dict[str, Any]) -> bool:
        """判断是否应该缓存工具执行结果"""
        # 基础判断
        if not self._cache_config.is_cacheable(tool_name):
            return False

        # 对于某些工具，即使配置了缓存，也需要根据输入判断是否缓存
        no_cache_tools = ["task_submit"]
        if tool_name in no_cache_tools:
            return False

        # 对于输入中包含敏感信息的，不缓存
        if self._contains_sensitive_info(inputs):
            return False

        return True

    def _contains_sensitive_info(self, inputs: dict[str, Any]) -> bool:
        """判断输入中是否包含敏感信息"""
        sensitive_keys = ["password", "token", "secret", "key", "credential"]

        def check_sensitive(data):
            if isinstance(data, dict):
                for key, value in data.items():
                    if any(sensitive in key.lower() for sensitive in sensitive_keys):
                        return True
                    if check_sensitive(value):
                        return True
            elif isinstance(data, str):
                # 简单检查字符串中是否包含敏感信息模式
                return any(sensitive in data.lower() for sensitive in sensitive_keys)
            return False

        return check_sensitive(inputs)

    async def _get_cached_result(
        self, tool_name: str, inputs: dict[str, Any]
    ) -> ToolExecutionResult | None:
        """获取缓存的结果"""
        if not self._cache_config.is_cacheable(tool_name):
            return None

        cache = self._get_cache()
        if cache is None:
            return None

        cache_key = self._generate_cache_key(tool_name, inputs)
        try:
            cached_data = await cache.get(cache_key)
            if cached_data is not None:
                self._cache_hits += 1
                logger.debug("缓存命中: %s", cache_key)
                # 重建 ToolExecutionResult
                return ToolExecutionResult(**cached_data)
        except Exception as e:
            logger.warning("读取缓存失败: %s", e)

        self._cache_misses += 1
        return None

    async def _set_cached_result(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        result: ToolExecutionResult,
    ) -> None:
        """缓存结果"""
        if not self._cache_config.is_cacheable(tool_name):
            return

        # 只缓存成功的结果
        if not result.success:
            return

        cache = self._get_cache()
        if cache is None:
            return

        cache_key = self._generate_cache_key(tool_name, inputs)
        ttl = self._cache_config.get_ttl(tool_name)

        try:
            # 将 ToolExecutionResult 转换为可序列化的字典
            cache_data = result.model_dump()
            await cache.set(cache_key, cache_data, ttl)
            logger.debug("缓存结果: %s, TTL: %d", cache_key, ttl)
        except Exception as e:
            logger.warning("写入缓存失败: %s", e)

    def get_cache_stats(self) -> dict[str, Any]:
        """获取缓存统计"""
        total = self._cache_hits + self._cache_misses
        hit_rate = self._cache_hits / total if total > 0 else 0

        return {
            "enabled": self._cache_config.enabled,
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "total": total,
            "hit_rate": round(hit_rate * 100, 2),
        }

    async def clear_tool_cache(self, tool_name: str | None = None) -> int:
        """清除工具缓存"""
        cache = self._get_cache()
        if cache is None:
            return 0

        pattern = f"tool:{tool_name}:*" if tool_name else "tool:*"
        try:
            count = await cache.clear_pattern(pattern)
            logger.info("清除缓存: %s, 数量: %d", pattern, count)
            return count
        except Exception as e:
            logger.warning("清除缓存失败: %s", e)
            return 0

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

    async def execute(
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
            import uuid

            tool_call_id = str(uuid.uuid4())

        # 检查是否在评估器执行上下文中（需要创建嵌套执行记录）
        is_evaluation_context = context.metadata.get("evaluation", False)
        evaluation_record_id = context.metadata.get("evaluation_record_id")

        # 如果是评估上下文，必须提供 evaluation_record_id
        if is_evaluation_context and not evaluation_record_id:
            raise ValueError(
                f"评估上下文中必须提供 evaluation_record_id | "
                f"tool_name={tool_name} | session_id={context.session_id}"
            )

        # 创建嵌套的评估器执行记录
        if evaluation_record_id:
            nested_record_id = await self._create_nested_execution_record(
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
            f"[ToolExecutor] 执行配置 | "
            f"timeout={timeout} | "
            f"use_runnable={use_runnable} | "
            f"use_cache={use_cache}"
        )

        # 获取工具定义（支持动态加载）
        tool = self._registry.get_optional(tool_name)

        # 如果工具未注册，尝试动态加载
        if tool is None:
            from tools.loader import get_dynamic_tool_loader

            loader = get_dynamic_tool_loader()
            if loader is not None:
                try:
                    logger.info(
                        f"[ToolExecutor] 工具未注册，尝试动态加载 | tool_name={tool_name}"
                    )
                    await loader.load_tool(tool_name)
                    # 重新获取工具
                    tool = self._registry.get(tool_name)
                except Exception as e:
                    logger.warning(
                        f"[ToolExecutor] 动态加载失败 | tool_name={tool_name} | error={e}"
                    )

        if tool is None:
            raise ToolNotFoundError(tool_name)

        logger.debug(
            f"[ToolExecutor] 工具定义 | "
            f"name={tool.name} | "
            f"category={tool.category}"
        )

        self._check_tool_level_permission(tool, context.agent_level)

        inputs = self._validate_inputs(tool, inputs)
        logger.debug(f"[ToolExecutor] 输入验证通过 | tool_name={tool_name}")

        # 智能判断是否应该缓存
        should_cache = use_cache and self._should_cache(tool_name, inputs)

        # 尝试从缓存获取结果
        if should_cache:
            cached_result = await self._get_cached_result(tool_name, inputs)
            if cached_result is not None:
                logger.info(
                    f"[ToolExecutor] 缓存命中 | tool_name={tool_name} | tool_call_id={tool_call_id}"
                )
                # 添加缓存标记（在 metadata 和 data 中都标记）
                if cached_result.metadata is None:
                    cached_result.metadata = {}
                cached_result.metadata["from_cache"] = True
                cached_result.metadata["duration_ms"] = 0

                # 在 data 中添加明显的缓存提示
                if isinstance(cached_result.data, dict):
                    cached_result.data["_cache_info"] = (
                        "⚠️ 此结果来自缓存，操作已在之前执行过，无需重复操作"
                    )
                elif isinstance(cached_result.data, str):
                    cached_result.data = f"⚠️ [缓存结果] {cached_result.data}\n\n注意：此结果来自缓存，操作已在之前执行过，无需重复操作。"

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
            else:
                logger.debug(
                    f"[ToolExecutor] 缓存未命中 | tool_name={tool_name} | tool_call_id={tool_call_id}"
                )
        else:
            logger.debug(
                f"[ToolExecutor] 缓存已禁用 | tool_name={tool_name} | tool_call_id={tool_call_id}"
            )

        # 发送开始进度
        await self._notify_progress(tool_call_id, 0.0, f"开始执行工具 {tool_name}")

        # 决定执行模式
        should_use_runnable = (
            use_runnable if use_runnable is not None else self._use_runnable_first
        )
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
                    logger.info(
                        f"[ToolExecutor] 使用 Runnable 模式 | tool_name={tool_name}"
                    )
                    await self._notify_progress(
                        tool_call_id, 10.0, "使用 Runnable 模式执行"
                    )
                    result = await self._execute_runnable(
                        runnable, inputs, timeout, tool_call_id
                    )
                    await self._notify_progress(tool_call_id, 100.0, "执行完成")
                    result = self._finalize_result(
                        result, start_time, tool_name, cache_hit=False, tool=tool
                    )

                    # 更新嵌套执行记录
                    if nested_record_id:
                        await self._update_nested_execution_record(
                            record_id=nested_record_id,
                            success=result.success,
                            output=result.data,
                            error=result.error,
                            duration_ms=(
                                result.metadata.get("duration_ms")
                                if result.metadata
                                else None
                            ),
                        )

                    logger.info(
                        f"[ToolExecutor] Runnable 执行完成 | "
                        f"tool_name={tool_name} | "
                        f"success={result.success} | "
                        f"duration_ms={result.metadata.get('duration_ms', 0) if result.metadata else 0}"
                    )

                    # 缓存结果
                    if should_cache and result.success:
                        await self._set_cached_result(tool_name, inputs, result)

                    return result

            # 回退到 handler 执行
            handler = self._handlers.get(tool_name)
            if handler is None:
                logger.error(f"[ToolExecutor] 未找到处理函数 | tool_name={tool_name}")
                raise ToolExecutionError(
                    tool_name, f"未找到工具 '{tool_name}' 的处理函数或 Runnable"
                )

            logger.info(f"[ToolExecutor] 使用 Handler 模式 | tool_name={tool_name}")
            await self._notify_progress(tool_call_id, 10.0, "使用 Handler 模式执行")
            result = await self._execute_handler(
                handler, tool_name, inputs, timeout, tool_call_id
            )
            await self._notify_progress(tool_call_id, 100.0, "执行完成")
            result = self._finalize_result(
                result, start_time, tool_name, cache_hit=False, tool=tool
            )

            # 更新嵌套执行记录
            if nested_record_id:
                await self._update_nested_execution_record(
                    record_id=nested_record_id,
                    success=result.success,
                    output=result.data,
                    error=result.error,
                    duration_ms=(
                        result.metadata.get("duration_ms") if result.metadata else None
                    ),
                )

            logger.info(
                f"[ToolExecutor] Handler 执行完成 | "
                f"tool_name={tool_name} | "
                f"success={result.success} | "
                f"duration_ms={result.metadata.get('duration_ms', 0) if result.metadata else 0}"
            )

            # 缓存结果
            if should_cache and result.success:
                await self._set_cached_result(tool_name, inputs, result)

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
                await self._update_nested_execution_record(
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
        logger.debug(
            f"[ToolExecutor._execute_runnable] 开始 | "
            f"tool_call_id={tool_call_id} | "
            f"timeout={timeout}"
        )

        try:
            await self._notify_progress(tool_call_id, 30.0, "准备执行 Runnable")

            if timeout:
                logger.debug(
                    f"[ToolExecutor._execute_runnable] 带超时执行 | timeout={timeout}"
                )
                await self._notify_progress(tool_call_id, 50.0, "执行中...")
                raw_result = await asyncio.wait_for(
                    runnable.ainvoke(inputs),
                    timeout=timeout,
                )
            else:
                await self._notify_progress(tool_call_id, 50.0, "执行中...")
                raw_result = await runnable.ainvoke(inputs)

            runnable_duration_ms = int((time.time() - runnable_start) * 1000)
            logger.debug(
                f"[ToolExecutor._execute_runnable] Runnable.ainvoke 完成 | "
                f"duration_ms={runnable_duration_ms}"
            )

            await self._notify_progress(tool_call_id, 90.0, "处理执行结果")

            # 将原始结果包装为 ToolExecutionResult
            if isinstance(raw_result, ToolExecutionResult):
                logger.debug(
                    f"[ToolExecutor._execute_runnable] 返回 ToolExecutionResult | "
                    f"success={raw_result.success}"
                )
                return raw_result
            else:
                logger.debug(
                    f"[ToolExecutor._execute_runnable] 包装为 ToolExecutionResult | "
                    f"result_type={type(raw_result).__name__}"
                )
                return create_success_result(data=raw_result)

        except TimeoutError:
            logger.warning(
                f"[ToolExecutor._execute_runnable] 执行超时 | "
                f"tool_call_id={tool_call_id} | "
                f"timeout={timeout}"
            )
            return create_failure_result(
                error=f"执行超时（{timeout}秒）",
                error_code="TIMEOUT",
            )
        except Exception as e:
            logger.error(
                f"[ToolExecutor._execute_runnable] 执行异常 | "
                f"tool_call_id={tool_call_id} | "
                f"error={str(e)}",
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
                logger.debug(
                    f"[ToolExecutor._execute_handler] 带超时执行 | timeout={timeout}"
                )
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
            logger.warning(
                f"[ToolExecutor._execute_handler] 执行超时 | "
                f"tool_name={tool_name} | "
                f"timeout={timeout}"
            )
            raise ToolExecutionError(tool_name, f"执行超时（{timeout}秒）") from None
        except Exception as e:
            logger.error(
                f"[ToolExecutor._execute_handler] 执行异常 | "
                f"tool_name={tool_name} | "
                f"error={str(e)}",
                exc_info=True,
            )
            raise ToolExecutionError(tool_name, str(e), cause=e) from e

    # BUG-FIX-fix_20260422_context_overflow: 工具输出截断阈值，防止巨大输出撑爆 LLM 上下文窗口
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

        # BUG-FIX-fix_20260422_context_overflow: 截断过大的工具输出，防止上下文窗口溢出
        result.output = self._truncate_output(result.output)

        # 输出结构验证：如果工具定义了 output_schema，验证输出是否符合
        if tool and tool.output_schema and result.success:
            try:
                import jsonschema as _js
                _js.validate(instance=result.data, schema=tool.output_schema)
            except Exception as schema_err:
                logger.warning(
                    f"[ToolExecutor] 输出不符合 output_schema | "
                    f"tool_name={tool_name} | error={schema_err}"
                )
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
            truncated = output[:self.MAX_TOOL_OUTPUT_LENGTH]
            total_len = len(output)
            logger.warning(
                f"[ToolExecutor] 工具输出已截断 | "
                f"original_length={total_len} | max_length={self.MAX_TOOL_OUTPUT_LENGTH}"
            )
            return truncated + (
                f"\n\n[输出已截断，共 {total_len} 字符，"
                f"仅显示前 {self.MAX_TOOL_OUTPUT_LENGTH} 字符]"
            )
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
            if isinstance(result.data, dict):
                current_input = result.data
            else:
                current_input = {"data": result.data}

        return result

    def _normalize_input_types(
        self, inputs: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """规范化输入参数类型，修复 LLM 返回的类型不一致问题"""
        if not isinstance(inputs, dict) or not isinstance(schema, dict):
            return inputs

        properties = schema.get("properties", {})
        normalized = dict(inputs)

        for key, value in normalized.items():
            if key not in properties:
                continue

            prop_schema = properties[key]
            expected_type = prop_schema.get("type")

            if expected_type == "boolean" and isinstance(value, str):
                lower_value = value.lower().strip()
                if lower_value in ("true", "1", "yes"):
                    normalized[key] = True
                    logger.debug(
                        f"[_normalize_input_types] 自动转换: {key}='{value}' -> True"
                    )
                elif lower_value in ("false", "0", "no"):
                    normalized[key] = False
                    logger.debug(
                        f"[_normalize_input_types] 自动转换: {key}='{value}' -> False"
                    )
            elif expected_type == "integer" and isinstance(value, str):
                try:
                    normalized[key] = int(value)
                    logger.debug(
                        f"[_normalize_input_types] 自动转换: {key}='{value}' -> {normalized[key]}"
                    )
                except ValueError:
                    pass
            elif expected_type == "number" and isinstance(value, str):
                try:
                    normalized[key] = float(value)
                    logger.debug(
                        f"[_normalize_input_types] 自动转换: {key}='{value}' -> {normalized[key]}"
                    )
                except ValueError:
                    pass
            elif expected_type == "object" and isinstance(value, str):
                parsed = self._try_parse_json_string(value)
                if parsed is not None:
                    normalized[key] = parsed
                    logger.debug(
                        f"[_normalize_input_types] 自动转换: {key} 从字符串解析为对象"
                    )

            if isinstance(normalized.get(key), dict) and expected_type == "object":
                self._normalize_nested_object(normalized[key], prop_schema)

        return normalized

    def _try_parse_json_string(self, value: str) -> dict | None:
        """尝试将 JSON 字符串解析为字典"""
        stripped = value.strip()
        if not stripped or not stripped.startswith("{"):
            return None
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def _normalize_nested_object(
        self, obj: dict[str, Any], schema: dict[str, Any]
    ) -> None:
        """递归规范化嵌套对象中的字符串类型字段"""
        nested_props = schema.get("properties", {})
        additional = schema.get("additionalProperties")

        for key, value in obj.items():
            if isinstance(value, str):
                prop_schema = nested_props.get(key)
                if prop_schema is None and additional and isinstance(additional, dict):
                    prop_schema = additional

                if prop_schema and isinstance(prop_schema, dict):
                    expected = prop_schema.get("type")
                    if expected == "object":
                        parsed = self._try_parse_json_string(value)
                        if parsed is not None:
                            obj[key] = parsed
                            logger.debug(
                                f"[_normalize_nested_object] {key} 从字符串解析为对象"
                            )
                            self._normalize_nested_object(obj[key], prop_schema)
                    elif expected == "boolean":
                        lower = value.lower().strip()
                        if lower in ("true", "1", "yes"):
                            obj[key] = True
                        elif lower in ("false", "0", "no"):
                            obj[key] = False
                    elif expected == "integer":
                        try:
                            obj[key] = int(value)
                        except ValueError:
                            pass
                    elif expected == "number":
                        try:
                            obj[key] = float(value)
                        except ValueError:
                            pass

            elif isinstance(value, dict):
                prop_schema = nested_props.get(key)
                if prop_schema is None and additional and isinstance(additional, dict):
                    prop_schema = additional
                if prop_schema and isinstance(prop_schema, dict):
                    self._normalize_nested_object(value, prop_schema)

    def _validate_inputs(self, tool: Tool, inputs: dict[str, Any]) -> dict[str, Any]:
        """验证输入参数"""

        if tool.name == "task_submit":
            self._fix_task_submit_inputs(inputs)

        self._fill_schema_defaults(tool, inputs)

        normalized_inputs = self._normalize_input_types(inputs, tool.input_schema)

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

    def _fix_task_submit_inputs(self, inputs: dict[str, Any]) -> None:
        """自动修复 task_submit 工具的常见 LLM 输入错误"""

        self._fix_object_field(inputs, "acceptance_criteria")
        self._fix_object_field(inputs, "metadata")

        self._fix_acceptance_criteria_inputs(inputs)

        task_scope = inputs.get("task_scope", "non_container")
        if task_scope == "non_container" and "target_type" in inputs:
            ac = inputs.get("acceptance_criteria")
            if not ac or not isinstance(ac, dict) or len(ac) == 0:
                target_id = inputs.get("target_id", "unknown")
                inputs["acceptance_criteria"] = {
                    "file_check": {
                        "input_params": {
                            "target_id": target_id,
                        }
                    }
                }
                logger.info(
                    f"[_fix_task_submit_inputs] acceptance_criteria 缺失或无效，使用默认 file_check"
                )

        if "goal" in inputs:
            goal = inputs["goal"]
            if isinstance(goal, str):
                try:
                    parsed = json.loads(goal)
                    if isinstance(parsed, dict):
                        inputs["goal"] = parsed
                        goal = parsed
                        logger.info(
                            "[_fix_task_submit_inputs] goal 从字符串解析为对象"
                        )
                except (json.JSONDecodeError, TypeError):
                    inputs["goal"] = {"title": goal[:50] if len(goal) > 50 else goal}
                    logger.info(
                        f"[_fix_task_submit_inputs] goal 从字符串转为 {{title: ...}}"
                    )
                    return

            if isinstance(goal, dict) and "title" not in goal:
                try:
                    if "description" in goal:
                        desc = goal["description"]
                        title = (
                            desc.split("。")[0]
                            .split(".")[0]
                            .split("，")[0]
                            .split(",")[0]
                            .strip()
                        )
                        if len(title) > 50:
                            title = title[:47] + "..."
                        if not title:
                            title = "未命名任务"
                        goal["title"] = title
                        logger.info(
                            f"[_fix_task_submit_inputs] 自动为 goal 添加 title: {title}"
                        )
                    else:
                        goal["title"] = "未命名任务"
                        logger.info("[_fix_task_submit_inputs] goal 使用默认 title")
                except Exception as e:
                    logger.error(f"[_fix_task_submit_inputs] 修复 goal 时出错: {e}")

        if "goal" not in inputs and "title" in inputs:
            logger.info(
                "[_fix_task_submit_inputs] 检测到 LLM 将参数平铺在顶层，自动包装为 goal"
            )
            goal_obj = {"title": inputs.pop("title")}
            if "description" in inputs:
                goal_obj["description"] = inputs.pop("description")
            if "requirements" in inputs:
                goal_obj["context"] = {"requirements": inputs.pop("requirements")}
            if "agent_config" in inputs:
                goal_obj.setdefault("context", {})["agent_config"] = inputs.pop(
                    "agent_config"
                )
            inputs["goal"] = goal_obj
            logger.info(f"[_fix_task_submit_inputs] 重组后的 goal: {goal_obj}")

    def _fix_acceptance_criteria_inputs(self, inputs: dict[str, Any]) -> None:
        """修复 acceptance_criteria 中 metric 对象缺少 input_params 的问题

        LLM 经常将 metric 的参数直接平铺（如 {"criteria": "..."}），
        而不是按照 schema 要求包装在 input_params 中（如 {"input_params": {"criteria": "..."}}）。
        此方法检测并自动修复这种格式错误。
        """
        ac = inputs.get("acceptance_criteria")
        if not ac or not isinstance(ac, dict):
            return

        known_keys = {"input_params", "expected_output", "pass_threshold"}

        for metric_id, metric_config in ac.items():
            if not isinstance(metric_config, dict):
                continue

            if "input_params" in metric_config:
                continue

            other_keys = {k for k in metric_config if k not in known_keys}
            if other_keys:
                input_params = {k: metric_config.pop(k) for k in list(other_keys)}
                metric_config["input_params"] = input_params
                logger.info(
                    f"[_fix_acceptance_criteria_inputs] metric '{metric_id}' 缺少 input_params，"
                    f"已将字段 {other_keys} 包装为 input_params"
                )
            else:
                metric_config["input_params"] = {}
                logger.info(
                    f"[_fix_acceptance_criteria_inputs] metric '{metric_id}' 缺少 input_params，"
                    f"已补充空 input_params"
                )

    def _fix_object_field(self, inputs: dict[str, Any], field_name: str) -> None:
        """修复 LLM 将 object 类型字段传为 JSON 字符串的问题"""
        if field_name not in inputs:
            return

        value = inputs[field_name]

        if isinstance(value, dict):
            return

        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                inputs.pop(field_name, None)
                return

            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    inputs[field_name] = parsed
                    logger.info(
                        f"[_fix_object_field] {field_name} 从字符串解析为对象"
                    )
                    return
                elif isinstance(parsed, list):
                    logger.warning(
                        f"[_fix_object_field] {field_name} 解析为列表而非对象，移除该字段"
                    )
                    inputs.pop(field_name, None)
                    return
            except (json.JSONDecodeError, TypeError):
                pass

            if stripped.startswith("{"):
                fixed = self._try_fix_truncated_json(stripped)
                if fixed is not None:
                    inputs[field_name] = fixed
                    logger.info(
                        f"[_fix_object_field] {field_name} 截断 JSON 修复成功"
                    )
                else:
                    logger.warning(
                        f"[_fix_object_field] {field_name} JSON 修复失败，使用空对象: {stripped[:100]}"
                    )
                    inputs[field_name] = {}
            else:
                logger.warning(
                    f"[_fix_object_field] {field_name} 不是有效对象，移除该字段: {type(value)}"
                )
                inputs.pop(field_name, None)

        elif isinstance(value, bool):
            logger.warning(
                f"[_fix_object_field] {field_name} 收到布尔值 True（LLM 错误），移除该字段"
            )
            inputs.pop(field_name, None)

        elif not isinstance(value, dict):
            logger.warning(
                f"[_fix_object_field] {field_name} 类型异常({type(value).__name__})，移除该字段"
            )
            inputs.pop(field_name, None)

    def _try_fix_truncated_json(self, json_str: str) -> dict | None:
        """尝试修复被截断的 JSON 字符串"""
        open_braces = json_str.count("{") - json_str.count("}")
        open_brackets = json_str.count("[") - json_str.count("]")

        in_string = False
        escape_next = False
        for ch in json_str:
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue

        if not in_string and open_braces >= 0 and open_brackets >= 0:
            fixed = json_str + "]" * max(0, open_brackets) + "}" * max(0, open_braces)
            try:
                parsed = json.loads(fixed)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass

        return None

    def _check_tool_level_permission(self, tool: Tool, agent_level: int) -> None:
        """检查工具级别权限（第二道防线）"""
        from tools.types import ToolLevel

        tool_level = getattr(tool, "level", None)
        if tool_level is None:
            return

        if isinstance(tool_level, str):
            try:
                tool_level = ToolLevel(tool_level)
            except ValueError:
                return

        has_permission = True
        if tool_level == ToolLevel.L1_ONLY:
            has_permission = agent_level == 1
        elif tool_level == ToolLevel.L1_L2_ONLY:
            has_permission = agent_level in (1, 2)

        if not has_permission:
            level_name = {1: "L1", 2: "L2", 3: "L3"}.get(agent_level, f"L{agent_level}")
            error_msg = (
                f"权限不足：工具 '{tool.name}' 需要 {tool_level.value} 权限，"
                f"当前 Agent 为 {level_name}"
            )
            logger.error(
                f"[ToolExecutor] 工具权限检查失败 | "
                f"tool_name={tool.name} | "
                f"tool_level={tool_level.value} | "
                f"agent_level=L{agent_level}"
            )
            raise ToolExecutionError(tool.name, error_msg)

        logger.debug(
            f"[ToolExecutor] 工具权限检查通过 | "
            f"tool_name={tool.name} | "
            f"tool_level={tool_level.value if hasattr(tool_level, 'value') else tool_level} | "
            f"agent_level=L{agent_level}"
        )

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
            "output": result.output if hasattr(result, "output") else result.data,
            "error": result.error,
        }

    async def _create_nested_execution_record(
        self,
        parent_record_id: str,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str | None = None,
    ) -> str | None:
        """创建嵌套的评估器执行记录"""
        try:

            db = self._db_session
            if db is None:
                from infrastructure.db import get_session_context

                async with get_session_context() as db_session:
                    return await self._create_nested_record_in_session(
                        db_session,
                        parent_record_id,
                        session_id,
                        tool_name,
                        tool_args,
                        tool_call_id,
                    )
            else:
                return await self._create_nested_record_in_session(
                    db, parent_record_id, session_id, tool_name, tool_args, tool_call_id
                )

        except Exception as e:
            logger.warning(
                f"[ToolExecutor] 创建嵌套执行记录失败 | tool_name={tool_name} | error={e}"
            )
            return None

    async def _create_nested_record_in_session(
        self,
        db,
        parent_record_id: str,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str | None = None,
    ) -> str | None:
        """在指定会话中创建嵌套执行记录"""
        from sqlalchemy import select

        from db.models import ExecutionRecord
        from db.repositories.execution_record_repo import ExecutionRecordRepository

        repo = ExecutionRecordRepository(db)

        parent_record = await db.execute(
            select(ExecutionRecord).where(ExecutionRecord.id == parent_record_id)
        )
        parent = parent_record.scalar_one_or_none()

        actual_session_id = session_id
        if parent:
            actual_session_id = parent.session_id
        else:
            logger.warning(
                f"[ToolExecutor] 父执行记录不存在，使用传入的 session_id | "
                f"parent_record_id={parent_record_id} | session_id={session_id}"
            )

        message_data = {
            "name": tool_name,
            "input": tool_args,
        }

        if tool_call_id:
            message_data["tool_call_id"] = tool_call_id

        record_id = await repo.save_execution_record(
            session_id=actual_session_id,
            message_data=message_data,
            type="tool",
            status="running",
            parent_record_id=parent_record_id,
            auto_commit=db is not self._db_session,
        )
        # BUG-FIX-fix_20260320_stream_sequence: save_execution_record 现在返回字典
        record_id = record_id["record_id"]

        logger.info(
            f"[ToolExecutor] 创建嵌套执行记录 | "
            f"record_id={record_id} | session_id={actual_session_id} | "
            f"parent_id={parent_record_id} | tool_name={tool_name}"
        )

        return record_id

    async def _update_nested_execution_record(
        self,
        record_id: str,
        success: bool,
        output: Any = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """更新嵌套的评估器执行记录"""
        try:

            db = self._db_session
            if db is None:
                from infrastructure.db import get_session_context

                async with get_session_context() as db_session:
                    await self._update_nested_record_in_session(
                        db_session, record_id, success, output, error, duration_ms
                    )
            else:
                await self._update_nested_record_in_session(
                    db, record_id, success, output, error, duration_ms
                )

        except Exception as e:
            logger.warning(
                f"[ToolExecutor] 更新嵌套执行记录失败 | record_id={record_id} | error={e}"
            )

    async def _update_nested_record_in_session(
        self,
        db,
        record_id: str,
        success: bool,
        output: Any = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """在指定会话中更新嵌套执行记录"""
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified

        from db.models import ExecutionRecord

        result = await db.execute(
            select(ExecutionRecord).where(ExecutionRecord.id == record_id)
        )
        record = result.scalar_one_or_none()

        if not record:
            logger.warning(f"[ToolExecutor] 嵌套执行记录不存在 | record_id={record_id}")
            return

        # 更新状态（只更新独立列）
        status_value = "completed" if success else "failed"
        record.status = status_value

        if output is not None:
            record.message_data["output"] = {"result": output}

        if error is not None:
            record.message_data["error"] = error

        if duration_ms is not None:
            record.message_data["duration_ms"] = duration_ms

        flag_modified(record, "message_data")

        if db is not self._db_session:
            await db.commit()

        logger.info(
            f"[ToolExecutor] 更新嵌套执行记录 | "
            f"record_id={record_id} | success={success} | duration_ms={duration_ms}"
        )

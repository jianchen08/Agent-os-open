"""
推理中间件

使用装饰器模式包装 ToolExecutor，在工具执行前进行推理检查
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from src.core.exceptions import ReasoningRequiredError
from src.tools.executor import ExecutionContext, ToolExecutor

from .extractor import ReasoningExtractor
from .interceptor import ReasoningInterceptor
from .validator import ReasoningValidator

logger = logging.getLogger(__name__)


class ReasoningMiddleware:
    """
    推理中间件（装饰器模式）

    包装 ToolExecutor，在工具执行前进行推理检查
    """

    def __init__(
        self,
        executor: ToolExecutor,
        config_path: str = "config/tools/reasoning_rules.yaml",
    ):
        """
        初始化中间件

        Args:
            executor: 原始工具执行器
            config_path: 推理配置文件路径
        """
        self.executor = executor
        self.interceptor = ReasoningInterceptor(config_path)
        self.extractor = ReasoningExtractor()
        self.validator = ReasoningValidator()

        # 内存缓存（只保留状态，不保留内容）
        self._reasoning_cache: dict[str, dict] = {}

    async def execute(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        context: dict[str, Any] | ExecutionContext,
        **kwargs: Any,
    ) -> Any:
        """
        执行工具（带推理检查）

        Args:
            tool_name: 工具名称
            inputs: 工具输入
            context: 执行上下文（必须包含 messages）
            **kwargs: 其他参数

        Returns:
            工具执行结果

        Raises:
            ReasoningRequiredError: 需要推理时抛出
        """
        # 统一处理 context：支持 dict 和 ExecutionContext 两种类型
        if isinstance(context, ExecutionContext):
            exec_ctx = context
            metadata = context.metadata or {}
            tool_call_id = metadata.get("tool_call_id", str(uuid.uuid4()))
            messages = metadata.get("messages", [])
        else:
            # context 是 dict
            tool_call_id = context.get("tool_call_id", str(uuid.uuid4()))
            messages = context.get("messages", [])
            # 创建 ExecutionContext
            exec_ctx = ExecutionContext(
                session_id=context.get("session_id", ""),
                user_id=context.get("user_id"),
                metadata=context,
            )

        logger.debug(
            "[ReasoningMiddleware] 开始执行 | tool=%s | call_id=%s",
            tool_name,
            tool_call_id,
        )

        # 1. 检查缓存
        cached = self._reasoning_cache.get(tool_call_id)
        if cached and cached.get("validated"):
            logger.info(
                "[ReasoningMiddleware] 推理已验证，直接执行 | tool=%s",
                tool_name,
            )
            # 使用已创建的 exec_ctx
            return await self.executor.execute(tool_name, inputs, exec_ctx, **kwargs)

        # 2. 推理拦截检查
        retry_count = cached.get("retry_count", 0) if cached else 0

        allowed, prompt = self.interceptor.check(
            tool_name=tool_name, messages=messages, retry_count=retry_count
        )

        if not allowed:
            # 更新缓存
            self._reasoning_cache[tool_call_id] = {
                "validated": False,
                "retry_count": retry_count + 1,
                "last_check_at": datetime.now().isoformat(),
            }

            logger.warning(
                "[ReasoningMiddleware] 推理检查失败 | tool=%s | retry=%d",
                tool_name,
                retry_count + 1,
            )

            # 抛出异常（确保 prompt 不为 None）
            if prompt is None:
                prompt = "请先分析此操作的意图、影响和执行策略"

            raise ReasoningRequiredError(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                reasoning_prompt=prompt,
                retry_count=retry_count + 1,
            )

        # 3. 提取推理内容
        reasoning_text = self.extractor.extract(messages)

        # 4. 验证推理质量
        _is_valid, quality = self.validator.validate(reasoning_text)

        # 5. 更新缓存
        self._reasoning_cache[tool_call_id] = {
            "validated": True,
            "retry_count": retry_count,
            "quality_score": quality.get("completeness_score", 0),
            "validated_at": datetime.now().isoformat(),
        }

        logger.info(
            "[ReasoningMiddleware] 推理验证通过 | tool=%s | score=%.2f",
            tool_name,
            quality.get("completeness_score", 0),
        )

        # 6. 执行工具（使用已创建的 exec_ctx）
        result = await self.executor.execute(tool_name, inputs, exec_ctx, **kwargs)

        # 7. 记录推理信息（可选）
        # 只有当 context 是 dict 时才直接修改，ExecutionContext 的 metadata 已在前面获取
        if reasoning_text:
            if isinstance(context, dict) and context.get("save_reasoning"):
                summary = self.extractor.extract_summary(reasoning_text)
                context["reasoning_summary"] = summary
                context["reasoning_quality"] = quality
                logger.debug("[ReasoningMiddleware] 推理摘要: %s", summary)
            elif isinstance(context, ExecutionContext) and context.metadata.get("save_reasoning"):
                summary = self.extractor.extract_summary(reasoning_text)
                context.metadata["reasoning_summary"] = summary
                context.metadata["reasoning_quality"] = quality
                logger.debug("[ReasoningMiddleware] 推理摘要: %s", summary)

        return result

    def clear_cache(self, tool_call_id: str | None = None) -> None:
        """
        清理缓存

        Args:
            tool_call_id: 工具调用 ID，为 None 时清理所有缓存
        """
        if tool_call_id:
            self._reasoning_cache.pop(tool_call_id, None)
            logger.debug("[ReasoningMiddleware] 清理缓存 | call_id=%s", tool_call_id)
        else:
            self._reasoning_cache.clear()
            logger.debug("[ReasoningMiddleware] 清理所有缓存")

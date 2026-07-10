"""
成本控制装饰器

提供 LLM 调用的预算检查和使用量记录装饰器
"""

import functools
from collections.abc import Callable
from typing import Any

# 异常类型在调用方处理，这里仅导入预算管理器
from src.core.tokenizer import get_token_counter
from src.cost_control.budget_manager import get_budget_manager


def budget_check(
    estimated_tokens: int | None = None,
    user_id_param: str | None = None,
    task_id_param: str | None = None,
    session_id_param: str | None = None,
    model_param: str | None = None,
):
    """
    预算检查装饰器

    在 LLM 调用前检查预算，调用后记录使用量

    Args:
        estimated_tokens: 预估 Token 数（如果为 None，则根据消息计算）
        user_id_param: 从参数中获取 user_id 的参数名
        task_id_param: 从参数中获取 task_id 的参数名
        session_id_param: 从参数中获取 session_id 的参数名
        model_param: 从参数中获取 model 的参数名

    Example:
        @budget_check(session_id_param="session_id", model_param="model_name")
        async def generate(self, messages, model_name="gpt-4", session_id=None):
            ...
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            budget_manager = get_budget_manager()
            token_counter = get_token_counter()

            # 提取上下文 ID
            user_id = kwargs.get(user_id_param) if user_id_param else None
            task_id = kwargs.get(task_id_param) if task_id_param else None
            session_id = kwargs.get(session_id_param) if session_id_param else None

            # 提取模型参数
            model = None
            if model_param:
                model = kwargs.get(model_param)
            # 如果没有指定 model_param，尝试从 self 获取
            elif args and hasattr(args[0], "model_name"):
                model = args[0].model_name

            # 计算预估 Token 数
            tokens_estimate = estimated_tokens
            if tokens_estimate is None:
                # 尝试从 messages 参数计算
                messages = kwargs.get("messages") or (args[1] if len(args) > 1 else None)
                if messages:
                    if model is None:
                        raise ValueError("计算 Token 需要模型参数，请设置 model_param 或确保方法有 model_name 属性")
                    tokens_estimate = token_counter.count_messages(messages, model)
                else:
                    tokens_estimate = 1000  # 默认预估

            # 检查预算
            await budget_manager.check_budget(
                estimated_tokens=tokens_estimate,
                user_id=user_id,
                task_id=task_id,
                session_id=session_id,
            )

            # 执行原函数
            result = await func(*args, **kwargs)

            # 记录实际使用量
            actual_tokens = tokens_estimate
            # 使用上面提取的 model，或者从结果中获取
            model_name = model if model else "unknown"

            # 尝试从结果中获取实际使用量
            if hasattr(result, "usage"):
                usage = result.usage
                if hasattr(usage, "total_tokens"):
                    actual_tokens = usage.total_tokens
            if hasattr(result, "model"):
                model_name = result.model

            # 记录使用量
            await budget_manager.record_usage(
                tokens=actual_tokens,
                model=model_name,
                user_id=user_id,
                task_id=task_id,
                session_id=session_id,
            )

            return result

        return wrapper

    return decorator


class BudgetContext:
    """
    预算上下文管理器

    用于在代码块中进行预算检查和使用量记录

    Example:
        async with BudgetContext(session_id="xxx") as ctx:
            # 检查预算
            await ctx.check(estimated_tokens=1000)

            # 执行 LLM 调用
            result = await llm.generate(messages)

            # 记录使用量
            await ctx.record(tokens=result.usage.total_tokens, model="gpt-4")
    """

    def __init__(
        self,
        user_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ):
        self.user_id = user_id
        self.task_id = task_id
        self.session_id = session_id
        self._budget_manager = get_budget_manager()

    async def __aenter__(self) -> "BudgetContext":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    async def check(self, estimated_tokens: int) -> bool:
        """检查预算"""
        return await self._budget_manager.check_budget(
            estimated_tokens=estimated_tokens,
            user_id=self.user_id,
            task_id=self.task_id,
            session_id=self.session_id,
        )

    async def record(self, tokens: int, model: str) -> None:
        """记录使用量"""
        await self._budget_manager.record_usage(
            tokens=tokens,
            model=model,
            user_id=self.user_id,
            task_id=self.task_id,
            session_id=self.session_id,
        )

    def get_status(self):
        """获取预算状态"""
        return self._budget_manager.get_budget_status(
            user_id=self.user_id,
            task_id=self.task_id,
            session_id=self.session_id,
        )

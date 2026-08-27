"""
Step 模块异常定义
"""

from typing import Any

from agentos_plugin_sdk.exceptions.base import DomainException


class StepException(DomainException):
    """步骤服务异常基类

    管道具名步骤（capabilities.steps）相关异常的基类。
    """

    pass


class StepNotFoundError(StepException):
    """步骤方法未声明异常

    内核以 ``config["_step_method"]`` 明示调用某步骤，但插件未注册对应
    handler 时抛出——fail-closed：声明缺失即拒绝执行，不静默退回默认
    execute 入口。

    Attributes:
        name: 被调用的步骤名
        declared_steps: 当前已注册的步骤名清单（排序稳定）
    """

    def __init__(
        self,
        name: str,
        declared_steps: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """初始化步骤未声明异常

        Args:
            name: 被调用的步骤名
            declared_steps: 当前已注册的步骤名清单（可选）
            details: 额外的错误详情（可选）
        """
        declared = sorted(declared_steps or [])
        error_details = details.copy() if details else {}
        error_details["step_name"] = name
        error_details["declared_steps"] = declared
        registered = ", ".join(declared) if declared else "无"
        super().__init__(
            f"步骤方法未声明: {name}（已注册步骤: {registered}）",
            code="STEP_NOT_FOUND",
            details=error_details,
        )
        self.name = name
        self.declared_steps = declared

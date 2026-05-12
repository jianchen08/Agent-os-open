"""
执行器工厂模块

提供统一的执行器创建接口，根据目标类型返回对应的执行器实例。
"""

import logging
from typing import Any

from src.orchestration.types import TaskRequest

logger = logging.getLogger(__name__)


class ExecutorFactory:
    """
    执行器工厂类

    根据 target_type 创建对应的执行器实例，支持：
    - agent: Agent 执行器
    - workflow: 工作流执行器
    - tool: 工具执行器
    """

    # 执行器类型映射
    _executors: dict[str, Any] = {}

    @classmethod
    def create_executor(cls, target_type: str) -> Any:
        """
        根据目标类型创建执行器

        Args:
            target_type: 目标类型，支持 "agent" | "workflow" | "tool"

        Returns:
            对应的执行器实例

        Raises:
            ValueError: 当 target_type 不支持时
        """
        target_type = target_type.lower()

        if target_type == "agent":
            return cls._get_agent_executor()
        elif target_type == "workflow":
            return cls._get_workflow_executor()
        elif target_type == "tool":
            return cls._get_tool_executor()
        else:
            raise ValueError(f"不支持的目标类型: {target_type}")

    @classmethod
    def _get_agent_executor(cls) -> Any:
        """
        获取 Agent 执行器实例

        Returns:
            AgentExecutor 实例
        """
        from src.orchestration.agent_executor import AgentExecutor

        if "agent" not in cls._executors:
            cls._executors["agent"] = AgentExecutor()
            logger.debug("创建 Agent 执行器实例")
        return cls._executors["agent"]

    @classmethod
    def _get_workflow_executor(cls) -> Any:
        """
        获取工作流执行器实例

        Returns:
            LangGraphWorkflowExecutor 实例
        """
        from src.workflows.langgraph_executor import LangGraphWorkflowExecutor

        if "workflow" not in cls._executors:
            cls._executors["workflow"] = LangGraphWorkflowExecutor()
            logger.debug("创建工作流执行器实例")
        return cls._executors["workflow"]

    @classmethod
    def _get_tool_executor(cls) -> Any:
        """
        获取工具执行器实例

        Returns:
            ToolExecutor 实例
        """
        from src.tools.executor import ToolExecutor
        from src.tools.registry import ToolRegistry

        if "tool" not in cls._executors:
            registry = ToolRegistry()
            cls._executors["tool"] = ToolExecutor(registry)
            logger.debug("创建工具执行器实例")
        return cls._executors["tool"]

    @classmethod
    def clear_cache(cls) -> None:
        """
        清除执行器缓存

        用于测试或需要重新初始化执行器的场景
        """
        cls._executors.clear()
        logger.debug("执行器缓存已清除")

    @classmethod
    def get_cached_executors(cls) -> dict[str, Any]:
        """
        获取已缓存的执行器

        Returns:
            执行器类型到实例的映射
        """
        return cls._executors.copy()


async def execute_with_factory(task: TaskRequest) -> dict[str, Any]:
    """
    使用执行器工厂执行任务

    根据任务配置中的 target_type 选择对应的执行器执行任务。

    Args:
        task: 任务请求

    Returns:
        执行结果
    """
    target_type = task.config.get("target_type", "agent")

    try:
        executor = ExecutorFactory.create_executor(target_type)

        if target_type == "agent":
            return await executor.execute_task(task)
        elif target_type == "workflow":
            # 工作流执行需要额外参数
            workflow = task.config.get("workflow")
            inputs = task.config.get("inputs", {})
            if workflow is None:
                return {
                    "success": False,
                    "error": "工作流执行缺少 workflow 参数",
                }
            return await executor.execute(workflow, inputs, task.config)
        elif target_type == "tool":
            # 工具执行
            tool_name = task.config.get("tool_name")
            inputs = task.config.get("inputs", {})
            if tool_name is None:
                return {
                    "success": False,
                    "error": "工具执行缺少 tool_name 参数",
                }
            from src.tools.executor import ExecutionContext

            context = ExecutionContext(
                session_id=task.config.get("session_id"),
                agent_id=task.config.get("agent_id"),
            )
            result = await executor.execute(tool_name, inputs, context)
            return {
                "success": result.success,
                "output": result.output,
                "error": result.error,
            }
        else:
            return {
                "success": False,
                "error": f"不支持的目标类型: {target_type}",
            }

    except Exception as e:
        logger.error(f"任务执行失败 | target_type={target_type} | error={e}", exc_info=True)
        return {"success": False, "error": str(e)}

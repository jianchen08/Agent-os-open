"""
工具协调器 - 负责工具的加载、转换和管理

职责：
- 加载和注册工具
- 转换工具格式（为 LangGraph/LangChain 准备）
- 管理工具注册表
- 处理动态工具加载
- 处理 subagent 工具调用（通过 TaskClient）
"""

import logging
from typing import Any

from pydantic import BaseModel, Field, create_model

from src.core.results import ToolExecutionResult
from src.orchestration.task_client import TaskClient
from src.tools.executor import ExecutionContext, ToolExecutor
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolCoordinator:
    """
    工具协调器

    负责工具的加载、转换和管理
    """

    def __init__(
        self,
        tool_ids: list[str],
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        session_id: str,
        user_id: str | None = None,
        task_client: TaskClient | None = None,
    ):
        """
        初始化工具协调器

        Args:
            tool_ids: 工具 ID 列表
            tool_registry: 工具注册表
            tool_executor: 工具执行器
            session_id: 会话 ID
            user_id: 用户 ID
            task_client: 任务客户端，用于处理 subagent 工具调用
        """
        self.tool_ids = tool_ids
        self.tool_registry = tool_registry
        self.tool_executor = tool_executor
        self.session_id = session_id
        self.user_id = user_id
        self.task_client = task_client

    def get_tools_for_graph(self) -> list[Any]:
        """
        获取 LangGraph 可用的工具列表

        支持动态加载：如果工具未注册，会尝试动态加载。

        Returns:
            LangChain 工具列表 (StructuredTool instances)
        """
        if not self.tool_ids:
            logger.debug("[ToolCoordinator] 没有配置工具ID，返回空工具列表")
            return []

        # 触发动态工具加载
        self._ensure_dynamic_tools_loaded()

        # 获取工具定义（只获取已注册的工具）
        tool_defs = []
        missing_tool_ids = []

        for tool_id in self.tool_ids:
            tool = self.tool_registry.get_optional(tool_id)
            if tool is not None:
                llm_format = tool.to_llm_format()
                tool_defs.append(llm_format)

                # 验证工具定义格式
                func_def = llm_format.get("function", {})
                parameters = func_def.get("parameters", {})
                if not parameters:
                    logger.warning(
                        f"工具参数schema为空 | tool_id={tool_id} | tool_name={func_def.get('name')}"
                    )
                else:
                    logger.debug(
                        f"工具定义验证通过 | tool_id={tool_id} | "
                        f"name={func_def.get('name')} | "
                        f"parameters_keys={list(parameters.get('properties', {}).keys())}"
                    )
            else:
                missing_tool_ids.append(tool_id)
                logger.warning(f"工具未注册且无法动态加载 | tool_id={tool_id}")

        if missing_tool_ids:
            logger.warning(
                f"共有 {len(missing_tool_ids)} 个工具未找到: {missing_tool_ids}"
            )

        logger.info(
            f"[ToolCoordinator] 工具加载完成 | "
            f"请求={len(self.tool_ids)} | "
            f"成功={len(tool_defs)} | "
            f"缺失={len(missing_tool_ids)}"
        )

        # 转换为 LangChain 工具格式
        return self._convert_to_langchain_tools(tool_defs)

    # BUG-FIX-fix_20260513_tool_injection_race: 非核心工具动态加载竞态条件
    # 问题根因: _ensure_dynamic_tools_loaded 使用 create_task 异步加载但不等待完成，
    #           导致后续同步获取工具时工具尚未注册
    # 修复方案: 使用 ensure_loaded_sync 同步加载，确保工具在获取前完成注册
    # 影响范围: 所有不在 CORE_SYSTEM_TOOLS 中的工具（playwright_test、list_directory 等）
    # 修复日期: 2026-05-13

    def _ensure_dynamic_tools_loaded(self) -> None:
        """确保动态工具已加载（同步方式，保证加载完成后才返回）"""
        from src.tools.loader import get_dynamic_tool_loader

        loader = get_dynamic_tool_loader()
        if loader is not None:
            try:
                loader.ensure_loaded_sync(self.tool_ids)
            except Exception as e:
                logger.warning(f"动态加载工具失败 | error={e}")

    def _convert_to_langchain_tools(self, tool_defs: list[dict[str, Any]]) -> list[Any]:
        """
        转换工具定义为 LangChain 工具格式

        Args:
            tool_defs: 工具定义列表

        Returns:
            LangChain 工具列表 (StructuredTool instances)
        """
        from langchain_core.tools import StructuredTool

        tools = []
        for tool_def in tool_defs:
            func_def = tool_def.get("function", {})

            # 创建工具执行函数
            tool_name = func_def.get("name", "")
            tool_description = func_def.get("description", "")

            if not tool_name:
                logger.warning("工具定义缺少name字段，跳过")
                continue

            # 创建闭包捕获当前 tool_name
            def make_tool_func(name: str):
                async def tool_func(**kwargs):
                    context = ExecutionContext(
                        session_id=self.session_id,
                        user_id=self.user_id,
                    )
                    result = await self.tool_executor.execute(
                        tool_name=name,
                        inputs=kwargs,
                        context=context,
                    )
                    return result.data if result.success else f"错误: {result.error}"

                return tool_func

            tool_func = make_tool_func(tool_name)

            # 获取参数 schema（字典格式）
            parameters = func_def.get("parameters", {})

            # 验证参数schema
            if not parameters:
                logger.warning(f"工具 {tool_name} 的参数schema为空，使用默认schema")
                parameters = {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }

            # 创建动态 Pydantic 模型
            try:
                args_schema = self._create_pydantic_model_from_schema(
                    tool_name, parameters
                )

                # 创建 StructuredTool
                tool = StructuredTool.from_function(
                    func=tool_func,
                    name=tool_name,
                    description=tool_description,
                    args_schema=args_schema,
                    coroutine=tool_func,
                )
                tools.append(tool)

                logger.debug(
                    f"LangChain工具创建成功 | name={tool_name} | "
                    f"args_schema={args_schema.__name__}"
                )
            except Exception as e:
                logger.error(f"创建工具 {tool_name} 失败 | error={e}")
                continue

        logger.info(f"[ToolCoordinator] LangChain工具创建完成 | count={len(tools)}")
        return tools

    def _create_pydantic_model_from_schema(
        self, tool_name: str, schema: dict[str, Any]
    ) -> type[BaseModel]:
        """
        从 JSON schema 创建动态 Pydantic 模型

        Args:
            tool_name: 工具名称（用于模型命名）
            schema: JSON schema 字典

        Returns:
            Pydantic 模型类
        """
        # 获取属性定义
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # 构建字段字典
        fields = {}
        for field_name, field_def in properties.items():
            field_type = field_def.get("type", "string")
            field_description = field_def.get("description", "")
            field_default = ... if field_name in required else None

            # 简单类型映射（可以根据需要扩展）
            if field_type == "string":
                fields[field_name] = (
                    str,
                    Field(default=field_default, description=field_description),
                )
            elif field_type == "integer" or field_type == "number":
                fields[field_name] = (
                    int,
                    Field(default=field_default, description=field_description),
                )
            elif field_type == "boolean":
                fields[field_name] = (
                    bool,
                    Field(default=field_default, description=field_description),
                )
            elif field_type == "array":
                fields[field_name] = (
                    list,
                    Field(default=field_default, description=field_description),
                )
            elif field_type == "object":
                fields[field_name] = (
                    dict,
                    Field(default=field_default, description=field_description),
                )
            else:
                # 默认使用 Any
                fields[field_name] = (
                    Any,
                    Field(default=field_default, description=field_description),
                )

        # 创建动态模型类
        model_name = f"{tool_name.replace('_', ' ').title().replace(' ', '')}Input"
        return create_model(model_name, **fields)

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext | None = None,
    ) -> ToolExecutionResult:
        """
        执行工具

        对于 subagent 工具，使用 TaskClient 提交任务。
        其他工具通过 tool_executor 执行。

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            context: 执行上下文

        Returns:
            工具执行结果

        Raises:
            ToolExecutionError: 执行失败
        """

        # 构建执行上下文
        if context is None:
            context = ExecutionContext(
                session_id=self.session_id,
                user_id=self.user_id,
            )

        # 处理 subagent 工具
        if tool_name == "subagent":
            return await self._execute_subagent_tool(arguments)

        # 其他工具通过 tool_executor 执行
        return await self.tool_executor.execute(
            tool_name=tool_name,
            inputs=arguments,
            context=context,
        )

    async def _execute_subagent_tool(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        """
        执行 subagent 工具

        使用 TaskClient 提交任务到全局调度器。

        Args:
            arguments: 工具参数，包含：
                - target_type: "agent" 或 "workflow"
                - agent_id: 目标 Agent ID（当 target_type="agent" 时）
                - workflow_id: 目标工作流 ID（当 target_type="workflow" 时）
                - prompt: 执行指令（当 target_type="agent" 时）
                - inputs: 输入参数（当 target_type="workflow" 时）

        Returns:
            工具执行结果
        """
        from src.core.exceptions import ToolExecutionError

        # 检查 TaskClient 是否已初始化
        if self.task_client is None:
            raise ToolExecutionError(
                tool_name="subagent",
                message="TaskClient 未初始化，无法调用 SubAgent",
            )

        target_type = arguments.get("target_type")

        if target_type == "agent":
            # 提交 Agent 任务
            agent_id = arguments.get("agent_id")
            prompt = arguments.get("prompt")
            description = arguments.get("description", f"调用 Agent: {agent_id}")

            if not agent_id:
                raise ToolExecutionError(
                    tool_name="subagent",
                    message="缺少 agent_id 参数",
                )
            if not prompt:
                raise ToolExecutionError(
                    tool_name="subagent",
                    message="缺少 prompt 参数",
                )

            logger.info(
                f"[ToolCoordinator] 提交 Agent 任务 | "
                f"agent_id={agent_id} | "
                f"description={description[:50]}..."
            )

            try:
                result = await self.task_client.submit_agent_task(
                    description=description,
                    prompt=prompt,
                    target_id=agent_id,
                )

                logger.info(
                    f"[ToolCoordinator] Agent 任务执行完成 | "
                    f"agent_id={agent_id}"
                )

                return ToolExecutionResult.create_completed(output=result)

            except Exception as e:
                logger.error(
                    f"[ToolCoordinator] Agent 任务执行失败 | "
                    f"agent_id={agent_id} | "
                    f"error={str(e)}"
                )
                raise ToolExecutionError(
                    tool_name="subagent",
                    message=f"Agent 任务执行失败: {str(e)}",
                    cause=e,
                )

        elif target_type == "workflow":
            # 提交 Workflow 任务
            workflow_id = arguments.get("workflow_id")
            inputs = arguments.get("inputs", {})
            description = arguments.get("description", f"执行工作流: {workflow_id}")

            if not workflow_id:
                raise ToolExecutionError(
                    tool_name="subagent",
                    message="缺少 workflow_id 参数",
                )

            logger.info(
                f"[ToolCoordinator] 提交 Workflow 任务 | "
                f"workflow_id={workflow_id} | "
                f"description={description[:50]}..."
            )

            try:
                # 构建工作流对象（简化处理，实际需要根据 workflow_id 获取）
                workflow = {"id": workflow_id, "name": workflow_id}

                result = await self.task_client.submit_workflow_task(
                    description=description,
                    workflow=workflow,
                    inputs=inputs,
                )

                logger.info(
                    f"[ToolCoordinator] Workflow 任务执行完成 | "
                    f"workflow_id={workflow_id}"
                )

                return ToolExecutionResult.create_completed(output=result)

            except Exception as e:
                logger.error(
                    f"[ToolCoordinator] Workflow 任务执行失败 | "
                    f"workflow_id={workflow_id} | "
                    f"error={str(e)}"
                )
                raise ToolExecutionError(
                    tool_name="subagent",
                    message=f"Workflow 任务执行失败: {str(e)}",
                    cause=e,
                )

        else:
            raise ToolExecutionError(
                tool_name="subagent",
                message=f"未知的 target_type: {target_type}，必须是 'agent' 或 'workflow'",
            )

    def cleanup(self) -> None:
        """清理工具协调器资源"""
        logger.debug("[ToolCoordinator] 工具协调器资源已清理")

"""
Agent 执行器

负责实际执行由全局调度器分配的 Agent 任务
"""

import logging
from typing import Any

from src.agents.loop import AgentLoop
from src.agents.types import AgentConfig
from src.core.results import AgentExecutionResult
from src.orchestration.scheduler import TaskRequest

logger = logging.getLogger(__name__)


class AgentExecutor:
    """
    Agent 执行器

    负责：
    1. 接收调度器分配的任务
    2. 创建并运行 AgentLoop
    3. 返回执行结果
    """

    def __init__(self):
        """初始化执行器"""
        self._running_agents: dict[str, AgentLoop] = {}

    async def execute_task(self, task: TaskRequest) -> dict[str, Any]:
        """
        执行任务

        修改：添加完整的 ExecutionRecord 创建和更新逻辑，确保与前端直接调用一致
        修改：添加 WebSocket 实时推送

        Args:
            task: 任务请求

        Returns:
            执行结果字典，包含 success, output, error, record_id 等字段
        """
        from src.api.websocket.message_bus import SourceType, get_message_bus
        from src.api.websocket.message_factory import MessageFactory
        from src.db.repositories.execution_record_repo import ExecutionRecordRepository
        from src.db.session import get_async_session

        execution_record_id: str | None = None
        record_repo: ExecutionRecordRepository | None = None
        db_session = None
        message_data: dict[str, Any] | None = None
        sequence: int = 0
        parent_record_id: str | None = None
        session_id: str = ""
        agent_config: AgentConfig | None = None
        msg_bus = get_message_bus()

        try:
            # 从任务配置中获取必要信息
            config_dict = task.config.get("agent_config", {})
            tool_registry = task.config.get("tool_registry")
            tool_executor = task.config.get("tool_executor")

            # 获取 parent_record_id 和 session_id（用于执行记录父子关系）
            parent_record_id = task.config.get("parent_record_id")
            session_id = task.config.get("session_id") or task.session_id

            # 验证必要参数
            if not config_dict:
                raise ValueError("任务配置缺少 agent_config")

            # 创建 AgentConfig
            agent_config = AgentConfig(**config_dict)

            # 确保 session_id 不为空
            if not session_id:
                session_id = task.task_id

            # 获取数据库会话并创建 ExecutionRecord
            async for db_session in get_async_session():
                record_repo = ExecutionRecordRepository(db_session)

                # 计算深度：根据 parent_record_id 计算
                from src.utils.id_encoder import parse_nested_id

                if parent_record_id:
                    try:
                        parent_parsed = parse_nested_id(parent_record_id)
                        parent_parsed.get("depth", 0) + 1
                    except Exception:
                        pass
                else:
                    pass

                # 获取序列号并生成嵌套ID
                from src.utils.id_encoder import generate_nested_id
                from src.utils.sequence_manager import get_next_sequence

                sequence = await get_next_sequence(parent_record_id)
                execution_record_id = generate_nested_id(parent_record_id, sequence, "exec")

                # 构建简化的 message_data
                message_data = {
                    "type": "ai",
                    "content": "",  # 初始为空，执行完成后更新
                    "status": "running",
                    "input": {
                        "prompt": task.prompt,
                        "agent_id": task.config.get("target_id"),
                        "agent_level": task.agent_level.value,
                        "task_id": task.task_id,
                    },
                }

                # 创建 ExecutionRecord
                execution_record_id = await record_repo.save_execution_record(
                    session_id=session_id,
                    message_data=message_data,
                    parent_record_id=parent_record_id,
                    record_id=execution_record_id,
                )

                logger.info(
                    f"创建 ExecutionRecord | record_id={execution_record_id} | "
                    f"task_id={task.task_id} | parent={parent_record_id}"
                )
                break  # 获取到会话后退出循环

            # 发送执行开始事件（WebSocket 推送）
            try:
                if msg_bus and execution_record_id:
                    start_msg = MessageFactory.create_message(
                        message_type="execution_start",
                        thread_id=session_id,
                        data={
                            "executionId": execution_record_id,
                            "executionType": "agent",
                            "name": agent_config.name,
                            "parentId": parent_record_id,
                            "input": {"prompt": task.prompt},
                        },
                    )
                    await msg_bus.emit(
                        session_id,
                        start_msg,
                        source_type=SourceType.SYSTEM,
                        source_id="agent_executor",
                    )
                    logger.debug(
                        f"发送 execution_start 事件 | session_id={session_id} | "
                        f"execution_id={execution_record_id}"
                    )
            except Exception as ws_error:
                # WebSocket 推送失败不应影响主流程
                logger.warning(f"发送 execution_start 事件失败 | error={ws_error}")

            # 如果没有提供 tool_registry 或 tool_executor，创建默认实例
            if tool_registry is None:
                logger.warning(
                    f"未提供 tool_registry，创建默认实例 | task_id={task.task_id}"
                )
                from src.tools.registry import ToolRegistry

                tool_registry = ToolRegistry()

            if tool_executor is None:
                logger.warning(
                    f"未提供 tool_executor，创建默认实例 | task_id={task.task_id}"
                )
                from src.tools.executor import ToolExecutor as OriginalToolExecutor

                tool_executor = OriginalToolExecutor(tool_registry)

            # 构建 extra_context，传递 parent_record_id 和 session_id
            extra_context = {
                "task_id": task.task_id,
                "parent_record_id": parent_record_id,
                "session_id": session_id,
            }

            # 使用工厂方法创建 AgentLoop（启用学习和监控，与前端一致）
            from src.agents.builder import create_agent_loop

            agent_loop = await create_agent_loop(
                config=agent_config,
                tool_registry=tool_registry,
                tool_executor=tool_executor,
                session_id=session_id,
                enable_checkpointing=False,  # 禁用检查点以避免序列化问题
                enable_learning=True,  # 启用学习功能（与前端一致）
                enable_monitoring=True,  # 启用监控（与前端一致）
                extra_context=extra_context,  # 传递额外上下文
            )

            # 记录运行中的 Agent
            self._running_agents[task.task_id] = agent_loop

            logger.info(
                f"开始执行 Agent 任务 | task_id={task.task_id} | level={task.agent_level.name} | agent={agent_config.name}"
            )

            # 执行任务
            result: AgentExecutionResult = await agent_loop.run(task.prompt)

            # 更新 ExecutionRecord 为完成状态
            if execution_record_id and record_repo:
                # 构建输出数据（不包含 tool_calls，保持与前端直接调用一致）
                {
                    "result": result.output,
                    "iterations": getattr(result, "iterations", 0),
                }

                # 构建工具调用数据
                tool_calls_data = [
                    tc.model_dump() for tc in getattr(result, "tool_calls", [])
                ]

                # 构建简化的 message_data
                updated_message_data = {
                    "type": "ai",
                    "content": result.output if result.success else "",
                    "status": "completed" if result.success else "failed",
                    "input": message_data["input"],
                }

                # 如果有工具调用，存储在根级别
                if tool_calls_data:
                    updated_message_data["tool_calls"] = tool_calls_data

                # 如果有错误，添加错误信息
                if not result.success and result.error:
                    updated_message_data["error"] = result.error

                await record_repo.update_execution_record(
                    record_id=execution_record_id,
                    message_data=updated_message_data,
                )

                logger.info(
                    f"更新 ExecutionRecord | record_id={execution_record_id} | "
                    f"status={'completed' if result.success else 'failed'}"
                )

            # 发送执行完成事件（WebSocket 推送）
            try:
                if msg_bus and execution_record_id:
                    done_msg = MessageFactory.create_message(
                        message_type="execution_done",
                        thread_id=session_id,
                        data={
                            "executionId": execution_record_id,
                            "success": result.success,
                            "output": {
                                "result": result.output,
                                "iterations": getattr(result, "iterations", 0),
                            },
                            "error": result.error,
                        },
                    )
                    await msg_bus.emit(
                        session_id,
                        done_msg,
                        source_type=SourceType.SYSTEM,
                        source_id="agent_executor",
                    )
                    logger.debug(
                        f"发送 execution_done 事件 | session_id={session_id} | "
                        f"execution_id={execution_record_id} | success={result.success}"
                    )
            except Exception as ws_error:
                # WebSocket 推送失败不应影响主流程
                logger.warning(f"发送 execution_done 事件失败 | error={ws_error}")

            # 返回结果
            return {
                "success": result.success,
                "output": result.output,
                "error": result.error,
                "iterations": getattr(result, "iterations", 0),
                "tool_calls": [
                    tc.model_dump() for tc in getattr(result, "tool_calls", [])
                ],
                "record_id": execution_record_id,  # 返回记录 ID
            }

        except Exception as e:
            logger.error(
                f"Agent 任务执行失败 | task_id={task.task_id} | error={e}",
                exc_info=True,
            )

            # 更新 ExecutionRecord 为失败状态
            if execution_record_id and record_repo:
                try:
                    # 获取现有记录
                    existing_record = await record_repo.get_record_by_id(execution_record_id)
                    if existing_record:
                        # 构建失败的 message_data
                        failed_message_data = existing_record.get("message_data", {})
                        failed_message_data["status"] = "failed"
                        failed_message_data["error"] = str(e)

                        await record_repo.update_execution_record(
                            record_id=execution_record_id,
                            message_data=failed_message_data,
                        )
                        logger.info(
                            f"更新 ExecutionRecord 为失败状态 | record_id={execution_record_id}"
                        )
                except Exception as update_error:
                    logger.error(
                        f"更新 ExecutionRecord 失败状态出错 | record_id={execution_record_id} | error={update_error}"
                    )

            # 发送执行失败事件（WebSocket 推送）
            try:
                if msg_bus:
                    error_msg = MessageFactory.create_message(
                        message_type="execution_error",
                        thread_id=session_id,
                        data={
                            "executionId": execution_record_id,
                            "error": str(e),
                        },
                    )
                    await msg_bus.emit(
                        session_id,
                        error_msg,
                        source_type=SourceType.SYSTEM,
                        source_id="agent_executor",
                    )
                    logger.debug(
                        f"发送 execution_error 事件 | session_id={session_id} | "
                        f"execution_id={execution_record_id}"
                    )
            except Exception as ws_error:
                # WebSocket 推送失败不应影响主流程
                logger.warning(f"发送 execution_error 事件失败 | error={ws_error}")

            return {
                "success": False,
                "output": None,
                "error": str(e),
                "record_id": execution_record_id,
            }
        finally:
            # 清理运行中的 Agent
            if task.task_id in self._running_agents:
                del self._running_agents[task.task_id]

    async def cancel_task(self, task_id: str) -> bool:
        """
        取消任务

        Args:
            task_id: 任务 ID

        Returns:
            是否成功取消
        """
        if task_id in self._running_agents:
            self._running_agents[task_id]
            # 这里可以实现 AgentLoop 的取消逻辑
            # 目前先简单标记
            logger.info(f"取消 Agent 任务 | task_id={task_id}")
            return True
        return False

    def get_running_tasks(self) -> dict[str, str]:
        """
        获取运行中的任务

        Returns:
            任务 ID 到 Agent 名称的映射
        """
        return {
            task_id: agent.config.name
            for task_id, agent in self._running_agents.items()
        }


# 全局执行器实例
_global_executor: AgentExecutor | None = None


def get_global_executor() -> AgentExecutor:
    """
    获取全局执行器实例

    Returns:
        全局执行器实例
    """
    global _global_executor
    if _global_executor is None:
        _global_executor = AgentExecutor()
    return _global_executor

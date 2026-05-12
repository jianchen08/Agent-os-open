"""
WebSocket 端点处理器

负责 WebSocket 连接的生命周期管理和消息处理
"""

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.builtin import load_agent
from src.agents.loop import AgentLoop
from src.agents.task_runner import TaskRunner
from src.api.websocket.message_bus import SourceType, get_message_bus
from src.api.websocket.message_factory import MessageFactory
from src.api.websocket.message_types import MessageTypes
from src.db.connection import get_session_context
from src.db.operation_queue import get_db_operation_queue
from src.tools.executor import ToolExecutor
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class WebSocketEndpointHandler:
    """
    WebSocket 端点处理器

    负责：
    - WebSocket 连接管理
    - Agent 初始化
    - 工具注册
    - 消息接收和分发
    - 错误处理
    """

    def __init__(
        self,
        websocket: WebSocket,
        thread_id: str,
        db: AsyncSession,
        user_id: str = "anonymous",
    ):
        """
        初始化 WebSocket 端点处理器

        Args:
            websocket: WebSocket 连接
            thread_id: 线程 ID
            db: 数据库会话（用于连接初始化，消息处理使用独立会话）
            user_id: 用户 ID（从 JWT Token 中提取）
        """
        self.websocket = websocket
        self.thread_id = thread_id
        self.db = db
        self.user_id = user_id
        self.agent_config = None
        self.agent_loop: AgentLoop | None = None
        self.tool_registry: ToolRegistry | None = None
        self.tool_executor: ToolExecutor | None = None
        self.task_executor: TaskRunner | None = None
        self._pending_tasks: set = set()

        # 数据库操作队列（用于序列化数据库操作）
        self._db_queue = get_db_operation_queue()

    async def handle(self):
        """
        处理 WebSocket 连接的完整生命周期

        包括：
        1. 建立连接
        2. 初始化 Agent
        3. 初始化工具
        4. 消息处理循环
        5. 清理资源
        """
        logger.info(f"[WS-NEW-CODE] handle() 开始 | thread_id={self.thread_id}")
        logger.debug(f"[WS] handle() 开始 | thread_id={self.thread_id}")

        # 1. 建立连接
        logger.debug(f"[WS] 准备调用 _connect() | thread_id={self.thread_id}")
        connect_result = await self._connect()
        logger.debug(
            f"[WS] _connect() 返回 | thread_id={self.thread_id} | result={connect_result}"
        )
        if not connect_result:
            logger.warning(
                f"[WS] _connect() 失败，终止处理 | thread_id={self.thread_id}"
            )
            return

        # 2. 初始化 Agent
        logger.debug(f"[WS] 准备调用 _initialize_agent() | thread_id={self.thread_id}")
        if not await self._initialize_agent():
            return

        # 3. 初始化工具
        logger.debug(f"[WS] 准备调用 _initialize_tools() | thread_id={self.thread_id}")
        if not await self._initialize_tools():
            return

        # 4. 创建 AgentLoop
        logger.debug(f"[WS] 准备调用 _create_agent_loop() | thread_id={self.thread_id}")
        if not await self._create_agent_loop():
            return

        # 5. 发送连接确认
        logger.debug(
            f"[WS] 准备调用 _send_connection_confirmation() | thread_id={self.thread_id}"
        )
        await self._send_connection_confirmation()

        # 6. 消息处理循环
        logger.debug(f"[WS] 准备调用 _message_loop() | thread_id={self.thread_id}")
        await self._message_loop()

    async def _connect(self) -> bool:
        """
        建立 WebSocket 连接（使用 MessageBus）

        Returns:
            是否连接成功
        """
        logger.debug(f"[WS] _connect() 开始 | thread_id={self.thread_id}")
        try:
            logger.debug(f"[WS] 获取 MessageBus | thread_id={self.thread_id}")
            message_bus = get_message_bus()
            logger.debug(
                f"[WS] 调用 register_connection() | thread_id={self.thread_id}"
            )
            result = await message_bus.register_connection(
                self.thread_id, self.websocket, metadata={"user_id": self.user_id}
            )
            logger.debug(
                f"[WS] register_connection() 返回 | "
                f"thread_id={self.thread_id} | success={result.get('success')}"
            )
            if not result.get("success"):
                logger.error(
                    f"[WS] WebSocket 连接建立失败 | thread_id={self.thread_id}"
                )
                return False
            logger.info(f"[WS] WebSocket 连接成功 | thread_id={self.thread_id}")
            return True
        except Exception as e:
            logger.error(
                f"[WS] WebSocket 连接异常 | thread_id={self.thread_id} | error={e}",
                exc_info=True,
            )
            # 如果 accept() 已经调用，才能 close()
            try:
                reason = f"Connection failed: {e}"
                await self.websocket.close(code=1011, reason=reason)
            except RuntimeError:
                # WebSocket 未被 accept，无需关闭
                pass
            return False

    async def _send_connection_confirmation(self):
        """发送连接确认消息（使用 MessageBus）"""
        logger.debug(
            f"[WS] _send_connection_confirmation() 开始 | thread_id={self.thread_id}"
        )
        try:
            message_bus = get_message_bus()
            data = {"user_id": self.user_id, "message": "WebSocket 连接已建立"}
            logger.debug(
                f"[WS] 创建连接确认消息 | thread_id={self.thread_id} | data={data}"
            )
            connection_message = MessageFactory.create_system_message(
                thread_id=self.thread_id,
                message_type=MessageTypes.CONNECTION_ESTABLISHED,
                data=data,
            )
            logger.debug(f"[WS] 调用 MessageBus.emit() | thread_id={self.thread_id}")
            success = await message_bus.emit(
                self.thread_id,
                connection_message,
                source_type=SourceType.SYSTEM,
                source_id="connection",
            )
            logger.debug(
                f"[WS] MessageBus.emit() 返回 | "
                f"thread_id={self.thread_id} | success={success}"
            )
            if not success:
                logger.warning(
                    f"[WS] 连接确认消息发送失败 | thread_id={self.thread_id}"
                )
            else:
                logger.info(f"[WS] 连接确认消息发送成功 | thread_id={self.thread_id}")
        except Exception as e:
            logger.error(
                f"[WS] 发送连接确认异常 | thread_id={self.thread_id} | error={e}",
                exc_info=True,
            )

    async def _initialize_agent(self) -> bool:
        """
        初始化 Agent

        从数据库读取会话绑定的 Agent 配置

        Returns:
            是否初始化成功
        """
        from sqlalchemy import select

        from src.core.constants import DEFAULT_AGENT_NAME
        from src.db.models import AgentConfig, Session

        try:
            # 1. 从数据库查询会话绑定的 agent_id
            # 注意：Session.id 不是唯一字段，必须同时使用 user_id 过滤
            query = select(Session).where(
                Session.id == self.thread_id,
                Session.user_id == self.user_id,
            )
            session_result = await self.db.execute(query)
            session = session_result.scalar_one_or_none()

            agent_config_obj = None

            if session and session.agent_id:
                # 会话绑定了特定的 agent_id，从数据库加载
                agent_query = select(AgentConfig).where(
                    AgentConfig.id == session.agent_id
                )
                agent_result = await self.db.execute(agent_query)
                db_agent = agent_result.scalar_one_or_none()

                if db_agent:
                    # 从数据库 AgentConfig 创建 Agent 配置对象
                    from src.agents.types import AgentConfig as AgentConfigType
                    from src.agents.types import AgentType

                    # 处理 agent_type（从字符串转为枚举）
                    agent_type_enum = AgentType.ATOMIC
                    if db_agent.agent_type:
                        agent_type_str = db_agent.agent_type.lower()
                        if agent_type_str == "main":
                            agent_type_enum = AgentType.MAIN
                        elif agent_type_str == "subagent":
                            agent_type_enum = AgentType.SUBAGENT
                        elif agent_type_str == "specialized":
                            agent_type_enum = AgentType.SPECIALIZED
                        elif agent_type_str == "atomic":
                            agent_type_enum = AgentType.ATOMIC
                        else:
                            try:
                                agent_type_enum = AgentType(agent_type_str)
                            except ValueError:
                                agent_type_enum = AgentType.ATOMIC

                    agent_config_obj = AgentConfigType(
                        name=db_agent.name,
                        description=db_agent.description or "",
                        agent_type=agent_type_enum,
                        model_name=db_agent.model_name,
                        model_params=db_agent.model_params or {},
                        system_prompt=db_agent.system_prompt,
                        tool_ids=db_agent.tool_ids or [],
                        hard_constraints=db_agent.hard_constraints or [],
                        soft_constraints=db_agent.soft_constraints or [],
                        context_variables=db_agent.context_variables or {},
                        static_vars=db_agent.static_vars or {},
                        dynamic_vars=db_agent.dynamic_vars or {},
                        input_schema=db_agent.input_schema or {},
                        output_schema=db_agent.output_schema or {},
                    )
                    logger.info(
                        f"[WS] 使用数据库 Agent | "
                        f"thread_id={self.thread_id} | "
                        f"agent_id={session.agent_id} | "
                        f"agent_name={db_agent.name} | "
                        f"model={db_agent.model_name}"
                    )
                else:
                    logger.warning(
                        f"[WS] 会话绑定的 Agent 不存在 | "
                        f"agent_id={session.agent_id}，使用默认 Agent"
                    )
                    # 回退到默认 Agent
                    agent_config_obj = load_agent(DEFAULT_AGENT_NAME)
            else:
                # 会话未绑定 agent，使用默认 Agent
                agent_config_obj = load_agent(DEFAULT_AGENT_NAME)
                logger.info(
                    f"[WS] 会话未绑定 Agent，使用默认 Agent | "
                    f"thread_id={self.thread_id} | "
                    f"agent_name={DEFAULT_AGENT_NAME}"
                )

            if not agent_config_obj:
                await self.websocket.close(code=1011, reason="Agent配置加载失败")
                return False

            self.agent_config = agent_config_obj
            return True

        except Exception as e:
            logger.error("Agent 加载失败 | error=%s", e, exc_info=True)
            await self.websocket.close(code=1011, reason=f"Agent加载失败: {e}")
            return False

    async def _initialize_tools(self) -> bool:
        """
        初始化工具系统（使用全局注册表）

        Returns:
            是否初始化成功
        """
        try:
            # 使用全局工具注册表（避免每次连接都重新注册）
            from src.tools.global_registry import get_global_tool_registry

            self.tool_registry = await get_global_tool_registry()

            # 创建 TaskRunner 和执行回调
            self.task_executor = TaskRunner()

            # 创建工具执行器
            self.tool_executor = ToolExecutor(registry=self.tool_registry)

            # 注册需要 session 的动态工具
            await self._register_dynamic_tools()

            return True
        except Exception as e:
            logger.error("工具初始化失败 | error=%s", e, exc_info=True)
            await self.websocket.close(code=1011, reason=f"工具初始化失败: {e}")
            return False

    async def _register_dynamic_tools(self):
        """
        注册需要数据库 session 的动态工具
        """
        # 只注册需要 session 的工具（如 task_submit）
        from src.tools.builtin.task_submit import TaskSubmitTool

        task_submit_tool = TaskSubmitTool(session=self.db)

        # 注册到全局注册表（会覆盖之前的实例）
        # 使用 register_with_handler 注册工具定义和处理函数
        tool_def = task_submit_tool.get_tool_definition()
        self.tool_registry.register_with_handler(
            tool=tool_def, handler=task_submit_tool.execute, overwrite=True
        )

        # 验证注册是否成功
        registered = self.tool_registry.get_runnable("task_submit")
        if registered:
            logger.info("[WebSocket] 动态工具注册完成 | task_submit 已成功注册")
        else:
            logger.error(
                "[WebSocket] 动态工具注册失败 | task_submit 注册后仍然无法获取！"
            )

    async def _execute_workflow_inline(
        self, task_id: str, workflow_id: str, goal: dict
    ) -> None:
        """
        直接执行工作流（内联执行，不通过任务队列）

        Args:
            task_id: 任务 ID
            workflow_id: 工作流 ID（如 "resource_generation"）
            goal: 任务目标
        """
        from src.services.workflow_service import WorkflowService

        logger.info(
            f"[WebSocket._execute_workflow_inline] 开始执行工作流 | "
            f"task_id={task_id} | workflow_id={workflow_id}"
        )

        try:
            # 创建 WorkflowService 实例
            workflow_service = WorkflowService(db=self.db)

            # 准备工作流输入
            workflow_inputs = {
                "task_id": task_id,
                "goal": goal,
                "user_id": self.user_id,
                "session_id": self.thread_id,
            }

            # 为 resource_generation 工作流准备 resource_requirement 输入
            if workflow_id == "resource_generation":
                # 从 goal 中提取信息构建 resource_requirement
                workflow_inputs["resource_requirement"] = {
                    "name": goal.get("title", "未知资源"),
                    "description": goal.get("description", ""),
                    "capabilities": [],
                    "context": {
                        "task_id": task_id,
                        "session_id": self.thread_id,
                    },
                }

            # 执行工作流
            result = await workflow_service.execute_workflow(
                workflow_id=workflow_id,
                inputs=workflow_inputs,
                timeout=300,  # 5分钟超时
            )

            # 记录执行结果
            # LangGraphWorkflowExecutor 返回 success 字段，而不是 status
            if result.get("success") is True:
                msg = (
                    f"[WS._execute_workflow_inline] 工作流完成 | "
                    f"task_id={task_id} | workflow_id={workflow_id}"
                )
                logger.info(msg)
            elif result.get("success") is False:
                error_msg = result.get("error", "未知错误")
                log_msg = (
                    f"[WS._execute_workflow_inline] 工作流失败 | "
                    f"task_id={task_id} | workflow_id={workflow_id} | "
                    f"error={error_msg}"
                )
                logger.error(log_msg)
            else:
                success_val = result.get("success")
                warn_msg = (
                    f"[WS._execute_workflow_inline] 工作流未完成 | "
                    f"task_id={task_id} | workflow_id={workflow_id} | "
                    f"success={success_val}"
                )
                logger.warning(warn_msg)

            # 返回执行结果
            return result

        except Exception as e:
            exc_msg = (
                f"[WS._execute_workflow_inline] 工作流执行异常 | "
                f"task_id={task_id} | workflow_id={workflow_id} | error={str(e)}"
            )
            logger.exception(exc_msg)
            # 返回失败结果
            return {
                "success": False,
                "error": str(e),
                "task_submitted": False,
            }

    async def _create_agent_loop(self) -> bool:
        """
        创建 AgentLoop 实例

        Returns:
            是否创建成功
        """
        try:
            self.agent_loop = AgentLoop(
                config=self.agent_config,
                tool_registry=self.tool_registry,
                tool_executor=self.tool_executor,
                user_id=self.user_id,
                session_id=self.thread_id,
                db_session=self.db,
                enable_learning=True,
                enable_monitoring=True,
                enable_checkpointing=False,
            )

            # 注册到 AgentLoopRegistry，以便在消息删除时能够重置图
            from src.agents.registry import get_agent_loop_registry
            registry = get_agent_loop_registry()
            registry.register(self.thread_id, self.agent_loop)
            logger.debug(f"[WebSocket] AgentLoop 已注册到 registry | thread_id={self.thread_id}")

            return True
        except Exception as e:
            logger.error("AgentLoop 创建失败 | error=%s", e, exc_info=True)
            message_bus = get_message_bus()
            error_msg = MessageFactory.create_error_message(
                thread_id=self.thread_id,
                error_code="AGENT_LOOP_INIT_FAILED",
                message=f"Agent 初始化失败：{str(e)}",
            )
            await message_bus.emit(
                self.thread_id,
                error_msg,
                source_type=SourceType.SYSTEM,
                source_id="agent_loop",
            )
            await self.websocket.close(code=1011, reason="AgentLoop 初始化失败")
            return False

    async def _message_loop(self):
        """
        消息处理循环

        持续接收消息并分发给处理器（串行接收，并行处理，支持多 Agent 并发）
        """

        try:
            logger.info("进入消息接收循环 | thread_id=%s", self.thread_id)

            # 使用队列和任务来支持并发处理
            message_queue = asyncio.Queue()
            processing_tasks: set[asyncio.Task] = set()

            # 启动消息接收任务（在后台持续接收消息）
            receive_task = asyncio.create_task(
                self._message_receiver(message_queue),
                name=f"message_receiver_{self.thread_id}"
            )

            # 启动消息处理任务（并发处理队列中的消息）
            processor_task = asyncio.create_task(
                self._message_processor(message_queue, processing_tasks),
                name=f"message_processor_{self.thread_id}"
            )

            # 等待任一任务完成（通常是接收任务在连接断开时完成）
            done, pending = await asyncio.wait(
                [receive_task, processor_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            # 取消剩余的任务
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # 等待所有处理任务完成
            if processing_tasks:
                await asyncio.gather(*processing_tasks, return_exceptions=True)

        except WebSocketDisconnect:
            await self._handle_disconnect()
        except RuntimeError as e:
            await self._handle_runtime_error(e)
        except Exception as e:
            await self._handle_generic_error(e)

    async def _message_receiver(self, queue: asyncio.Queue):
        """
        消息接收器 - 在后台持续接收消息并放入队列

        Args:
            queue: 消息队列
        """
        try:
            while True:
                if not await self._check_connection():
                    break

                # 接收消息
                data = await self.websocket.receive_json()
                # 验证接收到的数据类型
                if not isinstance(data, dict):
                    logger.error(f"[WebSocket] 接收到的消息不是字典类型 | type={type(data).__name__} | data={data!r} | thread_id={self.thread_id}")
                    continue

                # 记录接收到的消息（使用INFO级别确保可见）
                message_type = data.get('type', 'unknown')
                logger.info(f"[WebSocket] 收到消息 | type={message_type} | thread_id={self.thread_id} | data_keys={list(data.keys())}")
                logger.debug(f"[WebSocket] 消息详情 | data={data}")

                await queue.put(data)
                logger.info(f"[WebSocket] 消息已入队 | type={message_type} | thread_id={self.thread_id}")

        except WebSocketDisconnect:
            logger.info(f"[WebSocket] 接收器断开连接 | thread_id={self.thread_id}")
        except RuntimeError as e:
            # 处理连接未就绪的情况（如 "Need to call accept first"）
            error_msg = str(e).lower()
            if "not connected" in error_msg or "accept" in error_msg:
                logger.warning(
                    f"[WebSocket] 连接未就绪，接收器退出 | thread_id={self.thread_id} | error={e}"
                )
            else:
                logger.error(f"[WebSocket] 接收器运行时异常 | thread_id={self.thread_id} | error={e}", exc_info=True)
        except Exception as e:
            logger.error(f"[WebSocket] 接收器异常 | thread_id={self.thread_id} | error={e}", exc_info=True)

    async def _message_processor(self, queue: asyncio.Queue, processing_tasks: set):
        """
        消息处理器 - 并发处理队列中的消息

        Args:
            queue: 消息队列
            processing_tasks: 正在处理的任务集合
        """
        try:
            while True:
                # 从队列获取消息（带超时，以便检查是否需要退出）
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    # 检查连接是否仍然有效
                    if self.websocket.client_state.name != "CONNECTED":
                        logger.info(f"[WebSocket] 处理器检测到连接断开，退出 | thread_id={self.thread_id}")
                        break
                    # 连接仍然有效，继续等待消息
                    continue

                # 检查是否是退出信号
                if data is None:
                    logger.info(f"[WebSocket] 处理器收到退出信号 | thread_id={self.thread_id}")
                    break

                # 创建任务并发处理消息
                task = asyncio.create_task(
                    self._process_single_message(data),
                    name=f"msg_processor_{self.thread_id}_{len(processing_tasks)}"
                )
                processing_tasks.add(task)

                # 添加完成回调以清理任务
                def on_task_done(t):
                    processing_tasks.discard(t)
                    try:
                        exc = t.exception()
                        if exc:
                            logger.error(f"[WebSocket] 消息处理异常 | error={exc}")
                    except asyncio.CancelledError:
                        pass

                task.add_done_callback(on_task_done)

                # 记录并发统计（只在有实际并发时记录）
                active_count = len(processing_tasks)
                if active_count > 1:
                    logger.info(
                        f"[WebSocket] 并发处理消息 | "
                        f"thread_id={self.thread_id} | "
                        f"active_tasks={active_count}"
                    )

        except Exception as e:
            logger.error(f"[WebSocket] 处理器异常 | thread_id={self.thread_id} | error={e}")

    async def _process_single_message(self, data: dict):
        """
        处理单条消息

        使用数据库操作队列序列化数据库操作，避免并发事务冲突

        Args:
            data: 消息数据
        """
        from src.api.websocket.dispatcher import message_dispatcher

        # 验证消息数据类型
        if not isinstance(data, dict):
            logger.error(f"[WebSocket] 消息格式错误：期望字典类型，实际收到 {type(data).__name__} | thread_id={self.thread_id}")
            await self._send_error_message(f"Invalid message format: expected object, received {type(data).__name__}")
            return None

        message_type = data.get("type")
        logger.info(f"[WebSocket] 开始处理消息 | type={message_type} | thread_id={self.thread_id} | data={data}")

        try:
            # 启动数据库操作队列
            await self._db_queue.start()

            # 为每个消息创建独立的数据库会话
            async with get_session_context() as db_session:
                ctx = self._create_handler_context_with_db(db_session)
                await message_dispatcher.dispatch(ctx, data)
                logger.debug(f"[WebSocket] 消息处理完成 | type={message_type} | thread_id={self.thread_id}")
        except Exception as e:
            logger.error(f"[WebSocket] 消息处理异常 | type={message_type} | error={e}")
            # 发送错误消息到前端
            await self._send_error_message(str(e))

    def _create_handler_context_with_db(self, db_session: AsyncSession):
        """
        创建处理器上下文（使用指定的数据库会话）

        Args:
            db_session: 数据库会话

        Returns:
            HandlerContext 实例
        """
        from src.api.websocket.handlers.base import HandlerContext

        return HandlerContext(
            websocket=self.websocket,
            thread_id=self.thread_id,
            user_id=self.user_id,
            db=db_session,
            agent_loop=self.agent_loop,
            agent_config=self.agent_config,
        )

    async def _send_error_message(self, error_message: str):
        """
        发送错误消息到前端

        Args:
            error_message: 错误消息
        """
        try:
            message_bus = get_message_bus()
            error_msg = MessageFactory.create_error_message(
                thread_id=self.thread_id,
                error_code="MESSAGE_PROCESSING_ERROR",
                message=error_message,
            )
            await message_bus.emit(
                self.thread_id,
                error_msg,
                source_type=SourceType.SYSTEM,
                source_id="message_handler",
            )
        except Exception as e:
            logger.error(f"[WebSocket] 发送错误消息失败 | error={e}")

    async def _check_connection(self) -> bool:
        """
        检查 WebSocket 连接状态

        Returns:
            连接是否有效
        """
        from starlette.websockets import WebSocketState

        if self.websocket.client_state != WebSocketState.CONNECTED:
            logger.warning(
                "WebSocket 连接已断开，退出消息循环 | thread_id=%s | state=%s",
                self.thread_id,
                self.websocket.client_state,
            )
            return False
        return True

    async def _receive_and_dispatch_message(self):
        """
        接收并分发单条消息
        """
        from src.api.websocket.dispatcher import message_dispatcher

        data = await self.websocket.receive_json()
        message_type = data.get("type")
        logger.info("收到消息 | type=%s", message_type)

        try:
            ctx = self._create_handler_context()
            await message_dispatcher.dispatch(ctx, data)
        except Exception as e:
            await self._handle_message_error(e)

    def _create_handler_context(self):
        """
        创建处理器上下文

        Returns:
            HandlerContext 实例
        """
        from src.api.websocket.handlers.base import HandlerContext

        return HandlerContext(
            websocket=self.websocket,
            thread_id=self.thread_id,
            user_id=self.user_id,
            db=self.db,
            agent_loop=self.agent_loop,
            agent_config=self.agent_config,
        )

    async def _handle_message_error(self, error: Exception):
        """
        处理消息处理错误（使用 MessageBus）

        Args:
            error: 错误对象
        """
        logger.error("消息处理失败 | error=%s", error, exc_info=True)
        message_bus = get_message_bus()
        error_msg = MessageFactory.create_error_message(
            thread_id=self.thread_id,
            error_code="MESSAGE_PROCESSING_ERROR",
            message=f"消息处理失败：{str(error)}",
        )
        await message_bus.emit(
            self.thread_id,
            error_msg,
            source_type=SourceType.SYSTEM,
            source_id="message_handler",
        )

    async def _handle_disconnect(self):
        """处理 WebSocket 断开连接（使用 MessageBus）"""
        message_bus = get_message_bus()
        await message_bus.unregister_connection(self.thread_id, self.websocket)
        logger.info("WebSocket 断开连接 | thread_id=%s", self.thread_id)

    async def _handle_runtime_error(self, error: RuntimeError):
        """
        处理运行时错误（使用 MessageBus）

        Args:
            error: 运行时错误
        """
        if "not connected" in str(error).lower() or "accept" in str(error).lower():
            logger.warning(
                "WebSocket 连接异常 | thread_id=%s | error=%s",
                self.thread_id,
                error,
            )
        else:
            logger.exception(
                "WebSocket 运行时错误 | thread_id=%s | error=%s",
                self.thread_id,
                error,
            )
        message_bus = get_message_bus()
        await message_bus.unregister_connection(self.thread_id, self.websocket)

    async def _handle_generic_error(self, error: Exception):
        """
        处理通用错误（使用 MessageBus）

        Args:
            error: 错误对象
        """
        logger.exception(
            "WebSocket 错误 | thread_id=%s | error=%s", self.thread_id, error
        )
        message_bus = get_message_bus()
        await message_bus.unregister_connection(self.thread_id, self.websocket)

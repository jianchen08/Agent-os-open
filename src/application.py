"""应用服务容器 — 统一管理服务构建和引擎创建。

将分散在 start_server.py 中的服务构建、引擎创建、工具注册等逻辑
集中到 Application 类中，提供统一的服务获取入口。

用法::

    app = Application(project_root=Path("/path/to/project"))
    services = app.build_services(agent_registry=registry)
    engine = app.create_pipeline_engine(config, plugin_registry)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class Application:
    """应用服务容器。

    集中管理服务实例的创建、工具注册、PipelineEngine 和 TaskWorker 的构建。
    替代 start_server.py 中分散的 _build_services 和直接实例化逻辑。

    Attributes:
        project_root: 项目根目录路径
        services: 共享服务字典
    """

    def __init__(self, project_root: Path | None = None) -> None:
        """初始化应用容器。

        Args:
            project_root: 项目根目录，默认为当前工作目录
        """
        self.project_root: Path = project_root or Path.cwd()
        self.services: dict[str, Any] = {}

    def build_services(self, agent_registry: Any | None = None) -> dict[str, Any]:
        """构建共享服务字典。

        创建工具注册表、记忆存储等共享服务，
        插件通过 ctx.get_service() 自主获取。
        所有服务注册到 ServiceProvider（统一服务注入），不再写入 sys._agent_os_*。

        Args:
            agent_registry: Agent 注册表实例（可选）

        Returns:
            服务名称到实例的映射字典
        """
        services: dict[str, Any] = {}

        # 1. ToolRegistry — 工具注册表
        try:
            from tools.registry import ToolRegistry

            tool_registry = ToolRegistry()
            self._register_basic_tools(tool_registry)
            services["tool_registry"] = tool_registry
            logger.info(
                "服务已创建: tool_registry (%d 个基础工具)", tool_registry.count()
            )

            from tools.auto_loader import init_tool_auto_loader

            init_tool_auto_loader(tool_registry)
            logger.info("ToolAutoLoader 已初始化")
        except Exception as exc:
            logger.warning("创建 tool_registry 服务失败: %s", exc)

        # 2. JsonMemoryStore — 记忆存储
        json_store = None
        try:
            from memory.storage.json_store import JsonMemoryStore

            json_store = JsonMemoryStore()
            logger.info("服务已创建: JsonMemoryStore")
        except Exception as exc:
            logger.warning("创建 JsonMemoryStore 失败: %s", exc)

        if json_store is not None:
            services["memory_store"] = json_store
            services["semantic_storage"] = json_store

        # 3. MessageQueue — 管道间消息传递
        try:
            from infrastructure.message_queue import MessageQueue

            services["message_queue"] = MessageQueue()
            logger.info("服务已创建: message_queue")
        except Exception as exc:
            logger.warning("创建 message_queue 服务失败: %s", exc)

        # 4. ExecutionRecordStorage — 执行记录持久化
        try:
            from infrastructure.execution_record_storage import (
                ExecutionRecordStorage,
            )

            services["execution_record_storage"] = ExecutionRecordStorage(
                data_dir=str(self.project_root / "data" / "pipelines")
            )
            logger.info("服务已创建: execution_record_storage")
        except Exception as exc:
            logger.warning("创建 execution_record_storage 服务失败: %s", exc)

        # 5. EventBus — 事件总线
        try:
            from pipeline.event_bus import EventBus

            event_bus = EventBus()
            services["event_bus"] = event_bus
        except Exception as exc:
            logger.warning("创建 event_bus 服务失败: %s", exc)

        # 6. TaskService — 任务服务
        try:
            from tasks.service import TaskService

            task_service = TaskService()
            services["task_service"] = task_service
            logger.info("服务已创建: task_service")
        except Exception as exc:
            logger.warning("创建 task_service 服务失败: %s", exc)

        # 7. AgentRegistry — 供 TaskWorker 加载子 agent 配置
        if agent_registry is not None:
            services["agent_registry"] = agent_registry
            logger.info("服务已注入: agent_registry")

        # 8. PipelineCheckpointManager — 管道检查点
        try:
            from infrastructure.checkpoint.pipeline_checkpoint import (
                PipelineCheckpointManager,
            )

            services["checkpoint_manager"] = PipelineCheckpointManager()
            logger.info("服务已创建: checkpoint_manager")
        except Exception as exc:
            logger.warning("创建 checkpoint_manager 服务失败: %s", exc)

        # 9. ChannelGateway — 多渠道消息网关
        gateway = self.create_gateway()
        if gateway is not None:
            gateway.services = services
            services["channel_gateway"] = gateway
            logger.info("服务已创建: channel_gateway")

        # 统一注册到 ServiceProvider（替代 sys._agent_os_* 全局变量）
        self._register_to_service_provider(services)

        self.services = services
        return services

    @staticmethod
    def _register_to_service_provider(services: dict[str, Any]) -> None:
        """将服务字典注册到 ServiceProvider 单例。

        ServiceProvider 作为统一的服务注册中心，
        替代分散在 sys._agent_os_* 的全局变量模式。
        注册为幂等操作，已存在的服务不会被覆盖。

        Args:
            services: 服务名称到实例的映射字典
        """
        try:
            from infrastructure.service_provider import get_service_provider

            provider = get_service_provider()
            provider.register_services(services)
        except Exception as exc:
            logger.warning("注册服务到 ServiceProvider 失败: %s", exc)

    def create_gateway(self) -> Any | None:
        """创建 ChannelGateway 实例。

        Returns:
            ChannelGateway 实例，创建失败返回 None
        """
        try:
            from channels.gateway.channel_gateway import ChannelGateway

            gateway = ChannelGateway()
            logger.info("ChannelGateway 通过 Application 创建完成")
            return gateway
        except Exception as exc:
            logger.warning("创建 ChannelGateway 失败: %s", exc)
            return None

    def _register_basic_tools(self, registry: Any) -> None:
        """注册基础工具（无需依赖注入）。

        Args:
            registry: ToolRegistry 实例
        """
        import datetime
        import math as _math

        from tools.types import Tool, ToolSource

        # current_time
        def current_time(params: dict[str, Any]) -> str:
            """获取当前时间。"""
            return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            tool = Tool(
                name="current_time",
                description="获取当前日期和时间",
                source=ToolSource.BUILTIN,
                input_schema={
                    "type": "object",
                    "properties": {
                        "timezone": {
                            "type": "string",
                            "description": "时区（默认本地）",
                        },
                    },
                },
            )
            registry.register_with_handler(tool=tool, handler=current_time)
        except Exception as exc:
            logger.warning("注册基础工具 current_time 失败: %s", exc)

        # calculator
        def calculator(params: dict[str, Any]) -> str:
            """执行简单数学计算。"""
            expression = params.get("expression", "")
            if not expression:
                return "错误：未提供计算表达式"
            try:
                allowed_names = {
                    "abs": abs,
                    "round": round,
                    "min": min,
                    "max": max,
                    "pow": pow,
                    "sum": sum,
                    "pi": _math.pi,
                    "e": _math.e,
                    "sqrt": _math.sqrt,
                    "ceil": _math.ceil,
                    "floor": _math.floor,
                }
                result = eval(
                    expression, {"__builtins__": {}}, allowed_names
                )  # noqa: S307
                return str(result)
            except Exception as exc:
                return f"计算错误：{exc}"

        try:
            tool = Tool(
                name="calculator",
                description="执行简单数学计算，支持加减乘除和常用数学函数",
                source=ToolSource.BUILTIN,
                input_schema={
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "数学表达式，如 '123+456' 或 'sqrt(144)'",
                        },
                    },
                    "required": ["expression"],
                },
            )
            registry.register_with_handler(tool=tool, handler=calculator)
        except Exception as exc:
            logger.warning("注册基础工具 calculator 失败: %s", exc)

    def create_pipeline_engine(
        self,
        pipeline_config: Any,
        plugin_registry: Any,
        services: dict[str, Any] | None = None,
    ) -> Any:
        """创建 PipelineEngine 实例。

        Args:
            pipeline_config: 管道配置对象
            plugin_registry: 插件注册表
            services: 共享服务字典（默认使用 self.services）

        Returns:
            PipelineEngine 实例
        """
        from pipeline.engine import PipelineEngine

        svc = services or self.services
        checkpoint_mgr = svc.get("checkpoint_manager")
        engine = PipelineEngine(
            input_route_table=pipeline_config.input_route_table,
            output_route_table=pipeline_config.output_route_table,
            plugin_registry=plugin_registry,
            services=svc,
            checkpoint_manager=checkpoint_mgr,
        )
        logger.info("PipelineEngine 通过 Application 创建完成")
        return engine

    def create_task_worker(
        self,
        pipeline_config: Any,
        plugin_registry: Any,
        services: dict[str, Any] | None = None,
    ) -> Any | None:
        """创建 TaskWorker 实例。

        Args:
            pipeline_config: 管道配置对象
            plugin_registry: 插件注册表
            services: 共享服务字典（默认使用 self.services）

        Returns:
            TaskWorker 实例，创建失败返回 None
        """
        from infrastructure.task_worker import TaskWorker

        svc = services or self.services
        event_bus = svc.get("event_bus")
        task_service = svc.get("task_service")

        if not event_bus or not task_service:
            logger.warning("缺少 event_bus 或 task_service，TaskWorker 未初始化")
            return None

        task_worker = TaskWorker(
            task_service=task_service,
            plugin_registry=plugin_registry,
            input_route_table=pipeline_config.input_route_table,
            output_route_table=pipeline_config.output_route_table,
            services=svc,
            event_bus=event_bus,
        )
        logger.info("TaskWorker 通过 Application 创建完成")
        return task_worker

    def create_pipeline_factory(
        self,
        pipeline_config: Any,
        plugin_registry: Any,
    ) -> Callable[[], Any]:
        """创建 PipelineEngine 工厂函数。

        每次调用返回新的 PipelineEngine。
        工厂创建的引擎不传递 checkpoint_manager（用于 eval 场景）。
        工厂函数同时注册到 ServiceProvider。

        Args:
            pipeline_config: 管道配置对象
            plugin_registry: 插件注册表

        Returns:
            无参数工厂函数，每次调用返回新的 PipelineEngine 实例
        """
        from pipeline.engine import PipelineEngine

        def factory() -> Any:
            return PipelineEngine(
                input_route_table=pipeline_config.input_route_table,
                output_route_table=pipeline_config.output_route_table,
                plugin_registry=plugin_registry,
                services=self.services,
            )

        # 注册到 ServiceProvider，替代 sys._agent_os_pipeline_factory
        try:
            from infrastructure.service_provider import get_service_provider

            provider = get_service_provider()
            provider.register_services({"pipeline_factory": factory})
        except Exception as exc:
            logger.warning("注册 pipeline_factory 到 ServiceProvider 失败: %s", exc)

        return factory

    def get_service(self, name: str, *, default: Any = None) -> Any | None:
        """获取已注册的服务实例。

        Args:
            name: 服务名称
            default: 未找到时的默认返回值，默认 None

        Returns:
            服务实例，未找到返回 default
        """
        return self.services.get(name, default)

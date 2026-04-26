"""CLI 入口模块（Claude Code 风格交互体验）。

提供命令行交互式管道应用，包含：
- CLIApplication: Claude Code 风格 CLI 应用主类

特性：
- 斜杠命令系统（/help, /compact, /clear, /model 等）
- 底部状态栏（Agent 名称、模型、轮次、上下文占用）
- 多行输入（\\ 续行）
- 工具调用可视化
- 交互模式切换（Normal/Auto/Plan）
- 行内快捷语法（@path, !cmd, #memo）

启动方式::

    # 默认启动
    python -m channels.cli.cli_main

    # 指定管道配置
    python -m channels.cli.cli_main --config path/to/pipeline.yaml

    # 通过入口点
    agent-os
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys as _sys
import uuid as _uuid
from pathlib import Path
from typing import Any

from channels.cli.cli_commands import CommandResult, SlashCommandRegistry
from channels.cli.input_adapter import CLIInputAdapter
from channels.cli.output_adapter import CLIOutputAdapter
from pipeline.engine import PipelineEngine
from pipeline.registry import PluginRegistry
from pipeline.route import (
    InputRouteTable,
    OutputRouteTable,
)

logger = logging.getLogger(__name__)

_LOGGING_INITIALIZED = False

def setup_logging(
    debug: bool = False,
    log_dir: Path | str | None = None,
) -> None:
    """初始化统一日志系统（终端 + 文件）。

    在所有入口点调用一次即可。重复调用不会重复初始化。

    Args:
        debug: 是否启用 DEBUG 级别（终端也会显示管道内部日志）
        log_dir: 日志目录路径，默认为项目根目录下的 logs/
    """
    global _LOGGING_INITIALIZED
    if _LOGGING_INITIALIZED:
        return
    _LOGGING_INITIALIZED = True

    if log_dir is None:
        log_dir = _PROJECT_ROOT / "logs"
    else:
        log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_level = logging.DEBUG if debug else logging.INFO
    log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt="%H:%M:%S",
    )

    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        filename=log_dir / "agent_os.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(file_handler)

    logger.info(
        "Logging initialized: console_level=%s, file=agent_os.log (DEBUG)",
        "DEBUG" if debug else "INFO",
    )

    if not debug:
        _console_handler = logging.getLogger().handlers[0]
        _console_handler.addFilter(
            lambda record: not any(
                record.name.startswith(_ns)
                for _ns in (
                    "pipeline.chain", "pipeline.engine", "pipeline.config",
                    "pipeline.registry", "httpcore", "httpx", "LiteLLM",
                    "LiteLLM.proxy", "LiteLLM.router", "LiteLLM.litellm_logging",
                    "LiteLLM.http_handler",
                    "infrastructure", "tools.builtin", "tools.global_registry",
                    "plugins.core", "plugins.input", "plugins.output",
                    "evaluation", "tasks", "memory",
                    "__main__",
                )
            ) or record.levelno >= logging.WARNING
        )

# 默认管道配置路径（相对于包目录）
# 默认管道配置路径 -- 优先项目根目录的 config/，回退到 src/config/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_PIPELINE_CONFIG = _PROJECT_ROOT / "config" / "pipelines" / "default.yaml"

# Maximum number of messages retained in a persisted session
MAX_SESSION_MESSAGES = 100


def _get_session_dir() -> Path:
    """Get the directory used to store CLI session metadata."""
    session_dir = Path("data/session")
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _load_session_id(session_dir: Path) -> str | None:
    """Load the previous session ID from disk.

    Returns:
        The stored session ID string, or None if not found.
    """
    id_file = session_dir / ".current_session_id"
    if id_file.exists():
        try:
            return id_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return None


def _save_session_id(session_dir: Path, session_id: str) -> None:
    """Persist the current session ID to disk.

    Args:
        session_dir: Path to the session metadata directory.
        session_id: The session identifier to store.
    """
    id_file = session_dir / ".current_session_id"
    id_file.write_text(session_id, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI Application（Claude Code 风格）
# ---------------------------------------------------------------------------


class CLIApplication:
    """CLI 交互式管道应用（Claude Code 风格）。

    组装输入适配器、输出适配器、管道引擎及路由表，
    提供增强的交互式命令行循环。

    从 YAML 配置加载 LLMCore + ToolCore + Output 插件。

    交互模式：
    - Normal（默认）：正常对话，工具调用需确认
    - Auto：自动执行，不需确认
    - Plan：只读模式，只规划不执行

    Example::

        app = CLIApplication()
        app.setup_pipeline()
        asyncio.run(app.run())
    """

    def __init__(self, streaming: bool = True) -> None:
        """初始化 CLI 应用，创建各核心组件实例。

        Args:
            streaming: 是否启用流式输出，默认 True。
        """
        # 斜杠命令注册表
        self._command_registry = SlashCommandRegistry()

        # 输入/输出适配器
        self._input_adapter = CLIInputAdapter(
            prompt_str="> ",
            command_registry=self._command_registry,
        )
        self._output_adapter = CLIOutputAdapter()

        # 管道引擎
        self._engine: PipelineEngine | None = None
        self._plugin_registry = PluginRegistry()
        self._input_route_table = InputRouteTable()
        self._output_route_table = OutputRouteTable()
        self._streaming = streaming
        self._agent_config: Any | None = None
        self._services: dict[str, Any] = {}

        # 交互状态
        self._interaction_mode: str = "normal"  # normal / auto / plan
        self._show_thinking: bool = False
        self._turn_count: int = 0

        # 事件总线（用于接收子任务完成通知）
        from pipeline.event_bus import EventBus
        self._event_bus = EventBus()
        _sys._agent_os_event_bus = self._event_bus

    def setup_pipeline(self, config_path: str | Path | None = None) -> None:
        """设置真实管道配置（从 YAML 加载 LLMCore + ToolCore + Output 插件）。

        启动流程（Agent 只注入参数，插件自主读取）：
        1. 加载 YAML → build_plugin_registry() 实例化插件
        2. 创建共享服务（ToolRegistry、JsonMemoryStore）→ 注入 PipelineEngine
        3. 加载 Agent 配置 → 参数写入 state
        4. 插件运行时从 ctx.state / ctx.get_service() 自主获取

        Args:
            config_path: 管道配置 YAML 文件路径。
                默认使用 ``config/pipelines/default.yaml``。
        """
        from config.models import ModelConfigLoader
        from pipeline.config import build_plugin_registry, load_pipeline_config

        # 确定配置路径
        if config_path is None:
            config_path = _DEFAULT_PIPELINE_CONFIG

        config_path = Path(config_path)
        if not config_path.exists():
            # 回退到 src/ 下的 config/pipelines/
            project_root = Path(__file__).resolve().parent.parent.parent / "config" / "pipelines" / "default.yaml"
            if project_root.exists():
                config_path = project_root
            else:
                logger.error("Pipeline config not found at %s", config_path)
                raise FileNotFoundError(f"Pipeline config not found: {config_path}")

        logger.info("Loading pipeline config from: %s", config_path)

        # 创建 ModelConfigLoader 用于环境变量回退
        model_loader = ModelConfigLoader()

        # 加载管道配置
        try:
            pipeline_config = load_pipeline_config(config_path, model_loader=model_loader)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("Failed to load pipeline config: %s", exc)
            raise

        # 构建插件注册表（传入 model_loader 以支持从 llm.yaml defaults 读取默认模型）
        self._plugin_registry = build_plugin_registry(pipeline_config, model_loader=model_loader)

        # 使用配置中的路由表
        self._input_route_table = pipeline_config.input_route_table
        self._output_route_table = pipeline_config.output_route_table

        # 加载 Agent 配置（默认灵汐 lingxi）— 直接从 AgentRegistry 加载
        from agents.registry import AgentRegistry
        agent_registry = AgentRegistry()
        agent_config_dir = _PROJECT_ROOT / "config" / "agents"
        if agent_config_dir.exists():
            agent_registry.load_directory(agent_config_dir)

        # 创建共享服务 → 注入 PipelineEngine（agent_registry 需要在 _build_services 前创建）
        self._services = self._build_services(agent_registry=agent_registry)

        # 如果 ToolCore 存在，从 ToolRegistry 注册工具
        tool_core = self._plugin_registry.get_core("tool_execute")
        if tool_core is not None:
            tool_registry = self._services.get("tool_registry")
            if tool_registry is not None:
                try:
                    from tools.builtin import register_core_tools
                    registered = register_core_tools(tool_registry, session=None)
                    logger.info("ToolCore registered %d core tools", len(registered))
                except Exception as exc:
                    logger.warning("register_core_tools failed: %s", exc)
                tool_core.register_tools_from_registry(tool_registry)

            # BUG-FIX-fix_20260419_isolation_executor_not_injected:
            # 初始化隔离执行器并注入到 ToolCore，使工具能够在 Docker 容器中执行
            # 之前 IsolationExecutor 从未被实例化，导致 IsolationGuard 的容器隔离决策无法生效
            try:
                from isolation.executor import IsolationExecutor
                isolation_executor = IsolationExecutor()
                tool_core.set_isolation_executor(isolation_executor)
                logger.info("IsolationExecutor 已注入到 ToolCore")
            except Exception as exc:
                logger.warning("IsolationExecutor 初始化失败: %s，工具将在宿主机执行", exc)

        for candidate in ["default", "lingxi"]:
            self._agent_config = agent_registry.get(candidate)
            if self._agent_config:
                break
        if self._agent_config:
            logger.info(
                "Agent config loaded: %s (%s), level=%s",
                self._agent_config.config_id,
                self._agent_config.display_name,
                self._agent_config.level.value,
            )
        else:
            logger.info("No agent config loaded, using raw LLM without system prompt")

        # 创建管道引擎（直接调用，不需要 Worker 中间层）
        checkpoint_mgr = self._services.get("checkpoint_manager")
        self._engine = PipelineEngine(
            input_route_table=self._input_route_table,
            output_route_table=self._output_route_table,
            plugin_registry=self._plugin_registry,
            services=self._services,
            checkpoint_manager=checkpoint_mgr,
        )
        logger.info("PipelineEngine created (direct call, no Worker)")

        # Register llm_core as a service for context_window_guard
        llm_core_plugin = self._plugin_registry.get_core("llm_call")
        if llm_core_plugin is not None:
            self._services["llm_core"] = llm_core_plugin
            logger.info("Service registered: llm_core (from plugin registry)")

        # 初始化任务执行器（事件驱动，用于后台任务处理）
        try:
            from tasks.service import TaskService
            task_service = self._services.get("task_service") or TaskService()

            from infrastructure.task_worker import TaskWorker
            import yaml as _yaml
            _tw_config: dict[str, Any] = {}
            try:
                with open(config_path, encoding="utf-8") as _f:
                    _raw_cfg = _yaml.safe_load(_f) or {}
                _tw_config = _raw_cfg.get("task_worker", {})
            except Exception:
                pass
            self._task_worker = TaskWorker(
                task_service=task_service,
                plugin_registry=self._plugin_registry,
                input_route_table=self._input_route_table,
                output_route_table=self._output_route_table,
                services=self._services,
                event_bus=self._event_bus,
                config=_tw_config,
            )
            logger.info("Task worker initialized")

            _prt = self._input_route_table
            _ort = self._output_route_table
            _pr = self._plugin_registry
            _svc = self._services

            def _eval_pipeline_factory():
                return PipelineEngine(
                    input_route_table=_prt,
                    output_route_table=_ort,
                    plugin_registry=_pr,
                    services=_svc,
                )

            _sys._agent_os_pipeline_factory = _eval_pipeline_factory
        except Exception as exc:
            logger.warning("Failed to initialize task worker: %s", exc)
            self._task_worker = None
            console = self._output_adapter.console
            console.print(
                f"[bold red]⚠ 任务执行器初始化失败: {exc}[/bold red]\n"
                "[dim]任务提交功能将不可用，请检查日志排查原因[/dim]"
            )

        logger.info("Real pipeline setup complete: name=%s", pipeline_config.name)



    def _build_services(self, agent_registry: Any = None) -> dict[str, Any]:
        """构建共享服务字典。

        创建 ToolRegistry、记忆存储、MemoryService 等共享服务，
        插件通过 ctx.get_service() 自主获取。

        存储后端选择策略：
        1. 始终创建 JsonMemoryStore 作为内容存储
        2. 尝试创建 PgVectorRetriever（需 DATABASE_URL + SQLAlchemy 可用）
        3. PG 可用时：retrievers={"vector": vector_retriever, "keyword": json_store}
        4. PG 不可用时降级到只有 keyword 检索

        Returns:
            服务名称到实例的映射字典
        """
        services: dict[str, Any] = {}

        # 1. ToolRegistry — 工具注册表（基础工具直接注册，其余按需加载）
        try:
            from tools.registry import ToolRegistry
            from tools.types import Tool
            tool_registry = ToolRegistry()
            self._register_basic_tools(tool_registry)
            services["tool_registry"] = tool_registry
            import sys as _sys_local
            _sys_local._agent_os_tool_registry = tool_registry
            logger.info("Service created: tool_registry (%d basic tools registered)", tool_registry.count())

            from tools.auto_loader import init_tool_auto_loader
            init_tool_auto_loader(tool_registry)
            logger.info("ToolAutoLoader initialized with service registry")
        except Exception as exc:
            logger.warning("Failed to create tool_registry service: %s", exc)

        # 2. 始终创建 JsonMemoryStore 作为内容存储
        json_store: Any = None
        try:
            from memory.storage.json_store import JsonMemoryStore
            json_store = JsonMemoryStore()
            logger.info("Service created: JsonMemoryStore (content store)")
        except Exception as exc:
            logger.warning("Failed to create JsonMemoryStore: %s", exc)

        memory_store = json_store
        semantic_storage = json_store

        if memory_store is not None:
            services["memory_store"] = memory_store
        if semantic_storage is not None:
            services["semantic_storage"] = semantic_storage

        # 3. 尝试创建 PgVectorRetriever（PG 可用时）
        vector_retriever: Any = None
        try:
            from infrastructure.db import get_async_session, init_db
            from memory.storage.pgvector_retriever import PgVectorRetriever

            import asyncio
            session = asyncio.get_event_loop().run_until_complete(get_async_session())
            if session is not None and json_store is not None:
                # 初始化数据库表
                asyncio.get_event_loop().run_until_complete(init_db())

                # 构建 embedding 函数：使用 MemoryService.get_embedding 的占位实现
                # 实际由 embedding_service 提供，这里从 models config 加载
                embedding_fn = self._build_embedding_fn()

                vector_retriever = PgVectorRetriever(
                    session=session,
                    content_store=json_store,
                    embedding_fn=embedding_fn,
                )
                # 创建向量索引表
                asyncio.get_event_loop().run_until_complete(vector_retriever.ensure_tables())
                logger.info("Service created: PgVectorRetriever (vector retriever)")
        except Exception as exc:
            logger.info("PgVectorRetriever not available, falling back to keyword only: %s", exc)

        # 4. 构建 retrievers 字典
        retrievers: dict[str, Any] = {}
        if json_store is not None:
            retrievers["keyword"] = json_store
        if vector_retriever is not None:
            retrievers["vector"] = vector_retriever
            services["vector_retriever"] = vector_retriever
            logger.info("Service created: vector_retriever")

        # 5. retriever 服务 — 优先使用 vector_retriever
        if vector_retriever is not None:
            services["retriever"] = vector_retriever
            logger.info("Service created: retriever (backed by vector_retriever)")
        elif memory_store is not None and hasattr(memory_store, "search"):
            services["retriever"] = memory_store
            logger.info("Service created: retriever (backed by memory_store)")

        # 5.5 TagService + ChunkService — 压缩块持久化和标签管理
        tag_service: Any = None
        chunk_service: Any = None
        try:
            from memory.tag_service import TagService

            embedding_fn = self._build_embedding_fn()
            tag_service = TagService(
                content_store=json_store,
                vector_retriever=vector_retriever,
                embedding_fn=embedding_fn,
            )
            services["tag_service"] = tag_service
            logger.info("Service created: tag_service")
        except Exception as exc:
            logger.warning("Failed to create tag_service: %s", exc)

        try:
            from memory.chunk_service import ChunkService

            chunk_service = ChunkService(
                content_store=json_store,
                vector_retriever=vector_retriever,
                tag_service=tag_service,
            )
            services["chunk_service"] = chunk_service
            logger.info("Service created: chunk_service")
        except Exception as exc:
            logger.warning("Failed to create chunk_service: %s", exc)

        # 5.5.1 MemoryContextService — 上下文压缩共享服务
        try:
            from config.models import ModelConfigLoader as _MCL

            _loader = _MCL()
            _llm_data = _loader._load_llm_data()
            _defaults = _llm_data.get("defaults", {})
            _model_id = _defaults.get("chat", "")
            _llm_conf = _loader.get_llm_core_config(_model_id) if _model_id else {}
            _ctx_window = _llm_conf.get("context_window", 128000)

            from memory.memory_context_service import MemoryContextService

            context_service = MemoryContextService(
                config={
                    "context_window": _ctx_window,
                    "compress_trigger_ratio": 0.5,
                },
            )
            services["context_service"] = context_service
            logger.info(
                "Service created: context_service (context_window=%d)", _ctx_window,
            )
        except Exception as exc:
            logger.warning("Failed to create context_service: %s", exc)

        # 5.6 TagNetworkRetriever — 三阶段检索（同步创建，异步初始化在 run() 中）
        try:
            from memory.tag_network import TagNetworkConfig, TagNetworkRetriever

            tag_network_retriever = TagNetworkRetriever(config=TagNetworkConfig())
            services["tag_network_retriever"] = tag_network_retriever
            logger.info("Service created: tag_network_retriever (pending async init)")
        except Exception as exc:
            logger.warning("Failed to create tag_network_retriever: %s", exc)

        # 6. MemoryService — 记忆服务门面
        try:
            from memory.service import MemoryService

            memory_service = MemoryService(
                episode_storage=memory_store,
                semantic_storage=semantic_storage,
                retrievers=retrievers if retrievers else None,
                vector_retriever=vector_retriever,
                chunk_service=chunk_service,
                tag_service=tag_service,
            )
            services["memory_service"] = memory_service
            logger.info("Service created: memory_service (retrievers=%s)", list(retrievers.keys()))
        except Exception as exc:
            logger.warning("Failed to create memory_service: %s", exc)

        # 7. MessageQueue — 管道间消息传递
        try:
            from infrastructure.message_queue import MessageQueue

            message_queue = MessageQueue()
            services["message_queue"] = message_queue
            logger.info("Service created: message_queue")
        except Exception as exc:
            logger.warning("Failed to create message_queue service: %s", exc)

        # 8. ExecutionRecordStorage — 执行记录持久化
        try:
            from infrastructure.execution_record_storage import ExecutionRecordStorage

            execution_record_storage = ExecutionRecordStorage(
                data_dir=str(_PROJECT_ROOT / "data" / "pipelines")
            )
            services["execution_record_storage"] = execution_record_storage
            _sys._agent_os_execution_record_storage = execution_record_storage
            logger.info("Service created: execution_record_storage")
        except Exception as exc:
            logger.warning("Failed to create execution_record_storage service: %s", exc)

        # 9. TaskService — 任务服务（共享实例，供工具和插件使用）
        try:
            from tasks.service import TaskService

            event_bus_ref = self._event_bus

            def _on_task_state_change(task_id, old_status, new_status, **kwargs):
                """任务状态变更回调，桥接到 EventBus 通知提交者。"""
                if event_bus_ref is None:
                    return
                try:
                    task_obj = kwargs.get("task")
                    task_info = None
                    if task_obj and hasattr(task_obj, "title"):
                        task_info = {
                            "title": getattr(task_obj, "title", ""),
                            "error": getattr(task_obj, "error", "") or "",
                            "parent_task_id": getattr(task_obj, "parent_task_id", ""),
                            "priority": getattr(task_obj, "priority", ""),
                            "agent_name": getattr(task_obj, "agent_name", ""),
                        }
                    import asyncio
                    event_data = {
                        "task_id": task_id,
                        "old_status": old_status,
                        "new_status": new_status,
                        "source": "task_service",
                    }
                    if task_info is not None:
                        event_data["task"] = task_info
                    asyncio.create_task(event_bus_ref.emit("task_state_changed", event_data))
                except Exception:
                    pass

            task_service = TaskService(on_state_change=_on_task_state_change)
            services["task_service"] = task_service
            _sys._agent_os_task_service = task_service
            logger.info("Service created: task_service (with state change callback)")
        except Exception as exc:
            logger.warning("Failed to create task_service: %s", exc)

        # 9b. TimerManager — 任务超时计时器
        try:
            from tasks.timer_manager import TimerManager
            timer_manager = TimerManager.get_instance()
            services["timer_manager"] = timer_manager
            logger.info("Service created: timer_manager (idle_threshold=%ds)",
                        timer_manager.idle_threshold)
        except Exception as exc:
            logger.warning("Failed to create timer_manager: %s", exc)

        # 10. EventBus — 事件总线（共享实例，供任务系统使用）
        services["event_bus"] = self._event_bus

        # 10b. AgentRegistry — 供 TaskWorker 加载子 agent 配置
        if agent_registry is not None:
            services["agent_registry"] = agent_registry
            _sys._agent_os_agent_registry = agent_registry
            logger.info("Service injected: agent_registry (%d agents)", len(agent_registry._configs))
        else:
            logger.warning("agent_registry not provided to _build_services, TaskWorker will not be able to load sub-agent configs")

        # 11. PipelineCheckpointManager + PipelineRecovery — 管道检查点与恢复
        try:
            from infrastructure.checkpoint.pipeline_checkpoint import PipelineCheckpointManager
            from infrastructure.checkpoint.recovery import PipelineRecovery

            checkpoint_manager = PipelineCheckpointManager()
            recovery = PipelineRecovery(checkpoint_manager)
            services["checkpoint_manager"] = checkpoint_manager
            services["pipeline_recovery"] = recovery
            logger.info("Service created: checkpoint_manager, pipeline_recovery")
        except Exception as exc:
            logger.warning("Failed to create checkpoint services: %s", exc)

        # 12. 注入 CLI 交互通知器 — 子 Agent 人类交互支持
        try:
            from channels.cli.cli_interaction import CLIInteractionNotifier
            from human_interaction import get_human_interaction_service
            cli_notifier = CLIInteractionNotifier(console=self._output_adapter.console)
            human_svc = get_human_interaction_service()
            human_svc.set_notifier(cli_notifier)
            services["cli_notifier"] = cli_notifier
            services["human_interaction_service"] = human_svc
            logger.info("Service created: CLIInteractionNotifier -> HumanInteractionService")
        except Exception as exc:
            logger.warning("Failed to create CLIInteractionNotifier: %s", exc)

        _sys._agent_os_services = services

        from infrastructure.service_provider import get_service_provider
        sp = get_service_provider()
        for _sp_name in [
            "task_service", "execution_record_storage",
            "agent_registry", "event_bus",
        ]:
            _sp_inst = services.get(_sp_name)
            if _sp_inst is not None:
                sp.register(_sp_name, _sp_inst)

        return services

    @staticmethod
    def _build_embedding_fn() -> Any:
        """构建嵌入函数（异步，文本→向量）。

        尝试从 config 加载嵌入配置，构建可调用的嵌入函数。
        如果嵌入服务不可用，返回一个零向量占位函数。

        Returns:
            异步嵌入函数 (str -> list[float])
        """
        try:
            from config.models import ModelConfigLoader
            loader = ModelConfigLoader()
            embedding_cfg = loader._load_embedding_data()

            embeddings = embedding_cfg.get("embeddings", {})
            default_id = embedding_cfg.get("default_embedding", "")

            if default_id and default_id in embeddings:
                emb_info = embeddings[default_id]
                provider = emb_info.get("provider", "")

                # 根据 provider 类型构建嵌入函数
                if provider in ("openai", "openai_compatible"):
                    import os
                    api_key = os.environ.get(
                        emb_info.get("api_key_env", "OPENAI_API_KEY"), ""
                    )
                    base_url = emb_info.get("base_url")
                    model_name = emb_info.get("model", "text-embedding-3-small")

                    async def _openai_embed(text: str) -> list[float]:
                        """调用 OpenAI 兼容 API 生成嵌入向量。"""
                        try:
                            import httpx
                            url = f"{base_url}/embeddings" if base_url else "https://api.openai.com/v1/embeddings"
                            async with httpx.AsyncClient() as client:
                                resp = await client.post(
                                    url,
                                    headers={"Authorization": f"Bearer {api_key}"},
                                    json={"model": model_name, "input": text},
                                    timeout=30.0,
                                )
                                resp.raise_for_status()
                                data = resp.json()
                                return data["data"][0]["embedding"]
                        except Exception as e:
                            logger.warning("[EmbedFn] OpenAI 嵌入失败: %s", e)
                            return [0.0] * 1536

                    return _openai_embed
        except Exception as exc:
            logger.debug("[EmbedFn] 加载嵌入配置失败: %s", exc)

        # 降级：零向量占位函数
        async def _zero_embed(text: str) -> list[float]:
            """降级嵌入函数，返回零向量。"""
            logger.warning("[EmbedFn] 嵌入服务不可用，返回零向量")
            return [0.0] * 1536

        return _zero_embed

    def _register_basic_tools(self, registry: Any) -> None:
        """注册基础工具（无需依赖注入）。

        只注册 calculator 和 current_time 两个纯计算工具。
        需要服务依赖的工具（task_submit、task_manage 等）
        由 ToolCore 执行时自动从 services 注入。

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
                        "timezone": {"type": "string", "description": "时区（默认本地）"},
                    },
                },
            )
            registry.register_with_handler(tool=tool, handler=current_time)
        except Exception as exc:
            logger.warning("Failed to register basic tool current_time: %s", exc)

        # calculator
        def calculator(params: dict[str, Any]) -> str:
            """执行简单数学计算。"""
            expression = params.get("expression", "")
            if not expression:
                return "错误：未提供计算表达式"
            try:
                allowed_names = {
                    "abs": abs, "round": round, "min": min, "max": max,
                    "pow": pow, "sum": sum,
                    "pi": _math.pi, "e": _math.e,
                    "sqrt": _math.sqrt, "ceil": _math.ceil, "floor": _math.floor,
                }
                result = eval(expression, {"__builtins__": {}}, allowed_names)  # noqa: S307
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
            logger.warning("Failed to register basic tool calculator: %s", exc)

    # -----------------------------------------------------------------------
    # 非交互模式：发送单条消息并等待任务闭环
    # -----------------------------------------------------------------------

    async def run_single(self, message: str) -> None:
        """非交互模式：发送单条消息，等待后台任务闭环后退出。"""
        import time as _time

        t0 = _time.time()
        console = self._output_adapter.console

        console.print(f"\n[bold green]User:[/bold green] {message}\n")

        tw = getattr(self, "_task_worker", None)
        if tw and hasattr(tw, "start"):
            await tw.start()
            logger.info("TaskWorker started (single-message mode)")

        try:
            result = await self._engine.run(
                user_input=message,
                agent_config=self._agent_config,
                conversation_history=None,
                streaming=False,
                auto_approve=True,
                interaction_mode="auto",
            )
        except Exception as exc:
            console.print(f"\n[red]Engine error: {exc}[/red]")
            if tw and hasattr(tw, "stop"):
                await tw.stop()
            return

        elapsed_l1 = _time.time() - t0
        iters = result.get("iteration", 0)
        pipeline_id = result.get("pipeline_id", "")
        raw = result.get("raw_result", "")

        ts = self._services.get("task_service")
        task_ids = []
        if ts:
            try:
                storage = getattr(ts, "_storage", None)
                if storage is not None:
                    all_tasks = getattr(storage, "_tasks", {})
                    running_tasks = [
                        (tid, t) for tid, t in all_tasks.items()
                        if hasattr(t, "status") and t.status.value in ("running", "pending")
                    ]
                    if running_tasks:
                        running_tasks.sort(key=lambda x: getattr(x[1], "created_at", ""), reverse=True)
                        task_ids = [tid for tid, _ in running_tasks]
            except Exception:
                pass

        console.print(f"\n[dim]L1 done: {elapsed_l1:.1f}s, {iters} iterations, pipeline={pipeline_id}[/dim]")
        if task_ids:
            console.print(f"[dim]Tasks submitted: {task_ids}, waiting for completion...[/dim]\n")

            final_statuses = {}
            for _ in range(120):
                await asyncio.sleep(5)
                if ts:
                    try:
                        remaining = []
                        for tid in task_ids:
                            task = ts.get_task(tid)
                            if task:
                                status = task.status if hasattr(task, "status") else task.get("status", "?")
                                status_val = status.value if hasattr(status, "value") else str(status)
                                if status_val in ("completed", "failed", "cancelled"):
                                    final_statuses[tid] = status_val
                                else:
                                    remaining.append(tid)
                        if not remaining:
                            break
                    except Exception:
                        pass

            if final_statuses and self._engine:
                try:
                    summary_lines = []
                    for tid, st in final_statuses.items():
                        task_obj = ts.get_task(tid) if ts else None
                        title = getattr(task_obj, "title", tid) if task_obj else tid
                        summary_lines.append(f"- 任务 [{title}](id={tid}): {st}")
                    summary_text = "\n".join(summary_lines)
                    followup = (
                        f"[系统通知] 以下子任务已到达终态，请向用户汇报最终结果：\n{summary_text}\n\n"
                        "请用简洁的方式向用户汇报任务执行结果。"
                    )
                    followup_result = await self._engine.run(
                        user_input=followup,
                        agent_config=self._agent_config,
                        conversation_history=None,
                        streaming=False,
                        auto_approve=True,
                        interaction_mode="auto",
                    )
                    followup_raw = followup_result.get("raw_result", "")
                    if followup_raw:
                        console.print(f"\n[bold green]Agent:[/bold green] {followup_raw}")
                except Exception as exc:
                    logger.warning("run_single followup failed: %s", exc)

            elapsed_total = _time.time() - t0
            for tid in task_ids:
                status = final_statuses.get(tid, "timeout")
                console.print(f"\n[bold]Task {tid}: {status}[/bold]")
            console.print(f"[dim]Total: {elapsed_total:.1f}s[/dim]")
        else:
            console.print(f"\n[dim]No task submitted. Response: {str(raw)[:300]}[/dim]")

        if tw and hasattr(tw, "stop"):
            await tw.stop()

    # -----------------------------------------------------------------------
    # Claude Code 风格 REPL 循环
    # -----------------------------------------------------------------------

    async def run(self) -> None:
        """运行 Claude Code 风格 CLI 交互主循环。

        特性：
        - 斜杠命令处理（/help, /clear, /mode 等）
        - 底部状态栏渲染
        - 流式输出 + <think/> 过滤
        - 交互模式切换（Normal/Auto/Plan）
        - 工具调用可视化
        - 行内快捷语法
        """
        from rich.console import Console

        console = self._output_adapter.console
        agent_name = self._agent_config.display_name if self._agent_config else "Agent OS"
        model_name = self._get_model_name()

        # 显示启动横幅
        self._output_adapter.show_startup_banner(agent_name, self._interaction_mode)

        # 异步初始化 TagNetworkRetriever（从 PG 加载 Tag 向量和共现关系）
        tag_network_retriever = self._services.get("tag_network_retriever")
        vector_retriever = self._services.get("vector_retriever")
        if tag_network_retriever is not None and vector_retriever is not None:
            try:
                await tag_network_retriever.init_from_pg(vector_retriever)
            except Exception as exc:
                logger.warning("TagNetworkRetriever async init failed: %s", exc)

        # 启动任务执行器（如果可用）
        if hasattr(self, '_task_worker') and self._task_worker:
            if hasattr(self._task_worker, 'start'):
                await self._task_worker.start()
                logger.info("Task worker started")

        # 初始化状态栏
        self._output_adapter.update_status_bar(
            agent_name=agent_name,
            model_name=model_name,
            turn_count=0,
            context_pct=0.0,
            mode=self._interaction_mode,
            is_processing=False,
        )

        # 确定 Agent ID（从已加载的 agent_config 获取）
        agent_id = self._agent_config.config_id if self._agent_config else "lingxi"

        # 跨轮次对话历史：累积所有轮次的 messages
        conversation_history: list[dict[str, Any]] = []

        # 会话级 pipeline_run_id：同一会话共享，清空历史时重新生成
        # Try to resume the previous session from a persisted checkpoint
        session_dir = _get_session_dir()
        checkpoint_mgr = self._services.get("checkpoint_manager")
        restored = False

        saved_session_id = _load_session_id(session_dir)
        if saved_session_id is not None and checkpoint_mgr is not None:
            try:
                latest_checkpoint = await checkpoint_mgr.get_latest(saved_session_id)
                if latest_checkpoint is not None:
                    saved_messages = latest_checkpoint.get("state", {}).get("messages", [])
                    if isinstance(saved_messages, list) and saved_messages:
                        conversation_history = saved_messages
                        session_pipeline_id = saved_session_id
                        restored = True
            except Exception as exc:
                logger.debug("Failed to restore session checkpoint: %s", exc)

        # BUG-FIX-fix_20260418_session_pipeline_id:
        # 回退：当 session_pipeline_id 对应的检查点不存在时（如旧版本遗留的不匹配 ID），
        # 尝试从全局最新检查点恢复对话历史，优先使用 session_end 阶段的检查点。
        if not restored and checkpoint_mgr is not None:
            try:
                fallback_checkpoint = await checkpoint_mgr.get_latest_any(phase="session_end")
                if fallback_checkpoint is None:
                    fallback_checkpoint = await checkpoint_mgr.get_latest_any()
                if fallback_checkpoint is not None:
                    fb_messages = fallback_checkpoint.get("state", {}).get("messages", [])
                    fb_pipeline_id = fallback_checkpoint.get("metadata", {}).get("pipeline_id")
                    if isinstance(fb_messages, list) and fb_messages and fb_pipeline_id:
                        conversation_history = fb_messages
                        session_pipeline_id = fb_pipeline_id
                        restored = True
                        logger.info("Restored from fallback checkpoint: pipeline_id=%s", fb_pipeline_id)
            except Exception as exc:
                logger.debug("Fallback restoration failed: %s", exc)

        if not restored:
            session_pipeline_id = _uuid.uuid4().hex[:12]
            _save_session_id(session_dir, session_pipeline_id)
        else:
            _save_session_id(session_dir, session_pipeline_id)

        # Trim to MAX_SESSION_MESSAGES after restoration
        if len(conversation_history) > MAX_SESSION_MESSAGES:
            conversation_history = conversation_history[-MAX_SESSION_MESSAGES:]

        if restored:
            console.print(
                f"[dim]已恢复上次会话 ({len(conversation_history)} 条消息)，"
                f"使用 /clear 开启新会话[/dim]"
            )

        # REPL 主循环
        while True:
            # 渲染状态栏提示符
            status_text = self._output_adapter.status_bar.render_simple()
            self._input_adapter._prompt_str = f"{status_text} > "

            # 检查是否有子 Agent 待处理的交互请求
            cli_notifier = self._services.get("cli_notifier")
            if cli_notifier and cli_notifier.has_pending():
                human_svc = self._services.get("human_interaction_service")
                from channels.cli.cli_interaction import run_sub_conversation
                await run_sub_conversation(
                    console=console,
                    input_adapter=self._input_adapter,
                    notifier=cli_notifier,
                    interaction_service=human_svc,
                    idle_timeout=60,
                )
                continue

            # 读取用户输入
            initial_state = await self._input_adapter.receive()

            # 退出信号
            if initial_state.get("should_stop"):
                # Persist conversation history before exiting
                if conversation_history and checkpoint_mgr is not None:
                    try:
                        await checkpoint_mgr.save(
                            session_pipeline_id,
                            {"messages": conversation_history},
                            phase="session_end",
                        )
                    except Exception as exc:
                        logger.debug("Failed to save session on exit: %s", exc)

                # 停止任务执行器（会等待所有 pending 任务完成）
                if hasattr(self, '_task_worker') and self._task_worker and hasattr(self._task_worker, 'stop'):
                    await self._task_worker.stop()

                # 汇报所有已提交任务的最终状态
                ts = self._services.get("task_service")
                if ts and hasattr(ts, 'list_by_status'):
                    try:
                        from tasks.types import TaskStatus
                        all_tasks = []
                        for st in TaskStatus:
                            all_tasks.extend(ts.list_by_status(st))
                        if all_tasks:
                            console.print("\n[bold]任务状态汇总:[/bold]")
                            for t in all_tasks:
                                tid = t.id if hasattr(t, 'id') else str(t.get('id', '?'))
                                tstatus = t.status if hasattr(t, 'status') else t.get('status', '?')
                                tstatus_str = tstatus.value if hasattr(tstatus, 'value') else str(tstatus)
                                ttitle = t.title if hasattr(t, 'title') else t.get('title', '')
                                icon = "✅" if tstatus_str == "completed" else "❌" if tstatus_str == "failed" else "🔄"
                                console.print(f"  {icon} {tid[:12]} | {tstatus_str} | {ttitle}")
                    except Exception as exc:
                        logger.debug("任务状态汇总失败: %s", exc)

                self._output_adapter.show_system_message("感谢使用 Agent OS，再见！", "bold blue")
                break

            # 空输入 — 跳过
            if initial_state.get("empty_input"):
                continue

            # 斜杠命令处理
            slash_result = initial_state.get("slash_command")
            if slash_result and hasattr(slash_result, 'output'):
                if slash_result.output:
                    console.print(slash_result.output)
                if slash_result.should_exit:
                    # Persist conversation history before exiting
                    if conversation_history and checkpoint_mgr is not None:
                        try:
                            await checkpoint_mgr.save(
                                session_pipeline_id,
                                {"messages": conversation_history},
                                phase="session_end",
                            )
                        except Exception as exc:
                            logger.debug("Failed to save session on exit: %s", exc)
                    console.print("[bold blue]Goodbye![/bold blue]")
                    break
                continue

            # 退出处理
            if initial_state.get("should_stop"):
                # Persist conversation history before exiting
                if conversation_history and checkpoint_mgr is not None:
                    try:
                        await checkpoint_mgr.save(
                            session_pipeline_id,
                            {"messages": conversation_history},
                            phase="session_end",
                        )
                    except Exception as exc:
                        logger.debug("Failed to save session on exit: %s", exc)
                console.print("[bold blue]Goodbye![/bold blue]")
                break

            # 检查是否有 _handle_slash_command 方法（旧版兼容）
            if hasattr(self._input_adapter, 'is_slash_command') and self._input_adapter.is_slash_command(initial_state):
                cmd_result = await self._handle_slash_command(initial_state)
                if cmd_result is None:
                    continue
                if cmd_result.should_stop:
                    # Persist conversation history before exiting
                    if conversation_history and checkpoint_mgr is not None:
                        try:
                            await checkpoint_mgr.save(
                                session_pipeline_id,
                                {"messages": conversation_history},
                                phase="session_end",
                            )
                        except Exception as exc:
                            logger.debug("Failed to save session on exit: %s", exc)
                    self._output_adapter.show_system_message("感谢使用 Agent OS，再见！", "bold blue")
                    break
                if cmd_result.should_clear_history:
                    conversation_history.clear()
                    session_pipeline_id = _uuid.uuid4().hex[:12]
                    self._turn_count = 0
                    self._output_adapter.update_status_bar(turn_count=0, context_pct=0.0)
                    # Clean up persisted session checkpoints on disk
                    if cmd_result.should_clear_session:
                        old_session_id = _load_session_id(session_dir)
                        if old_session_id and checkpoint_mgr is not None:
                            try:
                                await checkpoint_mgr.cleanup_old(old_session_id, keep_count=0)
                            except Exception as exc:
                                logger.debug("Failed to cleanup session checkpoints: %s", exc)
                        _save_session_id(session_dir, session_pipeline_id)
                # 应用命令产生的 state 更新
                if cmd_result.state_updates:
                    self._apply_command_updates(cmd_result.state_updates)
                continue

            # Plan 模式：只显示规划，不实际执行
            if self._interaction_mode == "plan":
                self._output_adapter.show_system_message(
                    "[PLAN 模式] 不会执行任何操作，仅显示规划。使用 /mode normal 切换回正常模式。",
                    "yellow",
                )
                # 将用户输入展示为"规划"反馈
                user_input = initial_state.get("user_input", "")
                console.print(f"\n[dim][规划模式] 收到输入: {user_input}[/dim]")
                console.print("[dim]使用 /mode normal 或 /mode auto 切换模式后执行[/dim]\n")
                continue

            # 正常处理：通过 Engine.run() 直接执行
            user_input = initial_state.get("user_input", "")

            # 流式回调
            on_chunk = None
            if self._streaming:
                on_chunk = self._build_on_chunk_callback(console)

            # 更新状态栏：处理中
            task_stats = self._get_task_stats()
            self._output_adapter.update_status_bar(
                is_processing=True,
                pipeline_running=True,
                pipeline_iteration=0,
                pipeline_max_iterations=self._engine.max_iterations if self._engine else 0,
                running_task_count=task_stats["running"],
                pending_task_count=task_stats["pending"],
                completed_task_count=task_stats["completed"],
                failed_task_count=task_stats["failed"],
            )
            self._output_adapter.render_status_bar()

            # 执行管道 — 通过 Engine.run() 直接调用
            # BUG-FIX-fix_20260418_session_pipeline_id:
            # 传入 session_pipeline_id 使引擎自动检查点与会话恢复使用同一 ID，
            # 避免 session_pipeline_id 和 PipelineEngine._pipeline_id 不一致导致恢复失败。
            try:
                final_state = await self._engine.run(
                    user_input=user_input,
                    agent_config=self._agent_config,
                    conversation_history=conversation_history if conversation_history else None,
                    **{
                        "streaming": self._streaming,
                        "on_chunk": on_chunk,
                        "auto_approve": (self._interaction_mode == "auto"),
                        "interaction_mode": self._interaction_mode,
                        "pipeline_id": session_pipeline_id,
                    },
                )

                # 回填 pipeline_run_id 到关联的任务
                pipeline_run_id = final_state.get("pipeline_id", "")
                if pipeline_run_id:
                    # 检查是否有通过 task_submit 工具创建的任务
                    submitted_task_id = final_state.get("submitted_task_id")
                    if submitted_task_id:
                        task_service = self._services.get("task_service")
                        if task_service and hasattr(task_service, "bind_pipeline_run"):
                            try:
                                task_service.bind_pipeline_run(submitted_task_id, pipeline_run_id)
                                logger.info("Bound task %s to pipeline_run %s", submitted_task_id, pipeline_run_id)
                            except Exception as exc:
                                logger.warning("Failed to bind pipeline_run_id: %s", exc)
                    # 无论是否有任务，都记录 pipeline_id 到日志
                    logger.info("Pipeline run completed: pipeline_id=%s", pipeline_run_id)

                await self._output_adapter.send(final_state, streamed=self._streaming)

                # 显示管道产生的工具调用信息
                self._display_tool_calls_from_state(final_state)

                # 更新对话轮次
                self._turn_count += 1

                # 更新对话历史
                final_messages = final_state.get("messages", [])
                if final_messages:
                    conversation_history = list(final_messages)
                else:
                    # 如果管道没有维护 messages，手动构建
                    user_input = initial_state.get("user_input", "")
                    raw_result = final_state.get("raw_result", "")
                    if user_input:
                        conversation_history.append({"role": "user", "content": user_input})
                    if raw_result:
                        conversation_history.append({"role": "assistant", "content": raw_result})

                # Trim conversation history to MAX_SESSION_MESSAGES
                if len(conversation_history) > MAX_SESSION_MESSAGES:
                    conversation_history = conversation_history[-MAX_SESSION_MESSAGES:]

                # 更新状态栏
                ctx_pct = self._estimate_context_pct(conversation_history)
                task_stats = self._get_task_stats()
                iteration = final_state.get("iteration", 0)
                max_iterations = final_state.get("max_iterations", 0)
                self._output_adapter.update_status_bar(
                    turn_count=self._turn_count,
                    context_pct=ctx_pct,
                    is_processing=False,
                    pipeline_running=False,
                    pipeline_iteration=iteration,
                    pipeline_max_iterations=max_iterations,
                    running_task_count=task_stats["running"],
                    pending_task_count=task_stats["pending"],
                    completed_task_count=task_stats["completed"],
                    failed_task_count=task_stats["failed"],
                )

            except Exception as exc:
                await self._output_adapter.send({"error": str(exc)})
                task_stats = self._get_task_stats()
                self._output_adapter.update_status_bar(
                    is_processing=False,
                    pipeline_running=False,
                    running_task_count=task_stats["running"],
                    pending_task_count=task_stats["pending"],
                    completed_task_count=task_stats["completed"],
                    failed_task_count=task_stats["failed"],
                )

            # 管道结束后换行分隔
            console.print("")

    async def _handle_slash_command(self, state: dict[str, Any]) -> CommandResult | None:
        """处理斜杠命令。

        Args:
            state: 包含 _is_slash_command 标记的 state

        Returns:
            命令执行结果，None 表示跳过
        """
        user_input = state.get("user_input", "")

        # 构建命令执行上下文
        cmd_context = self._build_command_context()

        # 执行命令
        result = await self._command_registry.execute(user_input, cmd_context)
        return result

    def _build_command_context(self) -> dict[str, Any]:
        """构建斜杠命令执行上下文。

        Returns:
            包含 services/config/state 等引用的上下文字典
        """
        return {
            "services": self._services,
            "agent_config": self._agent_config,
            "mode": self._interaction_mode,
            "show_thinking": self._show_thinking,
            "turn_count": self._turn_count,
            "conversation_history": [],
            "last_state": {},
        }

    def _apply_command_updates(self, updates: dict[str, Any]) -> None:
        """应用斜杠命令产生的状态更新。

        Args:
            updates: 命令返回的 state_updates 字典
        """
        # 交互模式切换
        if "interaction_mode" in updates:
            new_mode = updates["interaction_mode"]
            if new_mode in ("normal", "auto", "plan"):
                self._interaction_mode = new_mode
                self._output_adapter.update_status_bar(mode=new_mode)
                # 更新输入提示符
                mode_label = new_mode.upper()
                agent_name = self._agent_config.display_name if self._agent_config else "Agent OS"
                self._input_adapter._prompt_str = f"[{mode_label}] {agent_name} > "

        # 思考过程显示切换
        if "show_thinking" in updates:
            self._show_thinking = updates["show_thinking"]
            self._output_adapter.show_thinking = self._show_thinking

        # 模型切换
        if "model_override" in updates:
            model_name = updates["model_override"]
            self._output_adapter.update_status_bar(model_name=model_name)

    def _build_on_chunk_callback(self, console: Console) -> Any:
        """构建流式输出的 on_chunk 回调。

        处理五种 chunk 类型：
        - type='text': 正常回复内容，逐 token 输出
        - type='thinking': 思考过程内容，根据 show_thinking 决定是否显示
        - type='tool_call': LLM 返回工具调用，实时显示工具名称
        - type='tool_result': 工具执行完成，显示执行结果
        - type='tool_start': 工具开始执行，显示执行中指示
        - type='iteration': 管道迭代进度，更新状态栏

        Args:
            console: rich Console 实例

        Returns:
            on_chunk 回调函数
        """
        _displayed_tool_indices: set[int] = set()

        def on_chunk(chunk: dict[str, Any]) -> None:
            """流式回调：将管道事件实时输出到终端。"""
            chunk_type = chunk.get("type", "text")
            content = chunk.get("content", "")

            if chunk_type == "thinking":
                if self._show_thinking and content:
                    console.print(content, end="", highlight=False)
                return

            if chunk_type == "text":
                if content:
                    console.print(content, end="", highlight=False)
                return

            if chunk_type == "tool_call":
                tool_calls_data = chunk.get("tool_calls", [])
                for tc in tool_calls_data:
                    tc_idx = getattr(tc, "index", 0)
                    if tc_idx in _displayed_tool_indices:
                        continue
                    func = getattr(tc, "function", None)
                    if func:
                        name = getattr(func, "name", "")
                        if name:
                            _displayed_tool_indices.add(tc_idx)
                            args_str = getattr(func, "arguments", "")
                            try:
                                import json as _json
                                args = _json.loads(args_str) if args_str else {}
                            except Exception:
                                args = {}
                            self._output_adapter.show_tool_call(name, args)
                return

            if chunk_type == "tool_start":
                tool_name = chunk.get("tool_name", "unknown")
                console.print(
                    f"  [dim yellow]>> 执行 {tool_name}...[/dim yellow]"
                )
                return

            if chunk_type == "tool_result":
                tool_name = chunk.get("tool_name", "unknown")
                result = chunk.get("result", "")
                success = chunk.get("success", True)
                duration_ms = chunk.get("duration_ms", 0)
                self._output_adapter.show_tool_result(
                    tool_name, result, success=success, duration_ms=duration_ms
                )
                return

            if chunk_type == "iteration":
                iteration = chunk.get("iteration", 0)
                max_iterations = chunk.get("max_iterations", 0)
                self._output_adapter.update_status_bar(
                    pipeline_iteration=iteration,
                    pipeline_max_iterations=max_iterations,
                    pipeline_running=True,
                )
                self._output_adapter.render_status_bar()
                return

        return on_chunk

    def _display_tool_calls_from_state(self, state: dict[str, Any]) -> None:
        """从管道最终 state 中显示工具调用信息（非流式模式的兜底显示）。

        流式模式下工具调用已通过 on_chunk 实时显示，此方法仅显示
        迭代信息等补充内容，避免重复显示。

        Args:
            state: 管道引擎的最终 state 字典
        """
        if not self._streaming:
            tool_results = state.get("tool_results")
            if tool_results and isinstance(tool_results, list):
                for tr in tool_results:
                    if isinstance(tr, dict):
                        tool_name = tr.get("tool_name", "unknown")
                        data = tr.get("data", tr.get("error", ""))
                        success = tr.get("success", True)
                        duration_ms = tr.get("duration_ms", 0)
                        self._output_adapter.show_tool_call(tool_name)
                        self._output_adapter.show_tool_result(
                            tool_name, str(data), success=success,
                            duration_ms=duration_ms,
                        )

            raw_tool_calls = state.get("raw_tool_calls")
            if raw_tool_calls and isinstance(raw_tool_calls, list):
                for tc in raw_tool_calls:
                    if isinstance(tc, dict):
                        func = tc.get("function", {})
                        name = func.get("name", tc.get("name", "unknown"))
                        args_str = func.get("arguments", tc.get("args", ""))
                        try:
                            import json
                            args = json.loads(args_str) if isinstance(args_str, str) else args_str
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        self._output_adapter.show_tool_call(name, args)

        iteration = state.get("iteration", 0)
        max_iterations = state.get("max_iterations", 0)
        if iteration and max_iterations and iteration > 1:
            self._output_adapter.show_iteration(iteration, max_iterations)

    def _get_model_name(self) -> str:
        """获取当前模型名称。

        Returns:
            模型名称字符串
        """
        if self._agent_config:
            if hasattr(self._agent_config, "model"):
                return self._agent_config.model
            if hasattr(self._agent_config, "config_id"):
                return self._agent_config.config_id
        return "unknown"

    def _get_task_stats(self) -> dict[str, int]:
        """收集任务状态统计。

        从 TaskService 中获取各状态的任务数量。

        Returns:
            包含 running/pending/completed/failed 计数的字典
        """
        stats = {"running": 0, "pending": 0, "completed": 0, "failed": 0}
        task_service = self._services.get("task_service")
        if task_service is None:
            return stats
        try:
            from tasks.types import TaskStatus
            stats["running"] = len(task_service.list_by_status(TaskStatus.RUNNING))
            stats["pending"] = len(task_service.list_by_status(TaskStatus.PENDING))
            stats["completed"] = len(task_service.list_by_status(TaskStatus.COMPLETED))
            stats["failed"] = len(task_service.list_by_status(TaskStatus.FAILED))
        except Exception as exc:
            logger.debug("Failed to collect task stats: %s", exc)
        return stats

    def _estimate_context_pct(self, history: list[dict[str, Any]]) -> float:
        """估算上下文占用百分比。

        Args:
            history: 对话历史消息列表

        Returns:
            占用百分比 (0-100)
        """
        if not history:
            return 0.0

        char_count = sum(
            len(m.get("content", "")) if isinstance(m, dict) else len(str(m))
            for m in history
        )
        estimated_tokens = char_count // 3
        max_context = 128000
        return min(100.0, estimated_tokens / max_context * 100)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI 入口函数，启动交互式管道应用。

    支持命令行参数：
    - ``--config PATH``: 指定管道配置 YAML 路径
    - ``--debug``: 启用调试日志
    - ``--no-streaming``: 禁用流式输出
    - ``--mode MODE``: 交互模式 (normal/auto/plan)
    """
    parser = argparse.ArgumentParser(description="Agent OS CLI — Claude Code 风格插件化管道交互式命令行")
    parser.add_argument("--config", type=str, default=None, help="管道配置 YAML 路径")
    parser.add_argument("--debug", action="store_true", help="启用调试日志")
    parser.add_argument("--no-streaming", action="store_true", help="禁用流式输出（默认启用）")
    parser.add_argument(
        "--mode", type=str, default="normal",
        choices=["normal", "auto", "plan"],
        help="交互模式 (normal/auto/plan)",
    )
    parser.add_argument("--message", "-m", type=str, default=None, help="直接发送消息（非交互模式）")
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    streaming = not args.no_streaming
    app = CLIApplication(streaming=streaming)
    app._interaction_mode = args.mode
    app.setup_pipeline(config_path=args.config)

    if args.message:
        asyncio.run(app.run_single(args.message))
    else:
        asyncio.run(app.run())


if __name__ == "__main__":
    main()

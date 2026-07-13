"""应用服务容器 — 统一管理服务构建和引擎创建。

作为后端服务构建的唯一入口，所有渠道（CLI、WebSocket、API）
都通过 Application.build_services() 获取共享服务字典。

职责边界：
- 构建所有后端服务（工具、记忆、任务、管道等）
- 创建 PipelineEngine 和 TaskWorker
- 注册到 ServiceProvider
- 不包含任何渠道特定逻辑（CLI 通知器、WebSocket 处理等由各渠道自行注入）

用法::

    app = Application(project_root=Path("/path/to/project"))
    services = app.build_services(agent_registry=registry)
    engine = app.create_pipeline_engine(config, plugin_registry)
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_DEFAULT_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

logger = logging.getLogger(__name__)


class Application:
    """应用服务容器。

    集中管理服务实例的创建、工具注册、PipelineEngine 和 TaskWorker 的构建。
    作为后端服务构建的唯一真相源，所有入口（CLI、WebSocket）都委托此类。

    Attributes:
        project_root: 项目根目录路径
        services: 共享服务字典
    """

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root: Path = project_root or Path.cwd()
        self.services: dict[str, Any] = {}

    def build_services(self, agent_registry: Any | None = None) -> dict[str, Any]:  # noqa: PLR0912,PLR0915
        """构建共享服务字典（唯一入口）。

        Args:
            agent_registry: Agent 注册表实例（可选）

        Returns:
            服务名称到实例的映射字典
        """
        services: dict[str, Any] = {}
        services["project_root"] = str(self.project_root)
        _t0 = _time.monotonic()

        # ── 1. ToolRegistry ──────────────────────────────
        try:
            from tools.registry import ToolRegistry  # noqa: PLC0415

            tool_registry = ToolRegistry()
            self._register_basic_tools(tool_registry)
            services["tool_registry"] = tool_registry
            logger.debug("服务已: tool_registry (%d 个基础工具)", tool_registry.count())

            from tools.auto_loader import init_tool_auto_loader  # noqa: PLC0415

            init_tool_auto_loader(tool_registry)
            logger.debug("ToolAutoLoader 已初始化")
        except Exception as exc:
            logger.warning("创建 tool_registry 服务失败: %s", exc)
        logger.debug("[STARTUP] 1.ToolRegistry: %.2fs", _time.monotonic() - _t0)
        _t0 = _time.monotonic()

        # ── 1.5 MediaProviderRegistry ────────────────────
        try:
            from tools.media.provider_registry import MediaProviderRegistry  # noqa: PLC0415

            media_registry = MediaProviderRegistry()
            config_path = self.project_root / "config" / "models" / "media_providers.yaml"
            logger.debug("[STARTUP] MediaProviderRegistry config_path=%s exists=%s", config_path, config_path.exists())
            if config_path.exists():
                media_registry.load_config(config_path)
                logger.debug("[STARTUP] MediaProviderRegistry config loaded, registering providers...")
                self._register_media_providers(media_registry)
            services["media_provider_registry"] = media_registry
            logger.info(
                "服务已创建: media_provider_registry (%d 个 Provider: %s)",
                len(media_registry.list_all()),
                [p.provider_name for p in media_registry.list_all()],
            )
        except Exception as exc:
            logger.warning("创建 MediaProviderRegistry 失败: %s", exc, exc_info=True)

        # ── 2. JsonMemoryStore ───────────────────────────
        json_store: Any = None
        try:
            from memory.storage.json_store import JsonMemoryStore  # noqa: PLC0415

            json_store = JsonMemoryStore()
            logger.debug("服务已: JsonMemoryStore")
        except Exception as exc:
            logger.warning("创建 JsonMemoryStore 失败: %s", exc)

        memory_store = json_store
        semantic_storage = json_store
        if memory_store is not None:
            services["memory_store"] = memory_store
        if semantic_storage is not None:
            services["semantic_storage"] = semantic_storage

        # ── 3. PgVectorRetriever（可选）──────────────────
        vector_retriever: Any = None
        try:
            from infrastructure.db import get_async_session, init_db  # noqa: PLC0415
            from memory.storage.pgvector_retriever import PgVectorRetriever  # noqa: PLC0415

            session = asyncio.get_event_loop().run_until_complete(get_async_session())
            if session is not None and json_store is not None:
                asyncio.get_event_loop().run_until_complete(init_db())
                embedding_fn = self._build_embedding_fn()
                vector_retriever = PgVectorRetriever(
                    session=session,
                    content_store=json_store,
                    embedding_fn=embedding_fn,
                )
                asyncio.get_event_loop().run_until_complete(vector_retriever.ensure_tables())
                logger.debug("服务已: PgVectorRetriever")
        except Exception as exc:
            logger.info("PgVectorRetriever 不可用，降级到 keyword 检索: %s", exc)
        logger.debug("[STARTUP] 3.PgVector: %.2fs", _time.monotonic() - _t0)
        _t0 = _time.monotonic()

        retrievers: dict[str, Any] = {}
        if json_store is not None:
            retrievers["keyword"] = json_store
        if vector_retriever is not None:
            retrievers["vector"] = vector_retriever
            services["vector_retriever"] = vector_retriever

        if vector_retriever is not None:
            services["retriever"] = vector_retriever
            logger.debug("服务已: retriever (vector)")
        elif memory_store is not None and hasattr(memory_store, "search"):
            services["retriever"] = memory_store
            logger.debug("服务已: retriever (memory_store)")

        # ── 4. TagService + ChunkService ─────────────────
        tag_service: Any = None
        chunk_service: Any = None
        try:
            from memory.tag_service import TagService  # noqa: PLC0415

            tag_service = TagService(
                content_store=json_store,
                vector_retriever=vector_retriever,
                embedding_fn=self._build_embedding_fn(),
                data_dir=str(self.project_root / "data" / "memory"),
            )
            services["tag_service"] = tag_service
            logger.debug("服务已: tag_service")
        except Exception as exc:
            logger.warning("创建 tag_service 失败: %s", exc)
        logger.debug("[STARTUP] 4.TagService: %.2fs", _time.monotonic() - _t0)
        _t0 = _time.monotonic()

        try:
            from memory.chunk_service import ChunkService  # noqa: PLC0415

            chunk_service = ChunkService(
                content_store=json_store,
                vector_retriever=vector_retriever,
                tag_service=tag_service,
                data_dir=str(self.project_root / "data" / "memory"),
            )
            services["chunk_service"] = chunk_service
            logger.debug("服务已: chunk_service")
        except Exception as exc:
            logger.warning("创建 chunk_service 失败: %s", exc)
        logger.debug("[STARTUP] 4.5.ChunkService: %.2fs", _time.monotonic() - _t0)
        _t0 = _time.monotonic()

        # ── 5. MemoryContextService ──────────────────────
        try:
            from config.models import get_model_config_loader as _get_loader  # noqa: PLC0415
            from memory.memory_context_service import MemoryContextService  # noqa: PLC0415

            _loader = _get_loader()
            _llm_data = _loader._load_llm_data()
            _defaults = _llm_data.get("defaults", {})
            _model_id = _defaults.get("chat", "")
            _llm_conf = _loader.get_llm_core_config(_model_id) if _model_id else {}
            _ctx_window = _llm_conf.get("context_window", 128000)

            from config.defaults import COMPRESS_TRIGGER_RATIO  # noqa: PLC0415

            context_service = MemoryContextService(
                config={
                    "context_window": _ctx_window,
                    "compress_trigger_ratio": COMPRESS_TRIGGER_RATIO,
                },
            )
            services["context_service"] = context_service
            logger.debug("服务已: context_service (context_window=%d)", _ctx_window)
        except Exception as exc:
            logger.warning("创建 context_service 失败: %s", exc)
        logger.debug("[STARTUP] 5.ContextService: %.2fs", _time.monotonic() - _t0)
        _t0 = _time.monotonic()

        # ── 6. TagNetworkRetriever ───────────────────────
        try:
            from memory.tag_network import TagNetworkConfig, TagNetworkRetriever  # noqa: PLC0415

            tag_network_retriever = TagNetworkRetriever(config=TagNetworkConfig())
            services["tag_network_retriever"] = tag_network_retriever
            logger.debug("服务已: tag_network_retriever")
        except Exception as exc:
            logger.warning("创建 tag_network_retriever 失败: %s", exc)
        logger.debug("[STARTUP] 6.TagNetwork: %.2fs", _time.monotonic() - _t0)
        _t0 = _time.monotonic()

        # ── 7. MemoryService ─────────────────────────────
        try:
            from memory.service import MemoryService  # noqa: PLC0415

            memory_service = MemoryService(
                episode_storage=memory_store,
                semantic_storage=semantic_storage,
                retrievers=retrievers if retrievers else None,
                vector_retriever=vector_retriever,
                chunk_service=chunk_service,
                tag_service=tag_service,
            )
            services["memory_service"] = memory_service
            logger.debug("服务已: memory_service (retrievers=%s)", list(retrievers.keys()))
        except Exception as exc:
            logger.warning("创建 memory_service 失败: %s", exc)
        logger.debug("[STARTUP] 7.MemoryService: %.2fs", _time.monotonic() - _t0)
        _t0 = _time.monotonic()

        # ── 8. ExecutionRecordStorage ────────────────────
        try:
            from infrastructure.execution_record_storage import ExecutionRecordStorage  # noqa: PLC0415

            services["execution_record_storage"] = ExecutionRecordStorage(
                data_dir=str(self.project_root / "data" / "pipelines")
            )
            logger.debug("服务已: execution_record_storage")
        except Exception as exc:
            logger.warning("创建 execution_record_storage 服务失败: %s", exc)

        # ── 9. MemoryMaintenanceService ─────────────────
        try:
            from memory.maintenance import MemoryMaintenanceService  # noqa: PLC0415

            _maintenance_config = self._load_maintenance_config()

            # 解析 review_agent 实际模型的上下文窗口，供复盘批次预算反推用。
            # 复用 #5 ContextService 的 loader 写法：review_agent model_tier → tiers 映射 → model 别名 → context_window。
            _review_ctx_window = 128000  # 兜底默认
            try:
                from config.models import get_model_config_loader as _get_loader  # noqa: PLC0415

                _loader = _get_loader()
                _tiers = _loader._load_llm_data().get("defaults", {}).get("tiers", {})
                _review_model_alias = _tiers.get("small", "")  # review_agent.yaml: model_tier=small
                if _review_model_alias:
                    _llm_conf = _loader.get_llm_core_config(_review_model_alias) or {}
                    _review_ctx_window = _llm_conf.get("context_window", 128000)
            except Exception as _wexc:
                logger.warning("[STARTUP] 解析 review_agent 上下文窗口失败，使用默认值: %s", _wexc)

            # 构造 task_lookup 回调：把 pipeline_run_id 反查到目标 agent 和任务标题。
            # 用延迟查找 task_service（它在本服务之后才创建），避免初始化顺序依赖。
            # 真实数据约 58% 的管道由任务系统创建可查到 agent，其余纯对话管道返回 None。
            _execution_storage = services.get("execution_record_storage")

            def _task_lookup(pipeline_run_id: str):
                """反查 pipeline_run_id -> 目标 agent + 任务标题。

                Returns:
                    {"agent": target_id, "title": task.title} 或 None
                """
                try:
                    task_service = services.get("task_service")
                    if task_service is None or _execution_storage is None:
                        return None
                    # pipeline_run_id -> root_task_id
                    root_map = getattr(_execution_storage, "_pipeline_root_map", {}) or {}
                    root_task_id = root_map.get(pipeline_run_id)
                    if not root_task_id:
                        return None
                    # task_service 暴露 storage；从 root_task 拿 target_id/title
                    task_storage = getattr(task_service, "_storage", None) or getattr(task_service, "storage", None)
                    if task_storage is None:
                        return None
                    task = task_storage.get(root_task_id)
                    if task is None:
                        return None
                    target_id = ""
                    if getattr(task, "metadata", None):
                        target_id = task.metadata.get("target_id") or ""
                    return {
                        "agent": target_id,
                        "title": getattr(task, "title", "") or "",
                    }
                except Exception:
                    return None

            _maintenance_service = MemoryMaintenanceService(
                storage=services.get("execution_record_storage"),
                chunk_db=services.get("chunk_service"),
                knowledge_service=getattr(memory_service, "_knowledge_service", None) if memory_service else None,
                config=_maintenance_config,
                memory_service=memory_service,
                task_lookup=_task_lookup,
                review_context_window=_review_ctx_window,
            )
            services["maintenance_service"] = _maintenance_service
            # 注册维护触发器
            _maintenance_service.register_triggers()
            logger.debug("服务已: maintenance_service (enabled=%s)", _maintenance_config.get("enabled", False))
        except Exception as exc:
            logger.warning("创建 maintenance_service 服务失败: %s", exc)

        # ── 10. EventBus ─────────────────────────────────
        # 使用 core event_bus 全局单例，确保 TaskWorker 和事件发射者使用同一实例
        try:
            from src.core.event_bus import get_event_bus  # noqa: PLC0415

            event_bus = get_event_bus()
            services["event_bus"] = event_bus
            logger.debug("服务已: event_bus (core singleton)")
        except Exception as exc:
            logger.warning("创建 event_bus 服务失败: %s", exc, exc_info=True)

        # ── 11. TaskService（通过 EventBus 自动广播状态变更）───
        try:
            from tasks.service import TaskService  # noqa: PLC0415

            task_service = TaskService(event_bus=services.get("event_bus"))
            services["task_service"] = task_service
            logger.debug("服务已: task_service (event_bus=%s)", "enabled" if services.get("event_bus") else "disabled")

            # 注入 task_repository 到 IsolationManager，启用按 workspace 销毁容器
            # 用同步版本：asyncio.get_event_loop().run_until_complete() 在 Python 3.12+
            # 同步上下文里会抛 RuntimeError，导致注入失败、容器永不清理
            try:
                from isolation.manager import get_isolation_manager_sync  # noqa: PLC0415

                _manager = get_isolation_manager_sync()
                _manager.set_task_repository(task_service._storage)
            except Exception as _exc:
                logger.warning("注入 task_repository 到 IsolationManager 失败: %s", _exc)
        except Exception as exc:
            logger.warning("创建 task_service 服务失败: %s", exc, exc_info=True)

        # ── 12. TimerManager ─────────────────────────────
        try:
            from tasks.timer_manager import TimerManager  # noqa: PLC0415

            timer_manager = TimerManager.get_instance()
            services["timer_manager"] = timer_manager
            logger.debug("服务已: timer_manager")
        except Exception as exc:
            logger.warning("创建 timer_manager 失败: %s", exc)

        # ── 13. AgentRegistry ────────────────────────────
        if agent_registry is not None:
            services["agent_registry"] = agent_registry
            logger.debug("服务已: agent_registry")

        # ── 14. PipelineCheckpointManager + PipelineRecovery ──
        try:
            from infrastructure.checkpoint.pipeline_checkpoint import PipelineCheckpointManager  # noqa: PLC0415
            from infrastructure.checkpoint.recovery import PipelineRecovery  # noqa: PLC0415

            checkpoint_manager = PipelineCheckpointManager()
            recovery = PipelineRecovery(checkpoint_manager)
            services["checkpoint_manager"] = checkpoint_manager
            services["pipeline_recovery"] = recovery
            logger.debug("服务已: checkpoint_manager, pipeline_recovery")
        except Exception as exc:
            logger.warning("创建 checkpoint 服务失败: %s", exc)

        # ── 15. SessionService ───────────────────────────
        try:
            from infrastructure.session import SessionService  # noqa: PLC0415

            session_dir = self.project_root / "data" / "sessions"
            services["session_service"] = SessionService(session_dir=session_dir)
            logger.debug("服务已: session_service")
        except Exception as exc:
            logger.warning("创建 session_service 失败: %s", exc)

        # ── 16. ChannelGateway ───────────────────────────
        gateway = self.create_gateway()
        if gateway is not None:
            gateway.services = services
            services["channel_gateway"] = gateway
            logger.debug("服务已: channel_gateway")

        logger.debug("[STARTUP] 8-16.rest: %.2fs", _time.monotonic() - _t0)

        # ── 17. api_store（会话存储）─────────────────────
        # 注入 channels.api.memory_store.store 单例到 services，
        # 供 infrastructure 层通过 MemoryStoreProtocol 消费，
        # 解耦 infrastructure 对 channels 的逆向依赖。
        try:
            from channels.api.memory_store import store as _api_store  # noqa: PLC0415

            services["api_store"] = _api_store
            logger.debug("服务已: api_store")
        except Exception as exc:
            logger.warning("注入 api_store 失败: %s", exc)

        # ── 统一注册到 ServiceProvider ───────────────────
        self._register_to_service_provider(services)

        self.services = services
        return services

    @staticmethod
    def _register_to_service_provider(services: dict[str, Any]) -> None:
        """将服务字典注册到 ServiceProvider 单例。"""
        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            provider = get_service_provider()
            provider.register_services(services)
        except Exception as exc:
            logger.warning("注册服务到 ServiceProvider 失败: %s", exc)

    def create_gateway(self) -> Any | None:
        """创建 ChannelGateway 实例。"""
        try:
            from channels.gateway.channel_gateway import ChannelGateway  # noqa: PLC0415

            gateway = ChannelGateway()
            logger.debug("ChannelGateway 通过 Application 创建完成")
            return gateway
        except Exception as exc:
            logger.warning("创建 ChannelGateway 失败: %s", exc)
            return None

    def _register_basic_tools(self, registry: Any) -> None:
        """注册基础工具（无需依赖注入）。"""
        import datetime  # noqa: PLC0415
        import math as _math  # noqa: PLC0415

        from tools.types import Tool, ToolSource  # noqa: PLC0415

        def current_time(params: dict[str, Any]) -> str:
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

        def calculator(params: dict[str, Any]) -> str:
            expression = params.get("expression", "")
            if not expression:
                return "错误：未提供计算表达式"
            try:
                # 使用 simpleeval（AST 白名单求值器）替代裸 eval，
                # 从源头杜绝 __builtins__ 逃逸与 __class__ 链攻击。
                # 常量进 names，可调用函数进 functions。
                from simpleeval import simple_eval  # noqa: PLC0415

                constants = {"pi": _math.pi, "e": _math.e}
                functions = {
                    "abs": abs,
                    "round": round,
                    "min": min,
                    "max": max,
                    "pow": pow,
                    "sum": sum,
                    "sqrt": _math.sqrt,
                    "ceil": _math.ceil,
                    "floor": _math.floor,
                }
                result = simple_eval(expression, names=constants, functions=functions)
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

    @staticmethod
    def _register_media_providers(media_registry: Any) -> None:
        """根据已加载的配置实例化并注册媒体 Provider。

        遍历 MediaProviderRegistry 中的配置条目，按 class_name
        动态导入 Provider 类并实例化后注册到 registry。

        Args:
            media_registry: MediaProviderRegistry 实例（已加载配置）
        """
        from tools.media.base import MediaProviderConfig, MediaType  # noqa: PLC0415

        _PROVIDER_CLASS_MAP: dict[str, str] = {  # noqa: N806
            "ComfyUIProvider": "tools.media.providers.comfyui_provider",
            "EdgeTTSProvider": "tools.media.providers.edge_tts_provider",
            "MiniMaxImageProvider": "tools.media.providers.minimax_provider",
            "MiniMaxMusicProvider": "tools.media.providers.minimax_music_provider",
            "MiniMaxVideoProvider": "tools.media.providers.minimax_video_provider",
            "MiniMaxTTSProvider": "tools.media.providers.minimax_tts_provider",
        }

        _MEDIA_TYPE_MAP: dict[str, MediaType] = {  # noqa: N806
            "tts": MediaType.TTS,
            "image": MediaType.IMAGE,
            "video": MediaType.VIDEO,
            "music": MediaType.MUSIC,
        }

        for media_type_key, type_config in media_registry._configs.items():
            providers_conf = type_config.get("providers", {})
            for provider_name, provider_conf in providers_conf.items():
                class_name = provider_conf.get("class", "")
                module_path = _PROVIDER_CLASS_MAP.get(class_name)
                if not module_path:
                    logger.warning("[MediaRegistry] 未知 Provider 类: %s，跳过", class_name)
                    continue

                try:
                    import importlib  # noqa: PLC0415

                    module = importlib.import_module(module_path)
                    provider_cls = getattr(module, class_name)

                    _MEDIA_TYPE_MAP.get(media_type_key, MediaType.IMAGE)
                    raw_config = dict(provider_conf.get("config", {}))

                    if not raw_config.get("api_key"):
                        raw_config["api_key"] = Application._resolve_api_key(
                            class_name,
                            raw_config,
                        )

                    provider_config = MediaProviderConfig(
                        class_name=class_name,
                        enabled=provider_conf.get("enabled", True),
                        priority=provider_conf.get("priority", 99),
                        config=raw_config,
                    )

                    provider_instance = provider_cls(config=provider_config)
                    media_registry.register(provider_instance)
                    logger.info(
                        "[MediaRegistry] 已注册 Provider: %s (%s)",
                        provider_name,
                        class_name,
                    )
                except Exception as exc:
                    logger.warning(
                        "[MediaRegistry] 注册 Provider '%s' 失败: %s",
                        class_name,
                        exc,
                    )

    @staticmethod
    def _resolve_api_key(class_name: str, raw_config: dict[str, Any]) -> str:  # noqa: ARG004
        """解析 Provider 所需的 API Key。

        依次尝试以下来源：
        1. raw_config 中已有的 api_key（调用方已保证非空时不会进入此方法）
        2. LLM 配置中对应 provider 的 api_key（通过 ModelConfigManager）
        3. 环境变量 MINIMAX_API_KEY / OPENAI_API_KEY 等
        4. 空字符串

        Args:
            class_name: Provider 类名
            raw_config: Provider 原始配置字典

        Returns:
            解析到的 API Key 字符串
        """
        _LLM_PROVIDER_MAP: dict[str, str] = {  # noqa: N806
            "MiniMaxImageProvider": "minimax",
            "MiniMaxMusicProvider": "minimax",
            "MiniMaxVideoProvider": "minimax",
            "MiniMaxTTSProvider": "minimax",
        }
        _ENV_KEY_MAP: dict[str, str] = {  # noqa: N806
            "MiniMaxImageProvider": "MINIMAX_API_KEY",
            "MiniMaxMusicProvider": "MINIMAX_API_KEY",
            "MiniMaxVideoProvider": "MINIMAX_API_KEY",
            "MiniMaxTTSProvider": "MINIMAX_API_KEY",
        }

        llm_provider = _LLM_PROVIDER_MAP.get(class_name)
        if llm_provider:
            try:
                from config.models import get_model_config_loader  # noqa: PLC0415

                loader = get_model_config_loader()
                provider_conf = loader.get_provider_config(llm_provider)
                if provider_conf:
                    keys = provider_conf.get("keys", [])
                    if keys:
                        return keys[0].get("api_key", "")
                    return provider_conf.get("api_key", "")
            except Exception:
                pass

        env_key = _ENV_KEY_MAP.get(class_name)
        if env_key:
            import os  # noqa: PLC0415

            return os.environ.get(env_key, "")

        return ""

    @staticmethod
    def _build_embedding_fn() -> Any:
        """构建嵌入函数（异步，文本→向量）。"""
        try:
            from config.models import get_model_config_loader  # noqa: PLC0415

            loader = get_model_config_loader()
            embedding_cfg = loader._load_embedding_data()
            embeddings = embedding_cfg.get("embeddings", {})
            default_id = embedding_cfg.get("default_embedding", "")

            if default_id and default_id in embeddings:
                emb_info = embeddings[default_id]
                provider = emb_info.get("provider", "")

                if provider in ("openai", "openai_compatible"):
                    import os  # noqa: PLC0415

                    api_key = os.environ.get(emb_info.get("api_key_env", "OPENAI_API_KEY"), "")
                    base_url = emb_info.get("base_url")
                    model_name = emb_info.get("model", "text-embedding-3-small")

                    async def _openai_embed(text: str) -> list[float]:
                        try:
                            import httpx  # noqa: PLC0415

                            url = f"{base_url}/embeddings" if base_url else _DEFAULT_OPENAI_EMBEDDINGS_URL
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

        async def _zero_embed(text: str) -> list[float]:
            logger.warning("[EmbedFn] 嵌入服务不可用，返回零向量")
            return [0.0] * 1536

        return _zero_embed

    def create_pipeline_engine(
        self,
        pipeline_config: Any,
        plugin_registry: Any,
        services: dict[str, Any] | None = None,
    ) -> Any:
        """创建并注册 PipelineEngine 实例（经 EngineRegistry，I1）。

        所有引擎必须经注册表创建——外部不私自 new 引擎。本方法封装
        register_pipeline，下游拿到的 engine 已在注册表中。
        """
        from pipeline.registry import get_engine_registry  # noqa: PLC0415

        svc = services or self.services
        entry = get_engine_registry().register_pipeline(
            input_route_table=pipeline_config.input_route_table,
            output_route_table=pipeline_config.output_route_table,
            plugin_registry=plugin_registry,
            services=svc,
        )
        if entry is None:
            logger.warning("create_pipeline_engine: register_pipeline 失败（四件套缺失）")
            return None
        logger.debug("PipelineEngine 经注册表创建完成: pid=%s", entry.engine.pipeline_id[:12])
        return entry.engine

    def create_task_worker(
        self,
        pipeline_config: Any,
        plugin_registry: Any,
        services: dict[str, Any] | None = None,
    ) -> Any | None:
        """创建 TaskWorker 实例。

        当 services 中缺少 event_bus 或 task_service 时，尝试懒创建并回填，
        避免因初始化顺序或依赖缺失导致 TaskWorker 无法启动。
        """
        from infrastructure.task_worker import TaskWorker  # noqa: PLC0415

        svc = services if services is not None else self.services

        event_bus = svc.get("event_bus")
        if not event_bus:
            logger.warning("services 中缺少 event_bus，尝试懒创建")
            # 使用 core event_bus 全局单例
            try:
                from src.core.event_bus import get_event_bus  # noqa: PLC0415

                event_bus = get_event_bus()
                svc["event_bus"] = event_bus
                logger.debug("event_bus 懒创建成功 (core singleton)")
            except Exception as exc:
                logger.error("event_bus 懒创建失败: %s", exc)
                return None

        task_service = svc.get("task_service")
        if not task_service:
            logger.warning("services 中缺少 task_service，尝试懒创建")
            try:
                from tasks.service import TaskService  # noqa: PLC0415

                task_service = TaskService(event_bus=event_bus)
                svc["task_service"] = task_service
                logger.debug("task_service 懒创建成功")
            except Exception as exc:
                logger.error("task_service 懒创建失败: %s", exc)
                return None

        task_worker = TaskWorker(
            task_service=task_service,
            plugin_registry=plugin_registry,
            input_route_table=pipeline_config.input_route_table,
            output_route_table=pipeline_config.output_route_table,
            services=svc,
            event_bus=event_bus,
        )
        logger.debug("TaskWorker 通过 Application 创建完成")
        return task_worker

    def create_pipeline_factory(
        self,
        pipeline_config: Any,
        plugin_registry: Any,
    ) -> Callable[[], Any]:
        """创建 PipelineEngine 工厂函数（经 EngineRegistry，I1）。

        factory() 每次调用都经 register_pipeline 创建并注册新引擎，
        不再私自 new 野引擎。下游（evaluation 等）拿到的 engine 已在注册表。
        """
        from pipeline.registry import get_engine_registry  # noqa: PLC0415

        def factory() -> Any:
            entry = get_engine_registry().register_pipeline(
                input_route_table=pipeline_config.input_route_table,
                output_route_table=pipeline_config.output_route_table,
                plugin_registry=plugin_registry,
                services=self.services,
            )
            if entry is None:
                logger.warning("pipeline_factory: register_pipeline 失败（四件套缺失）")
                return None
            return entry.engine

        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            provider = get_service_provider()
            provider.register_services({"pipeline_factory": factory})
        except Exception as exc:
            logger.warning("注册 pipeline_factory 到 ServiceProvider 失败: %s", exc)

        return factory

    def get_service(self, name: str, *, default: Any = None) -> Any | None:
        """获取已注册的服务实例。"""
        return self.services.get(name, default)

    def _load_maintenance_config(self) -> dict[str, Any]:
        """从 memory_storage.yaml 加载维护配置。

        Returns:
            维护配置字典
        """
        config_path = self.project_root / "config" / "system" / "memory_storage.yaml"
        if not config_path.exists():
            return {}
        try:
            import yaml  # noqa: PLC0415

            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            return data.get("maintenance", {}) if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("加载维护配置失败: %s", exc)
            return {}

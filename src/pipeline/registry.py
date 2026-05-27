"""插件与管道路由注册表。

PluginRegistry 管理管道内的插件注册，
PipelineRegistry 提供跨管道路由能力（平权式），
EngineRegistry 统一管理引擎实例注册与查找（替代三级查找）。

平权路由：管道之间平权，路由只是状态转移（A 的 state → B）。
等待策略由插件决定，不在框架硬编码。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, TYPE_CHECKING

from pipeline.plugin import (
    ICorePlugin,
    IOutputPlugin,
    IPlugin,
)
from pipeline.types import StateKeys

if TYPE_CHECKING:
    from pipeline.config_store import PipelineConfigStore
    from pipeline.engine import PipelineEngine
    from pipeline.event_bus import EventBus
    from pipeline.stream_bridge import PipelineStreamBridge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 引擎注册条目与统一注册表（替代三级查找 + _pipeline_thread_map）
# ---------------------------------------------------------------------------

@dataclass
class PipelineEntry:
    """管道引擎注册条目。

    封装 engine + bridge + thread_id + tags 的关联关系，
    替代原先分散在 ServiceProvider 字符串 key、_GLOBAL_SUSPENDED_ENGINES、
    _pipeline_thread_map 中的多套映射。

    Attributes:
        engine: PipelineEngine 实例
        bridge: 当前活跃的 PipelineStreamBridge（可为 None）
        thread_id: 对应的 WebSocket thread_id
        tags: 通用关联标签（可扩展），如 {"task_id": "xxx"}
        created_at: 条目创建时间
    """

    engine: Any  # PipelineEngine（用 Any 避免循环导入）
    bridge: Any | None = None  # PipelineStreamBridge | None
    drain_task: Any | None = None  # asyncio.Task | None — 后台 drain_loop 任务引用
    thread_id: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


MAX_TAGS_PER_PIPELINE = 8


class EngineRegistry:
    """统一管道引擎注册表（单例）。

    替代原先的三级查找（ServiceProvider.__running_engine_ +
    _GLOBAL_SUSPENDED_ENGINES + ServiceProvider.__suspended_engine_）和
    _pipeline_thread_map 映射表，提供统一的引擎注册、查找、bridge 管理接口。

    查找逻辑从"查三个地方判断状态"变为一次 registry.get() 调用，
    引擎状态从 entry.engine.is_suspended 属性读取。
    """

    _instance: ClassVar[EngineRegistry | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self._engines: dict[str, PipelineEntry] = {}

    @classmethod
    def get_instance(cls) -> EngineRegistry:
        """获取 EngineRegistry 单例。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def register(
        self,
        pipeline_id: str,
        engine: Any,
        thread_id: str = "",
        tags: dict[str, str] | None = None,
    ) -> PipelineEntry:
        """注册管道引擎实例。

        如果同 pipeline_id 已有 entry，保留其 bridge 和 drain_task，
        避免引擎 run() 开头 unregister + _run_loop register 的过程中
        丢失已绑定的流式桥接。

        Args:
            pipeline_id: 管道 ID
            engine: PipelineEngine 实例
            thread_id: 关联的 WebSocket thread_id
            tags: 关联标签，如 {"task_id": "xxx"}

        Returns:
            注册的 PipelineEntry
        """
        existing = self._engines.get(pipeline_id)
        entry = PipelineEntry(
            engine=engine,
            thread_id=thread_id,
            tags=tags or {},
        )
        if existing is not None:
            entry.bridge = existing.bridge
            entry.drain_task = existing.drain_task
        self._engines[pipeline_id] = entry
        logger.info(
            "[EngineRegistry] 注册引擎: pipeline=%s thread=%s is_suspended=%s total=%d preserved_bridge=%s",
            pipeline_id[:12], thread_id[:12] if thread_id else "",
            getattr(engine, "is_suspended", "?"),
            len(self._engines),
            entry.bridge is not None,
        )
        return entry

    def register_pipeline(
        self,
        *,
        pipeline_id: str = "",
        thread_id: str = "",
        tags: dict[str, str] | None = None,
        input_route_table: Any = None,
        output_route_table: Any = None,
        plugin_registry: Any = None,
        services: dict[str, Any] | None = None,
    ) -> PipelineEntry | None:
        """创建管道引擎并注册到 Registry。

        统一的引擎创建入口，替代 task_executor._create_pipeline_engine、
        PipelineContext.get_or_create_engine、message_bus 中的直接构造。
        内部创建 PipelineEngine 实例并注册。

        Args:
            pipeline_id: 指定 ID，空则自动生成
            thread_id: WebSocket 线程 ID
            tags: 关联标签，如 {"task_id": "xxx", "mode": "interactive"}
            input_route_table: 输入路由表
            output_route_table: 输出路由表
            plugin_registry: 插件注册表
            services: 服务容器

        Returns:
            PipelineEntry，创建失败返回 None

        Raises:
            ValueError: 标签数量超过 MAX_TAGS_PER_PIPELINE
        """
        if tags and len(tags) > MAX_TAGS_PER_PIPELINE:
            raise ValueError(
                f"标签数量超过限制: {len(tags)} > {MAX_TAGS_PER_PIPELINE}"
            )

        if pipeline_id and pipeline_id in self._engines:
            return self._engines[pipeline_id]

        if not input_route_table or not output_route_table or not plugin_registry:
            logger.warning(
                "[EngineRegistry] register_pipeline FAILED: irt=%s ort=%s pr=%s pid=%s",
                bool(input_route_table), bool(output_route_table), bool(plugin_registry),
                pipeline_id[:12] if pipeline_id else "",
            )
            return None

        from pipeline.engine import PipelineEngine

        svc = services or {}
        checkpoint_mgr = svc.get("checkpoint_manager") if svc else None

        engine = PipelineEngine(
            input_route_table=input_route_table,
            output_route_table=output_route_table,
            plugin_registry=plugin_registry,
            services=svc,
            checkpoint_manager=checkpoint_mgr,
        )

        if pipeline_id:
            engine.pipeline_id = pipeline_id

        logger.info("[EngineRegistry] register_pipeline: pid=%s tid=%s", engine.pipeline_id[:12], thread_id[:12] if thread_id else "")
        return self.register(engine.pipeline_id, engine, thread_id=thread_id, tags=tags)

    def revive_pipeline(
        self,
        pipeline_id: str,
        *,
        thread_id: str = "",
        tags: dict[str, str] | None = None,
        input_route_table: Any = None,
        output_route_table: Any = None,
        plugin_registry: Any = None,
        services: dict[str, Any] | None = None,
    ) -> PipelineEntry | None:
        """从历史记录恢复管道引擎。

        当引擎不在 Registry 中时，尝试从持久化存储加载历史记录，
        创建新 PipelineEngine 并注册。用于重启后恢复、超时清理后恢复。

        Args:
            pipeline_id: 管道 ID
            thread_id: WebSocket 线程 ID
            tags: 关联标签
            input_route_table: 输入路由表
            output_route_table: 输出路由表
            plugin_registry: 插件注册表
            services: 服务容器

        Returns:
            PipelineEntry，恢复失败返回 None
        """
        if pipeline_id in self._engines:
            return self._engines[pipeline_id]

        if not input_route_table or not output_route_table or not plugin_registry:
            logger.warning(
                "[EngineRegistry] revive_pipeline 缺少必要参数: pipeline=%s",
                pipeline_id[:12],
            )
            return None

        from pipeline.engine import PipelineEngine

        svc = services or {}
        checkpoint_mgr = svc.get("checkpoint_manager") if svc else None

        engine = PipelineEngine(
            input_route_table=input_route_table,
            output_route_table=output_route_table,
            plugin_registry=plugin_registry,
            services=svc,
            checkpoint_manager=checkpoint_mgr,
        )
        engine.pipeline_id = pipeline_id

        logger.info(
            "[EngineRegistry] 管道恢复: 创建新引擎 pipeline=%s thread=%s",
            pipeline_id[:12], thread_id[:12] if thread_id else "",
        )

        return self.register(pipeline_id, engine, thread_id=thread_id, tags=tags)

    def unregister(self, pipeline_id: str) -> PipelineEntry | None:
        """注销管道引擎实例。

        Args:
            pipeline_id: 管道 ID

        Returns:
            被移除的 PipelineEntry，不存在返回 None
        """
        entry = self._engines.pop(pipeline_id, None)
        if entry is not None:
            logger.info(
                "[EngineRegistry] 注销引擎: pipeline=%s total=%d",
                pipeline_id[:12], len(self._engines),
            )
        return entry

    def get(self, pipeline_id: str) -> PipelineEntry | None:
        """根据 pipeline_id 查找管道条目。

        替代原先的三级查找逻辑，一次调用即可获取引擎和关联数据。
        引擎状态通过 entry.engine.is_suspended 属性判断。

        Args:
            pipeline_id: 管道 ID

        Returns:
            PipelineEntry 实例，不存在返回 None
        """
        return self._engines.get(pipeline_id)

    def get_bridge(self, pipeline_id: str) -> Any | None:
        """获取管道的活跃 bridge。

        Args:
            pipeline_id: 管道 ID

        Returns:
            PipelineStreamBridge 实例，不存在返回 None
        """
        entry = self._engines.get(pipeline_id)
        return entry.bridge if entry else None

    def set_bridge(self, pipeline_id: str, bridge: Any) -> None:
        """设置管道的活跃 bridge。

        Args:
            pipeline_id: 管道 ID
            bridge: PipelineStreamBridge 实例
        """
        entry = self._engines.get(pipeline_id)
        if entry:
            entry.bridge = bridge

    def cancel_drain_task(self, pipeline_id: str) -> None:
        """取消管道的后台 drain_loop 任务。

        先通过 bridge.stop() 发送哨兵值立即终止 drain_loop，
        再取消 asyncio.Task 确保协程退出。
        供 stop_generation 等外部场景即时停止流式输出。

        Args:
            pipeline_id: 管道 ID
        """
        entry = self._engines.get(pipeline_id)
        if entry is None:
            return
        if entry.bridge is not None:
            entry.bridge.stop()
        if entry.drain_task is not None and not entry.drain_task.done():
            entry.drain_task.cancel()
        entry.drain_task = None

    def ensure_bridge(
        self,
        pipeline_id: str,
        sink: Any,
        *,
        auto_start_drain: bool = False,
        engine: Any = None,
        engine_task: Any = None,
    ) -> Any | None:
        """确保管道有活跃的 bridge，无则创建，有则复用并 reset。

        统一管理 bridge 的生命周期，消除 message_bus 和 task_executor
        中重复的 bridge 创建/复用逻辑。

        Args:
            pipeline_id: 管道 ID
            sink: IOutputSink 实例
            auto_start_drain: 是否自动启动后台 drain_loop
            engine: PipelineEngine 实例（auto_start_drain=True 时需要）
            engine_task: 引擎异步 Task（可选，用于 drain_loop 跟踪）

        Returns:
            PipelineStreamBridge 实例，失败返回 None
        """
        entry = self._engines.get(pipeline_id)
        if not entry:
            return None

        bridge = entry.bridge
        if bridge is None:
            from pipeline.stream_bridge import PipelineStreamBridge
            bridge = PipelineStreamBridge(
                pipeline_id=pipeline_id,
                output_sink=sink,
            )
            entry.bridge = bridge
        else:
            # 停止旧 drain task：先发哨兵值立即终止 drain_loop，
            # 再取消 asyncio.Task 确保协程退出，避免双消费者竞争。
            if entry.drain_task is not None and not entry.drain_task.done():
                bridge.stop()
                entry.drain_task.cancel()
                entry.drain_task = None
            bridge.reset_for_new_turn(
                message_id=f"msg_{uuid.uuid4().hex[:12]}"
            )

        if auto_start_drain and engine is not None:
            from pipeline.message_bus import _start_bg_drain
            _start_bg_drain(pipeline_id, bridge, engine, engine_task)

        return bridge

    def update_thread_id(self, pipeline_id: str, thread_id: str) -> None:
        """更新管道的 thread_id 映射。

        替代 ws_handler._pipeline_thread_map 的注册功能。

        Args:
            pipeline_id: 管道 ID
            thread_id: WebSocket thread_id
        """
        entry = self._engines.get(pipeline_id)
        if entry:
            entry.thread_id = thread_id

    def get_thread_id(self, pipeline_id: str) -> str:
        """根据 pipeline_id 查找对应的 ws_thread_id。

        替代 ws_handler._pipeline_thread_map 的查询功能。

        Args:
            pipeline_id: 管道 ID

        Returns:
            对应的 ws_thread_id，未找到返回空字符串
        """
        entry = self._engines.get(pipeline_id)
        return entry.thread_id if entry else ""

    def find_by_tag(self, *args: str) -> list[PipelineEntry]:
        """按关联标签查找管道条目。

        支持单标签和多标签查询：
        - find_by_tag("task_id", "task-1")
        - find_by_tag("task_id", "task-1", "mode", "interactive")

        Args:
            args: key-value 对，必须成对出现。

        Returns:
            匹配的 PipelineEntry 列表

        Raises:
            ValueError: 参数数量不为偶数
        """
        if len(args) % 2 != 0:
            raise ValueError("find_by_tag 参数必须为 key-value 对，如 find_by_tag('k', 'v') 或 find_by_tag('k1', 'v1', 'k2', 'v2')")
        pairs = [(args[i], args[i + 1]) for i in range(0, len(args), 2)]
        return [
            entry for entry in self._engines.values()
            if all(entry.tags.get(k) == v for k, v in pairs)
        ]

    def find_by_thread_id(self, thread_id: str) -> list[PipelineEntry]:
        """根据 thread_id 反查所有关联的管道条目。

        Args:
            thread_id: WebSocket thread_id

        Returns:
            匹配的 PipelineEntry 列表
        """
        return [
            entry for entry in self._engines.values()
            if entry.thread_id == thread_id
        ]

    def all_entries(self) -> dict[str, PipelineEntry]:
        """返回所有已注册的管道条目（只读快照）。"""
        return dict(self._engines)


def get_engine_registry() -> EngineRegistry:
    """获取全局 EngineRegistry 单例的便捷函数。"""
    return EngineRegistry.get_instance()


@dataclass
class RoutingRecord:
    """路由记录数据类。

    记录一次跨管道路由的完整信息，供查询和追踪使用。

    Attributes:
        source_id: 源管道 ID
        target_id: 目标管道 ID（路由后生成的新管道）
        target: 目标管道配置标识
        timestamp: 路由发生的时间戳
        status: 路由状态（pending / completed / failed）
    """

    source_id: str
    target_id: str
    target: str
    timestamp: float = field(default_factory=time.monotonic)
    status: str = "pending"


class PluginRegistry:
    """管道内插件注册表。

    管理插件实例的注册、查找和分类检索。

    Attributes:
        _plugins: 名称到插件实例的映射
        _core_plugins: core_type 到核心插件实例的映射
    """

    def __init__(self) -> None:
        self._plugins: dict[str, IPlugin] = {}
        self._core_plugins: dict[str, ICorePlugin] = {}
        # 缓存 output_plugins 列表，避免每轮迭代全量扫描
        self._output_plugins_cache: list[IOutputPlugin] | None = None

    def register(self, plugin: IPlugin) -> None:
        """注册一个插件实例。

        根据插件类型自动分类：IInputPlugin、ICorePlugin、IOutputPlugin
        分别存入对应映射表。

        Args:
            plugin: 插件实例
        """
        self._plugins[plugin.name] = plugin
        self._output_plugins_cache = None  # 缓存失效
        if isinstance(plugin, ICorePlugin):
            # Core 插件按 core_type 注册，但 register() 使用 plugin.name 作为 key
            # 与 register_core(name, plugin) 的 name 语义不同，发出警告
            logger.warning(
                "Core plugin '%s' registered via register(), consider using register_core() for explicit core_type mapping",
                plugin.name,
            )
            self._core_plugins[plugin.name] = plugin
        logger.debug("Plugin registered: %s (type=%s)", plugin.name, type(plugin).__name__)

    def register_core(self, name: str, plugin: ICorePlugin) -> None:
        """注册核心插件。

        使用自定义 name 作为 core_type 键。

        Args:
            name: 核心类型标识（如 llm_call, tool_execute）
            plugin: 核心插件实例
        """
        self._core_plugins[name] = plugin
        self._plugins[plugin.name] = plugin
        self._output_plugins_cache = None  # 缓存失效
        logger.debug("Core plugin registered: name=%s, plugin=%s", name, plugin.name)

    def get(self, name: str) -> IPlugin | None:
        """按名称获取插件实例。

        Args:
            name: 插件名称

        Returns:
            插件实例，不存在时返回 None
        """
        return self._plugins.get(name)

    def get_core(self, core_type: str) -> ICorePlugin | None:
        """按核心类型获取核心插件实例。

        Args:
            core_type: 核心类型标识（如 llm_call, tool_execute）

        Returns:
            核心插件实例，不存在时返回 None
        """
        return self._core_plugins.get(core_type)

    def get_output_plugins(
        self, core_type: str | None = None
    ) -> list[IOutputPlugin]:
        """获取所有输出插件列表（带缓存）。

        core_type 参数保留签名兼容性但不再用于过滤。
        Output 插件自身通过 execute() 内部逻辑判断是否需要处理。

        Args:
            core_type: 核心类型标识（保留签名兼容，不再用于过滤）

        Returns:
            所有输出插件列表，按优先级排序
        """
        if self._output_plugins_cache is not None:
            return self._output_plugins_cache
        output_plugins: list[IOutputPlugin] = []
        for plugin in self._plugins.values():
            if isinstance(plugin, IOutputPlugin):
                output_plugins.append(plugin)
        self._output_plugins_cache = sorted(output_plugins, key=lambda p: p.priority)
        return self._output_plugins_cache

    def fork(self) -> PluginRegistry:
        """创建插件注册表的深拷贝副本。

        每个 PipelineEngine 应持有独立的 PluginRegistry 实例，
        避免多个管道共享同一插件实例导致状态互相污染（如 TrackPlugin
        的 _record_count 在父子管道间累积）。

        对于有状态的插件（如 TrackPlugin），通过 type(plugin)(config)
        创建全新实例；对于无状态插件，直接复用原实例。

        Returns:
            全新的 PluginRegistry 实例，包含独立的新插件实例
        """
        new_registry = PluginRegistry()

        core_name_to_plugin_name: dict[str, str] = {}
        for core_name, plugin in self._core_plugins.items():
            core_name_to_plugin_name[core_name] = plugin.name

        for name, plugin in self._plugins.items():
            new_instance = plugin
            if hasattr(plugin, "_config"):
                try:
                    # 保留关键属性（adapter、router 等），避免 fork 后丢失
                    kwargs: dict[str, Any] = {"config": copy.deepcopy(plugin._config)}
                    if hasattr(plugin, "_adapter"):
                        kwargs["adapter"] = plugin._adapter
                    elif hasattr(plugin, "_router"):
                        kwargs["router"] = plugin._router
                    new_instance = type(plugin)(**kwargs)
                    # 恢复 fork 无法传递的运行时状态
                    for attr in ("_tools", "_tool_registry"):
                        if hasattr(plugin, attr):
                            setattr(new_instance, attr, getattr(plugin, attr))
                except Exception:
                    logger.debug(
                        "PluginRegistry.fork: 无法重建插件 %s, 复用原实例", name
                    )
            new_registry._plugins[name] = new_instance

        for core_name, plugin_name in core_name_to_plugin_name.items():
            new_registry._core_plugins[core_name] = new_registry._plugins[plugin_name]

            orig = self._plugins.get(plugin_name)
            forked = new_registry._plugins.get(plugin_name)
            if hasattr(orig, "_tools") and hasattr(forked, "_tools"):
                forked._tools = dict(orig._tools)
            if hasattr(orig, "_tool_registry") and hasattr(forked, "_tool_registry"):
                forked._tool_registry = orig._tool_registry

        return new_registry

    def replace(self, name: str, new_plugin: IPlugin) -> IPlugin | None:
        """替换已注册的插件。

        注销旧插件并以指定名称注册新插件，保留核心插件映射的同步。
        新插件将用 name 作为注册键（而非 new_plugin.name），
        确保替换后通过原名称仍可找到新插件。

        Args:
            name: 要替换的插件名称（同时作为新插件的注册键）
            new_plugin: 新插件实例

        Returns:
            被替换的旧插件实例，不存在时返回 None
        """
        old_plugin = self._plugins.pop(name, None)
        if old_plugin is not None:
            if isinstance(old_plugin, ICorePlugin) and name in self._core_plugins:
                del self._core_plugins[name]
        # 用指定的 name 作为键注册，而非 new_plugin.name
        self._plugins[name] = new_plugin
        self._output_plugins_cache = None  # 缓存失效
        if isinstance(new_plugin, ICorePlugin):
            self._core_plugins[name] = new_plugin
        try:
            new_name = new_plugin.name
        except Exception:
            new_name = "<error>"
        logger.info(
            "Plugin replaced: %s → %s", name, new_name,
        )
        return old_plugin

    def list_plugins(self) -> list[str]:
        """列出所有已注册插件的名称。

        Returns:
            插件名称列表
        """
        return list(self._plugins.keys())


class PipelineRegistry:
    """跨管道路由注册表（平权式）。

    管道之间平权，路由只是状态转移（A 的 state → B）。
    等待策略由插件决定，不在框架硬编码。

    提供：
    - 旧版 submit/route_to/release（兼容 M1 测试）
    - 新版 route()：创建目标管道并提交执行，返回新管道 ID
    - get_result() / get_routed_from() / get_routed_to() 查询方法
    - routing_log 路由记录追踪

    Attributes:
        _pipelines: 管道 ID 到管道配置的映射（M1 兼容）
        _counter: 管道 ID 自增计数器
        _results: 管道 ID 到最终状态的映射
        _routing_log: 路由记录列表
        _config_store: 管道配置存储（可选）
        _scheduler: 调度器实例（可选）
        _event_bus: 事件总线（可选）
    """

    def __init__(
        self,
        config_store: PipelineConfigStore | None = None,
        scheduler: Any | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._pipelines: dict[str, dict[str, Any]] = {}
        self._counter: int = 0
        self._results: dict[str, dict[str, Any]] = {}
        self._routing_log: list[RoutingRecord] = []
        self._config_store = config_store
        self._scheduler = scheduler
        self._event_bus = event_bus

    def register_config(self, pipeline_id: str, config: Any) -> None:
        """注册管道配置到 config_store。

        Args:
            pipeline_id: 管道配置标识
            config: PipelineConfig 实例
        """
        if self._config_store is not None:
            self._config_store.register(pipeline_id, config)
            logger.info("Pipeline config registered via registry: %s", pipeline_id)
        else:
            logger.warning("No config_store available, cannot register config: %s", pipeline_id)

    async def route(
        self,
        source_id: str,
        target: str,
        state: dict[str, Any],
    ) -> str:
        """路由到目标管道并启动执行。

        从 config_store 获取目标管道配置，构建子管道 initial_state，
        提交执行（调度器或 asyncio.create_task），记录路由日志。

        Args:
            source_id: 源管道 ID
            target: 目标管道配置标识
            state: 当前管道状态字典

        Returns:
            新管道实例 ID

        Raises:
            ValueError: 目标管道配置不存在时抛出
        """
        # 从 config_store 获取配置
        if self._config_store is not None:
            config = self._config_store.get(target)
            if config is None:
                raise ValueError(f"Pipeline config not found: {target}")
        else:
            config = None

        # 构建子管道 initial_state（白名单提取）
        child_state = self._build_child_state(state, target)

        # 创建新管道实例
        self._counter += 1
        pipeline_id = f"pipeline-{self._counter}"
        self._pipelines[pipeline_id] = {
            "target": target,
            "config": config,
            "parent_id": source_id,
            "status": "running",
            "initial_state": child_state,
        }
        logger.info(
            "Pipeline routed: source=%s → target=%s (pipeline_id=%s)",
            source_id, target, pipeline_id,
        )

        # 记录路由日志
        record = RoutingRecord(
            source_id=source_id,
            target_id=pipeline_id,
            target=target,
            status="pending",
        )
        self._routing_log.append(record)

        # 提交执行：调度器或 asyncio.create_task
        if self._scheduler is not None:
            await self._scheduler.submit(
                item={"pipeline_id": pipeline_id, "config": config, "initial_state": child_state},
                priority=5,
            )
            logger.debug("Pipeline submitted to scheduler: %s", pipeline_id)
        else:
            asyncio.create_task(self._run_child_pipeline(pipeline_id, config, child_state))
            logger.debug("Pipeline started as asyncio task: %s", pipeline_id)

        return pipeline_id

    def _build_child_state(
        self, parent_state: dict[str, Any], target: str
    ) -> dict[str, Any]:
        """从父管道状态白名单提取子管道初始状态。

        白名单字段：user_input, session_id, task_id, agent_level, core_type,
                    delegated_task, target_pipeline

        Args:
            parent_state: 父管道状态字典
            target: 目标管道配置标识

        Returns:
            子管道初始状态字典
        """
        allowed_keys = {
            StateKeys.SESSION_ID,
            StateKeys.TASK_ID,
            StateKeys.AGENT_LEVEL,
            StateKeys.CORE_TYPE,
            "user_input",
            "delegated_task",
            "task_title",
            "task_description",
            "task_status",
            "reject_count",
            "acceptance_criteria",
            "reject_reason",
            "task_reminder",
        }
        child_state: dict[str, Any] = {}
        for key in allowed_keys:
            if key in parent_state:
                child_state[key] = parent_state[key]
        child_state["target_pipeline"] = target
        child_state[StateKeys.PIPELINE_ID] = f"pipeline-{self._counter}"
        return child_state

    async def _run_child_pipeline(
        self,
        pipeline_id: str,
        config: Any,
        initial_state: dict[str, Any],
    ) -> None:
        """执行子管道并将结果存入 _results，emit 完成事件。

        Args:
            pipeline_id: 管道实例 ID
            config: 管道配置（PipelineConfig 或 None）
            initial_state: 子管道初始状态
        """
        try:
            # 子管道的实际执行需要 PipelineEngine，
            # 此处仅记录完成状态，真实执行由外部调度器或测试 Mock 驱动
            # 在生产环境中，config 会包含构建 PipelineEngine 所需信息
            result: dict[str, Any] = {
                "pipeline_id": pipeline_id,
                "status": "completed",
                "initial_state": initial_state,
            }
            self._results[pipeline_id] = result

            # 更新路由记录状态
            for record in self._routing_log:
                if record.target_id == pipeline_id:
                    record.status = "completed"
                    break

            # emit 完成事件
            if self._event_bus is not None:
                await self._event_bus.emit(
                    "pipeline_completed",
                    {"pipeline_id": pipeline_id, "status": "completed", "result": result},
                )

            logger.info("Child pipeline completed: %s", pipeline_id)

        except Exception as exc:
            logger.error("Child pipeline failed: %s, error: %s", pipeline_id, exc)
            self._results[pipeline_id] = {
                "pipeline_id": pipeline_id,
                "status": "failed",
                "error": str(exc),
            }

            # 更新路由记录状态
            for record in self._routing_log:
                if record.target_id == pipeline_id:
                    record.status = "failed"
                    break

            # emit 完成事件
            if self._event_bus is not None:
                await self._event_bus.emit(
                    "pipeline_completed",
                    {"pipeline_id": pipeline_id, "status": "failed", "error": str(exc)},
                )

    def get_result(self, pipeline_id: str) -> dict[str, Any] | None:
        """获取管道执行结果。

        Args:
            pipeline_id: 管道实例 ID

        Returns:
            管道最终状态字典，未完成或不存在时返回 None
        """
        return self._results.get(pipeline_id)

    def get_routed_from(self, pipeline_id: str) -> list[RoutingRecord]:
        """查询路由到指定管道的所有路由记录。

        Args:
            pipeline_id: 目标管道 ID

        Returns:
            以该管道为目标的路由记录列表
        """
        return [r for r in self._routing_log if r.target_id == pipeline_id]

    def get_routed_to(self, pipeline_id: str) -> list[RoutingRecord]:
        """查询从指定管道路由出去的所有路由记录。

        Args:
            pipeline_id: 源管道 ID

        Returns:
            从该管道出发的路由记录列表
        """
        return [r for r in self._routing_log if r.source_id == pipeline_id]

    # --- M1 兼容方法 ---

    def submit(
        self, target: str, config: dict[str, Any], parent_id: str | None = None
    ) -> str:
        """提交新管道实例（M1 兼容）。

        Args:
            target: 管道目标标识
            config: 管道配置
            parent_id: 父管道 ID（可选）

        Returns:
            新管道实例 ID
        """
        self._counter += 1
        pipeline_id = f"pipeline-{self._counter}"
        self._pipelines[pipeline_id] = {
            "target": target,
            "config": config,
            "parent_id": parent_id,
            "status": "pending",
        }
        logger.info("Pipeline submitted: id=%s, target=%s", pipeline_id, target)
        return pipeline_id

    def route_to(self, target: str, context: dict[str, Any]) -> str:
        """路由到目标管道（M1 兼容）。

        Args:
            target: 目标管道标识
            context: 路由上下文

        Returns:
            目标管道 ID
        """
        pipeline_id = self.submit(target, config=context)
        logger.info("Routed to target: %s (pipeline_id=%s)", target, pipeline_id)
        return pipeline_id

    def release(self, pipeline_id: str) -> None:
        """释放管道实例（M1 兼容）。

        Args:
            pipeline_id: 管道 ID
        """
        if pipeline_id in self._pipelines:
            self._pipelines[pipeline_id]["status"] = "released"
            logger.info("Pipeline released: id=%s", pipeline_id)
        else:
            logger.warning("Pipeline not found for release: id=%s", pipeline_id)

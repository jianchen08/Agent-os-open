"""统一管道引擎注册表。"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, ClassVar

from pipeline.pipeline_entry import MAX_TAGS_PER_PIPELINE, PipelineEntry

logger = logging.getLogger(__name__)


class EngineRegistry:
    """统一管道引擎注册表（单例）。"""

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
        """注册管道引擎实例。"""

        existing = self._engines.get(pipeline_id)

        _effective_thread_id = thread_id

        if not _effective_thread_id and existing is not None and existing.thread_id:
            _effective_thread_id = existing.thread_id

        # tags 合并而非覆盖：新 tags 补充到已有 tags 上，避免丢 agent_id

        _merged_tags = dict(existing.tags) if existing else {}

        if tags:
            _merged_tags.update(tags)

        entry = PipelineEntry(
            engine=engine,
            thread_id=_effective_thread_id,
            tags=_merged_tags,
        )

        if existing is not None:
            entry.bridge = existing.bridge

            entry.drain_task = existing.drain_task

            entry.engine_task = existing.engine_task

            entry.init_sequence(existing.msg_sequence)

        else:
            self._resume_entry_sequence(entry, pipeline_id)

        self._engines[pipeline_id] = entry

        logger.info(
            "[EngineRegistry] 注册引擎: pipeline=%s thread=%s is_suspended=%s total=%d preserved_bridge=%s",
            pipeline_id[:12],
            thread_id[:12] if thread_id else "",
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
        """创建管道引擎并注册到 Registry。"""

        if tags and len(tags) > MAX_TAGS_PER_PIPELINE:
            raise ValueError(f"标签数量超过限制: {len(tags)} > {MAX_TAGS_PER_PIPELINE}")

        if pipeline_id and pipeline_id in self._engines:
            entry = self._engines[pipeline_id]

            if services and hasattr(entry.engine, "update_services"):
                entry.engine.update_services(services)

            return entry

        if not input_route_table or not output_route_table or not plugin_registry:
            logger.warning(
                "[EngineRegistry] register_pipeline FAILED: irt=%s ort=%s pr=%s pid=%s",
                bool(input_route_table),
                bool(output_route_table),
                bool(plugin_registry),
                pipeline_id[:12] if pipeline_id else "",
            )

            return None

        from pipeline.engine import PipelineEngine  # noqa: PLC0415

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

        logger.info(
            "[EngineRegistry] register_pipeline: pid=%s tid=%s tags=%s",
            engine.pipeline_id[:12],
            thread_id[:12] if thread_id else "",
            tags,
        )

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
        """从历史记录恢复管道引擎。"""

        if pipeline_id in self._engines:
            return self._engines[pipeline_id]

        if not input_route_table or not output_route_table or not plugin_registry:
            logger.warning(
                "[EngineRegistry] revive_pipeline 缺少必要参数: pipeline=%s",
                pipeline_id[:12],
            )

            return None

        from pipeline.engine import PipelineEngine  # noqa: PLC0415

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
            pipeline_id[:12],
            thread_id[:12] if thread_id else "",
        )

        return self.register(pipeline_id, engine, thread_id=thread_id, tags=tags)

    def unregister(self, pipeline_id: str) -> PipelineEntry | None:
        """注销管道引擎实例。"""

        entry = self._engines.pop(pipeline_id, None)

        if entry is not None:
            logger.info(
                "[EngineRegistry] 注销引擎: pipeline=%s total=%d",
                pipeline_id[:12],
                len(self._engines),
            )

        return entry

    def get(self, pipeline_id: str) -> PipelineEntry | None:
        """根据 pipeline_id 查找管道条目。"""

        return self._engines.get(pipeline_id)

    def _resume_entry_sequence(self, entry: PipelineEntry, pipeline_id: str) -> None:
        """从 DB 已有记录续接 PipelineEntry 的共享 sequence 计数器。"""

        try:
            from infrastructure.service_provider import ServiceProvider  # noqa: PLC0415

            provider = ServiceProvider()

            storage = provider.get("execution_record_storage")

            if storage and hasattr(storage, "list_by_pipeline"):
                existing = storage.list_by_pipeline(pipeline_id)[0]

                if existing:
                    max_seq = max(r.sequence for r in existing)

                    entry.init_sequence(max_seq)

        except Exception:
            logger.debug(
                "_resume_entry_sequence: 续接失败 pipeline=%s",
                pipeline_id[:12],
                exc_info=True,
            )

    def get_bridge(self, pipeline_id: str) -> Any | None:
        """获取管道的活跃 bridge。"""

        entry = self._engines.get(pipeline_id)

        return entry.bridge if entry else None

    def set_bridge(self, pipeline_id: str, bridge: Any) -> None:
        """设置管道的活跃 bridge。"""

        entry = self._engines.get(pipeline_id)

        if entry:
            entry.bridge = bridge

    def cancel_drain_task(self, pipeline_id: str) -> None:
        """取消管道的后台 drain_loop 任务及引擎任务。"""

        entry = self._engines.get(pipeline_id)

        if entry is None:
            return

        if entry.bridge is not None:
            entry.bridge.stop()

        if entry.drain_task is not None and not entry.drain_task.done():
            entry.drain_task.cancel()

        entry.drain_task = None

        # engine_task 是 asyncio.Task（主循环），可以真正取消。

        if entry.engine_task is not None and not entry.engine_task.done():
            entry.engine_task.cancel()

            logger.info("[EngineRegistry] 已取消引擎任务: pipeline=%s", pipeline_id[:12])

    def ensure_bridge(
        self,
        pipeline_id: str,
        sink: Any,
        *,
        auto_start_drain: bool = False,
        engine: Any = None,
        engine_task: Any = None,
        message_id: str | None = None,
    ) -> Any | None:
        """确保管道有活跃的 bridge，无则创建，有则复用并 reset。"""

        entry = self._engines.get(pipeline_id)

        if not entry:
            logger.debug("[DRAIN] ensure_bridge: entry NOT FOUND | pipeline=%s", pipeline_id[:12])

            return None

        bridge = entry.bridge

        logger.debug(
            "[DRAIN] ensure_bridge: pipeline=%s has_bridge=%s",
            pipeline_id[:12],
            bridge is not None,
        )

        if bridge is None:
            from pipeline.stream_bridge import PipelineStreamBridge  # noqa: PLC0415

            bridge = PipelineStreamBridge(
                pipeline_id=pipeline_id,
                output_sink=sink,
                message_id=message_id,
            )

            entry.bridge = bridge

            self._resume_entry_sequence(entry, pipeline_id)

        else:
            # 复用既有 bridge：取消残留的 drain_task（兼容旧引用），刷新 sink 与 message_id。

            if entry.drain_task is not None and not entry.drain_task.done():
                bridge.stop()

                entry.drain_task.cancel()

                entry.drain_task = None

            if sink is not None:
                bridge.output_sink = sink

            # message_id 统一为 hex 格式（无 msg_ 前缀），由 bridge 内部生成

            bridge.reset_for_new_turn(message_id=message_id or uuid.uuid4().hex[:12])

        return bridge

    def update_thread_id(self, pipeline_id: str, thread_id: str) -> None:
        """更新管道的 thread_id 映射。"""

        entry = self._engines.get(pipeline_id)

        if entry:
            entry.thread_id = thread_id

    def get_thread_id(self, pipeline_id: str) -> str:
        """根据 pipeline_id 查找对应的 ws_thread_id。"""

        entry = self._engines.get(pipeline_id)

        return entry.thread_id if entry else ""

    def find_by_tag(self, *args: str) -> list[PipelineEntry]:
        """按关联标签查找管道条目。"""

        if len(args) % 2 != 0:
            raise ValueError("find_by_tag 参数必须为 key-value 对")

        pairs = [(args[i], args[i + 1]) for i in range(0, len(args), 2)]

        return [entry for entry in self._engines.values() if all(entry.tags.get(k) == v for k, v in pairs)]

    def find_by_thread_id(self, thread_id: str) -> list[PipelineEntry]:
        """根据 thread_id 反查所有关联的管道条目。"""

        return [entry for entry in self._engines.values() if entry.thread_id == thread_id]

    def all_entries(self) -> dict[str, PipelineEntry]:
        """返回所有已注册的管道条目（只读快照）。"""

        return dict(self._engines)


def get_engine_registry() -> EngineRegistry:
    """获取全局 EngineRegistry 单例的便捷函数。"""

    return EngineRegistry.get_instance()

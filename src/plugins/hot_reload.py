"""Plugin hot-reload system.

Extends config/reload.py to provide plugin-aware hot-reload for
agent and tool YAML configurations in the config/ directory.

Features:
- File watcher using watchdog (same library as config/reload.py)
- Debounced file change detection (300ms window)
- Parse, validate, and reload affected plugins
- Rollback on validation or reload failure
- Event emission via the pipeline EventBus
- REST API integration for manual reload control

Typical usage::

    from plugins.hot_reload import PluginHotReloader

    reloader = PluginHotReloader(
        config_dir="config",
        agent_registry=agent_registry,
        tool_registry=tool_registry,
    )
    reloader.start()
"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from config.schema import ConfigSchemaValidator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class PluginStatus(str, Enum):
    """Plugin load status."""

    LOADED = "loaded"
    ERROR = "error"
    UNLOADED = "unloaded"
    PENDING = "pending"


@dataclass
class PluginRecord:
    """Tracks a single plugin config's lifecycle.

    Attributes:
        config_path: Absolute path to the YAML file.
        config_type: One of 'agent', 'tool', 'pipeline', etc.
        config_id: The unique identifier inside the YAML (e.g. agent config_id).
        status: Current load status.
        last_loaded_at: Monotonic timestamp of last successful load.
        last_error: Error message from the last failed load, if any.
        raw_data: Last successfully loaded YAML data (used for rollback).
        version: Optional version string from the YAML.
    """

    config_path: str
    config_type: str
    config_id: str = ""
    status: PluginStatus = PluginStatus.PENDING
    last_loaded_at: float = 0.0
    last_error: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)
    version: str = ""


@dataclass
class ReloadEvent:
    """Represents a single reload operation result.

    Attributes:
        config_path: The file that triggered the reload.
        config_type: Determined config type.
        event_type: 'created', 'modified', or 'deleted'.
        success: Whether the reload succeeded.
        error: Error message on failure.
        rolled_back: Whether a rollback was performed.
        timestamp: Monotonic timestamp.
    """

    config_path: str
    config_type: str
    event_type: str
    success: bool = False
    error: str | None = None
    rolled_back: bool = False
    timestamp: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# File watcher handler with debounce
# ---------------------------------------------------------------------------


class PluginConfigWatchHandler(FileSystemEventHandler):
    """Watches for YAML changes in config/ with debouncing.

    Ignores temporary files (starting with ``.`` or ``~``).

    Args:
        callback: Called as ``callback(event_type, file_path)`` after debounce.
        debounce_seconds: Minimum interval between identical events.
    """

    def __init__(
        self,
        callback: Callable[[str, str], None],
        debounce_seconds: float = 0.3,
    ) -> None:
        super().__init__()
        self._callback = callback
        self._debounce_seconds = debounce_seconds
        self._last_processed: dict[str, float] = {}
        self._lock = threading.Lock()

    def _should_process(self, path: str) -> bool:
        p = Path(path)
        if p.name.startswith(".") or p.name.startswith("~"):
            return False
        return p.suffix in (".yaml", ".yml")

    def _debounce_and_notify(self, event_type: str, file_path: str) -> None:
        now = time.monotonic()
        with self._lock:
            last_time = self._last_processed.get(file_path, 0.0)
            if now - last_time < self._debounce_seconds:
                return
            self._last_processed[file_path] = now

        try:
            self._callback(event_type, file_path)
        except Exception:
            logger.exception(
                "Watch callback error | event=%s | path=%s",
                event_type,
                file_path,
            )

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_process(event.src_path):
            self._debounce_and_notify("modified", event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_process(event.src_path):
            self._debounce_and_notify("created", event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_process(event.src_path):
            self._debounce_and_notify("deleted", event.src_path)


# ---------------------------------------------------------------------------
# Core hot-reload manager
# ---------------------------------------------------------------------------


class PluginHotReloader:
    """Plugin-aware configuration hot-reload manager.

    Monitors the config/ directory for YAML changes and applies them to
    the appropriate registry (AgentRegistry, ToolRegistry, etc.).

    Lifecycle:
    1. ``start()`` begins watching.
    2. File changes trigger ``_on_file_change()``.
    3. The file is parsed and validated.
    4. On success, the old plugin is unregistered and the new one registered.
    5. On failure, the old version is kept (rollback).
    6. ``stop()`` ends watching.

    Args:
        config_dir: Root config directory (typically ``config/``).
        agent_registry: AgentRegistry instance (optional, for agent config reload).
        tool_registry: ToolRegistry instance (optional, for tool config reload).
        event_bus: EventBus instance for emitting reload events.
        debounce_seconds: Debounce window in seconds (default 0.3).
        validator: ConfigSchemaValidator instance (created if not provided).
    """

    def __init__(
        self,
        config_dir: str | Path = "config",
        agent_registry: Any | None = None,
        tool_registry: Any | None = None,
        event_bus: Any | None = None,
        debounce_seconds: float = 0.3,
        validator: ConfigSchemaValidator | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._disabled_plugins: set[str] = set()
        self._config_center: Any | None = None
        self._agent_registry = agent_registry
        self._tool_registry = tool_registry
        self._event_bus = event_bus
        self._debounce_seconds = debounce_seconds
        self._validator = validator or ConfigSchemaValidator()

        self._observer: Observer | None = None
        self._running = False

        # Thread pool for non-blocking reload dispatch
        self._reload_executor: Any = None

        # Track loaded plugins by their absolute path
        self._records: dict[str, PluginRecord] = {}
        self._records_lock = threading.Lock()

        # Reload history (most recent first, capped at 200)
        self._history: list[ReloadEvent] = []
        self._max_history = 200

        # Registered reload callbacks
        self._callbacks: list[Callable[[ReloadEvent], None]] = []

    # -- Public API --------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the file watcher is active."""
        return self._running

    def add_callback(self, callback: Callable[[ReloadEvent], None]) -> None:
        """Register a callback invoked on every reload event.

        Args:
            callback: Function receiving a ReloadEvent.
        """
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[ReloadEvent], None]) -> bool:
        """Remove a previously registered callback.

        Args:
            callback: The callback to remove.

        Returns:
            True if the callback was found and removed.
        """
        if callback in self._callbacks:
            self._callbacks.remove(callback)
            return True
        return False

    def disable_plugin(self, config_id: str) -> bool:
        """禁用插件，使其不被重载。"""
        if config_id in self._disabled_plugins:
            return False
        self._disabled_plugins.add(config_id)
        return True

    def enable_plugin(self, config_id: str) -> bool:
        """启用已禁用的插件。"""
        if config_id not in self._disabled_plugins:
            return False
        self._disabled_plugins.discard(config_id)
        return True

    def integrate_with_config_center(self, config_center) -> None:
        """与 ConfigCenter 集成，注册各子目录的监听回调。"""
        self._config_center = config_center
        prefixes = ["agents/", "tools/", "pipelines/", "models/"]
        for prefix in prefixes:
            config_center.watch(prefix, self._on_file_change)

    def start(self) -> None:
        """Start watching config/ for changes.

        监听来源选择（避免重复监听）：
        - ConfigCenter 已运行（生产环境）-> 订阅它的回调
        - 否则（测试/独立运行）-> 独立 watchdog Observer

        Idempotent -- calling while already running is a no-op.
        """
        if self._running:
            logger.warning("PluginHotReloader already running")
            return

        if not self._config_dir.exists():
            logger.error("Config directory does not exist: %s", self._config_dir)
            return

        # 优先：订阅 ConfigCenter（生产环境主路径）
        try:
            from config.config_center import get_config_center  # noqa: PLC0415

            center = get_config_center()
            if center.is_running:
                self.integrate_with_config_center(center)
                self._running = True
                logger.info(
                    "PluginHotReloader started (subscribed to ConfigCenter) | config_dir=%s",
                    self._config_dir,
                )
                return
        except Exception as exc:
            logger.debug("ConfigCenter 不可用，回退到独立 watchdog: %s", exc)

        # 回退：独立 watchdog（测试场景或 ConfigCenter 未启动）
        handler = PluginConfigWatchHandler(
            callback=self._on_file_change,
            debounce_seconds=self._debounce_seconds,
        )

        self._observer = Observer()
        self._observer.schedule(handler, str(self._config_dir), recursive=True)
        self._observer.start()
        self._running = True
        logger.info(
            "PluginHotReloader started (standalone watchdog) | config_dir=%s | debounce=%.2fs",
            self._config_dir,
            self._debounce_seconds,
        )

    def stop(self) -> None:
        """Stop watching."""
        self._running = False
        # 订阅模式：取消 ConfigCenter 回调
        if self._config_center is not None:
            try:
                for prefix in ["agents/", "tools/", "pipelines/", "models/"]:
                    self._config_center.unwatch(prefix, self._on_file_change)
            except Exception:
                pass
            self._config_center = None
        # 独立模式：停止 watchdog Observer
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        logger.info("PluginHotReloader stopped")

    # -- Manual reload API -------------------------------------------------

    def reload_plugin(self, config_path: str) -> ReloadEvent:
        """Manually trigger reload of a specific plugin config.

        Args:
            config_path: Absolute or relative path to the YAML file.

        Returns:
            ReloadEvent describing the outcome.
        """
        path = Path(config_path)
        if not path.is_absolute():
            path = self._config_dir / path
        return self._do_reload("modified", str(path))

    def reload_all(self) -> list[ReloadEvent]:
        """Reload every YAML file under config/.

        Returns:
            List of ReloadEvent for each file processed.
        """
        results: list[ReloadEvent] = []
        if not self._config_dir.exists():
            return results

        for yaml_file in sorted(self._config_dir.rglob("*.yaml")):
            if yaml_file.name.startswith(".") or yaml_file.name.startswith("~"):
                continue
            event = self._do_reload("modified", str(yaml_file))
            results.append(event)

        for yaml_file in sorted(self._config_dir.rglob("*.yml")):
            if yaml_file.name.startswith(".") or yaml_file.name.startswith("~"):
                continue
            event = self._do_reload("modified", str(yaml_file))
            results.append(event)

        return results

    def get_plugin_status(self) -> list[dict[str, Any]]:
        """Return status information for all tracked plugins.

        Returns:
            List of dicts with keys: config_path, config_type, config_id,
            status, last_loaded_at, last_error, version.
        """
        with self._records_lock:
            return [
                {
                    "config_path": r.config_path,
                    "config_type": r.config_type,
                    "config_id": r.config_id,
                    "status": r.status.value,
                    "last_loaded_at": r.last_loaded_at,
                    "last_error": r.last_error,
                    "version": r.version,
                }
                for r in self._records.values()
            ]

    def get_reload_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent reload events.

        Args:
            limit: Maximum number of events to return.

        Returns:
            List of event dicts (most recent first).
        """
        return [
            {
                "config_path": e.config_path,
                "config_type": e.config_type,
                "event_type": e.event_type,
                "success": e.success,
                "error": e.error,
                "rolled_back": e.rolled_back,
                "timestamp": e.timestamp,
            }
            for e in list(reversed(self._history))[:limit]
        ]

    # -- Internal ----------------------------------------------------------

    def _on_file_change(self, event_type: str, file_path: str, _metadata: Any = None) -> None:
        """Entry point called by the watchdog handler.

        Dispatches to _do_reload in a background thread so the watchdog
        thread is never blocked by I/O-heavy reload work.

        Args:
            event_type: 'modified', 'created', or 'deleted'.
            file_path: Absolute path to the changed file.
        """
        # Lazily create a single-thread executor on first use
        if self._reload_executor is None:
            from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

            self._reload_executor = ThreadPoolExecutor(max_workers=1)

        self._reload_executor.submit(self._do_reload_safe, event_type, file_path)

    def _do_reload_safe(self, event_type: str, file_path: str) -> None:
        """Run _do_reload with exception guard (executed in worker thread)."""
        try:
            result = self._do_reload(event_type, file_path)
            if not result.success:
                logger.warning(
                    "Hot-reload failed | event=%s | path=%s | error=%s",
                    event_type,
                    file_path,
                    result.error,
                )
        except Exception:
            logger.exception(
                "Unexpected error in hot-reload | event=%s | path=%s",
                event_type,
                file_path,
            )

    def _do_reload(self, event_type: str, file_path: str) -> ReloadEvent:  # noqa: PLR0911
        """Execute a single reload operation.

        For 'deleted': unregister the plugin.
        For 'created'/'modified': parse, validate, reload.

        Args:
            event_type: File event type.
            file_path: Absolute path.

        Returns:
            ReloadEvent with the outcome.
        """
        config_type = self._determine_config_type(file_path)
        logger.info(
            "Config change detected | event=%s | type=%s | path=%s",
            event_type,
            config_type,
            file_path,
        )

        # Handle deletion
        if event_type == "deleted":
            return self._handle_deleted(file_path, config_type)

        # Parse YAML
        path = Path(file_path)
        if not path.exists():
            return self._make_event(
                file_path,
                config_type,
                event_type,
                success=False,
                error="File does not exist",
            )

        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            return self._handle_parse_error(
                file_path,
                config_type,
                event_type,
                f"YAML parse error: {exc}",
            )

        if not isinstance(data, dict):
            return self._handle_parse_error(
                file_path,
                config_type,
                event_type,
                f"Expected dict, got {type(data).__name__}",
            )

        # Check if plugin is disabled
        config_id = data.get("config_id", data.get("name", ""))
        if config_id in self._disabled_plugins:
            return self._make_event(
                file_path,
                config_type,
                event_type,
                success=True,
                error="disabled",
            )

        # Validate
        errors = self._validate_config(data, config_type, file_path)
        if errors:
            return self._handle_validation_error(
                file_path,
                config_type,
                event_type,
                errors,
            )

        # Apply reload
        return self._apply_reload(file_path, config_type, event_type, data)

    def _handle_deleted(
        self,
        file_path: str,
        config_type: str,
    ) -> ReloadEvent:
        """Unregister a plugin whose config file was deleted.

        Args:
            file_path: Deleted file path.
            config_type: Determined config type.

        Returns:
            ReloadEvent with the outcome.
        """
        with self._records_lock:
            record = self._records.get(file_path)

        if record is None:
            # Unknown file, nothing to do
            return self._make_event(
                file_path,
                config_type,
                "deleted",
                success=True,
            )

        # Attempt to unregister from the appropriate registry
        try:
            self._unregister_from_registry(record)
        except Exception as exc:
            logger.error(
                "Failed to unregister deleted plugin | path=%s | error=%s",
                file_path,
                exc,
            )

        with self._records_lock:
            self._records[file_path] = PluginRecord(
                config_path=file_path,
                config_type=config_type,
                config_id=record.config_id,
                status=PluginStatus.UNLOADED,
                last_error=None,
            )

        event = self._make_event(
            file_path,
            config_type,
            "deleted",
            success=True,
        )
        self._emit_event("plugin_unloaded", {"file_path": file_path, "config_type": config_type})
        return event

    def _handle_parse_error(
        self,
        file_path: str,
        config_type: str,
        event_type: str,
        error: str,
    ) -> ReloadEvent:
        """Handle a YAML parse error -- keep old version if possible."""
        with self._records_lock:
            record = self._records.get(file_path)

        if record and record.status == PluginStatus.LOADED:
            # Keep old version (implicit rollback)
            logger.warning(
                "Keeping previous version after parse error | path=%s | error=%s",
                file_path,
                error,
            )
            record.last_error = error
            event = self._make_event(
                file_path,
                config_type,
                event_type,
                success=False,
                error=error,
                rolled_back=True,
            )
        else:
            with self._records_lock:
                self._records[file_path] = PluginRecord(
                    config_path=file_path,
                    config_type=config_type,
                    status=PluginStatus.ERROR,
                    last_error=error,
                )
            event = self._make_event(
                file_path,
                config_type,
                event_type,
                success=False,
                error=error,
            )

        self._emit_event(
            "reload_failed",
            {"file_path": file_path, "error": error, "rolled_back": event.rolled_back},
        )
        return event

    def _handle_validation_error(
        self,
        file_path: str,
        config_type: str,
        event_type: str,
        errors: list[str],
    ) -> ReloadEvent:
        """Handle validation errors -- keep old version if possible."""
        error_msg = "; ".join(errors)

        with self._records_lock:
            record = self._records.get(file_path)

        if record and record.status == PluginStatus.LOADED:
            # Keep old version (implicit rollback)
            logger.warning(
                "Keeping previous version after validation error | path=%s | errors=%s",
                file_path,
                error_msg,
            )
            record.last_error = error_msg
            event = self._make_event(
                file_path,
                config_type,
                event_type,
                success=False,
                error=error_msg,
                rolled_back=True,
            )
        else:
            with self._records_lock:
                self._records[file_path] = PluginRecord(
                    config_path=file_path,
                    config_type=config_type,
                    status=PluginStatus.ERROR,
                    last_error=error_msg,
                )
            event = self._make_event(
                file_path,
                config_type,
                event_type,
                success=False,
                error=error_msg,
            )

        self._emit_event(
            "reload_failed",
            {"file_path": file_path, "error": error_msg, "rolled_back": event.rolled_back},
        )
        return event

    def _apply_reload(
        self,
        file_path: str,
        config_type: str,
        event_type: str,
        data: dict[str, Any],
    ) -> ReloadEvent:
        """Apply the reload: unregister old, register new.

        On failure, restore the previous record (rollback).

        Args:
            file_path: Config file path.
            config_type: Config type ('agent', 'tool', etc.).
            event_type: File event type.
            data: Parsed YAML data.

        Returns:
            ReloadEvent with the outcome.
        """
        # Snapshot current record for rollback
        with self._records_lock:
            old_record = self._records.get(file_path)
            old_data = copy.deepcopy(old_record.raw_data) if old_record else None

        config_id = data.get("config_id", data.get("name", ""))
        version = data.get("version", "")

        try:
            # Unregister old if exists
            if old_record and old_record.status == PluginStatus.LOADED:
                self._unregister_from_registry(old_record)

            # Register new (pass file_path so it can load from disk directly)
            self._register_to_registry(config_type, data, file_path=file_path)

            # Update tracking record
            new_record = PluginRecord(
                config_path=file_path,
                config_type=config_type,
                config_id=config_id,
                status=PluginStatus.LOADED,
                last_loaded_at=time.monotonic(),
                last_error=None,
                raw_data=data,
                version=version,
            )
            with self._records_lock:
                self._records[file_path] = new_record

            event = self._make_event(
                file_path,
                config_type,
                event_type,
                success=True,
            )
            self._emit_event(
                "plugin_reloaded",
                {
                    "file_path": file_path,
                    "config_type": config_type,
                    "config_id": config_id,
                    "event_type": event_type,
                },
            )
            logger.info(
                "Hot-reload succeeded | type=%s | id=%s | path=%s",
                config_type,
                config_id,
                file_path,
            )
            return event

        except Exception as exc:
            # Rollback: restore old record
            logger.error(
                "Hot-reload failed, rolling back | type=%s | path=%s | error=%s",
                config_type,
                file_path,
                exc,
            )

            # Try to restore old data
            rolled_back = False
            if old_data is not None:
                try:
                    self._register_to_registry(config_type, old_data)
                    rolled_back = True
                    logger.info("Rollback succeeded | path=%s", file_path)
                except Exception as rb_exc:
                    logger.error("Rollback also failed: %s", rb_exc)

            # Update record to reflect error
            with self._records_lock:
                self._records[file_path] = PluginRecord(
                    config_path=file_path,
                    config_type=config_type,
                    config_id=config_id,
                    status=PluginStatus.ERROR if not rolled_back else PluginStatus.LOADED,
                    last_error=str(exc),
                    raw_data=old_data if rolled_back else (old_record.raw_data if old_record else {}),
                    version=version,
                )

            event = self._make_event(
                file_path,
                config_type,
                event_type,
                success=False,
                error=str(exc),
                rolled_back=rolled_back,
            )
            self._emit_event(
                "reload_failed",
                {
                    "file_path": file_path,
                    "config_type": config_type,
                    "error": str(exc),
                    "rolled_back": rolled_back,
                },
            )
            return event

    # -- Registry operations -----------------------------------------------

    def _register_to_registry(
        self,
        config_type: str,
        data: dict[str, Any],
        file_path: str | None = None,
    ) -> None:
        """Register config data to the appropriate registry.

        Args:
            config_type: 'agent', 'tool', etc.
            data: Parsed and validated YAML data.
            file_path: Optional file path. If provided and file exists on disk,
                it will be loaded directly (avoids writing a temp file).

        Raises:
            ValueError: If the registry is not set or config_type is unsupported.
        """
        if config_type == "agent":
            if self._agent_registry is None:
                raise ValueError("AgentRegistry not configured")
            from agents.loader import AgentConfigLoader  # noqa: PLC0415

            # Prefer loading from disk if the file exists
            load_path = file_path
            if load_path and Path(load_path).exists():
                config = AgentConfigLoader.load_from_yaml(load_path)
            else:
                # Fallback: write temp YAML for the loader
                config = AgentConfigLoader.load_from_yaml(_write_temp_yaml(data))
            self._agent_registry.register(config)

        elif config_type == "tool":
            if self._tool_registry is None:
                raise ValueError("ToolRegistry not configured")
            # Tool configs are read-only definitions; mark as loaded
            logger.debug("Tool config reload recorded (definition update)")

        elif config_type == "model":
            from config.models import invalidate_all_llm_caches  # noqa: PLC0415

            invalidate_all_llm_caches()
            logger.info("Model config hot-reloaded: %s", file_path)

        else:
            logger.debug("No registry action for config type: %s", config_type)

    def _unregister_from_registry(self, record: PluginRecord) -> None:
        """Unregister a plugin from the appropriate registry.

        Args:
            record: The PluginRecord to unregister.
        """
        if record.config_type == "agent" and self._agent_registry:
            if record.config_id:
                self._agent_registry.unregister(record.config_id)
                logger.info("Unregistered agent: %s", record.config_id)

        elif record.config_type == "tool" and self._tool_registry:
            # Tool unregistration handled separately
            logger.debug("Tool config unload recorded: %s", record.config_path)

        elif record.config_type == "model":
            from config.models import invalidate_all_llm_caches  # noqa: PLC0415

            invalidate_all_llm_caches()
            logger.info("Model config cache invalidated on delete: %s", record.config_path)

    # -- Validation --------------------------------------------------------

    def _validate_config(
        self,
        data: dict[str, Any],
        config_type: str,
        file_path: str,
    ) -> list[str]:
        """Validate config data against the schema.

        Args:
            data: Parsed YAML data.
            config_type: Detected config type.
            file_path: File path for logging.

        Returns:
            List of error strings. Empty means valid.
        """
        errors: list[str] = []

        if config_type == "agent":
            errors.extend(self._validator.validate_agent_config(data))
        elif config_type == "pipeline":
            errors.extend(self._validator.validate_pipeline_config(data))
        elif config_type == "model":
            errors.extend(self._validator.validate_model_config(data))
        # Unknown types: only check it's a non-empty dict
        elif not data:
            errors.append("Configuration is empty")

        return errors

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _determine_config_type(file_path: str) -> str:  # noqa: PLR0911
        """Determine config type from file path.

        Same logic as ConfigReloader._determine_config_type but also
        recognizes 'tools' and 'evaluation_metrics' directories.

        Args:
            file_path: Absolute file path.

        Returns:
            Config type string.
        """
        path = Path(file_path)
        for part in path.parts:
            lower = part.lower()
            if lower == "pipelines":
                return "pipeline"
            if lower == "agents":
                return "agent"
            if lower == "templates":
                return "template"
            if lower == "triggers":
                return "trigger"
            if lower == "tools":
                return "tool"
            if lower == "models":
                return "model"
            if lower == "evaluation_metrics":
                return "evaluation_metric"
        return "unknown"

    def _make_event(
        self,
        file_path: str,
        config_type: str,
        event_type: str,
        *,
        success: bool = False,
        error: str | None = None,
        rolled_back: bool = False,
    ) -> ReloadEvent:
        """Create a ReloadEvent, add to history, and notify callbacks.

        Args:
            file_path: Config file path.
            config_type: Config type.
            event_type: File event type.
            success: Whether the operation succeeded.
            error: Error message on failure.
            rolled_back: Whether a rollback occurred.

        Returns:
            The created ReloadEvent.
        """
        event = ReloadEvent(
            config_path=file_path,
            config_type=config_type,
            event_type=event_type,
            success=success,
            error=error,
            rolled_back=rolled_back,
        )

        # Add to history (thread-safe)
        self._history.append(event)
        while len(self._history) > self._max_history:
            self._history.pop(0)

        # Notify callbacks
        for callback in self._callbacks:
            try:
                callback(event)
            except Exception:
                logger.exception("Reload callback error")

        return event

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event via the EventBus if available.

        Uses ``asyncio.run_coroutine_threadsafe`` because the watchdog
        handler runs in a non-async thread.

        Args:
            event_type: Event name.
            data: Event data dict.
        """
        if self._event_bus is None:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            asyncio.ensure_future(
                self._event_bus.emit(event_type, data),
                loop=loop,
            )
        else:
            # No running loop; try to emit synchronously in a new loop
            try:
                asyncio.get_event_loop().run_until_complete(
                    self._event_bus.emit(event_type, data),
                )
            except Exception:
                logger.debug("Could not emit event (no event loop available)")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

_temp_counter = 0


def _write_temp_yaml(data: dict[str, Any]) -> Path:
    """Write data to a temporary YAML file for loading by AgentConfigLoader.

    AgentConfigLoader.load_from_yaml requires a file path, so we create
    a temporary file. The temp file is cleaned up lazily.

    Args:
        data: YAML data to write.

    Returns:
        Path to the temporary file.
    """
    import tempfile  # noqa: PLC0415

    global _temp_counter  # noqa: PLW0603
    _temp_counter += 1

    tmp_dir = Path(tempfile.gettempdir()) / "agent_os_hot_reload"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"_hot_reload_{_temp_counter}.yaml"

    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

    return tmp_path

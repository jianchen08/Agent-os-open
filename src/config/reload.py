"""配置热重载系统。

基于 watchdog 监听配置文件变更，支持防抖、回调通知和多种配置类型热重载。

典型用法::

    from config import ConfigReloader

    reloader = ConfigReloader(config_dir="config")
    reloader.register_reloader("agent", my_agent_reloader)
    reloader.add_callback(my_callback)
    reloader.start()
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class ConfigReloadHandler(FileSystemEventHandler):
    """配置文件变更处理器。

    监听 .yaml/.yml 文件的修改、创建、删除事件，
    忽略临时文件（以 . 或 ~ 开头）。

    Args:
        callback: 文件变更回调，签名为 ``(event_type, file_path)``。
        debounce_seconds: 防抖间隔（秒），同一文件在此间隔内的重复事件被忽略。
    """

    def __init__(
        self,
        callback: Callable[[str, str], None],
        debounce_seconds: float = 1.0,
    ) -> None:
        super().__init__()
        self._callback = callback
        self._debounce_seconds = debounce_seconds
        self._last_processed: dict[str, float] = {}
        self._lock = threading.Lock()

    def _should_process(self, path: str) -> bool:
        """判断是否应该处理该文件事件。

        仅处理 .yaml/.yml 文件，忽略以 ``.`` 或 ``~`` 开头的临时文件。

        Args:
            path: 事件文件路径。

        Returns:
            是否应处理。
        """
        p = Path(path)

        # 忽略临时文件
        if p.name.startswith(".") or p.name.startswith("~"):
            return False

        # 仅处理 YAML 配置文件
        return p.suffix in (".yaml", ".yml")

    def _debounce_and_notify(self, event_type: str, file_path: str) -> None:
        """防抖后通知回调。

        同一文件在 ``debounce_seconds`` 内的重复事件将被忽略。

        Args:
            event_type: 事件类型（modified/created/deleted）。
            file_path: 文件路径。
        """
        now = time.monotonic()
        with self._lock:
            last_time = self._last_processed.get(file_path, 0.0)
            if now - last_time < self._debounce_seconds:
                return
            self._last_processed[file_path] = now

        try:
            self._callback(event_type, file_path)
        except Exception:
            logger.exception("回调执行失败 | event=%s | path=%s", event_type, file_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        """处理文件修改事件。"""
        if event.is_directory:
            return
        if self._should_process(event.src_path):
            self._debounce_and_notify("modified", event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        """处理文件创建事件。"""
        if event.is_directory:
            return
        if self._should_process(event.src_path):
            self._debounce_and_notify("created", event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        """处理文件删除事件。"""
        if event.is_directory:
            return
        if self._should_process(event.src_path):
            self._debounce_and_notify("deleted", event.src_path)


class ConfigReloader:
    """配置热重载管理器。

    基于 watchdog 监听配置目录，支持：

    - 多目录监听（config/、agents/ 等）
    - 按配置类型分发重载（pipeline/agent/template/trigger）
    - 回调注册/注销
    - 启动/停止
    - 防抖处理

    通过 ``register_reloader`` 注入配置类型的重载器，与具体 Registry 解耦。

    Args:
        config_dir: 主配置目录路径。
        debounce_seconds: 防抖间隔（秒）。
    """

    def __init__(
        self,
        config_dir: str | Path = "config",
        debounce_seconds: float = 1.0,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._debounce_seconds = debounce_seconds
        self._observer: Observer | None = None
        self._running = False
        self._watch_dirs: list[Path] = []
        self._callbacks: list[Callable[[str, str, dict[str, Any]], None]] = []
        self._reloaders: dict[str, Callable[[str], Any]] = {}

    def add_watch_dir(self, dir_path: str | Path) -> None:
        """添加额外的监听目录。

        Args:
            dir_path: 目录路径。
        """
        self._watch_dirs.append(Path(dir_path))

    def add_callback(
        self, callback: Callable[[str, str, dict[str, Any]], None]
    ) -> None:
        """注册配置变更回调。

        回调签名：``callback(event_type, file_path, context)``
        其中 ``context`` 包含 ``config_type`` 等元信息。

        Args:
            callback: 回调函数。
        """
        self._callbacks.append(callback)

    def remove_callback(
        self, callback: Callable[[str, str, dict[str, Any]], None]
    ) -> bool:
        """注销配置变更回调。

        Args:
            callback: 之前注册的回调函数。

        Returns:
            是否成功移除。
        """
        if callback in self._callbacks:
            self._callbacks.remove(callback)
            return True
        return False

    def register_reloader(
        self, config_type: str, reloader: Callable[[str], Any]
    ) -> None:
        """注册配置类型的重载器。

        重载器签名：``reloader(file_path) -> Any``
        当对应类型的配置文件变更时被调用。

        Args:
            config_type: 配置类型标识（如 ``pipeline``、``agent``、``template``、``trigger``）。
            reloader: 重载函数。
        """
        self._reloaders[config_type] = reloader

    def start(self) -> None:
        """启动配置热重载。

        监听主配置目录和所有额外添加的目录。
        若已在运行则忽略。
        """
        if self._running:
            logger.warning("配置热重载已在运行")
            return

        if not self._config_dir.exists():
            logger.error("配置目录不存在: %s", self._config_dir)
            return

        handler = ConfigReloadHandler(
            callback=self._on_file_change,
            debounce_seconds=self._debounce_seconds,
        )

        self._observer = Observer()
        self._observer.schedule(handler, str(self._config_dir), recursive=True)

        for watch_dir in self._watch_dirs:
            if watch_dir.exists():
                self._observer.schedule(handler, str(watch_dir), recursive=True)
                logger.info("监听额外目录: %s", watch_dir)
            else:
                logger.warning("监听目录不存在: %s", watch_dir)

        self._observer.start()
        self._running = True
        logger.info("配置热重载已启动 | config_dir=%s", self._config_dir)

    def stop(self) -> None:
        """停止配置热重载。"""
        self._running = False

        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

        logger.info("配置热重载已停止")

    def is_running(self) -> bool:
        """检查热重载是否在运行。

        Returns:
            是否在运行。
        """
        return self._running

    def _on_file_change(self, event_type: str, file_path: str) -> None:
        """文件变更回调入口。

        判断配置类型并调用对应重载器，然后通知所有回调。

        Args:
            event_type: 事件类型。
            file_path: 变更文件路径。
        """
        config_type = self._determine_config_type(file_path)
        logger.info(
            "检测到配置变更 | event=%s | type=%s | path=%s",
            event_type, config_type, file_path,
        )

        self._reload_config(config_type, file_path)

        # 通知所有回调
        context: dict[str, Any] = {"config_type": config_type}
        for callback in self._callbacks:
            try:
                callback(event_type, file_path, context)
            except Exception:
                logger.exception("回调执行失败 | event=%s | path=%s", event_type, file_path)

    def _determine_config_type(self, file_path: str) -> str:
        """根据文件路径判断配置类型。

        路径规则：
        - 包含 ``pipelines`` → ``pipeline``
        - 包含 ``agents`` → ``agent``
        - 包含 ``templates`` → ``template``
        - 包含 ``triggers`` → ``trigger``
        - 其他 → ``unknown``

        Args:
            file_path: 文件路径。

        Returns:
            配置类型标识。
        """
        path = Path(file_path)
        parts = path.parts

        # 从路径中查找配置类型关键词
        for part in parts:
            lower = part.lower()
            if lower == "pipelines":
                return "pipeline"
            if lower == "agents":
                return "agent"
            if lower == "templates":
                return "template"
            if lower == "triggers":
                return "trigger"

        return "unknown"

    def _reload_config(self, config_type: str, file_path: str) -> None:
        """执行配置重载。

        调用已注册的对应类型重载器，错误不外泄。

        Args:
            config_type: 配置类型。
            file_path: 变更文件路径。
        """
        reloader = self._reloaders.get(config_type)
        if not reloader:
            logger.debug("未注册 %s 类型的重载器，跳过", config_type)
            return

        try:
            reloader(file_path)
            logger.info("配置重载成功 | type=%s | path=%s", config_type, file_path)
        except Exception:
            logger.exception("配置重载失败 | type=%s | path=%s", config_type, file_path)

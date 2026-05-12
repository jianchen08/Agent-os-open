"""
配置热更新服务

监听配置文件变化，自动同步到数据库。
支持 Agent、Workflow、工具配置的实时更新。
"""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class ConfigFileHandler(FileSystemEventHandler):
    """配置文件变化处理器"""

    def __init__(
        self,
        callback: Callable[[str, str], None],
        debounce_seconds: float = 1.0,
    ):
        """
        初始化处理器

        Args:
            callback: 文件变化回调函数 (event_type, file_path)
            debounce_seconds: 防抖时间（秒）
        """
        super().__init__()
        self._callback = callback
        self._debounce_seconds = debounce_seconds
        self._pending_files: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _should_process(self, path: str) -> bool:
        """判断是否应该处理该文件"""
        p = Path(path)
        # 只处理 YAML 文件
        if p.suffix not in (".yaml", ".yml"):
            return False
        # 忽略临时文件
        if p.name.startswith(".") or p.name.startswith("~"):
            return False
        return True

    def on_modified(self, event: FileSystemEvent) -> None:
        """文件修改事件"""
        if event.is_directory:
            return
        if self._should_process(event.src_path):
            self._callback("modified", event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        """文件创建事件"""
        if event.is_directory:
            return
        if self._should_process(event.src_path):
            self._callback("created", event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        """文件删除事件"""
        if event.is_directory:
            return
        if self._should_process(event.src_path):
            self._callback("deleted", event.src_path)


class ConfigHotReloader:
    """配置热更新服务"""

    def __init__(
        self,
        config_dir: str = "config",
        debounce_seconds: float = 1.0,
    ):
        """
        初始化热更新服务

        Args:
            config_dir: 配置文件目录
            debounce_seconds: 防抖时间（秒）
        """
        self._config_dir = Path(config_dir)
        self._debounce_seconds = debounce_seconds
        self._observer: Observer | None = None
        self._running = False
        self._pending_syncs: set[str] = set()
        self._sync_task: asyncio.Task | None = None
        self._session_factory: Callable | None = None
        self._callbacks: list[Callable[[str, str, dict], None]] = []

    def set_session_factory(self, session_factory: Callable) -> None:
        """设置数据库会话工厂"""
        self._session_factory = session_factory

    def add_callback(self, callback: Callable[[str, str, dict], None]) -> None:
        """
        添加配置变化回调

        Args:
            callback: 回调函数 (config_type, config_id, result)
        """
        self._callbacks.append(callback)

    def _on_file_change(self, event_type: str, file_path: str) -> None:
        """文件变化回调"""
        logger.info("检测到配置文件变化 | event=%s | path=%s", event_type, file_path)
        self._pending_syncs.add(file_path)

    async def _sync_loop(self) -> None:
        """同步循环 - 定期检查并同步变化的配置"""
        while self._running:
            await asyncio.sleep(self._debounce_seconds)

            if not self._pending_syncs:
                continue

            # 获取待同步文件
            files_to_sync = self._pending_syncs.copy()
            self._pending_syncs.clear()

            # 执行同步
            await self._sync_files(files_to_sync)

    async def _sync_files(self, files: set[str]) -> None:
        """同步指定文件到数据库"""
        if not self._session_factory:
            logger.warning("未设置数据库会话工厂，跳过同步")
            return

        from src.config.loader import ConfigLoader

        # 按类型分组
        agents_files = []
        workflows_files = []

        for file_path in files:
            p = Path(file_path)
            rel_path = p.relative_to(self._config_dir)
            parts = rel_path.parts

            if len(parts) > 0:
                if parts[0] == "agents":
                    agents_files.append(file_path)
                elif parts[0] == "workflows":
                    workflows_files.append(file_path)

        # 同步到数据库
        async with self._session_factory() as session:
            loader = ConfigLoader(str(self._config_dir))
            result = {"agents": [], "workflows": []}

            try:
                if agents_files:
                    result["agents"] = await loader.load_agents(session)
                    logger.info("Agent 配置已同步 | count=%d", len(result["agents"]))

                if workflows_files:
                    result["workflows"] = await loader.load_workflows(session)
                    logger.info("工作流配置已同步 | count=%d", len(result["workflows"]))

                # 触发回调
                for callback in self._callbacks:
                    try:
                        callback("sync_complete", "", result)
                    except Exception as e:
                        logger.error("回调执行失败: %s", e)

            except Exception as e:
                logger.error("配置同步失败: %s", e, exc_info=True)

    def start(self) -> None:
        """启动热更新服务"""
        if self._running:
            logger.warning("热更新服务已在运行")
            return

        if not self._config_dir.exists():
            logger.error("配置目录不存在: %s", self._config_dir)
            return

        # 创建文件监听器
        handler = ConfigFileHandler(
            callback=self._on_file_change,
            debounce_seconds=self._debounce_seconds,
        )

        self._observer = Observer()
        self._observer.schedule(
            handler,
            str(self._config_dir),
            recursive=True,
        )
        self._observer.start()
        self._running = True

        # 启动同步循环
        loop = asyncio.get_event_loop()
        self._sync_task = loop.create_task(self._sync_loop())

        logger.info("配置热更新服务已启动 | config_dir=%s", self._config_dir)

    def stop(self) -> None:
        """停止热更新服务"""
        self._running = False

        if self._sync_task:
            self._sync_task.cancel()
            self._sync_task = None

        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

        logger.info("配置热更新服务已停止")

    def is_running(self) -> bool:
        """检查服务是否运行中"""
        return self._running


# 全局单例
_hot_reloader: ConfigHotReloader | None = None


def get_hot_reloader() -> ConfigHotReloader:
    """获取热更新服务单例"""
    global _hot_reloader
    if _hot_reloader is None:
        _hot_reloader = ConfigHotReloader()
    return _hot_reloader


def init_hot_reloader(
    config_dir: str = "config",
    session_factory: Callable | None = None,
) -> ConfigHotReloader:
    """
    初始化热更新服务

    Args:
        config_dir: 配置目录
        session_factory: 数据库会话工厂

    Returns:
        热更新服务实例
    """
    global _hot_reloader
    _hot_reloader = ConfigHotReloader(config_dir=config_dir)
    if session_factory:
        _hot_reloader.set_session_factory(session_factory)
    return _hot_reloader

"""配置中心 — 热加载统一入口。

基于 watchfiles 实现目录/文件监听，支持防抖（500ms）+ 内容哈希去重，
提供 watch / reload / get 三个公共接口。

设计原则：
- 在已有 config/reload.py（ConfigReloader）基础上抽象统一入口，避免重复实现
- 通过 watchfiles 异步监听文件变更，通过回调通知上层（Agent Registry、PluginHotReloader 等）
- 读写锁保证并发安全
- 加载失败时回滚旧配置 + 写审计日志
- 环境变量类、Redis 类不参与热加载（见代码注释）

典型用法::

    from config.config_center import ConfigCenter

    center = ConfigCenter(config_root="config")
    center.watch("config/agents/", on_change=my_agent_callback)
    await center.start()

安全策略：
- 配置变更审批已移至业务层（human_interaction / security_check 插件），ConfigCenter 不再拦截
- 非配置类文件（.env、Redis 配置等）不纳入监听范围
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections.abc import Callable, Coroutine  # noqa: F401
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEBOUNCE_SECONDS = 0.5  # 防抖窗口 500ms
MAX_AUDIT_HISTORY = 500  # 审计日志最大条数

# 不参与热加载的目录/文件模式
_EXCLUDED_PATTERNS: set[str] = {
    ".env",
    ".git",
    "__pycache__",
    ".bak",
    "*.pyc",
}

# 不参与热加载的文件名（环境变量类、Redis 类等）
_EXCLUDED_FILES: set[str] = {
    ".env",
    ".env.local",
    ".env.production",
    "redis.conf",
    "docker-compose.yml",
    "Dockerfile",
}


# ---------------------------------------------------------------------------
# 审计记录
# ---------------------------------------------------------------------------


@dataclass
class AuditEntry:
    """热加载审计记录。

    Attributes:
        file_path: 变更文件路径。
        event_type: 事件类型（created/modified/deleted）。
        config_type: 配置类型（agent/pipeline/tool/model 等）。
        success: 是否成功。
        rolled_back: 是否回滚。
        error: 错误信息。
        timestamp: 时间戳。
        content_hash: 文件内容哈希（去重用）。
    """

    file_path: str
    event_type: str
    config_type: str
    success: bool = False
    rolled_back: bool = False
    error: str | None = None
    timestamp: float = field(default_factory=time.monotonic)
    content_hash: str = ""


# ---------------------------------------------------------------------------
# 读写锁（简易实现）
# ---------------------------------------------------------------------------


class _RWLock:
    """简易读写锁：多读单写。

    读操作可并发，写操作独占。适用于读多写少的配置热加载场景。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._readers = threading.Lock()
        self._read_count = 0

    def acquire_read(self) -> None:
        """获取读锁。"""
        with self._readers:
            self._read_count += 1
            if self._read_count == 1:
                self._lock.acquire()

    def release_read(self) -> None:
        """释放读锁。"""
        with self._readers:
            self._read_count -= 1
            if self._read_count == 0:
                self._lock.release()

    def acquire_write(self) -> None:
        """获取写锁（独占）。"""
        self._lock.acquire()

    def release_write(self) -> None:
        """释放写锁。"""
        self._lock.release()


class _ReadGuard:
    """读锁上下文管理器。"""

    def __init__(self, rwlock: _RWLock) -> None:
        self._rwlock = rwlock

    def __enter__(self) -> _ReadGuard:
        self._rwlock.acquire_read()
        return self

    def __exit__(self, *args: Any) -> None:
        self._rwlock.release_read()


class _WriteGuard:
    """写锁上下文管理器。"""

    def __init__(self, rwlock: _RWLock) -> None:
        self._rwlock = rwlock

    def __enter__(self) -> _WriteGuard:
        self._rwlock.acquire_write()
        return self

    def __exit__(self, *args: Any) -> None:
        self._rwlock.release_write()


# ---------------------------------------------------------------------------
# ConfigCenter
# ---------------------------------------------------------------------------


class ConfigCenter:
    """配置中心 — 热加载统一入口。

    职责：
    1. 基于 watchfiles 监听 config/ 目录变更
    2. 防抖 + 内容哈希去重，避免重复加载
    3. 读写锁保证并发安全
    4. 加载失败时回滚旧配置 + 审计日志
    5. 暴露 watch / reload / get 三个公共接口

    与现有模块的关系：
    - 在 ConfigReloader（config/reload.py）基础上抽象，复用其类型判断逻辑
    - Agent Registry 通过 watch 回调接收变更通知
    - PluginHotReloader 通过集成方法接入 ConfigCenter

    Args:
        config_root: 配置根目录（默认 config/）。
        debounce_seconds: 防抖窗口（秒），默认 500ms。
    """

    def __init__(
        self,
        config_root: str | Path = "config",
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        self._config_root = Path(config_root)
        self._debounce_seconds = debounce_seconds

        # 文件内容哈希缓存 {file_path_str: sha256_hex}
        self._content_hashes: dict[str, str] = {}
        # 配置数据缓存 {file_path_str: parsed_yaml_data}
        self._config_cache: dict[str, dict[str, Any]] = {}

        # 读写锁
        self._rwlock = _RWLock()

        # 已注册的监听回调 {path_prefix_str: [callback, ...]}
        self._watchers: dict[str, list[Callable[[str, str, dict[str, Any]], None]]] = {}
        self._watchers_lock = threading.Lock()

        # 防抖状态 {file_path_str: last_process_time}
        self._debounce_state: dict[str, float] = {}
        self._debounce_lock = threading.Lock()

        # 审计日志
        self._audit_log: list[AuditEntry] = []
        self._audit_lock = threading.Lock()

        # watchfiles 异步任务
        self._watch_task: asyncio.Task | None = None
        self._running = False
        self._stop_event = asyncio.Event()
        # 启动就绪事件：start() 完成 _running 置位后 set，
        # 供调用方同步等待就绪（解决 lifespan 中 fire-and-forget create_task 后
        # 立即检测 is_running 的时序竞态）。
        self._ready_event = asyncio.Event()

        # 审批逻辑已移至业务层（human_interaction / security_check 插件），
        # ConfigCenter 不再在加载层拦截配置变更。

    # -- 公共接口 -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """配置中心是否在运行。"""
        return self._running

    def watch(
        self,
        path_prefix: str,
        callback: Callable[[str, str, dict[str, Any]], None],
    ) -> None:
        """注册配置变更监听。

        当 path_prefix 下的文件发生变更时，调用 callback。
        callback 签名: callback(event_type, file_path, context)
        context 包含 config_type 等元信息。

        Args:
            path_prefix: 监听的路径前缀（相对于 config_root），如 "agents/"。
            callback: 变更回调函数。
        """
        with self._watchers_lock:
            normalized = self._normalize_prefix(path_prefix)
            if normalized not in self._watchers:
                self._watchers[normalized] = []
            self._watchers[normalized].append(callback)
            logger.info("注册配置监听: prefix=%s", normalized)

    def unwatch(
        self,
        path_prefix: str,
        callback: Callable[[str, str, dict[str, Any]], None],
    ) -> bool:
        """取消配置变更监听。

        Args:
            path_prefix: 之前注册的路径前缀。
            callback: 之前注册的回调函数。

        Returns:
            是否成功取消。
        """
        with self._watchers_lock:
            normalized = self._normalize_prefix(path_prefix)
            callbacks = self._watchers.get(normalized)
            if callbacks and callback in callbacks:
                callbacks.remove(callback)
                if not callbacks:
                    del self._watchers[normalized]
                return True
        return False

    def reload(self, path: str) -> dict[str, Any]:
        """手动重载指定配置文件。

        读取文件、验证、更新缓存、通知回调。
        失败时回滚旧配置。

        Args:
            path: 配置文件路径（相对于 config_root 或绝对路径）。

        Returns:
            结果字典: {success, error, rolled_back, config_type}

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: YAML 解析失败。
        """
        abs_path = self._resolve_path(path)
        if not abs_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {abs_path}")

        config_type = self._determine_config_type(str(abs_path))
        event_type = "manual_reload"

        # 读取并计算哈希
        try:
            with open(abs_path, encoding="utf-8") as f:
                content = f.read()
            data = yaml.safe_load(content)
            if not isinstance(data, dict):
                # 非 dict 内容抛 ValueError（既有测试锁定此契约：
                # tests/unit/test_config_center.py::test_reload_non_dict_yaml_raises_value_error）。
                # YAMLError 走 _handle_load_failure 是因为它是解析期错误（可恢复），
                # 而非 dict 是内容语义错误（调用方应修正），二者语义不同。
                raise ValueError(f"YAML 内容必须是字典类型: {abs_path}")
        except yaml.YAMLError as e:
            return self._handle_load_failure(
                str(abs_path),
                event_type,
                config_type,
                f"YAML 解析失败: {e}",
            )

        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # 写锁：更新缓存
        path_str = str(abs_path)
        old_hash = None

        with _WriteGuard(self._rwlock):
            self._config_cache.get(path_str)
            old_hash = self._content_hashes.get(path_str)

            # 内容哈希去重
            if content_hash == old_hash:
                logger.debug("配置内容未变化，跳过重载: %s", abs_path)
                return {"success": True, "error": None, "rolled_back": False, "config_type": config_type}

            # 更新缓存
            self._config_cache[path_str] = data
            self._content_hashes[path_str] = content_hash

        # 通知回调
        context = {"config_type": config_type, "content_hash": content_hash}
        self._notify_watchers(event_type, str(abs_path), context)

        # 审计日志
        self._write_audit(
            AuditEntry(
                file_path=path_str,
                event_type=event_type,
                config_type=config_type,
                success=True,
                content_hash=content_hash,
            )
        )

        logger.info("配置重载成功: type=%s path=%s", config_type, abs_path)
        return {"success": True, "error": None, "rolled_back": False, "config_type": config_type}

    def get(self, path: str) -> dict[str, Any] | None:
        """获取已缓存的配置数据。

        若缓存未命中，尝试从磁盘读取并缓存。

        Args:
            path: 配置文件路径（相对于 config_root 或绝对路径）。

        Returns:
            配置数据字典，未找到返回 None。
        """
        abs_path = self._resolve_path(path)
        path_str = str(abs_path)

        # 读锁：查询缓存
        with _ReadGuard(self._rwlock):
            cached = self._config_cache.get(path_str)
            if cached is not None:
                return cached

        # 缓存未命中，尝试从磁盘读取
        if not abs_path.exists():
            return None

        try:
            with open(abs_path, encoding="utf-8") as f:
                content = f.read()
            data = yaml.safe_load(content)
            if not isinstance(data, dict):
                return None
        except Exception:
            return None

        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # 写锁：更新缓存
        with _WriteGuard(self._rwlock):
            self._config_cache[path_str] = data
            self._content_hashes[path_str] = content_hash

        return data

    async def start(self) -> None:
        """启动配置中心（异步）。

        基于 watchfiles 监听 config_root 目录变更。

        所有可能的退出路径都会 set `_ready_event`，确保调用方通过
        `wait_ready()` 等待时不会永久阻塞（无论成功还是失败）。
        """
        try:
            if self._running:
                logger.warning("ConfigCenter 已在运行")
                return

            if not self._config_root.exists():
                logger.error("配置目录不存在: %s", self._config_root)
                return

            self._running = True
            self._stop_event.clear()

            try:
                from watchfiles import awatch  # noqa: PLC0415
            except ImportError:
                logger.warning("watchfiles 未安装，回退到 watchdog 模式。建议安装: pip install watchfiles")
                self._running = False
                return

            logger.info("ConfigCenter 启动 | config_root=%s", self._config_root)

            async for changes in awatch(
                self._config_root,
                stop_event=self._stop_event,
            ):
                if not self._running:
                    break
                await self._process_changes(changes)

            logger.info("ConfigCenter 已停止")
        finally:
            # 无论成功启动、早退还是异常，都通知等待方，避免死等
            self._ready_event.set()

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        """等待 ConfigCenter 启动就绪。

        用于 lifespan 中 create_task(start()) 后同步等待 _running 置位，
        解决 PluginHotReloader 检测 is_running 的时序竞态。

        Args:
            timeout: 最大等待秒数。

        Returns:
            是否在超时前就绪（_running 为 True 表示真正启动）。
        """
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self._running

    def stop(self) -> None:
        """停止配置中心。"""
        self._running = False
        self._stop_event.set()
        self._ready_event.clear()
        logger.info("ConfigCenter 停止请求已发送")

    def get_audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取审计日志。

        Args:
            limit: 最大返回条数。

        Returns:
            审计日志列表（最新在前）。
        """
        with self._audit_lock:
            entries = list(reversed(self._audit_log))[:limit]
        return [
            {
                "file_path": e.file_path,
                "event_type": e.event_type,
                "config_type": e.config_type,
                "success": e.success,
                "rolled_back": e.rolled_back,
                "error": e.error,
                "timestamp": e.timestamp,
                "content_hash": e.content_hash,
            }
            for e in entries
        ]

    # -- 内部方法 -----------------------------------------------------------

    async def _process_changes(
        self,
        changes: set[tuple[int, str]],
    ) -> None:
        """处理 watchfiles 检测到的文件变更。

        对每个变更事件进行防抖过滤和内容哈希去重，
        然后通知对应的回调。

        Args:
            changes: watchfiles 返回的变更集合 {(change_type, path)}。
        """
        from watchfiles import Change  # noqa: PLC0415

        event_map = {
            Change.added: "created",
            Change.modified: "modified",
            Change.deleted: "deleted",
        }

        for change_type, path_str in changes:
            # 过滤非 YAML 文件
            path = Path(path_str)
            if path.suffix not in (".yaml", ".yml"):
                continue

            # 过滤临时文件
            if path.name.startswith(".") or path.name.startswith("~"):
                continue

            # 过滤不参与热加载的文件
            if self._is_excluded(path):
                continue

            event_type = event_map.get(change_type, "modified")

            # 防抖
            if not self._check_debounce(path_str):
                continue

            # 处理变更（在线程池中执行以避免阻塞事件循环）
            await asyncio.to_thread(
                self._handle_file_change,
                event_type,
                path_str,
            )

    def _handle_file_change(self, event_type: str, path_str: str) -> None:
        """处理单个文件变更事件（同步，在线程池中执行）。

        Args:
            event_type: 事件类型。
            path_str: 文件绝对路径。
        """
        config_type = self._determine_config_type(path_str)

        # 删除事件
        if event_type == "deleted":
            with _WriteGuard(self._rwlock):
                self._config_cache.pop(path_str, None)
                self._content_hashes.pop(path_str, None)

            context = {"config_type": config_type}
            self._notify_watchers(event_type, path_str, context)
            self._write_audit(
                AuditEntry(
                    file_path=path_str,
                    event_type=event_type,
                    config_type=config_type,
                    success=True,
                )
            )
            return

        # 读取并计算哈希
        path = Path(path_str)
        if not path.exists():
            return

        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            data = yaml.safe_load(content)
            if not isinstance(data, dict):
                self._handle_load_failure(
                    path_str,
                    event_type,
                    config_type,
                    f"YAML 内容不是字典类型: {path_str}",
                )
                return
        except Exception as e:
            self._handle_load_failure(path_str, event_type, config_type, str(e))
            return

        new_hash = hashlib.sha256(content.encode()).hexdigest()

        # 写锁：检查去重 + 更新缓存
        with _WriteGuard(self._rwlock):
            old_hash = self._content_hashes.get(path_str)
            if new_hash == old_hash:
                logger.debug("配置内容未变化，跳过: %s", path_str)
                return
            self._config_cache[path_str] = data
            self._content_hashes[path_str] = new_hash

        # 通知回调
        context = {"config_type": config_type, "content_hash": new_hash}
        self._notify_watchers(event_type, path_str, context)

        # 审计日志
        self._write_audit(
            AuditEntry(
                file_path=path_str,
                event_type=event_type,
                config_type=config_type,
                success=True,
                content_hash=new_hash,
            )
        )

        logger.info("配置自动重载: type=%s event=%s path=%s", config_type, event_type, path_str)

    def _handle_load_failure(
        self,
        path_str: str,
        event_type: str,
        config_type: str,
        error: str,
    ) -> dict[str, Any]:
        """处理加载失败：回滚旧配置 + 写审计日志。

        Args:
            path_str: 文件路径。
            event_type: 事件类型。
            config_type: 配置类型。
            error: 错误信息。

        Returns:
            结果字典。
        """
        rolled_back = False

        with _ReadGuard(self._rwlock):
            old_data = self._config_cache.get(path_str)

        if old_data is not None:
            # 回滚：旧缓存保留不变
            rolled_back = True
            logger.warning(
                "配置加载失败，保留旧配置 | path=%s | error=%s",
                path_str,
                error,
            )
        else:
            # 无旧配置可回滚
            logger.error(
                "配置加载失败（无旧配置可回滚）| path=%s | error=%s",
                path_str,
                error,
            )

        self._write_audit(
            AuditEntry(
                file_path=path_str,
                event_type=event_type,
                config_type=config_type,
                success=False,
                rolled_back=rolled_back,
                error=error,
            )
        )

        return {
            "success": False,
            "error": error,
            "rolled_back": rolled_back,
            "config_type": config_type,
        }

    def _notify_watchers(
        self,
        event_type: str,
        file_path: str,
        context: dict[str, Any],
    ) -> None:
        """通知匹配的监听回调。

        Args:
            event_type: 事件类型。
            file_path: 变更文件路径。
            context: 上下文信息。
        """
        with self._watchers_lock:
            matched_callbacks: list[Callable] = []
            for prefix, callbacks in self._watchers.items():
                if file_path.startswith(prefix) or self._path_matches_prefix(file_path, prefix):
                    matched_callbacks.extend(callbacks)

        for callback in matched_callbacks:
            try:
                callback(event_type, file_path, context)
            except Exception:
                logger.exception(
                    "配置变更回调执行失败 | event=%s | path=%s",
                    event_type,
                    file_path,
                )

    def _write_audit(self, entry: AuditEntry) -> None:
        """写入审计日志。

        Args:
            entry: 审计记录。
        """
        with self._audit_lock:
            self._audit_log.append(entry)
            while len(self._audit_log) > MAX_AUDIT_HISTORY:
                self._audit_log.pop(0)

    def _check_debounce(self, path_str: str) -> bool:
        """检查防抖：同一文件在防抖窗口内的重复事件被过滤。

        Args:
            path_str: 文件路径。

        Returns:
            True 表示应处理，False 表示应跳过。
        """
        now = time.monotonic()
        with self._debounce_lock:
            last_time = self._debounce_state.get(path_str, 0.0)
            if now - last_time < self._debounce_seconds:
                return False
            self._debounce_state[path_str] = now
        return True

    # -- 工具方法 -----------------------------------------------------------

    @staticmethod
    def _determine_config_type(file_path: str) -> str:  # noqa: PLR0911
        """根据文件路径判断配置类型。

        路径规则：
        - 包含 agents → agent
        - 包含 pipelines → pipeline
        - 包含 tools → tool
        - 包含 models → model
        - 包含 templates → template
        - 包含 triggers → trigger
        - 包含 evaluation_metrics → evaluation_metric
        - 其他 → unknown

        Args:
            file_path: 文件路径。

        Returns:
            配置类型标识。
        """
        path = Path(file_path)
        for part in path.parts:
            lower = part.lower()
            if lower == "agents":
                return "agent"
            if lower == "pipelines":
                return "pipeline"
            if lower == "tools":
                return "tool"
            if lower == "models":
                return "model"
            if lower == "templates":
                return "template"
            if lower == "triggers":
                return "trigger"
            if lower == "evaluation_metrics":
                return "evaluation_metric"
            if lower == "evaluation":
                return "evaluation"
            if lower == "isolation":
                return "isolation"
            if lower == "modules":
                return "module"
        return "unknown"

    def _resolve_path(self, path: str) -> Path:
        """将相对路径解析为绝对路径。

        Args:
            path: 相对或绝对路径。

        Returns:
            绝对路径。
        """
        p = Path(path)
        if p.is_absolute():
            return p
        return self._config_root / p

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        """标准化路径前缀。

        Args:
            prefix: 原始前缀。

        Returns:
            标准化后的前缀。
        """
        p = Path(prefix)
        return str(p).replace("\\", "/")

    @staticmethod
    def _path_matches_prefix(file_path: str, prefix: str) -> bool:
        """检查文件路径是否匹配某个前缀。

        Args:
            file_path: 文件绝对路径。
            prefix: 注册的前缀。

        Returns:
            是否匹配。
        """
        normalized = file_path.replace("\\", "/")
        return prefix in normalized

    def _is_excluded(self, path: Path) -> bool:
        """检查文件是否应排除在热加载之外。

        环境变量类、Redis 类、Docker 配置等不参与热加载。

        Args:
            path: 文件路径。

        Returns:
            是否应排除。
        """
        name = path.name
        if name in _EXCLUDED_FILES:
            return True

        # 排除 .env 系列文件
        if name.startswith(".env"):
            return True

        # 排除备份文件
        return bool(name.endswith(".bak") or name.endswith(".bak~"))


# ---------------------------------------------------------------------------
# 全局单例（懒初始化）
# ---------------------------------------------------------------------------

_global_center: ConfigCenter | None = None
_global_lock = threading.Lock()


def get_config_center() -> ConfigCenter:
    """获取全局 ConfigCenter 单例。

    Returns:
        ConfigCenter 实例。
    """
    global _global_center  # noqa: PLW0603
    if _global_center is None:
        with _global_lock:
            if _global_center is None:
                _global_center = ConfigCenter()
    return _global_center

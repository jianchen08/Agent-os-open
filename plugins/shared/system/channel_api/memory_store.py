"""基于字典的内存存储，支持 JSON 文件持久化。

从 channels.api.models 中提取的 MemoryStore 类，
管理用户、线程、消息、记忆和会话5种资源。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# SessionModel 原依赖 infrastructure.session.models（0.1 包），0.2 环境下该包不存在。
# 这里本地化定义等价的 dataclass，字段与 0.1 保持一致，解除对 infrastructure 的依赖。
@dataclass
class SessionModel:
    """会话模型 — 管道历史的引用集合（本地化版本）。

    会话只是一个标记，记录哪些管道属于这个会话。
    不负责创建管道、生成 pipeline_id 或管理管道生命周期。

    Attributes:
        session_id: 会话标签，创建后固定不变
        channel_type: 来源通道 — "cli" 或 "web"
        channel_ref: 通道级引用
        pipeline_ids: 属于这个会话的 pipeline_run_id 引用列表
        active_pipeline_id: 最近一次使用的 pipeline_run_id（仅引用）
        created_at: 创建时间戳
        last_active_at: 最后活跃时间戳
        metadata: 扩展元数据
    """

    session_id: str = ""
    channel_type: str = "cli"
    channel_ref: str = ""
    pipeline_ids: list[str] = field(default_factory=list)
    active_pipeline_id: str = ""
    created_at: float | None = None
    last_active_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        """更新最后活跃时间戳。"""
        self.last_active_at = time.time()

    def register_pipeline(self, pipeline_id: str, set_active: bool = True) -> None:
        """将一个管道 ID 注册到本会话的引用集合中。"""
        if pipeline_id and pipeline_id not in self.pipeline_ids:
            self.pipeline_ids.append(pipeline_id)
        if set_active:
            self.active_pipeline_id = pipeline_id
        self.touch()

    def clear(self) -> None:
        """清空管道引用列表。"""
        self.pipeline_ids.clear()
        self.active_pipeline_id = ""
        self.touch()

_log = logging.getLogger(__name__)


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_time(s: str) -> float:
    """将 ISO 格式时间字符串转为 Unix 时间戳，解析失败返回 0.0。"""
    if not s:
        return 0.0
    try:
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except (ValueError, OSError) as exc:
        _log.warning("解析 ISO 时间失败，返回 0.0 | value=%r err=%s", s, exc)
        return 0.0


class MemoryStore:
    """基于字典的内存存储，支持 JSON 文件持久化。

    存储用户、线程等数据。初始化时创建演示用户 demo/demo123。
    当指定 persist_dir 时，线程和会话数据会自动持久化到 JSON 文件。
    消息数据仅存储在管道执行记录（YAML）中，不在本 store 中保存。

    Attributes:
        users: 用户存储字典，key 为用户名
        threads: 线程存储字典，key 为线程 ID
        memories: 记忆存储字典，key 为记忆 ID
        refresh_tokens: refresh token 黑名单（已登出的 token）
        sessions: SessionModel 桥接映射
    """

    def __init__(self, persist_dir: str | None = None) -> None:
        """初始化内存存储，创建演示用户。

        Args:
            persist_dir: 持久化目录路径，为 None 则不持久化
        """
        self.users: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.memories: dict[str, dict[str, Any]] = {}
        # token 撤销统一走 TokenManager（Redis）。
        self.sessions: dict[str, SessionModel] = {}
        self._persist_dir = persist_dir
        self._persist_lock = threading.Lock()
        self._load_failed: bool = False

        self._create_default_users()
        self._load_persisted_data()

    def _create_default_users(self) -> None:
        """创建默认管理员用户。

        仅当显式配置了环境变量 DEFAULT_ADMIN_PASSWORD 时才创建 admin 账号，
        密码使用 bcrypt 哈希存储，从不保存明文。
        未配置时不创建任何默认用户，避免落入无人知晓的兜底密码陷阱。
        """
        import os  # noqa: PLC0415

        from password import hash_password  # noqa: PLC0415

        admin_password = os.environ.get("DEFAULT_ADMIN_PASSWORD")
        if not admin_password:
            _log.warning(
                "未配置 DEFAULT_ADMIN_PASSWORD，不创建默认 admin 账号。"
                "请在 .env 中设置 DEFAULT_ADMIN_PASSWORD 后重启，否则 admin 登录将失败（401）。"
            )
            return

        self.users["admin"] = {
            "id": "admin_user_001",
            "username": "admin",
            "password": hash_password(admin_password),
            "email": "admin@example.com",
            "role": "admin",
            "created_at": _now_iso(),
        }

    def _persist_file(self) -> str | None:
        """返回持久化文件路径。"""
        if not self._persist_dir:
            return None
        return os.path.join(self._persist_dir, "store.json")

    def _load_persisted_data(self) -> None:
        """从 JSON 文件加载持久化数据。

        threads 是唯一数据源。加载每个 thread 时自动派生对应的 SessionModel，
        无需在 JSON 中存储 sessions 段。
        """
        path = self._persist_file()
        if not path:
            return
        if not os.path.exists(path):  # noqa: PTH110
            persist_dir = os.path.dirname(path)  # noqa: PTH120
            if os.path.exists(persist_dir) and os.listdir(persist_dir):  # noqa: PTH110,PTH208
                _log.warning("store.json 不存在但 persist_dir 非空，可能数据丢失: %s", persist_dir)
                self._load_failed = True
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for tid, tdata in data.get("threads", {}).items():
                self.threads[tid] = tdata
                self.sessions[tid] = SessionModel(
                    session_id=tdata.get("id", tid),
                    channel_type="web",
                    channel_ref=tdata.get("id", ""),
                    pipeline_ids=tdata.get("pipeline_ids", []),
                    active_pipeline_id=tdata.get("active_pipeline_id", ""),
                    created_at=_parse_iso_time(tdata.get("created_at", "")),
                    last_active_at=_parse_iso_time(tdata.get("updated_at", "")),
                    metadata=tdata.get("metadata", {}),
                )
        except Exception as e:
            _log.error("持久化数据加载失败: %s [path=%s]", e, path, exc_info=True)
            self._load_failed = True
        else:
            # 加载成功，清除失败标志
            self._load_failed = False

    def _save_persisted_data(self) -> None:
        """将线程数据持久化到 JSON 文件。

        只持久化 threads 数据。SessionModel 在加载时从 thread 字段自动派生。
        消息数据由管道执行记录（YAML）独立管理。
        """
        if self._load_failed:
            _log.error("持久化数据加载曾失败，禁止写入以防止覆盖旧数据。请手动检查 store.json 是否损坏。")
            return

        path = self._persist_file()
        if not path:
            return
        with self._persist_lock:
            try:
                if path and os.path.exists(path):  # noqa: PTH110
                    backup_path = path + ".bak"
                    try:
                        import shutil  # noqa: PLC0415

                        shutil.copy2(path, backup_path)
                    except OSError as exc:
                        _log.warning("备份 store.json 失败，继续写入 | path=%s err=%s", path, exc)

                data = {"threads": self.threads}
                os.makedirs(os.path.dirname(path), exist_ok=True)  # noqa: PTH103,PTH120
                tmp_path = path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                if os.path.exists(path):  # noqa: PTH110
                    os.replace(tmp_path, path)  # noqa: PTH105
                else:
                    os.rename(tmp_path, path)  # noqa: PTH104
            except Exception as e:
                _log.warning("持久化保存失败: %s [path=%s]", e, path)

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """根据用户名查找用户。"""
        return self.users.get(username)

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        """根据用户 ID 查找用户。"""
        for user in self.users.values():
            if user["id"] == user_id:
                return user
        return None

    def create_user(
        self,
        username: str,
        password: str,
        email: str | None = None,
    ) -> dict[str, Any]:
        """创建新用户并存入内存。

        密码使用 bcrypt 哈希存储，从不保存明文。

        Args:
            username: 用户名
            password: 明文密码（将被哈希）
            email: 可选邮箱

        Returns:
            创建的用户字典

        Raises:
            ValueError: 用户名已存在
        """
        if username in self.users:
            raise ValueError(f"用户名 '{username}' 已存在")

        from password import hash_password  # noqa: PLC0415

        user_id = uuid.uuid4().hex[:12]
        user = {
            "id": user_id,
            "username": username,
            "password": hash_password(password),
            "email": email,
            "created_at": _now_iso(),
        }
        self.users[username] = user
        return user

    def get_session(self, thread_id: str) -> SessionModel | None:
        """获取指定线程关联的会话模型。

        Args:
            thread_id: 线程 ID

        Returns:
            关联的 SessionModel，不存在则返回 None
        """
        return self.sessions.get(thread_id)

    # ---- Memory 存储操作 ----

    def create_memory(
        self,
        content: str,
        memory_type: str = "episode",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """创建记忆条目。"""
        mem_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        memory = {
            "id": mem_id,
            "content": content,
            "memory_type": memory_type,
            "tags": tags or [],
            "score": 0.0,
            "created_at": now,
        }
        self.memories[mem_id] = memory
        return memory

    def get_memory(self, mem_id: str) -> dict[str, Any] | None:
        """获取记忆条目。"""
        return self.memories.get(mem_id)

    def list_memories(
        self,
        memory_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """列出记忆条目。"""
        items = list(self.memories.values())
        if memory_type:
            items = [m for m in items if m["memory_type"] == memory_type]
        return items[offset : offset + limit]

    def search_memories(
        self,
        query: str,
        top_k: int = 5,
        method: str = "keyword",
    ) -> list[dict[str, Any]]:
        """搜索记忆条目（简易关键词匹配）。"""
        query_lower = query.lower()
        scored: list[tuple[float, dict[str, Any]]] = []
        for m in self.memories.values():
            content_lower = m["content"].lower()
            if query_lower in content_lower:
                # 简易评分：匹配次数 / 内容长度
                count = content_lower.count(query_lower)
                score = count / max(len(content_lower), 1)
                scored.append((score, {**m, "score": round(score, 4)}))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:top_k]]

    def delete_memory(self, mem_id: str) -> bool:
        """删除记忆条目。"""
        if mem_id not in self.memories:
            return False
        del self.memories[mem_id]
        return True

    # token 撤销统一走 TokenManager（Redis）。


# 模块级单例
store = MemoryStore(
    persist_dir=str(Path(__file__).resolve().parent.parent.parent.parent / "data" / "api_store"),
)

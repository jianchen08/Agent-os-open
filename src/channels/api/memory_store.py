"""基于字典的内存存储，支持 JSON 文件持久化。

从 channels.api.models 中提取的 MemoryStore 类，
管理用户、线程、消息、记忆和会话5种资源。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infrastructure.session.models import SessionModel

_log = logging.getLogger(__name__)

# 本模块的 store 单例在 import 时即创建默认用户，其密码依赖环境变量。
# 不同入口 import 顺序不可控（可能早于 app_factory 的 load_dotenv），
# 故在此主动确保 .env 已加载，避免读到空环境变量而无法创建 admin。
try:
    from config.models import _load_dotenv_once  # noqa: PLC0415

    _load_dotenv_once()
except Exception:  # noqa: BLE001
    _log.warning("加载 .env 失败，默认用户可能无法正确创建", exc_info=True)


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
    except Exception:
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
        # 用户线程索引：user_id -> thread_id 列表，加速 get_user_threads 查询
        self._user_thread_index: dict[str, list[str]] = {}

        # 线程消息存储：thread_id -> 消息列表（带 sequence 字段）
        self._messages: dict[str, list[dict[str, Any]]] = {}
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

        from src.auth.password import hash_password  # noqa: PLC0415

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
            # 从已加载的 threads 中构建用户线程索引
            self._rebuild_user_thread_index()

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
                    except Exception:
                        _log.warning("备份 store.json 失败，继续写入")

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

    def _rebuild_user_thread_index(self) -> None:
        """从 threads 字典重建用户线程索引。

        在数据加载完成后调用，确保索引与实际数据一致。
        """
        self._user_thread_index.clear()
        for tid, thread in self.threads.items():
            user_id = thread.get("user_id")
            if user_id:
                self._user_thread_index.setdefault(user_id, []).append(tid)

    def _add_thread_to_index(self, user_id: str, thread_id: str) -> None:
        """将线程添加到用户线程索引中。

        Args:
            user_id: 用户 ID
            thread_id: 线程 ID
        """
        idx_list = self._user_thread_index.get(user_id)
        if idx_list is None:
            self._user_thread_index[user_id] = [thread_id]
        elif thread_id not in idx_list:
            idx_list.append(thread_id)

    def _remove_thread_from_index(self, user_id: str, thread_id: str) -> None:
        """从用户线程索引中移除线程。

        Args:
            user_id: 用户 ID
            thread_id: 线程 ID
        """
        idx_list = self._user_thread_index.get(user_id)
        if idx_list and thread_id in idx_list:
            idx_list.remove(thread_id)
            if not idx_list:
                del self._user_thread_index[user_id]

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

        from src.auth.password import hash_password  # noqa: PLC0415

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

    def create_thread(
        self,
        user_id: str,
        title: str | None = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        intent: str | None = None,
    ) -> dict[str, Any]:
        """创建新线程。

        Args:
            user_id: 创建者用户 ID
            title: 线程标题，默认为空字符串
            agent_id: 关联的 Agent ID
            metadata: 线程元数据
            intent: 线程意图描述

        Returns:
            创建的线程字典
        """
        thread_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        thread = {
            "id": thread_id,
            "user_id": user_id,
            "title": title or "",
            "agent_id": agent_id,
            "metadata": metadata or {},
            "intent": intent,
            "current_state": "active",
            "pipeline_ids": [],
            "active_pipeline_id": "",
            "created_at": now,
            "updated_at": now,
        }
        self.threads[thread_id] = thread
        self._add_thread_to_index(user_id, thread_id)
        self._save_persisted_data()
        return thread

    def update_thread(
        self,
        thread_id: str,
        title: str | None = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """更新线程属性。"""
        thread = self.threads.get(thread_id)
        if thread is None:
            return None
        if title is not None:
            thread["title"] = title
            thread["intent"] = title
        if agent_id is not None:
            thread["agent_id"] = agent_id
        if metadata is not None:
            thread["metadata"] = {**thread.get("metadata", {}), **metadata}
        thread["updated_at"] = _now_iso()
        self._save_persisted_data()
        return thread

    def get_user_threads(self, user_id: str) -> list[dict[str, Any]]:
        """获取指定用户的所有线程。

        使用 _user_thread_index 索引加速查找，避免遍历全量 threads 字典。
        """
        thread_ids = self._user_thread_index.get(user_id, [])
        return [{**self.threads[tid]} for tid in thread_ids if tid in self.threads]

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        """获取指定线程详情。"""
        return self.threads.get(thread_id)

    def delete_thread(self, thread_id: str) -> bool:
        """删除指定线程及其消息和关联会话。

        Returns:
            删除成功返回 True，线程不存在返回 False
        """
        if thread_id not in self.threads:
            return False
        user_id = self.threads[thread_id].get("user_id")
        del self.threads[thread_id]
        self.sessions.pop(thread_id, None)
        if user_id:
            self._remove_thread_from_index(user_id, thread_id)
        self._save_persisted_data()
        return True

    def set_session(self, thread_id: str, session: SessionModel) -> None:
        """将 SessionModel 关联到指定线程，并同步 pipeline_ids 到 thread 字典。

        Args:
            thread_id: 线程 ID
            session: 要关联的会话模型实例
        """
        self.sessions[thread_id] = session
        thread = self.threads.get(thread_id)
        if thread is not None:
            thread["pipeline_ids"] = list(session.pipeline_ids)
            thread["active_pipeline_id"] = session.active_pipeline_id
        self._save_persisted_data()

    def add_message(
        self,
        thread_id: str,
        message_id: str,
        role: str,
        content: str,
        sequence: int,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """向线程添加消息。

        Args:
            thread_id: 线程 ID
            message_id: 消息 ID
            role: 消息角色（user/assistant/tool/system）
            content: 消息内容
            sequence: 消息序号
            metadata: 可选元数据

        Returns:
            创建的消息字典
        """
        msg: dict[str, Any] = {
            "id": message_id,
            "thread_id": thread_id,
            "role": role,
            "content": content,
            "sequence": sequence,
            "timestamp": _now_iso(),
            "metadata": metadata or {},
        }
        self._messages.setdefault(thread_id, []).append(msg)
        return msg

    def get_messages(
        self,
        thread_id: str,
        limit: int = 20,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
    ) -> dict[str, Any]:
        """获取线程的消息列表（支持分页）。

        Args:
            thread_id: 线程 ID
            limit: 每页数量
            before_sequence: 游标分页的 sequence 边界
            after_sequence: 断线补漏的 sequence 边界

        Returns:
            包含 messages、total、has_more 的分页结果字典
        """
        all_msgs = self._messages.get(thread_id, [])

        # 按 before_sequence 过滤
        if before_sequence is not None:
            filtered = [m for m in all_msgs if m["sequence"] < before_sequence]
        else:
            filtered = list(all_msgs)

        # 按 after_sequence 过滤
        if after_sequence is not None:
            filtered = [m for m in filtered if m["sequence"] > after_sequence]

        total = len(all_msgs)
        filtered_total = len(filtered)
        has_more = filtered_total > limit

        # 取最后 limit 条（即最新的 limit 条）
        page = filtered[-limit:] if filtered_total > limit else filtered

        return {
            "messages": page,
            "total": total,
            "has_more": has_more,
        }

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

"""Pydantic 数据模型与内存存储。

定义 API 请求/响应的数据模型，以及基于字典的内存存储实现。
包含演示用户 demo/demo123。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


# ============================================================
# 请求/响应模型
# ============================================================

class LoginRequest(BaseModel):
    """登录请求模型。"""
    username: str
    password: str


class RegisterRequest(BaseModel):
    """注册请求模型。"""
    username: str
    password: str
    email: str | None = None


class TokenResponse(BaseModel):
    """Token 响应模型。"""
    access_token: str
    refresh_token: str
    expires_in: int = Field(description="access token 有效期（秒）")
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """用户信息响应模型。"""
    id: str
    username: str
    email: str | None = None
    created_at: str


class ThreadCreate(BaseModel):
    """创建线程请求模型。"""
    title: str | None = None


class ThreadResponse(BaseModel):
    """线程响应模型，字段名与前端 mapThreadToSession 对齐。"""
    thread_id: str
    intent: str | None = None
    current_state: str = "active"
    created_at: str
    updated_at: str
    agent_id: str | None = None


class MessageResponse(BaseModel):
    """消息响应模型，字段名与前端 mapBackendMessageToMessage 对齐。"""
    id: str
    thread_id: str
    role: str
    content: str
    timestamp: str
    sequence: int = 0
    parentId: str | None = None


# ============================================================
# 内存存储
# ============================================================

def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    """基于字典的内存存储。

    存储用户、线程和消息数据。初始化时创建演示用户 demo/demo123。

    Attributes:
        users: 用户存储字典，key 为用户名
        threads: 线程存储字典，key 为线程 ID
        messages: 消息存储字典，key 为线程 ID，value 为消息列表
        refresh_tokens: refresh token 黑名单（已登出的 token）
    """

    def __init__(self) -> None:
        """初始化内存存储，创建演示用户。"""
        self.users: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.refresh_tokens: set[str] = set()  # 已撤销的 refresh token

        # 创建演示用户 demo/demo123
        demo_id = str(uuid.uuid4())
        self.users["demo"] = {
            "id": demo_id,
            "username": "demo",
            "password": "demo12345",  # 内存存储，明文即可（8位以上满足前端验证）
            "email": "demo@example.com",
            "role": "user",
            "created_at": _now_iso(),
        }

        # 创建管理员用户
        admin_id = str(uuid.uuid4())
        self.users["admin"] = {
            "id": admin_id,
            "username": "admin",
            "password": "admin123",
            "email": "admin@example.com",
            "role": "admin",
            "created_at": _now_iso(),
        }

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """根据用户名查找用户。"""
        return self.users.get(username)

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        """根据用户 ID 查找用户。"""
        for user in self.users.values():
            if user["id"] == user_id:
                return user
        return None

    def create_user(self, username: str, password: str, email: str | None = None) -> dict[str, Any]:
        """创建新用户并存入内存。

        Args:
            username: 用户名
            password: 密码
            email: 可选邮箱

        Returns:
            创建的用户字典

        Raises:
            ValueError: 用户名已存在
        """
        if username in self.users:
            raise ValueError(f"用户名 '{username}' 已存在")

        user_id = str(uuid.uuid4())
        user = {
            "id": user_id,
            "username": username,
            "password": password,
            "email": email,
            "created_at": _now_iso(),
        }
        self.users[username] = user
        return user

    def create_thread(self, user_id: str, title: str | None = None) -> dict[str, Any]:
        """创建新线程。

        Args:
            user_id: 创建者用户 ID
            title: 线程标题，默认为空字符串

        Returns:
            创建的线程字典
        """
        thread_id = str(uuid.uuid4())
        now = _now_iso()
        thread = {
            "id": thread_id,
            "user_id": user_id,
            "title": title or "",
            "created_at": now,
            "updated_at": now,
        }
        self.threads[thread_id] = thread
        self.messages[thread_id] = []
        return thread

    def get_user_threads(self, user_id: str) -> list[dict[str, Any]]:
        """获取指定用户的所有线程。"""
        return [
            {
                **t,
                "message_count": len(self.messages.get(t["id"], [])),
            }
            for t in self.threads.values()
            if t["user_id"] == user_id
        ]

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        """获取指定线程详情。"""
        thread = self.threads.get(thread_id)
        if thread is None:
            return None
        return {
            **thread,
            "message_count": len(self.messages.get(thread_id, [])),
        }

    def delete_thread(self, thread_id: str) -> bool:
        """删除指定线程及其消息。

        Returns:
            删除成功返回 True，线程不存在返回 False
        """
        if thread_id not in self.threads:
            return False
        del self.threads[thread_id]
        self.messages.pop(thread_id, None)
        return True

    def get_messages(self, thread_id: str) -> list[dict[str, Any]]:
        """获取指定线程的所有消息。"""
        return self.messages.get(thread_id, [])

    def revoke_refresh_token(self, token: str) -> None:
        """将 refresh token 加入撤销列表。"""
        self.refresh_tokens.add(token)

    def is_token_revoked(self, token: str) -> bool:
        """检查 refresh token 是否已被撤销。"""
        return token in self.refresh_tokens


# 全局内存存储实例
store = MemoryStore()

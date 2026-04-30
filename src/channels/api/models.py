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


class ThreadUpdate(BaseModel):
    """更新线程请求模型。"""
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
# Agent 相关模型
# ============================================================

class AgentResponse(BaseModel):
    """Agent 配置响应模型。"""
    config_id: str
    name: str
    display_name: str = ""
    description: str = ""
    agent_type: str = "specialized"
    category: str = ""
    level: str = "L3"
    system_prompt: str = ""
    tool_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    is_active: bool = True
    version: str = "1.0.0"


class AgentListResponse(BaseModel):
    """Agent 列表响应模型。"""
    items: list[AgentResponse]
    total: int


# ============================================================
# Task 相关模型
# ============================================================

class TaskCreate(BaseModel):
    """创建任务请求模型。"""
    title: str
    description: str | None = None
    agent_id: str | None = None
    priority: int = 5
    tags: list[str] = Field(default_factory=list)
    input_data: dict[str, Any] = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    """更新任务请求模型。"""
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    tags: list[str] | None = None


class TaskResponse(BaseModel):
    """任务响应模型。"""
    id: str
    title: str
    description: str | None = None
    status: str = "pending"
    priority: int = 5
    agent_id: str | None = None
    thread_id: str | None = None
    created_by: str | None = None
    tags: list[str] = Field(default_factory=list)
    input_data: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""


class TaskListResponse(BaseModel):
    """任务列表响应模型。"""
    items: list[TaskResponse]
    total: int


class TaskSubmitResponse(BaseModel):
    """任务提交响应模型。"""
    task_id: str
    status: str
    message: str


class TaskEvaluateRequest(BaseModel):
    """任务评估请求模型。"""
    metric_ids: list[str] = Field(default_factory=list)
    input_params: dict[str, dict[str, Any]] = Field(default_factory=dict)


class TaskEvaluateResponse(BaseModel):
    """任务评估响应模型。"""
    task_id: str
    overall_passed: bool
    summary: str
    results: list[dict[str, Any]] = Field(default_factory=list)


# ============================================================
# Tool 相关模型
# ============================================================

class ToolResponse(BaseModel):
    """工具响应模型。"""
    name: str
    description: str = ""
    category: str = ""
    source: str = ""
    level: str = "all"
    status: str = "active"
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolListResponse(BaseModel):
    """工具列表响应模型。"""
    items: list[ToolResponse]
    total: int


# ============================================================
# Memory 相关模型
# ============================================================

class MemorySearchRequest(BaseModel):
    """记忆搜索请求模型。"""
    query: str
    memory_type: str | None = None
    top_k: int = 5
    method: str = "keyword"


class MemoryResponse(BaseModel):
    """记忆条目响应模型。"""
    id: str
    content: str = ""
    memory_type: str = ""
    tags: list[str] = Field(default_factory=list)
    score: float = 0.0
    created_at: str = ""


class MemoryListResponse(BaseModel):
    """记忆列表响应模型。"""
    items: list[MemoryResponse]
    total: int


# ============================================================
# Evaluation 相关模型
# ============================================================

class MetricResponse(BaseModel):
    """评估指标响应模型。"""
    id: str
    name: str = ""
    description: str = ""
    metric_type: str = "tool"
    evaluator_id: str = ""
    is_red_line: bool = False
    default_weight: float = 1.0
    level: int = 1
    tags: list[str] = Field(default_factory=list)
    status: str = "active"


class MetricDetailResponse(MetricResponse):
    """评估指标详情响应模型。"""
    default_config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    includes: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)


class MetricListResponse(BaseModel):
    """评估指标列表响应模型。"""
    items: list[MetricResponse]
    total: int


# ============================================================
# 通用分页和列表模型
# ============================================================

class PaginatedQuery(BaseModel):
    """分页查询参数。"""
    limit: int = Field(default=20, ge=1, le=100, description="每页数量")
    offset: int = Field(default=0, ge=0, description="偏移量")


class ErrorResponse(BaseModel):
    """标准错误响应模型。"""
    error: dict[str, Any] = Field(description="错误详情")


class HealthResponse(BaseModel):
    """健康检查响应模型。"""
    status: str = "ok"
    version: str = "1.0.0"
    uptime_seconds: float = 0.0


# ============================================================
# 内存存储
# ============================================================

def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    """基于字典的内存存储。

    存储用户、线程、消息、任务等数据。初始化时创建演示用户 demo/demo123。

    Attributes:
        users: 用户存储字典，key 为用户名
        threads: 线程存储字典，key 为线程 ID
        messages: 消息存储字典，key 为线程 ID，value 为消息列表
        tasks: 任务存储字典，key 为任务 ID
        memories: 记忆存储字典，key 为记忆 ID
        refresh_tokens: refresh token 黑名单（已登出的 token）
    """

    def __init__(self) -> None:
        """初始化内存存储，创建演示用户。"""
        self.users: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.memories: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: set[str] = set()  # 已撤销的 refresh token

        # 创建演示用户 demo/demo123
        demo_id = uuid.uuid4().hex[:12]
        self.users["demo"] = {
            "id": demo_id,
            "username": "demo",
            "password": "demo12345",  # 内存存储，明文即可（8位以上满足前端验证）
            "email": "demo@example.com",
            "role": "user",
            "created_at": _now_iso(),
        }

        # 创建管理员用户
        admin_id = uuid.uuid4().hex[:12]
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

    def create_user(
        self, username: str, password: str, email: str | None = None,
    ) -> dict[str, Any]:
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

        user_id = uuid.uuid4().hex[:12]
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
        thread_id = uuid.uuid4().hex[:12]
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

    def update_thread(
        self, thread_id: str, title: str | None = None,
    ) -> dict[str, Any] | None:
        """更新线程标题。"""
        thread = self.threads.get(thread_id)
        if thread is None:
            return None
        if title is not None:
            thread["title"] = title
        thread["updated_at"] = _now_iso()
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

    def add_message(
        self,
        thread_id: str,
        message_id: str,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """向指定线程添加一条消息。

        Args:
            thread_id: 线程 ID
            message_id: 消息 ID
            role: 消息角色 (user / assistant / tool)
            content: 消息内容
            **kwargs: 额外字段（如 thinking, tool_calls 等）

        Returns:
            创建的消息字典
        """
        now = _now_iso()
        msg: dict[str, Any] = {
            "id": message_id,
            "thread_id": thread_id,
            "role": role,
            "content": content,
            "created_at": now,
            **kwargs,
        }
        if thread_id not in self.messages:
            self.messages[thread_id] = []
        self.messages[thread_id].append(msg)
        # 更新线程的 updated_at
        thread = self.threads.get(thread_id)
        if thread:
            thread["updated_at"] = now
        return msg

    def search_messages(
        self, query: str, limit: int = 20, offset: int = 0,
    ) -> list[dict[str, Any]]:
        """搜索所有线程中包含查询关键词的消息。

        Args:
            query: 搜索关键词
            limit: 返回数量
            offset: 偏移量

        Returns:
            匹配的消息列表
        """
        results: list[dict[str, Any]] = []
        query_lower = query.lower()
        for thread_id, msgs in self.messages.items():
            for m in msgs:
                if query_lower in m.get("content", "").lower():
                    results.append({**m, "thread_id": thread_id})
        return results[offset:offset + limit]

    # ---- Task 存储操作 ----

    def create_task(
        self,
        user_id: str,
        title: str,
        description: str | None = None,
        agent_id: str | None = None,
        priority: int = 5,
        tags: list[str] | None = None,
        input_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建新任务。"""
        task_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        task = {
            "id": task_id,
            "title": title,
            "description": description or "",
            "status": "pending",
            "priority": priority,
            "agent_id": agent_id,
            "thread_id": None,
            "created_by": user_id,
            "tags": tags or [],
            "input_data": input_data or {},
            "result": None,
            "created_at": now,
            "updated_at": now,
        }
        self.tasks[task_id] = task
        return task

    def get_task(
        self, task_id: str,
    ) -> dict[str, Any] | None:
        """获取任务详情。"""
        return self.tasks.get(task_id)

    def get_user_tasks(self, user_id: str) -> list[dict[str, Any]]:
        """获取用户的所有任务。"""
        return [
            t for t in self.tasks.values()
            if t.get("created_by") == user_id
        ]

    def get_all_tasks(self) -> list[dict[str, Any]]:
        """获取所有任务。"""
        return list(self.tasks.values())

    def update_task(self, task_id: str, **kwargs: Any) -> dict[str, Any] | None:
        """更新任务字段。"""
        task = self.tasks.get(task_id)
        if task is None:
            return None
        allowed_keys = {
            "title", "description", "status",
            "priority", "tags", "result", "thread_id",
        }
        for key, value in kwargs.items():
            if key in allowed_keys and value is not None:
                task[key] = value
        task["updated_at"] = _now_iso()
        return task

    def delete_task(self, task_id: str) -> bool:
        """删除任务。"""
        if task_id not in self.tasks:
            return False
        del self.tasks[task_id]
        return True

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
        return items[offset:offset + limit]

    def search_memories(
        self, query: str, top_k: int = 5, method: str = "keyword",
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

    def revoke_refresh_token(self, token: str) -> None:
        """将 refresh token 加入撤销列表。"""
        self.refresh_tokens.add(token)

    def is_token_revoked(self, token: str) -> bool:
        """检查 refresh token 是否已被撤销。"""
        return token in self.refresh_tokens


# 全局内存存储实例
store = MemoryStore()

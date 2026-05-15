"""
统一并发管理器

整合 LLM 并发控制、Agent 层级并发控制和工作流并发控制，
提供统一的并发管理接口。

架构设计：
- LLM 层并发控制：提供商级、模型级、请求类型级
- Agent 层并发控制：L1/L2/L3 层级
- 工作流层并发控制：全局工作流并发限制
"""

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.config.settings import get_settings
from src.orchestration.types import AgentLevel

logger = logging.getLogger(__name__)


class RequestType(str, Enum):
    """LLM 请求类型"""
    CHAT = "chat"
    COMPLETION = "completion"
    EMBEDDING = "embedding"
    TOOL_USE = "tool_use"


@dataclass
class ConcurrencyConfig:
    """并发控制配置

    Attributes:
        provider_limits: 提供商级并发限制
        model_limits: 模型级并发限制
        request_type_limits: 请求类型级并发限制
        agent_level_limits: Agent 层级并发限制
        workflow_limit: 工作流并发限制
        default_limit: 默认并发限制
    """
    provider_limits: dict[str, int] = field(default_factory=lambda: {
        "zhipu": 2,
        "openai": 10,
        "anthropic": 5,
    })
    model_limits: dict[str, int] = field(default_factory=lambda: {
        # OpenAI 模型
        "gpt-4": 3,
        "gpt-3.5-turbo": 8,
        # 智谱模型
        "glm-4": 2,
        "glm-3-turbo": 4,
        # Anthropic 模型
        "claude-3-opus": 2,
        "claude-3-sonnet": 4,
    })
    request_type_limits: dict[str, int] = field(default_factory=lambda: {
        RequestType.CHAT: 15,
        RequestType.COMPLETION: 10,
        RequestType.EMBEDDING: 20,
        RequestType.TOOL_USE: 12,
    })
    agent_level_limits: dict[AgentLevel, int] = field(default_factory=lambda: {
        AgentLevel.L1: 2,
        AgentLevel.L2: 10,
        AgentLevel.L3: 50,
    })
    workflow_limit: int = 20
    default_limit: int = 2


@dataclass
class ConcurrencyStats:
    """并发统计信息

    Attributes:
        llm_active_requests: LLM 活跃请求数
        llm_by_provider: 按提供商统计
        llm_by_model: 按模型统计
        llm_by_request_type: 按请求类型统计
        agent_active: Agent 活跃数
        agent_by_level: 按层级统计
        workflow_active: 工作流活跃数
        total_acquires: 总获取次数
        total_releases: 总释放次数
        total_timeouts: 总超时次数
        last_updated: 最后更新时间
    """
    llm_active_requests: int = 0
    llm_by_provider: dict[str, int] = field(default_factory=dict)
    llm_by_model: dict[str, int] = field(default_factory=dict)
    llm_by_request_type: dict[str, int] = field(default_factory=dict)
    agent_active: int = 0
    agent_by_level: dict[str, int] = field(default_factory=dict)
    workflow_active: int = 0
    total_acquires: int = 0
    total_releases: int = 0
    total_timeouts: int = 0
    last_updated: datetime = field(default_factory=datetime.now)


class LevelPrioritySemaphore:
    """按 AgentLevel 优先级分配的信号量。

    L1 优先于 L2，L2 优先于 L3。
    当有更高层级任务等待且有容量时，低层级任务会被阻塞。
    """

    def __init__(self, level_limits: dict[AgentLevel, int]) -> None:
        self._level_limits = level_limits
        self._level_active: dict[AgentLevel, int] = {l: 0 for l in AgentLevel}
        self._level_waiting: dict[AgentLevel, int] = {l: 0 for l in AgentLevel}
        self._waiters: list[asyncio.Future] = []

    async def acquire(self, level: AgentLevel, timeout: float | None = None) -> bool:
        """按优先级获取许可。如果更高层级有任务在等待且有容量，当前任务需要等待。"""
        self._level_waiting[level] += 1
        try:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._waiters.append(fut)

            deadline = None
            if timeout is not None:
                deadline = loop.time() + timeout

            try:
                while True:
                    higher_can_acquire = any(
                        self._level_waiting[hl] > 0
                        and self._level_active[hl] < self._level_limits[hl]
                        for hl in AgentLevel
                        if hl.value < level.value
                    )

                    at_capacity = self._level_active[level] >= self._level_limits[level]

                    if not higher_can_acquire and not at_capacity:
                        self._level_active[level] += 1
                        return True

                    if deadline is not None:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            return False
                        try:
                            await asyncio.wait_for(asyncio.shield(fut), timeout=remaining)
                        except TimeoutError:
                            return False
                        except asyncio.CancelledError:
                            return False
                    else:
                        await fut

                    fut = loop.create_future()
                    self._waiters.append(fut)
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)
        finally:
            self._level_waiting[level] -= 1

    def release(self, level: AgentLevel) -> None:
        """释放许可并同步唤醒等待者。"""
        self._level_active[level] = max(0, self._level_active[level] - 1)
        self._waiters = [w for w in self._waiters if not w.done()]
        for w in self._waiters:
            w.set_result(None)

    def wake_all(self) -> None:
        """唤醒所有等待者（用于 reset 场景）。"""
        for w in self._waiters:
            if not w.done():
                w.set_result(None)
        self._waiters.clear()


class ConcurrencyManager:
    """
    统一并发管理器

    提供多层级并发控制：
    1. LLM 层：提供商级、模型级、请求类型级
    2. Agent 层：L1/L2/L3 层级（按层级优先级分配）
    3. 工作流层：全局工作流并发限制

    特性：
    - 线程安全的单例模式
    - 支持超时获取
    - 提供统计接口
    - 向后兼容旧接口
    """

    _instance: "ConcurrencyManager | None" = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "ConcurrencyManager":
        """单例模式创建实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """初始化并发管理器"""
        if self._initialized:
            return

        self._initialized = True
        self._config = self._load_config()

        # LLM 信号量
        self._provider_semaphores: dict[str, asyncio.Semaphore] = {}
        self._model_semaphores: dict[str, asyncio.Semaphore] = {}
        self._request_type_semaphores: dict[str, asyncio.Semaphore] = {}
        self._default_semaphore: asyncio.Semaphore | None = None

        # Agent 层级优先级信号量
        self._agent_priority_semaphore: LevelPrioritySemaphore | None = None

        # 工作流信号量
        self._workflow_semaphore: asyncio.Semaphore | None = None

        # 活跃计数
        self._llm_active: dict[str, int] = {}
        self._agent_active: dict[AgentLevel, int] = {
            AgentLevel.L1: 0,
            AgentLevel.L2: 0,
            AgentLevel.L3: 0,
        }
        self._workflow_active: int = 0

        # 统计信息
        self._stats = ConcurrencyStats()

        logger.info("[并发管理器] 已初始化")

    def _load_config(self) -> ConcurrencyConfig:
        """从配置加载并发限制"""
        settings = get_settings()
        config = ConcurrencyConfig()

        # 加载提供商配置
        config.provider_limits = {
            "zhipu": getattr(settings, "llm_zhipu_max_concurrent", 2),
            "openai": getattr(settings, "llm_openai_max_concurrent", 10),
            "anthropic": getattr(settings, "llm_anthropic_max_concurrent", 5),
        }

        # 加载模型配置
        config.model_limits = {
            "gpt-4": getattr(settings, "llm_gpt4_max_concurrent", 3),
            "gpt-3.5-turbo": getattr(settings, "llm_gpt35_max_concurrent", 8),
            "glm-4": getattr(settings, "llm_glm4_max_concurrent", 2),
            "glm-3-turbo": getattr(settings, "llm_glm3_max_concurrent", 4),
            "claude-3-opus": getattr(settings, "llm_claude_opus_max_concurrent", 2),
            "claude-3-sonnet": getattr(settings, "llm_claude_sonnet_max_concurrent", 4),
        }

        # 加载请求类型配置
        config.request_type_limits = {
            RequestType.CHAT: getattr(settings, "llm_chat_max_concurrent", 15),
            RequestType.COMPLETION: getattr(settings, "llm_completion_max_concurrent", 10),
            RequestType.EMBEDDING: getattr(settings, "llm_embedding_max_concurrent", 20),
            RequestType.TOOL_USE: getattr(settings, "llm_tool_use_max_concurrent", 12),
        }

        # 默认限制
        config.default_limit = getattr(settings, "llm_default_max_concurrent", 2)

        return config

    def _ensure_semaphores_initialized(self) -> None:
        """确保信号量已初始化（延迟初始化）"""
        if self._default_semaphore is not None:
            return

        # 初始化提供商信号量
        for provider, limit in self._config.provider_limits.items():
            self._provider_semaphores[provider] = asyncio.Semaphore(limit)
            logger.debug("[并发管理器] 提供商信号量已初始化 | provider=%s, limit=%s", provider, limit)

        # 初始化模型信号量
        for model, limit in self._config.model_limits.items():
            self._model_semaphores[model] = asyncio.Semaphore(limit)
            logger.debug("[并发管理器] 模型信号量已初始化 | model=%s, limit=%s", model, limit)

        # 初始化请求类型信号量
        for request_type, limit in self._config.request_type_limits.items():
            self._request_type_semaphores[request_type] = asyncio.Semaphore(limit)
            logger.debug("[并发管理器] 请求类型信号量已初始化 | type=%s, limit=%s", request_type, limit)

        # 初始化默认信号量
        self._default_semaphore = asyncio.Semaphore(self._config.default_limit)
        logger.debug("[并发管理器] 默认信号量已初始化 | limit=%s", self._config.default_limit)

        # 初始化 Agent 层级优先级信号量
        self._agent_priority_semaphore = LevelPrioritySemaphore(self._config.agent_level_limits)
        logger.debug("[并发管理器] Agent 层级优先级信号量已初始化")

        # 初始化工作流信号量
        self._workflow_semaphore = asyncio.Semaphore(self._config.workflow_limit)
        logger.debug("[并发管理器] 工作流信号量已初始化 | limit=%s", self._config.workflow_limit)

    # ==================== LLM 并发控制 ====================

    def get_llm_semaphore(
        self,
        provider: str = "default",
        model: str | None = None,
        request_type: str | None = None,
    ) -> asyncio.Semaphore:
        """
        获取 LLM 并发控制信号量

        按优先级返回对应的信号量实例：
        1. 模型级信号量（如果指定且存在）
        2. 提供商级信号量（如果指定且存在）
        3. 请求类型级信号量（如果指定且存在）
        4. 默认信号量

        Args:
            provider: 提供商名称 (zhipu, openai, anthropic, default)
            model: 模型名称（可选）
            request_type: 请求类型（可选）

        Returns:
            asyncio.Semaphore: 信号量实例
        """
        self._ensure_semaphores_initialized()

        # 1. 优先使用模型级信号量
        if model and model in self._model_semaphores:
            return self._model_semaphores[model]

        # 2. 使用提供商级信号量
        if provider in self._provider_semaphores:
            return self._provider_semaphores[provider]

        # 3. 使用请求类型级信号量
        if request_type and request_type in self._request_type_semaphores:
            return self._request_type_semaphores[request_type]

        # 4. 返回默认信号量
        return self._default_semaphore or asyncio.Semaphore(2)

    async def acquire_llm(
        self,
        provider: str = "default",
        model: str | None = None,
        request_type: str | None = None,
        timeout: float | None = None,
    ) -> bool:
        """
        获取 LLM 调用许可

        Args:
            provider: 提供商名称
            model: 模型名称
            request_type: 请求类型
            timeout: 超时时间（秒），None 表示无限等待

        Returns:
            bool: 是否成功获取许可
        """
        self._ensure_semaphores_initialized()
        semaphore = self.get_llm_semaphore(provider, model, request_type)

        try:
            if timeout is not None:
                await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
            else:
                await semaphore.acquire()

            # 更新计数
            key = f"{provider}:{model or 'default'}:{request_type or 'default'}"
            self._llm_active[key] = self._llm_active.get(key, 0) + 1
            self._stats.total_acquires += 1
            self._stats.llm_active_requests += 1
            self._stats.llm_by_provider[provider] = self._stats.llm_by_provider.get(provider, 0) + 1
            if model:
                self._stats.llm_by_model[model] = self._stats.llm_by_model.get(model, 0) + 1
            if request_type:
                self._stats.llm_by_request_type[request_type] = self._stats.llm_by_request_type.get(request_type, 0) + 1
            self._stats.last_updated = datetime.now()

            logger.debug("[并发管理器] LLM 许可已获取 | provider=%s, model=%s", provider, model)
            return True

        except TimeoutError:
            self._stats.total_timeouts += 1
            logger.warning("[并发管理器] 获取 LLM 信号量超时 | provider=%s, model=%s, timeout=%s", provider, model, timeout)
            return False

    def release_llm(
        self,
        provider: str = "default",
        model: str | None = None,
        request_type: str | None = None,
    ) -> None:
        """
        释放 LLM 调用许可

        Args:
            provider: 提供商名称
            model: 模型名称
            request_type: 请求类型
        """
        semaphore = self.get_llm_semaphore(provider, model, request_type)

        try:
            semaphore.release()

            # 更新计数
            key = f"{provider}:{model or 'default'}:{request_type or 'default'}"
            if key in self._llm_active and self._llm_active[key] > 0:
                self._llm_active[key] -= 1
            self._stats.total_releases += 1
            self._stats.llm_active_requests = max(0, self._stats.llm_active_requests - 1)
            if provider in self._stats.llm_by_provider and self._stats.llm_by_provider[provider] > 0:
                self._stats.llm_by_provider[provider] -= 1
            if model and model in self._stats.llm_by_model and self._stats.llm_by_model[model] > 0:
                self._stats.llm_by_model[model] -= 1
            if request_type and request_type in self._stats.llm_by_request_type and self._stats.llm_by_request_type[request_type] > 0:
                self._stats.llm_by_request_type[request_type] -= 1
            self._stats.last_updated = datetime.now()

            logger.debug("[并发管理器] LLM 许可已释放 | provider=%s, model=%s", provider, model)

        except ValueError:
            # 信号量已经释放，忽略错误
            pass

    @asynccontextmanager
    async def llm_context(
        self,
        provider: str = "default",
        model: str | None = None,
        request_type: str | None = None,
        timeout: float | None = None,
    ):
        """
        LLM 并发控制上下文管理器

        Args:
            provider: 提供商名称
            model: 模型名称
            request_type: 请求类型
            timeout: 超时时间（秒）

        Yields:
            bool: 是否成功获取许可
        """
        acquired = await self.acquire_llm(provider, model, request_type, timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self.release_llm(provider, model, request_type)

    # ==================== Agent 并发控制 ====================

    async def acquire_agent(
        self,
        level: AgentLevel,
        timeout: float | None = None,
    ) -> bool:
        """
        获取 Agent 执行许可（按层级优先级）

        L1 优先于 L2，L2 优先于 L3。
        当有更高层级任务等待时，低层级任务会被阻塞。

        Args:
            level: Agent 层级
            timeout: 超时时间（秒），None 表示无限等待

        Returns:
            bool: 是否成功获取许可
        """
        self._ensure_semaphores_initialized()

        acquired = await self._agent_priority_semaphore.acquire(level, timeout)
        if not acquired:
            self._stats.total_timeouts += 1
            logger.warning("[并发管理器] 获取 Agent 信号量超时 | level=%s, timeout=%s", level.name, timeout)
            return False

        # 更新计数
        self._agent_active[level] += 1
        self._stats.total_acquires += 1
        self._stats.agent_active += 1
        level_name = level.name
        self._stats.agent_by_level[level_name] = self._stats.agent_by_level.get(level_name, 0) + 1
        self._stats.last_updated = datetime.now()

        logger.debug("[并发管理器] Agent 许可已获取 | level=%s", level.name)
        return True

    def release_agent(self, level: AgentLevel) -> None:
        """
        释放 Agent 执行许可

        Args:
            level: Agent 层级
        """
        if self._agent_active[level] <= 0:
            return

        self._agent_active[level] -= 1
        self._stats.total_releases += 1
        self._stats.agent_active = max(0, self._stats.agent_active - 1)
        level_name = level.name
        if level_name in self._stats.agent_by_level and self._stats.agent_by_level[level_name] > 0:
            self._stats.agent_by_level[level_name] -= 1
        self._stats.last_updated = datetime.now()

        if self._agent_priority_semaphore is not None:
            self._agent_priority_semaphore.release(level)

        logger.debug("[并发管理器] Agent 许可已释放 | level=%s", level.name)

    @asynccontextmanager
    async def agent_context(
        self,
        level: AgentLevel,
        timeout: float | None = None,
    ):
        """
        Agent 并发控制上下文管理器

        Args:
            level: Agent 层级
            timeout: 超时时间（秒）

        Yields:
            bool: 是否成功获取许可
        """
        acquired = await self.acquire_agent(level, timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self.release_agent(level)

    # ==================== 工作流并发控制 ====================

    async def acquire_workflow(self, timeout: float | None = None) -> bool:
        """
        获取工作流执行许可

        Args:
            timeout: 超时时间（秒），None 表示无限等待

        Returns:
            bool: 是否成功获取许可
        """
        self._ensure_semaphores_initialized()

        try:
            if timeout is not None:
                await asyncio.wait_for(self._workflow_semaphore.acquire(), timeout=timeout)
            else:
                await self._workflow_semaphore.acquire()

            # 更新计数
            self._workflow_active += 1
            self._stats.total_acquires += 1
            self._stats.workflow_active += 1
            self._stats.last_updated = datetime.now()

            logger.debug("[并发管理器] 工作流许可已获取")
            return True

        except TimeoutError:
            self._stats.total_timeouts += 1
            logger.warning("[并发管理器] 获取工作流信号量超时 | timeout=%s", timeout)
            return False

    def release_workflow(self) -> None:
        """释放工作流执行许可"""
        # 确保信号量已初始化
        self._ensure_semaphores_initialized()

        if self._workflow_semaphore is None:
            return

        try:
            self._workflow_semaphore.release()

            # 更新计数
            if self._workflow_active > 0:
                self._workflow_active -= 1
            self._stats.total_releases += 1
            self._stats.workflow_active = max(0, self._stats.workflow_active - 1)
            self._stats.last_updated = datetime.now()

            logger.debug("[并发管理器] 工作流许可已释放")

        except ValueError:
            # 信号量已经释放，忽略错误
            pass

    @asynccontextmanager
    async def workflow_context(self, timeout: float | None = None):
        """
        工作流并发控制上下文管理器

        Args:
            timeout: 超时时间（秒）

        Yields:
            bool: 是否成功获取许可
        """
        acquired = await self.acquire_workflow(timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self.release_workflow()

    # ==================== 统计接口 ====================

    def get_stats(self) -> dict[str, Any]:
        """
        获取并发统计信息

        Returns:
            dict: 统计信息
        """
        # 确保 agent_by_level 包含所有层级
        agent_by_level = {}
        for level in AgentLevel:
            agent_by_level[level.name] = self._stats.agent_by_level.get(level.name, 0)

        return {
            "llm": {
                "active_requests": self._stats.llm_active_requests,
                "by_provider": dict(self._stats.llm_by_provider),
                "by_model": dict(self._stats.llm_by_model),
                "by_request_type": dict(self._stats.llm_by_request_type),
            },
            "agents": {
                "active": self._stats.agent_active,
                "by_level": agent_by_level,
            },
            "workflows": {
                "active": self._stats.workflow_active,
            },
            "totals": {
                "acquires": self._stats.total_acquires,
                "releases": self._stats.total_releases,
                "timeouts": self._stats.total_timeouts,
            },
            "last_updated": self._stats.last_updated.isoformat(),
        }

    def get_config(self) -> dict[str, Any]:
        """
        获取当前配置

        Returns:
            dict: 配置信息
        """
        return {
            "provider_limits": dict(self._config.provider_limits),
            "model_limits": dict(self._config.model_limits),
            "request_type_limits": {k.value if isinstance(k, Enum) else k: v for k, v in self._config.request_type_limits.items()},
            "agent_level_limits": {k.name: v for k, v in self._config.agent_level_limits.items()},
            "workflow_limit": self._config.workflow_limit,
            "default_limit": self._config.default_limit,
        }

    # ==================== 管理接口 ====================

    def reset(self) -> None:
        """
        重置并发管理器

        主要用于测试，生产环境一般不需要调用
        """
        if self._agent_priority_semaphore is not None:
            self._agent_priority_semaphore.wake_all()
        self._provider_semaphores.clear()
        self._model_semaphores.clear()
        self._request_type_semaphores.clear()
        self._default_semaphore = None
        self._agent_priority_semaphore = None
        self._workflow_semaphore = None
        self._llm_active.clear()
        self._agent_active = {
            AgentLevel.L1: 0,
            AgentLevel.L2: 0,
            AgentLevel.L3: 0,
        }
        self._workflow_active = 0
        self._stats = ConcurrencyStats()
        logger.info("[并发管理器] 已重置")

    @classmethod
    def get_instance(cls) -> "ConcurrencyManager":
        """
        获取单例实例

        Returns:
            ConcurrencyManager: 并发管理器实例
        """
        return cls()


# 导出公共接口
__all__ = [
    "ConcurrencyManager",
    "ConcurrencyConfig",
    "ConcurrencyStats",
    "RequestType",
]

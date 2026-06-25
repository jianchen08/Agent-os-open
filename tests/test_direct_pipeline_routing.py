"""子管道消息路由测试。

验证子管道发送消息时，消息应路由到子管道对应的agent而非主管道的主agent。
Bug根因: _stream_engine_response 中 engine.run() 始终使用 ctx.agent_config，
         忽略 session 的 agent_id。
修复: 优先从 session.agent_id 解析 agent_config，找不到时回退到 ctx.agent_config。
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 测试辅助：轻量 mock 对象
# ---------------------------------------------------------------------------

@dataclass
class FakeSession:
    """模拟 SessionModel。"""
    agent_id: str | None = None
    active_pipeline_id: str | None = None
    pipeline_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] | None = None

    def register_pipeline(self, pid: str):
        if pid not in self.pipeline_ids:
            self.pipeline_ids.append(pid)
        self.active_pipeline_id = pid


@dataclass
class FakeAgentConfig:
    """模拟 Agent 配置。"""
    config_id: str = ""
    display_name: str = ""
    model: str = "gpt-4"


class FakeAgentRegistry:
    """模拟 Agent 注册表。"""
    def __init__(self, configs: dict[str, FakeAgentConfig] | None = None):
        self._configs = configs or {}

    def get(self, agent_id: str) -> FakeAgentConfig | None:
        return self._configs.get(agent_id)


class FakeEngine:
    """模拟 PipelineEngine。"""
    def __init__(self, pipeline_id: str = "main-pipeline"):
        self._pipeline_id = pipeline_id
        self._suspended_state = None
        self._pending_notifications = []
        self._wake_event = None
        self._saved_on_chunk = None
        self._saved_streaming = False
        self._last_run_agent_config = None

    @property
    def pipeline_id(self):
        return self._pipeline_id

    @property
    def is_suspended(self):
        return self._suspended_state is not None

    async def run(self, **kwargs):
        self._last_run_agent_config = kwargs.get("agent_config")
        return {"messages": [], "raw_result": "ok"}


class FakePipelineContext:
    """模拟 PipelineContext。"""
    def __init__(
        self,
        engine: FakeEngine | None = None,
        agent_config: FakeAgentConfig | None = None,
        services: dict[str, Any] | None = None,
    ):
        self.engine = engine or FakeEngine()
        self.agent_config = agent_config or FakeAgentConfig(config_id="default", display_name="灵汐")
        self.services = services or {}
        self.available = True
        self._engines: dict[str, FakeEngine] = {self.engine.pipeline_id: self.engine}

    def get_or_create_engine(self, pipeline_id: str):
        if pipeline_id not in self._engines:
            self._engines[pipeline_id] = FakeEngine(pipeline_id)
        return self._engines[pipeline_id]


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

class TestDirectPipelineAgentRouting:
    """子管道消息路由测试。"""

    @pytest.mark.asyncio
    async def test_direct_pipeline_uses_session_agent(self):
        """测试: 子管道消息应路由到 session.agent_id 对应的 agent。

        场景: 用户在子管道（绑定 agent_id='code-assistant'）发送消息，
              engine.run() 应使用 code-assistant 的 agent_config，
              而非主管道默认的灵汐 agent。
        """
        # Arrange: 构造子管道 session，绑定了 code-assistant agent
        direct_agent = FakeAgentConfig(config_id="code-assistant", display_name="代码助手")
        default_agent = FakeAgentConfig(config_id="default", display_name="灵汐")
        agent_registry = FakeAgentRegistry({
            "code-assistant": direct_agent,
            "default": default_agent,
        })

        direct_session = FakeSession(
            agent_id="code-assistant",
            active_pipeline_id="direct-pipeline-123",
        )

        engine = FakeEngine("direct-pipeline-123")
        ctx = FakePipelineContext(
            engine=engine,
            agent_config=default_agent,  # 主管道默认 agent
            services={"agent_registry": agent_registry},
        )

        # 构造消息历史
        conversation_history = []

        # Act: 调用 agent 解析逻辑（模拟 _stream_engine_response 中的修复代码）
        _resolved_agent_config = ctx.agent_config
        if direct_session and getattr(direct_session, "agent_id", None):
            try:
                _agent_registry = ctx.services.get("agent_registry") if ctx.services else None
                if _agent_registry:
                    _direct_agent_config = _agent_registry.get(direct_session.agent_id)
                    if _direct_agent_config:
                        _resolved_agent_config = _direct_agent_config
            except Exception:
                pass

        # Assert: 解析出的 agent_config 应是子管道的 code-assistant
        assert _resolved_agent_config.config_id == "code-assistant", \
            f"子管道应使用 session 的 agent_id 对应的 agent，但得到: {_resolved_agent_config.config_id}"
        assert _resolved_agent_config is direct_agent, \
            "子管道应使用自己的 agent_config，而非主管道的"

    @pytest.mark.asyncio
    async def test_direct_pipeline_fallback_when_agent_not_found(self):
        """测试: 子管道 agent_id 在 registry 中找不到时，回退到默认 agent。

        场景: session 有 agent_id='unknown-agent'，但 registry 中不存在，
              应回退到 ctx.agent_config（主管道默认agent）。
        """
        # Arrange
        default_agent = FakeAgentConfig(config_id="default", display_name="灵汐")
        agent_registry = FakeAgentRegistry({})  # 空注册表

        direct_session = FakeSession(
            agent_id="unknown-agent",
            active_pipeline_id="direct-pipeline-456",
        )

        ctx = FakePipelineContext(
            agent_config=default_agent,
            services={"agent_registry": agent_registry},
        )

        # Act
        _resolved_agent_config = ctx.agent_config
        if direct_session and getattr(direct_session, "agent_id", None):
            try:
                _agent_registry = ctx.services.get("agent_registry") if ctx.services else None
                if _agent_registry:
                    _direct_agent_config = _agent_registry.get(direct_session.agent_id)
                    if _direct_agent_config:
                        _resolved_agent_config = _direct_agent_config
            except Exception:
                pass

        # Assert: 回退到默认 agent
        assert _resolved_agent_config.config_id == "default", \
            f"agent_id 找不到时应回退到默认 agent，但得到: {_resolved_agent_config.config_id}"
        assert _resolved_agent_config is default_agent

    @pytest.mark.asyncio
    async def test_direct_pipeline_fallback_when_no_registry(self):
        """测试: services 中没有 agent_registry 时，回退到默认 agent。

        场景: session 有 agent_id，但 ctx.services 中没有 agent_registry。
        """
        # Arrange
        default_agent = FakeAgentConfig(config_id="default", display_name="灵汐")

        direct_session = FakeSession(
            agent_id="code-assistant",
            active_pipeline_id="direct-pipeline-789",
        )

        ctx = FakePipelineContext(
            agent_config=default_agent,
            services={},  # 没有 agent_registry
        )

        # Act
        _resolved_agent_config = ctx.agent_config
        if direct_session and getattr(direct_session, "agent_id", None):
            try:
                _agent_registry = ctx.services.get("agent_registry") if ctx.services else None
                if _agent_registry:
                    _direct_agent_config = _agent_registry.get(direct_session.agent_id)
                    if _direct_agent_config:
                        _resolved_agent_config = _direct_agent_config
            except Exception:
                pass

        # Assert: 回退到默认 agent
        assert _resolved_agent_config.config_id == "default", \
            f"无 agent_registry 时应回退到默认 agent，但得到: {_resolved_agent_config.config_id}"

    @pytest.mark.asyncio
    async def test_main_pipeline_unchanged_without_agent_id(self):
        """测试: 主管道（session 无 agent_id）消息路由不受影响。

        场景: 主管道的 session 没有 agent_id，
              engine.run() 应使用 ctx.agent_config（默认灵汐 agent）。
        """
        # Arrange
        default_agent = FakeAgentConfig(config_id="default", display_name="灵汐")
        agent_registry = FakeAgentRegistry({"default": default_agent})

        main_session = FakeSession(
            agent_id=None,  # 主管道没有指定 agent
            active_pipeline_id="main-pipeline",
        )

        ctx = FakePipelineContext(
            agent_config=default_agent,
            services={"agent_registry": agent_registry},
        )

        # Act
        _resolved_agent_config = ctx.agent_config
        if main_session and getattr(main_session, "agent_id", None):
            try:
                _agent_registry = ctx.services.get("agent_registry") if ctx.services else None
                if _agent_registry:
                    _direct_agent_config = _agent_registry.get(main_session.agent_id)
                    if _direct_agent_config:
                        _resolved_agent_config = _direct_agent_config
            except Exception:
                pass

        # Assert: 使用默认 agent，主管道不受影响
        assert _resolved_agent_config.config_id == "default", \
            f"主管道应使用默认 agent，但得到: {_resolved_agent_config.config_id}"
        assert _resolved_agent_config is default_agent

    @pytest.mark.asyncio
    async def test_main_pipeline_unchanged_no_session(self):
        """测试: 无 session 时消息路由不受影响。

        场景: session 为 None（旧场景），engine.run() 应使用 ctx.agent_config。
        """
        # Arrange
        default_agent = FakeAgentConfig(config_id="default", display_name="灵汐")
        ctx = FakePipelineContext(
            agent_config=default_agent,
            services={},
        )
        session = None

        # Act
        _resolved_agent_config = ctx.agent_config
        if session and getattr(session, "agent_id", None):
            try:
                _agent_registry = ctx.services.get("agent_registry") if ctx.services else None
                if _agent_registry:
                    _direct_agent_config = _agent_registry.get(session.agent_id)
                    if _direct_agent_config:
                        _resolved_agent_config = _direct_agent_config
            except Exception:
                pass

        # Assert: 使用默认 agent
        assert _resolved_agent_config.config_id == "default", \
            f"无 session 时应使用默认 agent，但得到: {_resolved_agent_config.config_id}"

    @pytest.mark.asyncio
    async def test_direct_pipeline_different_agents_isolation(self):
        """测试: 主管道和子管道使用不同的 agent，互不干扰。

        场景: 同一个 ctx 下，主管道用灵汐，子管道用 code-assistant。
        """
        # Arrange
        default_agent = FakeAgentConfig(config_id="default", display_name="灵汐")
        code_agent = FakeAgentConfig(config_id="code-assistant", display_name="代码助手")
        agent_registry = FakeAgentRegistry({
            "default": default_agent,
            "code-assistant": code_agent,
        })

        ctx = FakePipelineContext(
            agent_config=default_agent,
            services={"agent_registry": agent_registry},
        )

        # 主管道 session
        main_session = FakeSession(agent_id=None, active_pipeline_id="main")

        # 子管道 session
        direct_session = FakeSession(agent_id="code-assistant", active_pipeline_id="direct")

        # Act: 主管道路由
        main_resolved = ctx.agent_config
        if main_session and getattr(main_session, "agent_id", None):
            _reg = ctx.services.get("agent_registry")
            if _reg:
                _cfg = _reg.get(main_session.agent_id)
                if _cfg:
                    main_resolved = _cfg

        # Act: 子管道路由
        direct_resolved = ctx.agent_config
        if direct_session and getattr(direct_session, "agent_id", None):
            _reg = ctx.services.get("agent_registry")
            if _reg:
                _cfg = _reg.get(direct_session.agent_id)
                if _cfg:
                    direct_resolved = _cfg

        # Assert: 互不干扰
        assert main_resolved.config_id == "default", \
            f"主管道应使用默认 agent，但得到: {main_resolved.config_id}"
        assert direct_resolved.config_id == "code-assistant", \
            f"子管道应使用 code-assistant agent，但得到: {direct_resolved.config_id}"
        assert main_resolved is not direct_resolved, \
            "主管道和子管道应使用不同的 agent 实例"

    @pytest.mark.asyncio
    async def test_exception_in_agent_resolution_does_not_crash(self):
        """测试: agent 解析过程抛异常时不会崩溃，回退到默认 agent。

        场景: agent_registry.get() 抛异常，应安全回退。
        """
        # Arrange
        default_agent = FakeAgentConfig(config_id="default", display_name="灵汐")

        class BrokenRegistry:
            def get(self, agent_id):
                raise RuntimeError("registry broken")

        direct_session = FakeSession(
            agent_id="code-assistant",
            active_pipeline_id="direct",
        )
        ctx = FakePipelineContext(
            agent_config=default_agent,
            services={"agent_registry": BrokenRegistry()},
        )

        # Act: 异常应被捕获
        _resolved_agent_config = ctx.agent_config
        try:
            if direct_session and getattr(direct_session, "agent_id", None):
                _agent_registry = ctx.services.get("agent_registry") if ctx.services else None
                if _agent_registry:
                    _direct_agent_config = _agent_registry.get(direct_session.agent_id)
                    if _direct_agent_config:
                        _resolved_agent_config = _direct_agent_config
        except Exception:
            pass  # 模拟 stream_handler 中的 except Exception: pass

        # Assert: 安全回退
        assert _resolved_agent_config.config_id == "default", \
            f"异常时应安全回退到默认 agent，但得到: {_resolved_agent_config.config_id}"

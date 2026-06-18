"""禁止静默降级到默认 Agent（灵汐/lingxi）的回归测试。

覆盖修复点（P0-安全）：
- WS 消息入口：三层（子管道→线程→ctx默认）解析不到 agent 时，不再回退到
  启动加载的默认 Agent，直接发 NO_AGENT_CONFIGURED。
- _resolve_agent_from_thread：解析失败日志不再暗示降级。
- checkpoint recovery._load_agent_base_state：找不到原始 Agent 返回空 dict，
  docstring/日志不再提"回退到默认 Agent"。
- routes_tasks.create_task：agent_id 为空时 400 拒绝（不创建无执行者的任务）。
- routes_threads.update_thread_agent：agent_id 不在 registry 时 400 拒绝。

设计原则（参考 tests/test_isolation_fallback.py）：
- 直接导入被测真实模块（不重写算法副本），用 monkeypatch/patch 注入伪依赖。
- fail-closed 断言用 `pytest.raises` 或 `success is False` + 错误串匹配。
- 每个被测类一个 TestXxx，带 docstring 说明覆盖契约。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 确保 src 在 import 路径上（与 test_agent_config_guard.py 同套路）
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ═══════════════════════════════════════════════════════════════
# P0: checkpoint recovery._load_agent_base_state — 禁止回退到默认 Agent
# ═══════════════════════════════════════════════════════════════


class TestCheckpointRecoveryNoFallback:
    """P0: 找不到原始 Agent 时返回空状态，禁止静默回退到默认 Agent。

    覆盖契约：_load_agent_base_state 在 agent_config_id 缺失/registry 未命中/
    抛异常时，一律返回空 dict，绝不返回默认 Agent 的状态。
    """

    @staticmethod
    def _make_recovery():
        from infrastructure.checkpoint.recovery import PipelineRecovery
        return PipelineRecovery(checkpoint_manager=MagicMock())

    def test_missing_agent_config_id_returns_empty(self, monkeypatch):
        """agent_config_id 为空时返回空 dict。"""
        recovery = self._make_recovery()

        result = recovery._load_agent_base_state(agent_config_id=None)

        assert result == {}, "无 agent_config_id 必须返回空 dict"

    def test_registry_miss_returns_empty(self, monkeypatch):
        """agent_config_id 在 registry 未命中时返回空 dict，不回退到默认。"""
        # 伪 service_provider：registry.get 一律返回 None
        fake_provider = MagicMock()
        fake_registry = MagicMock()
        fake_registry.get.return_value = None
        fake_provider.get.return_value = fake_registry
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            lambda: fake_provider,
        )
        recovery = self._make_recovery()

        result = recovery._load_agent_base_state(agent_config_id="nonexistent")

        assert result == {}, "registry 未命中必须返回空 dict，禁止回退到默认 Agent"
        # registry.get 确实被查询过
        fake_registry.get.assert_called_once_with("nonexistent")

    def test_load_exception_returns_empty(self, monkeypatch):
        """registry 查询抛异常时返回空 dict，不崩溃不回退。"""
        fake_provider = MagicMock()
        fake_registry = MagicMock()
        fake_registry.get.side_effect = RuntimeError("boom")
        fake_provider.get.return_value = fake_registry
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            lambda: fake_provider,
        )
        recovery = self._make_recovery()

        result = recovery._load_agent_base_state(agent_config_id="broken")

        assert result == {}, "registry 异常时必须返回空 dict，禁止回退"

    def test_no_provider_returns_empty(self, monkeypatch):
        """service_provider 不可用时返回空 dict。"""
        def _raise():
            raise RuntimeError("no provider")
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            _raise,
        )
        recovery = self._make_recovery()

        result = recovery._load_agent_base_state(agent_config_id="some-id")

        assert result == {}

    def test_found_agent_returns_its_state(self, monkeypatch):
        """对照用例：找到原始 Agent 时正常返回它的 to_state()。"""
        fake_agent = MagicMock()
        fake_agent.to_state.return_value = {"system_prompt": "x", "tool_ids": ["a"]}
        fake_registry = MagicMock()
        fake_registry.get.return_value = fake_agent
        fake_provider = MagicMock()
        fake_provider.get.return_value = fake_registry
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            lambda: fake_provider,
        )
        recovery = self._make_recovery()

        result = recovery._load_agent_base_state(agent_config_id="real-agent")

        assert result == {"system_prompt": "x", "tool_ids": ["a"]}


# ═══════════════════════════════════════════════════════════════
# P0: app_factory._resolve_agent_from_thread — 解析失败不暗示降级
# ═══════════════════════════════════════════════════════════════


class TestResolveAgentFromThreadNoFallbackHint:
    """P0: 线程 agent 解析失败时返回 None，日志不得暗示降级到默认 Agent。

    覆盖契约：找不到 agent 时函数返回 None（fail-closed 由调用方负责），
    不存在"自动用默认 agent"的语义。
    """

    def test_agent_not_in_registry_returns_none(self, monkeypatch):
        from channels.websocket import app_factory

        # 伪 api_store：线程指定了 agent_id='ghost'
        fake_store = MagicMock()
        fake_store.get_thread.return_value = {"agent_id": "ghost"}
        monkeypatch.setattr(app_factory, "api_store", fake_store)

        # 伪 registry：找不到 ghost
        fake_registry = MagicMock()
        fake_registry.get.return_value = None
        fake_registry.list_all.return_value = []
        fake_provider = MagicMock()
        fake_provider.get.return_value = fake_registry
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            lambda: fake_provider,
        )

        result = app_factory._resolve_agent_from_thread("thread-1")

        assert result is None, "找不到 agent 必须返回 None，由调用方 fail-closed"

    def test_thread_without_agent_id_returns_none(self, monkeypatch):
        from channels.websocket import app_factory

        fake_store = MagicMock()
        fake_store.get_thread.return_value = {"agent_id": ""}
        monkeypatch.setattr(app_factory, "api_store", fake_store)

        result = app_factory._resolve_agent_from_thread("thread-2")

        assert result is None


# ═══════════════════════════════════════════════════════════════
# P0: app_factory WS 入口 — 三层解析不到直接 fail（不再 ctx 兜底）
# ═══════════════════════════════════════════════════════════════


class TestWSMessageEntryNoFallback:
    """P0: WS 入口移除了 _pipeline_ctx.agent_config（灵汐）兜底分支。

    覆盖契约：源码中不得存在"用 _pipeline_ctx.agent_config 作为最终回退"
    的赋值语句；找不到 agent 的唯一归宿是发 NO_AGENT_CONFIGURED 错误。
    用源码静态检查锁定（避免复杂的 WebSocket 协程 mock）。
    """

    def test_no_ctx_agent_config_fallback_in_source(self):
        """WS 入口分支不得再用 _pipeline_ctx.agent_config 兜底。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "channels" / "websocket" / "app_factory.py"
        ).read_text(encoding="utf-8")

        # 旧的兜底赋值语句必须消失
        assert "_agent_config = _pipeline_ctx.agent_config" not in src, (
            "app_factory.py 不得再用 _pipeline_ctx.agent_config 作为最终回退，"
            "那是静默降级到默认 Agent（灵汐）的入口"
        )
        # "最终回退：系统启动时加载的默认 Agent（lingxi）" 注释必须消失
        assert "系统启动时加载的默认 Agent" not in src, (
            "不得保留'回退到默认 Agent'的注释，会误导维护者重新引入降级"
        )

    def test_fail_closed_error_code_present(self):
        """三层解析不到 agent 时必须发 NO_AGENT_CONFIGURED。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "channels" / "websocket" / "app_factory.py"
        ).read_text(encoding="utf-8")
        assert "NO_AGENT_CONFIGURED" in src
        assert "禁止静默降级到默认 Agent" in src


# ═══════════════════════════════════════════════════════════════
# P0: routes_tasks.create_task — 空 agent_id 在创建期拦截
# ═══════════════════════════════════════════════════════════════


class TestTaskCreationRejectsEmptyAgent:
    """P0: 创建任务必须显式指定 agent_id，空值在创建期直接 400。

    覆盖契约：源码中 create_task 在构造 TaskModel 前必须校验 body.agent_id
    非空，且错误码为 MISSING_TARGET_AGENT。用静态检查锁定分支存在。
    """

    def test_create_task_validates_agent_id(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "channels" / "api" / "routes_tasks.py"
        ).read_text(encoding="utf-8")

        assert "MISSING_TARGET_AGENT" in src, (
            "create_task 必须在创建期校验 agent_id 非空并返回 MISSING_TARGET_AGENT"
        )
        assert "if not body.agent_id" in src, (
            "create_task 必须有 if not body.agent_id 的前置校验分支"
        )

    def test_no_empty_target_id_in_task_model(self):
        """TaskModel 构造处不得再出现 target_id 的空值兜底。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "channels" / "api" / "routes_tasks.py"
        ).read_text(encoding="utf-8")
        # 旧的 body.agent_id or "" 兜底赋值必须消失
        assert '"target_id": body.agent_id or ""' not in src, (
            "create_task 不得再用 'body.agent_id or \"\"' 兜底空 target_id"
        )


# ═══════════════════════════════════════════════════════════════
# P0: routes_threads.update_thread_agent — 无效 agent_id 在写入前拦截
# ═══════════════════════════════════════════════════════════════


class TestThreadAgentUpdateValidates:
    """P0: 线程绑定 agent 前校验 agent_id 在 registry 存在。

    覆盖契约：update_thread_agent 在写 store 前必须查 agent_registry，
    不存在则返回 AGENT_NOT_FOUND，防止线程存入无效 agent_id 后在 WS 入口静默降级。
    """

    def test_update_thread_validates_agent_in_registry(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "channels" / "api" / "routes_threads.py"
        ).read_text(encoding="utf-8")

        assert "AGENT_NOT_FOUND" in src, (
            "update_thread_agent 必须在 agent_id 不存在时返回 AGENT_NOT_FOUND"
        )
        assert "agent_registry.get(agent_id) is None" in src, (
            "update_thread_agent 必须查询 agent_registry.get(agent_id) 做存在性校验"
        )

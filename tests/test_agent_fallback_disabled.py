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
from unittest.mock import AsyncMock, MagicMock

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


class TestWSMessageEntryNoFallback:
    """P0: WS 入口只做转发，完全不解析/猜测 agent。

    架构原则：路口（WS 入口）不区分来源、不碰 agent。agent 由数据源决定：
    - 主管道：会话映射 thread["agent_id"]（注册时写入 tags）
    - 子任务管道：任务数据 task.metadata["target_id"]（注册时写入 tags）
    引擎 idle 重启用引擎自带 _agent_config；revive 从持久化数据重建。
    WS 入口只负责 pipeline_id → 注册表找引擎 → 转发消息。
    """

    def test_no_agent_resolution_in_ws_entry(self):
        """WS 入口源码中不得再有 agent 解析函数。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "channels" / "websocket" / "app_factory.py"
        ).read_text(encoding="utf-8")
        assert "_resolve_sub_pipeline_agent_config" not in src, (
            "WS 入口不得再有 _resolve_sub_pipeline_agent_config——路口不解析 agent"
        )
        assert "_resolve_agent_from_thread" not in src, (
            "WS 入口不得再有 _resolve_agent_from_thread——路口不解析 agent"
        )

    def test_handle_incoming_message_no_agent_config(self):
        """handle_incoming_message 调用不得传 agent_config（只转发）。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "channels" / "websocket" / "app_factory.py"
        ).read_text(encoding="utf-8")
        # 调用处不应有 agent_config= 参数
        assert "agent_config=_agent_config" not in src, (
            "handle_incoming_message 调用不得传 agent_config——WS 入口只转发"
        )

    def test_register_tags_has_agent_id(self):
        """会话创建时 api_store 存储 agent_id（会话模块负责，谁创建谁重建）。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "channels" / "api" / "routes_threads.py"
        ).read_text(encoding="utf-8")
        assert '"agent_id": agent_id' in src, (
            "create_thread 必须把 agent_id 写入 api_store——谁创建谁负责"
        )


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


# ═══════════════════════════════════════════════════════════════
# P0: pipeline_reviver — 检查点作为 agent 恢复第二数据源
# ═══════════════════════════════════════════════════════════════


class TestReviveFromCheckpoint:
    """P0: try_revive_pipeline 接入检查点 agent_config_id 恢复 agent 身份。

    覆盖契约：task.target_id 关联断裂时，从检查点的 agent_config_id 恢复
    正确 agent，而非 fail-closed 或降级到默认 Agent。
    """

    def test_load_agent_from_checkpoint_function_exists(self):
        """pipeline_reviver 必须有 _load_agent_from_checkpoint 函数。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "pipeline" / "pipeline_reviver.py"
        ).read_text(encoding="utf-8")
        assert "async def _load_agent_from_checkpoint" in src, (
            "pipeline_reviver 必须实现 _load_agent_from_checkpoint，"
            "作为 task.target_id 失败时的第二数据源"
        )

    def test_revive_uses_checkpoint_as_second_source(self):
        """try_revive_pipeline 在 task.target_id 失败后必须尝试检查点。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "pipeline" / "pipeline_reviver.py"
        ).read_text(encoding="utf-8")
        assert "_load_agent_from_checkpoint(pipeline_id, provider)" in src, (
            "try_revive_pipeline 必须在 _load_agent_config 失败后调用"
            "_load_agent_from_checkpoint 作为第二数据源"
        )

    @pytest.mark.asyncio
    async def test_load_agent_from_checkpoint_returns_agent(self):
        """检查点有 agent_config_id 且 registry 命中 → 返回该 agent。"""
        from pipeline.pipeline_reviver import _load_agent_from_checkpoint

        fake_agent = MagicMock()
        fake_agent.config_id = "code-assistant"
        fake_registry = MagicMock()
        fake_registry.get.return_value = fake_agent
        fake_checkpoint_mgr = MagicMock()
        fake_checkpoint_mgr.get_latest = AsyncMock(return_value={
            "state": {"agent_config_id": "code-assistant"},
            "metadata": {},
        })
        fake_provider = MagicMock()
        fake_provider.get.side_effect = lambda k: {
            "checkpoint_manager": fake_checkpoint_mgr,
            "agent_registry": fake_registry,
        }.get(k)

        result = await _load_agent_from_checkpoint("pipe-123", fake_provider)

        assert result is fake_agent, "检查点恢复应返回 agent_config_id 对应的 agent"

    @pytest.mark.asyncio
    async def test_load_agent_from_checkpoint_no_checkpoint_returns_none(self):
        """无检查点 → 返回 None（由上层 fail-closed）。"""
        from pipeline.pipeline_reviver import _load_agent_from_checkpoint

        fake_checkpoint_mgr = MagicMock()
        fake_checkpoint_mgr.get_latest = AsyncMock(return_value=None)
        fake_provider = MagicMock()
        fake_provider.get.side_effect = lambda k: {
            "checkpoint_manager": fake_checkpoint_mgr,
        }.get(k)

        result = await _load_agent_from_checkpoint("pipe-no-cp", fake_provider)
        assert result is None

    @pytest.mark.asyncio
    async def test_load_agent_from_checkpoint_no_agent_id_returns_none(self):
        """检查点存在但无 agent_config_id → 返回 None。"""
        from pipeline.pipeline_reviver import _load_agent_from_checkpoint

        fake_checkpoint_mgr = MagicMock()
        fake_checkpoint_mgr.get_latest = AsyncMock(return_value={
            "state": {},  # 无 agent_config_id
            "metadata": {},
        })
        fake_provider = MagicMock()
        fake_provider.get.side_effect = lambda k: {
            "checkpoint_manager": fake_checkpoint_mgr,
        }.get(k)

        result = await _load_agent_from_checkpoint("pipe-no-id", fake_provider)
        assert result is None


# ═══════════════════════════════════════════════════════════════
# P0: stop_generation — 清理正确的缓存（EngineRegistry）
# ═══════════════════════════════════════════════════════════════


class TestStopGenerationClearsRegistry:
    """P0: stop_generation 重置引擎状态，不注销引擎条目。

    停止输出只取消 engine_task + 重置 _run_started，保留注册表条目。
    重发消息时 _find_engine 找到引擎（idle 态），走 _start_idle_engine
    用引擎自带 _agent_config（上次 run() 绑定的），不触发 revive。
    """

    def test_stop_generation_resets_run_started(self):
        """stop_generation 重置 _run_started，不 unregister。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "channels" / "websocket" / "app_factory.py"
        ).read_text(encoding="utf-8")

        assert "_eng._run_started = False" in src, (
            "stop_generation 必须重置 _run_started 让引擎回到 idle 态"
        )
        assert "_get_reg().unregister(_pid)" not in src, (
            "stop_generation 不应 unregister——保留条目让重发走 idle 重启"
        )

    def test_stop_generation_keeps_entry(self):
        """stop_generation 注释说明保留注册表条目。"""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "channels" / "websocket" / "app_factory.py"
        ).read_text(encoding="utf-8")
        assert "保留注册表条目" in src

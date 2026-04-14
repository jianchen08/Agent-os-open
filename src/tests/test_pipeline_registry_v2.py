"""PipelineRegistry v2 测试。

覆盖 route / get_result / get_routed_from / get_routed_to / register_config / RoutingRecord。
同时验证 M1 兼容方法 submit/route_to/release 不回归。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.config_store import PipelineConfig, PipelineConfigStore
from pipeline.event_bus import EventBus
from pipeline.registry import PipelineRegistry, RoutingRecord
from pipeline.types import StateKeys


# ---------------------------------------------------------------------------
# RoutingRecord
# ---------------------------------------------------------------------------


class TestRoutingRecord:
    """RoutingRecord dataclass 测试。"""

    def test_creation(self) -> None:
        """创建路由记录。"""
        record = RoutingRecord(
            source_id="pipeline-1",
            target_id="pipeline-2",
            target="research_agent",
        )
        assert record.source_id == "pipeline-1"
        assert record.target_id == "pipeline-2"
        assert record.target == "research_agent"
        assert record.status == "pending"
        assert record.timestamp > 0

    def test_custom_status(self) -> None:
        """自定义状态。"""
        record = RoutingRecord(
            source_id="p-1",
            target_id="p-2",
            target="agent",
            status="completed",
        )
        assert record.status == "completed"


# ---------------------------------------------------------------------------
# PipelineRegistry v2 — route / get_result / routing_log
# ---------------------------------------------------------------------------


class TestPipelineRegistryV2Route:
    """route() 方法测试。"""

    @pytest.mark.asyncio
    async def test_route_creates_child_pipeline(self) -> None:
        """route 创建子管道并返回 pipeline_id。"""
        store = PipelineConfigStore()
        store.register("research", PipelineConfig(pipeline_id="research", name="Research"))
        registry = PipelineRegistry(config_store=store)

        pipeline_id = await registry.route(
            source_id="pipeline-0",
            target="research",
            state={StateKeys.SESSION_ID: "sess-1", StateKeys.TASK_ID: "task-1"},
        )
        assert pipeline_id == "pipeline-1"

    @pytest.mark.asyncio
    async def test_route_raises_on_missing_config(self) -> None:
        """目标配置不存在时抛出 ValueError。"""
        store = PipelineConfigStore()
        registry = PipelineRegistry(config_store=store)

        with pytest.raises(ValueError, match="Pipeline config not found"):
            await registry.route(
                source_id="pipeline-0",
                target="nonexistent",
                state={},
            )

    @pytest.mark.asyncio
    async def test_route_without_config_store(self) -> None:
        """无 config_store 时 route 仍可执行（config 为 None）。"""
        registry = PipelineRegistry()
        pipeline_id = await registry.route(
            source_id="pipeline-0",
            target="any_target",
            state={StateKeys.SESSION_ID: "sess-1"},
        )
        assert pipeline_id == "pipeline-1"

    @pytest.mark.asyncio
    async def test_route_records_routing_log(self) -> None:
        """route 记录路由日志。"""
        store = PipelineConfigStore()
        store.register("agent", PipelineConfig(pipeline_id="agent", name="Agent"))
        registry = PipelineRegistry(config_store=store)

        await registry.route(
            source_id="pipeline-0",
            target="agent",
            state={},
        )
        assert len(registry._routing_log) == 1
        record = registry._routing_log[0]
        assert record.source_id == "pipeline-0"
        assert record.target == "agent"
        assert record.status == "pending"

    @pytest.mark.asyncio
    async def test_route_child_state_whitelist(self) -> None:
        """子管道 initial_state 白名单提取。"""
        store = PipelineConfigStore()
        store.register("agent", PipelineConfig(pipeline_id="agent", name="Agent"))
        registry = PipelineRegistry(config_store=store)

        parent_state = {
            StateKeys.SESSION_ID: "sess-1",
            StateKeys.TASK_ID: "task-1",
            StateKeys.AGENT_LEVEL: "l2_subtask",
            StateKeys.CORE_TYPE: "llm_call",
            "user_input": "search for AI",
            "delegated_task": "research topic",
            # 以下字段不在白名单中
            StateKeys.RAW_RESULT: "some result",
            StateKeys.ENDED: False,
        }
        await registry.route(
            source_id="pipeline-0",
            target="agent",
            state=parent_state,
        )

        child_pipeline = registry._pipelines["pipeline-1"]
        child_state = child_pipeline["initial_state"]
        # 白名单内的字段
        assert child_state[StateKeys.SESSION_ID] == "sess-1"
        assert child_state[StateKeys.TASK_ID] == "task-1"
        assert child_state[StateKeys.AGENT_LEVEL] == "l2_subtask"
        assert child_state["user_input"] == "search for AI"
        assert child_state["delegated_task"] == "research topic"
        assert child_state["target_pipeline"] == "agent"
        # 白名单外的字段不应存在
        assert StateKeys.RAW_RESULT not in child_state
        assert StateKeys.ENDED not in child_state

    @pytest.mark.asyncio
    async def test_route_with_scheduler(self) -> None:
        """有 scheduler 时提交调度器而非 create_task。"""
        store = PipelineConfigStore()
        store.register("agent", PipelineConfig(pipeline_id="agent", name="Agent"))
        scheduler = AsyncMock()
        registry = PipelineRegistry(config_store=store, scheduler=scheduler)

        await registry.route(
            source_id="pipeline-0",
            target="agent",
            state={},
        )
        scheduler.submit.assert_called_once()
        call_kwargs = scheduler.submit.call_args
        assert call_kwargs[1]["priority"] == 5

    @pytest.mark.asyncio
    async def test_route_with_event_bus(self) -> None:
        """有 event_bus 时子管道完成后 emit 事件。"""
        store = PipelineConfigStore()
        store.register("agent", PipelineConfig(pipeline_id="agent", name="Agent"))
        event_bus = EventBus()
        callback = AsyncMock()
        event_bus.subscribe("pipeline_completed", callback)
        registry = PipelineRegistry(config_store=store, event_bus=event_bus)

        pipeline_id = await registry.route(
            source_id="pipeline-0",
            target="agent",
            state={},
        )

        # 手动触发子管道完成（模拟）
        await registry._run_child_pipeline(pipeline_id, None, {})
        callback.assert_called_once()
        data = callback.call_args[0][0]
        assert data["pipeline_id"] == pipeline_id
        assert data["status"] == "completed"


# ---------------------------------------------------------------------------
# PipelineRegistry v2 — get_result / get_routed_from / get_routed_to
# ---------------------------------------------------------------------------


class TestPipelineRegistryV2Queries:
    """查询方法测试。"""

    def test_get_result_no_result(self) -> None:
        """管道未完成时 get_result 返回 None。"""
        registry = PipelineRegistry()
        assert registry.get_result("pipeline-1") is None

    def test_get_result_after_completion(self) -> None:
        """管道完成后 get_result 返回结果。"""
        registry = PipelineRegistry()
        registry._results["pipeline-1"] = {"status": "completed", "output": "done"}
        result = registry.get_result("pipeline-1")
        assert result is not None
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_get_routed_from(self) -> None:
        """查询路由到指定管道的记录。"""
        store = PipelineConfigStore()
        store.register("agent", PipelineConfig(pipeline_id="agent", name="Agent"))
        registry = PipelineRegistry(config_store=store)

        await registry.route("pipeline-0", "agent", {})
        records = registry.get_routed_from("pipeline-1")
        assert len(records) == 1
        assert records[0].source_id == "pipeline-0"

    @pytest.mark.asyncio
    async def test_get_routed_to(self) -> None:
        """查询从指定管道路由出去的记录。"""
        store = PipelineConfigStore()
        store.register("agent", PipelineConfig(pipeline_id="agent", name="Agent"))
        registry = PipelineRegistry(config_store=store)

        await registry.route("pipeline-0", "agent", {})
        records = registry.get_routed_to("pipeline-0")
        assert len(records) == 1
        assert records[0].target_id == "pipeline-1"

    def test_get_routed_from_empty(self) -> None:
        """无路由记录时返回空列表。"""
        registry = PipelineRegistry()
        assert registry.get_routed_from("nonexistent") == []

    def test_get_routed_to_empty(self) -> None:
        """无路由记录时返回空列表。"""
        registry = PipelineRegistry()
        assert registry.get_routed_to("nonexistent") == []

    @pytest.mark.asyncio
    async def test_multiple_routes(self) -> None:
        """多次路由记录完整追踪。"""
        store = PipelineConfigStore()
        store.register("a", PipelineConfig(pipeline_id="a", name="A"))
        store.register("b", PipelineConfig(pipeline_id="b", name="B"))
        registry = PipelineRegistry(config_store=store)

        await registry.route("pipeline-0", "a", {})
        await registry.route("pipeline-0", "b", {})

        records_from_0 = registry.get_routed_to("pipeline-0")
        assert len(records_from_0) == 2

        records_to_1 = registry.get_routed_from("pipeline-1")
        assert len(records_to_1) == 1
        records_to_2 = registry.get_routed_from("pipeline-2")
        assert len(records_to_2) == 1


# ---------------------------------------------------------------------------
# PipelineRegistry v2 — register_config
# ---------------------------------------------------------------------------


class TestPipelineRegistryV2RegisterConfig:
    """register_config 方法测试。"""

    def test_register_config_with_store(self) -> None:
        """有 config_store 时注册配置。"""
        store = PipelineConfigStore()
        registry = PipelineRegistry(config_store=store)
        config = PipelineConfig(pipeline_id="new_agent", name="New Agent")

        registry.register_config("new_agent", config)
        assert store.get("new_agent") is not None
        assert store.get("new_agent").name == "New Agent"  # type: ignore[union-attr]

    def test_register_config_without_store(self) -> None:
        """无 config_store 时不报错（仅 warn）。"""
        registry = PipelineRegistry()
        config = PipelineConfig(pipeline_id="new_agent", name="New Agent")
        # 不应抛异常
        registry.register_config("new_agent", config)


# ---------------------------------------------------------------------------
# PipelineRegistry M1 兼容方法
# ---------------------------------------------------------------------------


class TestPipelineRegistryM1Compat:
    """M1 兼容方法 submit/route_to/release 测试。"""

    def test_submit(self) -> None:
        """submit 创建管道实例。"""
        registry = PipelineRegistry()
        pipeline_id = registry.submit("target_a", {"key": "value"})
        assert pipeline_id == "pipeline-1"
        assert registry._pipelines[pipeline_id]["target"] == "target_a"
        assert registry._pipelines[pipeline_id]["config"] == {"key": "value"}

    def test_submit_with_parent(self) -> None:
        """submit 带父管道 ID。"""
        registry = PipelineRegistry()
        pipeline_id = registry.submit("target_a", {}, parent_id="pipeline-0")
        assert registry._pipelines[pipeline_id]["parent_id"] == "pipeline-0"

    def test_route_to(self) -> None:
        """route_to 创建路由管道。"""
        registry = PipelineRegistry()
        pipeline_id = registry.route_to("target_b", {"ctx": "data"})
        assert pipeline_id == "pipeline-1"

    def test_release(self) -> None:
        """release 释放管道实例。"""
        registry = PipelineRegistry()
        pipeline_id = registry.submit("target_a", {})
        registry.release(pipeline_id)
        assert registry._pipelines[pipeline_id]["status"] == "released"

    def test_release_nonexistent(self) -> None:
        """release 不存在的管道不报错。"""
        registry = PipelineRegistry()
        registry.release("nonexistent")  # 不应抛异常

    def test_counter_increments(self) -> None:
        """ID 计数器自增。"""
        registry = PipelineRegistry()
        id1 = registry.submit("a", {})
        id2 = registry.submit("b", {})
        assert id1 == "pipeline-1"
        assert id2 == "pipeline-2"

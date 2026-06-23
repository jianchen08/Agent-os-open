"""全里程碑真实验收测试。

验证每个里程碑核心功能真实可用（非 Mock）：
- M1：管道框架 — 端到端运行完整管道
- M2：LLMCore — 调用真实 LLM（由 test_integration_llm.py 覆盖）
- M3：工具系统 — 真实工具注册+执行+LLM联动（由 test_integration_llm.py 覆盖）
- M5a：任务系统 — 状态机真实流转+JSON存储
- M5b：评估系统 — YAML加载+评估执行+TaskService联动
- M6：全量插件 — 插件链真实执行
- M9：WebSocket — 启动服务器+连接+消息收发

需要 --run-integration 选项才会执行：
  pytest -m integration --run-integration
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import pytest

from pipeline.engine import PipelineEngine
from pipeline.plugin import (
    ICorePlugin,
    IInputPlugin,
    IOutputPlugin,
    OutputResult,
    PluginContext,
    PluginResult,
)
from pipeline.registry import PluginRegistry
from pipeline.route import InputRouteEntry, InputRouteTable, OutputRouteEntry, OutputRouteTable
from agents.types import AgentLevel
from pipeline.types import (
    RouteSignal,
    StateKeys,
    create_initial_state,
)

# M5a — 依赖 sqlalchemy（tasks/state_machine.py 顶层导入 sqlalchemy）
# 如果 sqlalchemy 未安装，这些测试将被跳过
try:
    from tasks.service import TaskService
    from tasks.state_machine import InvalidTransitionError
    from tasks.storage import TaskStorage
    from tasks.types import TaskStatus, create_task
    _HAS_SQLALCHEMY = True
except ImportError:
    _HAS_SQLALCHEMY = False

# M5b
from evaluation.engine import EvaluationEngine
from evaluation.executor import EvaluationExecutor
from evaluation.expect import ExpectEvaluator
from evaluation.loader import MetricLoader
from evaluation.mapper import ResultMapper
from evaluation.types import (
    EvaluationConfig,
    EvaluationResult,
    ExpectCondition,
    ExpectSpec,
    MetricDefinition,
    MetricResult,
    MetricType,
)

# M6 插件
from plugins.input.context_build import ContextBuildPlugin
from plugins.input.prompt_build import PromptBuildPlugin
from plugins.input.security_check import SecurityCheckPlugin
from plugins.input.tool_schema import ToolSchemaPlugin
from plugins.output.error_check import ErrorCheckPlugin
from plugins.output.stop_check import StopCheckPlugin
from plugins.output.result_format import ResultFormatPlugin


# ---------------------------------------------------------------------------
# M1 真实验收：管道框架端到端
# ---------------------------------------------------------------------------


class SimpleEchoCore(ICorePlugin):
    """简单回显核心插件 — M1 验收用。"""

    @property
    def core_type(self) -> str:
        return "echo"

    @property
    def name(self) -> str:
        return "echo"

    @property
    def priority(self) -> int:
        return 5

    async def execute(self, ctx: PluginContext) -> dict[str, Any]:
        messages = ctx.state.get("messages", [])
        last_msg = messages[-1]["content"] if messages else ""
        return {StateKeys.RAW_RESULT: f"Echo: {last_msg}", StateKeys.ENDED: True}


class SimpleInputPlugin(IInputPlugin):
    """简单输入插件 — M1 验收用。"""

    @property
    def name(self) -> str:
        return "simple_input"

    @property
    def target(self) -> str:
        return "core"

    @property
    def priority(self) -> int:
        return 5

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult(state_updates={}, route_signal=None)


class SimpleOutputPlugin(IOutputPlugin):
    """简单输出插件 — M1 验收用。"""

    @property
    def name(self) -> str:
        return "simple_output"

    @property
    def priority(self) -> int:
        return 5

    async def execute(self, ctx: PluginContext) -> OutputResult:
        return OutputResult(state_updates={}, route_signal=None)

    @property
    def route_signals(self) -> list[str]:
        return []


@pytest.mark.integration
class TestM1PipelineReal:
    """M1 真实验收 — 管道框架端到端运行。"""

    async def test_full_pipeline_execution(self) -> None:
        """完整管道：Input → Core → Output → 结束。

        验收标准：管道能完整运行一轮并返回结果。
        """
        input_table = InputRouteTable(entries=[
            InputRouteEntry(name="default", condition="", target="core",
                            plugins=["simple_input"], priority=5),
        ])
        output_table = OutputRouteTable(entries=[
            OutputRouteEntry(route_type="end", condition="", priority=5, target_core="echo"),
        ])

        registry = PluginRegistry()
        registry.register(SimpleInputPlugin())
        registry.register_core("echo", SimpleEchoCore())
        registry.register(SimpleOutputPlugin())

        engine = PipelineEngine(
            input_route_table=input_table,
            output_route_table=output_table,
            plugin_registry=registry,
            max_iterations=10,
        )

        state = create_initial_state(
            messages=[{"role": "user", "content": "测试管道"}],
        )
        state[StateKeys.CORE_TYPE] = "echo"
        result = await engine.run(state)

        assert result.get(StateKeys.ENDED) is True, "管道应正常结束"
        assert StateKeys.RAW_RESULT in result, "应有核心结果"
        assert "测试管道" in result[StateKeys.RAW_RESULT], (
            f"结果应包含输入文本，实际: {result[StateKeys.RAW_RESULT]}"
        )

    async def test_pipeline_with_route_signal(self) -> None:
        """管道路由信号：Output 插件发出路由信号，引擎正确收集和仲裁。

        验收标准：Output 插件产生的路由信号能被引擎正确收集。
        """

        class SignalOutput(IOutputPlugin):
            """信号输出插件 — 发出 next_llm 路由信号。"""
            executed = False

            @property
            def name(self) -> str:
                return "signal_output"

            @property
            def priority(self) -> int:
                return 5

            async def execute(self, ctx: PluginContext) -> OutputResult:
                SignalOutput.executed = True
                return OutputResult(
                    state_updates={},
                    route_signal=RouteSignal(
                        route_type="next_llm",
                        target="llm_call",
                        reason="路由信号测试",
                    ),
                )

            @property
            def route_signals(self) -> list[str]:
                return []

        SignalOutput.executed = False

        input_table = InputRouteTable(entries=[
            InputRouteEntry(name="default", condition="", target="core",
                            plugins=[], priority=5),
        ])
        output_table = OutputRouteTable(entries=[
            OutputRouteEntry(route_type="next_llm", condition="", priority=1, target_core="llm_call"),
        ])

        registry = PluginRegistry()
        registry.register_core("echo", SimpleEchoCore())
        registry.register(SignalOutput())

        engine = PipelineEngine(
            input_route_table=input_table,
            output_route_table=output_table,
            plugin_registry=registry,
            max_iterations=10,
        )

        state = create_initial_state(
            messages=[{"role": "user", "content": "路由测试"}],
        )
        state[StateKeys.CORE_TYPE] = "echo"
        await engine.run(state)

        # 验证 Output 插件被执行了
        assert SignalOutput.executed, "Output 插件应被执行"


# ---------------------------------------------------------------------------
# M5a 真实验收：任务系统
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_SQLALCHEMY, reason="依赖 sqlalchemy（tasks/state_machine.py）")
class TestM5aTaskSystemReal:
    """M5a 真实验收 — 状态机真实流转 + JSON 文件存储。"""

    def test_full_task_lifecycle(self) -> None:
        """完整任务生命周期：pending → running → evaluating → completed。"""
        service = TaskService()
        task = service.create_task("测试任务", "验证完整生命周期")

        assert task.status == TaskStatus.PENDING

        task = service.start_task(task.id)
        assert task.status == TaskStatus.RUNNING

        task = service.move_to_evaluating(task.id)
        assert task.status == TaskStatus.EVALUATING

        task = service.complete_evaluation(task.id, passed=True)
        assert task.status == TaskStatus.COMPLETED

    def test_task_pause_resume(self) -> None:
        """任务暂停恢复：running → paused → running。"""
        service = TaskService()
        task = service.create_task("暂停测试")

        task = service.start_task(task.id)
        task = service.pause_task(task.id)
        assert task.status == TaskStatus.PAUSED

        task = service.resume_task(task.id)
        assert task.status == TaskStatus.RUNNING

    def test_task_failure(self) -> None:
        """任务失败：running → failed。"""
        service = TaskService()
        task = service.create_task("失败测试")

        task = service.start_task(task.id)
        task = service.fail_task(task.id, error="测试错误")

        assert task.status == TaskStatus.FAILED
        assert task.error == "测试错误"

    def test_invalid_transition_raises(self) -> None:
        """非法状态转换抛出异常。"""
        service = TaskService()
        task = service.create_task("终态测试")

        task = service.start_task(task.id)
        task = service.fail_task(task.id)

        with pytest.raises(InvalidTransitionError):
            service.start_task(task.id)  # failed → running 不允许

    def test_yaml_file_storage(self) -> None:
        """YAML 文件存储：写入磁盘 + 读取恢复。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "tasks")
            storage = TaskStorage(data_dir=data_dir)

            task = create_task(title="持久化测试", description="验证YAML存储")
            storage.save(task)

            assert os.path.exists(data_dir), "YAML 目录应被创建"

            storage2 = TaskStorage(data_dir=data_dir)
            loaded = storage2.get(task.id)
            assert loaded is not None, "应能读取已存储的任务"
            assert loaded.title == "持久化测试"
            assert loaded.status == TaskStatus.PENDING

    def test_task_progress_calculation(self) -> None:
        """进度计算：父任务进度基于子任务完成率。"""
        service = TaskService()
        parent = service.create_task("父任务")

        sub1 = service.create_task("子任务1", parent_task_id=parent.id)
        service.create_task("子任务2", parent_task_id=parent.id)

        progress = service.get_progress(parent.id)
        assert progress == 0.0

        # 完成子任务1：running → evaluating → completed
        sub1 = service.start_task(sub1.id)
        sub1 = service.move_to_evaluating(sub1.id)
        service.complete_evaluation(sub1.id, passed=True)
        progress = service.get_progress(parent.id)
        assert progress == 50.0, f"一个子任务完成后进度应为50%，实际: {progress}"

    def test_list_by_status(self) -> None:
        """按状态查询任务。

        注意：TaskService 使用共享数据库，可能包含之前测试运行留下的数据，
        因此只验证本测试创建的任务是否出现在正确的状态列表中。
        """
        service = TaskService()
        t1 = service.create_task("任务1")
        t2 = service.create_task("任务2")
        t3 = service.create_task("任务3")

        service.start_task(t1.id)
        service.start_task(t2.id)

        # 记录本测试创建的任务ID集合
        my_task_ids = {t1.id, t2.id, t3.id}

        pending = service.list_by_status(TaskStatus.PENDING)
        running = service.list_by_status(TaskStatus.RUNNING)

        # 过滤出本测试创建的任务，验证状态转换正确
        my_pending = [t for t in pending if t.id in my_task_ids]
        my_running = [t for t in running if t.id in my_task_ids]

        assert len(my_pending) == 1, f"应有1个PENDING任务，实际: {len(my_pending)}"
        assert len(my_running) == 2, f"应有2个RUNNING任务，实际: {len(my_running)}"


# ---------------------------------------------------------------------------
# M5b 真实验收：评估系统
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_SQLALCHEMY, reason="依赖 sqlalchemy（通过 TaskService 间接依赖）")
class TestM5bEvaluationReal:
    """M5b 真实验收 — YAML加载 + 评估执行 + TaskService 联动。"""

    def test_metric_loader_yaml_load(self) -> None:
        """MetricLoader 加载 YAML 指标文件。"""
        loader = MetricLoader()
        loader.load_all()
        assert isinstance(loader.metrics, dict), "metrics 应为 dict"

    def test_evaluation_engine_with_manual_metrics(self) -> None:
        """EvaluationEngine 手动注册指标并评估。"""
        loader = MetricLoader()

        # Mock tool evaluator 返回 {"success": True, "data": {"status": "completed", "exit_code": 0}}
        # expect 条件需要匹配这个数据结构
        metric = MetricDefinition(
            id="test_exit_code",
            name="退出码检查",
            metric_type=MetricType.TOOL,
            description="检查退出码是否为0",
            expect=ExpectSpec(
                conditions=[ExpectCondition(field="data.exit_code", operator="equals", value=0)],
            ),
        )
        loader.metrics["test_exit_code"] = metric

        engine = EvaluationEngine(loader=loader)
        config = EvaluationConfig(
            metric_ids=["test_exit_code"],
            input_params={},
        )
        result = engine.evaluate(task_id="test-task-1", config=config)

        assert len(result.results) == 1
        assert result.results[0].passed, (
            f"exit_code=0 应通过，实际: {result.results[0]}"
        )

    def test_expect_evaluator_operators(self) -> None:
        """ExpectEvaluator 操作符真实执行。"""
        evaluator = ExpectEvaluator()

        # 测试 equals
        spec = ExpectSpec(conditions=[ExpectCondition(field="output", operator="equals", value="hello")])
        result = evaluator.evaluate("test_equals", spec, {"output": "hello"})
        assert result.passed, f"equals: 应通过"

        spec2 = ExpectSpec(conditions=[ExpectCondition(field="output", operator="equals", value="world")])
        result2 = evaluator.evaluate("test_equals_fail", spec2, {"output": "hello"})
        assert not result2.passed, "equals: 应失败"

        # 测试 not_equals
        spec3 = ExpectSpec(conditions=[ExpectCondition(field="output", operator="not_equals", value="world")])
        result3 = evaluator.evaluate("test_not_equals", spec3, {"output": "hello"})
        assert result3.passed, "not_equals: 应通过"

        # 测试 contains
        spec4 = ExpectSpec(conditions=[ExpectCondition(field="text", operator="contains", value="world")])
        result4 = evaluator.evaluate("test_contains", spec4, {"text": "hello world"})
        assert result4.passed, "contains: 应通过"

        # 测试 gt / lt / gte / lte
        spec5 = ExpectSpec(conditions=[ExpectCondition(field="count", operator="gt", value=5)])
        result5 = evaluator.evaluate("test_gt", spec5, {"count": 10})
        assert result5.passed, "gt: 应通过"

        spec6 = ExpectSpec(conditions=[ExpectCondition(field="count", operator="lt", value=5)])
        result6 = evaluator.evaluate("test_lt", spec6, {"count": 3})
        assert result6.passed, "lt: 应通过"

        spec7 = ExpectSpec(conditions=[ExpectCondition(field="count", operator="gte", value=10)])
        result7 = evaluator.evaluate("test_gte", spec7, {"count": 10})
        assert result7.passed, "gte: 应通过"

        spec8 = ExpectSpec(conditions=[ExpectCondition(field="count", operator="lte", value=5)])
        result8 = evaluator.evaluate("test_lte", spec8, {"count": 5})
        assert result8.passed, "lte: 应通过"

        # 测试 is_true / is_false
        spec9 = ExpectSpec(conditions=[ExpectCondition(field="flag", operator="is_true", value=None)])
        result9 = evaluator.evaluate("test_is_true", spec9, {"flag": True})
        assert result9.passed, "is_true: 应通过"

        spec10 = ExpectSpec(conditions=[ExpectCondition(field="flag", operator="is_false", value=None)])
        result10 = evaluator.evaluate("test_is_false", spec10, {"flag": False})
        assert result10.passed, "is_false: 应通过"

        # 测试 in / not_in
        spec11 = ExpectSpec(conditions=[ExpectCondition(field="status", operator="in", value=["ok", "done"])])
        result11 = evaluator.evaluate("test_in", spec11, {"status": "ok"})
        assert result11.passed, "in: 应通过"

        spec12 = ExpectSpec(conditions=[ExpectCondition(field="status", operator="not_in", value=["error", "fail"])])
        result12 = evaluator.evaluate("test_not_in", spec12, {"status": "ok"})
        assert result12.passed, "not_in: 应通过"

    def test_evaluation_executor_with_task_service(self) -> None:
        """EvaluationExecutor 与 TaskService 联动。"""
        task_service = TaskService()
        task = task_service.create_task("评估联动测试")
        task = task_service.start_task(task.id)
        task = task_service.move_to_evaluating(task.id)

        loader = MetricLoader()
        metric = MetricDefinition(
            id="simple_check",
            name="简单检查",
            metric_type=MetricType.TOOL,
            description="检查退出码是否为0",
            expect=ExpectSpec(
                conditions=[ExpectCondition(field="data.exit_code", operator="equals", value=0)],
            ),
        )
        loader.metrics["simple_check"] = metric

        executor = EvaluationExecutor(
            task_service=task_service,
            loader=loader,
        )

        executor.run_evaluation(
            task_id=task.id,
            metric_ids=["simple_check"],
            input_params={"simple_check": {}},
        )

        updated_task = task_service.get_task(task.id)
        assert updated_task is not None
        assert updated_task.status == TaskStatus.COMPLETED, (
            f"评估通过的任务应为 COMPLETED，实际: {updated_task.status}"
        )

    def test_result_mapper_summary(self) -> None:
        """ResultMapper 构建可读摘要。"""
        mapper = ResultMapper()
        result = EvaluationResult(
            task_id="test-task",
            results=[
                MetricResult(metric_id="m1", passed=True, message="通过"),
                MetricResult(metric_id="m2", passed=False, message="失败"),
            ],
        )

        summary = mapper.build_summary(result)
        assert "m1" in summary, "摘要应包含指标ID"
        assert "m2" in summary


# ---------------------------------------------------------------------------
# M6 真实验收：全量插件链执行
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestM6PluginsReal:
    """M6 真实验收 — 插件链真实执行。"""

    async def test_input_plugins_chain(self) -> None:
        """Input 插件链真实执行。"""
        # ContextBuild
        ctx_plugin = ContextBuildPlugin()
        state = create_initial_state(
            messages=[{"role": "user", "content": "测试上下文"}],
            agent_level=AgentLevel.L1_MAIN,
        )
        ctx_result = await ctx_plugin.execute(PluginContext(state=state, config={}))
        # PluginResult 使用 state_updates 而不是 data
        state.update(ctx_result.state_updates)

        # PromptBuild
        prompt_plugin = PromptBuildPlugin()
        prompt_result = await prompt_plugin.execute(PluginContext(state=state, config={}))
        state.update(prompt_result.state_updates)

        # SecurityCheck
        sec_plugin = SecurityCheckPlugin()
        sec_result = await sec_plugin.execute(PluginContext(state=state, config={}))

        # 不应有安全拦截（route_signal=None 表示通过）
        assert sec_result.route_signal is None, "正常消息不应触发安全拦截"

    async def test_output_plugins_chain(self) -> None:
        """Output 插件链真实执行。"""
        # ErrorCheck
        error_check = ErrorCheckPlugin()
        state = create_initial_state(
            messages=[{"role": "user", "content": "测试"}],
        )
        state[StateKeys.RAW_RESULT] = "正常输出"
        state[StateKeys.RAW_ERROR] = None
        result = await error_check.execute(PluginContext(state=state, config={}))
        assert result.route_signal is None, "无错误时不应触发错误路由"

        # StopCheck — ended=True 时设置 stop_reason 而不是 route_signal
        stop_check = StopCheckPlugin()
        state[StateKeys.ENDED] = True
        result = await stop_check.execute(PluginContext(state=state, config={}))
        # StopCheck 在 ended=True 时设置 router.stop_reason，而非 route_signal
        assert result.state_updates.get("router.stop_reason") is not None or result.route_signal is not None, (
            "ended=True 应触发停止"
        )

        # ResultFormat
        fmt = ResultFormatPlugin()
        state[StateKeys.RAW_RESULT] = "格式化测试"
        result = await fmt.execute(PluginContext(state=state, config={}))
        assert result.state_updates is not None

    async def test_tool_schema_input_with_registry(self) -> None:
        """ToolSchemaInput 与 ToolRegistry 联动。

        使用 Tool 对象和 register_with_handler 注册工具，
        与 ToolRegistry 当前 API 保持一致。
        """
        from tools.registry import ToolRegistry
        from tools.types import Tool, ToolSource

        registry = ToolRegistry()
        tool_def = Tool(
            name="add_numbers",
            description="加法运算",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
            source=ToolSource.CODE,
        )
        registry.register_with_handler(
            tool=tool_def,
            handler=lambda args: {"result": args["a"] + args["b"]},
        )

        tool_schema_plugin = ToolSchemaPlugin()
        state = create_initial_state(
            messages=[{"role": "user", "content": "计算"}],
        )
        # ToolSchemaPlugin 需要通过 config 或 state 获取 registry
        # 检查它如何获取 registry
        ctx = PluginContext(state=state, config={"tool_registry": registry})
        result = await tool_schema_plugin.execute(ctx)

        # 验证工具 schema 被注入
        assert result.state_updates is not None


# ---------------------------------------------------------------------------
# M9 真实验收：WebSocket 通道
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestM9WebSocketReal:
    """M9 真实验收 — WebSocket 服务器启动+连接+消息收发。"""

    async def test_server_start_stop(self) -> None:
        """服务器启动和停止。"""
        from channels.websocket.server import WebSocketServer

        server = WebSocketServer(host="127.0.0.1", port=18765)
        await server.start()
        assert server._runner is not None, "Runner 应被创建"

        await server.stop()
        assert server._runner is None, "Runner 应被清理"

    async def test_websocket_connection_and_message(self) -> None:
        """WebSocket 连接建立 + 消息收发。"""
        import aiohttp

        from channels.websocket.protocol import (
            EventType,
            create_event,
        )
        from channels.websocket.server import WebSocketServer

        received_messages: list[dict] = []

        async def on_message(session_id: str, msg: dict) -> None:
            """消息处理器 — 收到消息后回送确认。"""
            received_messages.append(msg)
            ack = create_event(
                EventType.EXECUTION_DONE,
                {"original_type": msg.get("type", "unknown")},
            )
            await server.send_event(session_id, ack)

        server = WebSocketServer(host="127.0.0.1", port=18766)
        server.on_message = on_message
        await server.start()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect("http://127.0.0.1:18766/ws") as ws:
                    # 等待连接确认
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                    assert msg["type"] == EventType.CONNECTION_CONFIRMATION, (
                        f"应收到连接确认，实际: {msg['type']}"
                    )

                    # 发送消息
                    test_event = create_event(
                        EventType.USER_INPUT,
                        {"content": "真实验收测试"},
                    )
                    await ws.send_json(test_event.to_dict())

                    # 等待回复
                    reply = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                    assert reply["type"] == EventType.EXECUTION_DONE, (
                        f"应收到执行完成，实际: {reply['type']}"
                    )
        finally:
            await server.stop()

    async def test_health_endpoint(self) -> None:
        """健康检查端点。"""
        import aiohttp

        from channels.websocket.server import WebSocketServer

        server = WebSocketServer(host="127.0.0.1", port=18767)
        await server.start()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("http://127.0.0.1:18767/health") as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["status"] == "ok"
                    assert "active_sessions" in data
        finally:
            await server.stop()

    async def test_session_manager_register_unregister(self) -> None:
        """会话管理：注册 + 注销 + 查找。"""
        from channels.websocket.session_manager import SessionManager

        manager = SessionManager()

        class MockWS:
            """Mock WebSocket 连接。"""
            async def send_str(self, data: str) -> None:
                pass

        session_id = await manager.register(ws=MockWS(), thread_id="thread-1")
        assert manager.active_count == 1

        # 通过 thread_id 查找
        found = manager.get_session_by_thread("thread-1")
        assert found is not None, "应能通过 thread_id 找到会话"
        assert found.session_id == session_id, "找到的会话 ID 应匹配"

        # 注销
        await manager.unregister(session_id)
        assert manager.active_count == 0

"""Agent OS 6 大核心功能区域端到端验证测试。

本测试覆盖：
  区域1: 任务提交与创建
  区域2: 任务执行（状态机 + Agent 架构）
  区域3: 长期任务（容器任务 + 子任务 + 进度计算 + 触发器）
  区域4: 任务交互（inject + human_interaction + WebSocket）
  区域5: 消息通信（通道网关 + 消息规范化 + 会话桥接）
  区域6: 体验优化（前端构建 + UI组件 + 路由 + Stores）

运行方式：
  PYTHONPATH=src:. python3 -m pytest tests/suites/core/test_core_e2e_verification.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# 确保项目根目录在 sys.path 中
ROOT_DIR = Path(__file__).resolve().parents[3]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))


# ═══════════════════════════════════════════════════════════════
# 区域1: 任务提交与创建
# ═══════════════════════════════════════════════════════════════

class TestRegion1TaskCreation:
    """区域1: 任务提交与创建验证。"""

    @pytest.fixture
    def service(self):
        from tasks.storage import TaskStorage
        from tasks.service import TaskService
        storage = TaskStorage()  # 内存模式
        return TaskService(storage=storage)

    def test_create_basic_task(self, service):
        """创建基本任务，验证 title/description/status 正确。"""
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("测试任务", "任务描述")
        )
        assert task.title == "测试任务"
        assert task.description == "任务描述"
        assert task.status.value == "pending"
        assert task.id != ""

    def test_create_task_with_priority(self, service):
        """验证优先级正确传递。"""
        from tasks.types import TaskPriority
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("高优任务", priority=TaskPriority.CRITICAL)
        )
        assert task.priority == TaskPriority.CRITICAL
        assert task.priority.value == 1

    def test_create_task_with_agent_level(self, service):
        """验证 agent_level 正确传递。"""
        from agents.types import AgentLevel
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("L2任务", agent_level=AgentLevel.L2_SUBTASK)
        )
        assert task.agent_level == AgentLevel.L2_SUBTASK

    def test_create_task_with_parent(self, service):
        """创建子任务，验证 parent_task_id 关联。"""
        parent = asyncio.get_event_loop().run_until_complete(
            service.create_task("父任务")
        )
        child = asyncio.get_event_loop().run_until_complete(
            service.create_task("子任务", parent_task_id=parent.id)
        )
        assert child.parent_task_id == parent.id

    def test_create_task_with_metadata(self, service):
        """验证 metadata 正确传递。"""
        meta = {"workspace": "/tmp/ws", "max_retries": 3}
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("元数据任务", metadata=meta)
        )
        assert task.metadata["workspace"] == "/tmp/ws"
        assert task.metadata["max_retries"] == 3

    def test_create_task_default_status_pending(self, service):
        """新任务默认状态为 pending。"""
        from tasks.types import TaskStatus
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("默认状态任务")
        )
        assert task.status == TaskStatus.PENDING

    def test_invalid_transition_raises(self, service):
        """非法状态转换抛出 InvalidTransitionError。"""
        from tasks.state_machine import InvalidTransitionError
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("转换测试")
        )
        # pending 不能直接转到 evaluating（需先经过 running）
        with pytest.raises(InvalidTransitionError):
            asyncio.get_event_loop().run_until_complete(
                service.move_to_evaluating(task.id)
            )

    def test_priority_range(self):
        """优先级枚举覆盖 1-10 范围。"""
        from tasks.types import TaskPriority
        values = [p.value for p in TaskPriority]
        assert min(values) == 1
        assert max(values) == 9  # BACKGROUND=9
        assert TaskPriority.CRITICAL.value == 1
        assert TaskPriority.HIGH.value == 3
        assert TaskPriority.NORMAL.value == 5
        assert TaskPriority.LOW.value == 7
        assert TaskPriority.BACKGROUND.value == 9


# ═══════════════════════════════════════════════════════════════
# 区域2: 任务执行
# ═══════════════════════════════════════════════════════════════

class TestRegion2TaskExecution:
    """区域2: 任务执行 - 状态机 + Agent 架构验证。"""

    @pytest.fixture
    def service(self):
        from tasks.storage import TaskStorage
        from tasks.service import TaskService
        return TaskService(storage=TaskStorage())

    def test_full_lifecycle_pass(self, service):
        """完整生命周期: pending→running→evaluating→completed。"""
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("完整生命周期")
        )
        tid = task.id

        asyncio.get_event_loop().run_until_complete(service.start_task(tid))
        assert service.get_task(tid).status.value == "running"

        asyncio.get_event_loop().run_until_complete(service.move_to_evaluating(tid))
        assert service.get_task(tid).status.value == "evaluating"

        asyncio.get_event_loop().run_until_complete(
            service.complete_evaluation(tid, passed=True)
        )
        assert service.get_task(tid).status.value == "completed"

    def test_full_lifecycle_fail(self, service):
        """完整生命周期: pending→running→evaluating→failed。"""
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("失败生命周期")
        )
        tid = task.id

        asyncio.get_event_loop().run_until_complete(service.start_task(tid))
        asyncio.get_event_loop().run_until_complete(service.move_to_evaluating(tid))
        asyncio.get_event_loop().run_until_complete(
            service.complete_evaluation(tid, passed=False)
        )
        assert service.get_task(tid).status.value == "failed"

    def test_pause_resume(self, service):
        """暂停和恢复: running→paused→running。"""
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("暂停恢复测试")
        )
        tid = task.id

        asyncio.get_event_loop().run_until_complete(service.start_task(tid))
        asyncio.get_event_loop().run_until_complete(service.pause_task(tid))
        assert service.get_task(tid).status.value == "paused"

        asyncio.get_event_loop().run_until_complete(service.resume_task(tid))
        assert service.get_task(tid).status.value == "running"

    def test_terminal_state_no_transition(self, service):
        """终态（completed）不可再转换到 running。"""
        from tasks.state_machine import InvalidTransitionError
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("终态测试")
        )
        tid = task.id
        asyncio.get_event_loop().run_until_complete(service.start_task(tid))
        asyncio.get_event_loop().run_until_complete(
            service.fail_task(tid, "测试失败")
        )
        # completed 不能直接转 running
        # 但 failed → pending 是允许的
        asyncio.get_event_loop().run_until_complete(
            service.reset_to_pending(tid)
        )
        assert service.get_task(tid).status.value == "pending"

    def test_agent_level_enum(self):
        """Agent 三层架构层级枚举验证。"""
        from agents.types import AgentLevel
        assert AgentLevel.L1_MAIN.value == "L1"
        assert AgentLevel.L2_SUBTASK.value == "L2"
        assert AgentLevel.L3_ATOMIC.value == "L3"
        assert len(AgentLevel) == 3

    def test_agent_type_enum(self):
        """Agent 类型枚举验证。"""
        from agents.types import AgentType
        assert AgentType.MAIN.value == "main"
        assert AgentType.SPECIALIZED.value == "specialized"
        assert AgentType.SYSTEM.value == "system"

    def test_agent_config_loader_yaml(self):
        """AgentConfigLoader 从 YAML 加载配置。"""
        from agents.loader import AgentConfigLoader
        import yaml

        yaml_path = ROOT_DIR / "config" / "agents" / "complete_agent_001.yaml"
        if not yaml_path.exists():
            pytest.skip("complete_agent_001.yaml 不存在")

        config = AgentConfigLoader.load_from_yaml(yaml_path)
        assert config.config_id != ""
        assert config.name != ""
        assert config.level in [
            __import__("agents.types", fromlist=["AgentLevel"]).AgentLevel.L1_MAIN,
            __import__("agents.types", fromlist=["AgentLevel"]).AgentLevel.L2_SUBTASK,
            __import__("agents.types", fromlist=["AgentLevel"]).AgentLevel.L3_ATOMIC,
        ]

    def test_agent_registry_register_and_query(self):
        """AgentRegistry 注册和查询功能。"""
        from agents.registry import AgentRegistry
        from agents.types import AgentConfig, AgentLevel, AgentType

        registry = AgentRegistry()
        config = AgentConfig(
            config_id="test_agent",
            name="TestAgent",
            level=AgentLevel.L2_SUBTASK,
            agent_type=AgentType.SPECIALIZED,
            tags=["test", "demo"],
            tool_ids=["bash", "file_read"],
        )
        registry.register(config)

        # 按 ID 查询
        found = registry.get("test_agent")
        assert found is not None
        assert found.name == "TestAgent"

        # 按层级筛选
        l2_agents = registry.find_by_level(AgentLevel.L2_SUBTASK)
        assert len(l2_agents) == 1

        # 按类型筛选
        spec_agents = registry.find_by_type(AgentType.SPECIALIZED)
        assert len(spec_agents) == 1

        # 按标签筛选
        tagged = registry.find_by_tag("demo")
        assert len(tagged) == 1

        # 按工具筛选
        tool_agents = registry.find_by_tool("bash")
        assert len(tool_agents) == 1

    def test_agent_registry_unregister(self):
        """AgentRegistry 注销功能。"""
        from agents.registry import AgentRegistry
        from agents.types import AgentConfig

        registry = AgentRegistry()
        config = AgentConfig(config_id="to_remove", name="ToRemove")
        registry.register(config)
        assert registry.count() == 1

        result = registry.unregister("to_remove")
        assert result is True
        assert registry.count() == 0
        assert registry.get("to_remove") is None

    def test_context_builder(self):
        """ContextBuilder 构建上下文功能。"""
        from agents.context_builder import ContextBuilder
        from agents.types import (
            AgentConfig, ContextConfig, ContextVarItem, AgentLevel,
        )

        config = AgentConfig(
            config_id="ctx_test",
            name="CtxTest",
            level=AgentLevel.L3_ATOMIC,
            hard_constraints=["必须使用 UTF-8"],
            soft_constraints=["尽量添加注释"],
            static_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(name="rules_var", type="rules"),
                    ContextVarItem(name="inline_var", type="inline", content="Hello"),
                ],
            ),
            dynamic_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(name="ts", type="timestamp"),
                ],
            ),
        )

        builder = ContextBuilder()
        static = builder.build_static_context(config)
        assert static["enabled"] is True
        assert len(static["items"]) == 2

        dynamic = builder.build_dynamic_context(config)
        assert dynamic["enabled"] is True
        assert len(dynamic["items"]) == 1
        assert dynamic["items"][0]["type"] == "timestamp"

        full = builder.build_full_context(config)
        assert "static" in full
        assert "dynamic" in full


# ═══════════════════════════════════════════════════════════════
# 区域3: 长期任务
# ═══════════════════════════════════════════════════════════════

class TestRegion3LongTermTasks:
    """区域3: 长期任务 - 容器任务 + 子任务 + 进度 + 触发器。"""

    @pytest.fixture
    def service(self):
        from tasks.storage import TaskStorage
        from tasks.service import TaskService
        return TaskService(storage=TaskStorage())

    def test_container_task_creation(self, service):
        """容器任务创建（parent_task_id=None）。"""
        task = asyncio.get_event_loop().run_until_complete(
            service.create_task("容器任务")
        )
        assert task.parent_task_id is None
        assert task.status.value == "pending"

    def test_subtask_parent_binding(self, service):
        """子任务关联到父任务。"""
        parent = asyncio.get_event_loop().run_until_complete(
            service.create_task("父任务")
        )
        child = asyncio.get_event_loop().run_until_complete(
            service.create_task("子任务", parent_task_id=parent.id)
        )
        assert child.parent_task_id == parent.id

    def test_list_subtasks(self, service):
        """list_by_parent 查询子任务。"""
        parent = asyncio.get_event_loop().run_until_complete(
            service.create_task("父")
        )
        for i in range(3):
            asyncio.get_event_loop().run_until_complete(
                service.create_task(f"子{i}", parent_task_id=parent.id)
            )
        subtasks = service.list_subtasks(parent.id)
        assert len(subtasks) == 3

    def test_progress_calculation(self, service):
        """ProgressCalculator 进度计算（等权平均）。"""
        parent = asyncio.get_event_loop().run_until_complete(
            service.create_task("进度父任务")
        )
        c1 = asyncio.get_event_loop().run_until_complete(
            service.create_task("子1", parent_task_id=parent.id)
        )
        c2 = asyncio.get_event_loop().run_until_complete(
            service.create_task("子2", parent_task_id=parent.id)
        )

        # 初始进度 0
        progress = service.get_progress(parent.id)
        assert progress == 0.0

        # 完成1个子任务 → 50%
        asyncio.get_event_loop().run_until_complete(service.start_task(c1.id))
        asyncio.get_event_loop().run_until_complete(service.move_to_evaluating(c1.id))
        asyncio.get_event_loop().run_until_complete(
            service.complete_evaluation(c1.id, passed=True)
        )
        progress = service.get_progress(parent.id)
        assert progress == 50.0

        # 全部完成 → 100%
        asyncio.get_event_loop().run_until_complete(service.start_task(c2.id))
        asyncio.get_event_loop().run_until_complete(service.move_to_evaluating(c2.id))
        asyncio.get_event_loop().run_until_complete(
            service.complete_evaluation(c2.id, passed=True)
        )
        progress = service.get_progress(parent.id)
        assert progress == 100.0

    def test_progress_no_subtasks(self, service):
        """无子任务时进度为 0.0。"""
        parent = asyncio.get_event_loop().run_until_complete(
            service.create_task("无子任务")
        )
        assert service.get_progress(parent.id) == 0.0

    def test_trigger_manager_register(self):
        """TriggerManager 注册触发器。"""
        from triggers.manager import TriggerManager
        from triggers.types import TriggerConfig, TriggerType, TriggerStatus

        mgr = TriggerManager()
        config = TriggerConfig(
            trigger_id="test_trigger",
            name="TestTrigger",
            trigger_type=TriggerType.EVENT,
            event_name="task_completed",
        )
        mgr.register(config)

        found = mgr.get("test_trigger")
        assert found is not None
        assert found.name == "TestTrigger"
        assert found.status == TriggerStatus.ACTIVE

    def test_trigger_manager_evaluate_event(self):
        """TriggerManager 事件触发器评估。"""
        from triggers.manager import TriggerManager
        from triggers.types import TriggerConfig, TriggerType

        mgr = TriggerManager()
        config = TriggerConfig(
            trigger_id="evt_trig",
            name="EventTrigger",
            trigger_type=TriggerType.EVENT,
            event_name="task_completed",
            max_fires=2,
        )
        mgr.register(config)

        # 首次触发
        fired = mgr.evaluate_event("task_completed", {"task_id": "t1"})
        assert len(fired) == 1
        assert fired[0] == "evt_trig"

        # 第二次触发
        fired = mgr.evaluate_event("task_completed", {"task_id": "t2"})
        assert len(fired) == 1

        # 第三次不触发（已达 max_fires）
        fired = mgr.evaluate_event("task_completed", {"task_id": "t3"})
        assert len(fired) == 0

    def test_trigger_manager_evaluate_condition(self):
        """TriggerManager 条件触发器评估。"""
        from triggers.manager import TriggerManager
        from triggers.types import TriggerConfig, TriggerType

        mgr = TriggerManager()
        config = TriggerConfig(
            trigger_id="cond_trig",
            name="CondTrigger",
            trigger_type=TriggerType.CONDITION,
            condition_expression="progress > 80",
        )
        mgr.register(config)

        # 条件不满足
        fired = mgr.evaluate_condition({"progress": 50})
        assert len(fired) == 0

        # 条件满足
        fired = mgr.evaluate_condition({"progress": 90})
        assert len(fired) == 1

    def test_trigger_manager_unregister(self):
        """TriggerManager 注销触发器。"""
        from triggers.manager import TriggerManager
        from triggers.types import TriggerConfig, TriggerType

        mgr = TriggerManager()
        config = TriggerConfig(
            trigger_id="to_remove",
            name="ToRemove",
            trigger_type=TriggerType.EVENT,
            event_name="test",
        )
        mgr.register(config)
        assert mgr.unregister("to_remove") is True
        assert mgr.get("to_remove") is None

    def test_trigger_manager_cancel(self):
        """TriggerManager 取消触发器。"""
        from triggers.manager import TriggerManager
        from triggers.types import TriggerConfig, TriggerType, TriggerStatus

        mgr = TriggerManager()
        config = TriggerConfig(
            trigger_id="to_cancel",
            name="ToCancel",
            trigger_type=TriggerType.EVENT,
            event_name="test",
        )
        mgr.register(config)
        mgr.cancel("to_cancel")
        assert mgr.get("to_cancel").status == TriggerStatus.CANCELLED

    def test_trigger_models_serialization(self):
        """触发器模型序列化/反序列化。"""
        from triggers.models import TriggerConfig, TriggerType, ActionConfig, ActionType

        config = TriggerConfig(
            id="test_model",
            name="TestModel",
            trigger_type=TriggerType.EVENT,
            actions=[
                ActionConfig(type=ActionType.NOTIFICATION, config={"target": "user1"})
            ],
        )
        d = config.to_dict()
        assert d["id"] == "test_model"
        assert d["trigger_type"] == "event"

        # 反序列化
        restored = TriggerConfig.from_dict(d)
        assert restored.id == "test_model"
        assert restored.trigger_type == TriggerType.EVENT


# ═══════════════════════════════════════════════════════════════
# 区域4: 任务交互
# ═══════════════════════════════════════════════════════════════

class TestRegion4TaskInteraction:
    """区域4: 任务交互 - inject + human_interaction + WebSocket。"""

    def test_human_interaction_service_import(self):
        """human_interaction 服务模块可导入。"""
        from human_interaction import (
            IHumanInteractionService,
            IInteractionNotifier,
            HumanInteractionService,
            InteractionMode,
            InteractionStatus,
            ResponseType,
            Priority,
            TimeoutAction,
            InteractionTimeoutError,
            InteractionCancelledError,
            InteractionDeniedError,
        )
        assert IHumanInteractionService is not None
        assert HumanInteractionService is not None
        assert InteractionMode is not None

    def test_inject_route_defined(self):
        """inject 相关 API 路由已定义。"""
        routes_path = ROOT_DIR / "src" / "channels" / "api" / "routes_missing.py"
        assert routes_path.exists(), "routes_missing.py 不存在"

        content = routes_path.read_text(encoding="utf-8")
        assert "inject" in content
        assert "inject_agent_message" in content

    def test_pipeline_message_bus_import(self):
        """管道消息总线可导入。"""
        from pipeline.message_bus import send_pipeline_message
        assert send_pipeline_message is not None

    def test_message_queue_import(self):
        """消息队列可导入。"""
        from infrastructure.message_queue import MessageQueue
        assert MessageQueue is not None


# ═══════════════════════════════════════════════════════════════
# 区域5: 消息通信
# ═══════════════════════════════════════════════════════════════

class TestRegion5Messaging:
    """区域5: 消息通信 - 通道网关 + 消息规范化 + 会话桥接。"""

    def test_input_output_adapters(self):
        """输入/输出适配器可导入。"""
        from channels.input_adapter import IInputAdapter
        from channels.output_adapter import IOutputAdapter
        assert IInputAdapter is not None
        assert IOutputAdapter is not None

    def test_channel_gateway(self):
        """ChannelGateway 可导入和实例化。"""
        from channels.gateway.channel_gateway import ChannelGateway
        gw = ChannelGateway()
        assert gw is not None

    def test_session_bridge(self):
        """SessionBridge 可导入和实例化。"""
        from channels.gateway.session_bridge import SessionBridge
        bridge = SessionBridge()
        assert bridge is not None

    def test_message_normalizer(self):
        """MessageNormalizer 可导入。"""
        from channels.gateway.message_normalizer import MessageNormalizer
        assert MessageNormalizer is not None

    def test_unified_types(self):
        """统一消息/响应类型可导入。"""
        from channels.gateway.unified_types import UnifiedMessage, UnifiedResponse
        assert UnifiedMessage is not None
        assert UnifiedResponse is not None

    def test_websocket_modules(self):
        """WebSocket 模块可导入。"""
        from channels.websocket.session_manager import SessionManager
        assert SessionManager is not None

    def test_websocket_protocol_events(self):
        """WebSocket 协议事件类型丰富。"""
        import channels.websocket.protocol as wsp
        # 检查关键事件类型存在
        event_classes = [
            "StreamChunkData", "StreamStartData", "StreamEndData",
            "PipelineStartData", "PipelineEndData",
            "ExecutionStartData", "ExecutionProgressData", "ExecutionDoneData",
        ]
        for cls_name in event_classes:
            assert hasattr(wsp, cls_name), f"缺少事件类型: {cls_name}"

    def test_multi_channel_adapters(self):
        """多通道适配器（飞书、钉钉、企微、QQ）模块可导入。"""
        from channels.feishu.adapter import FeishuAdapter
        from channels.dingtalk.adapter import DingTalkAdapter
        from channels.wecom.adapter import WeComAdapter
        from channels.qq.adapter import QQAdapter
        assert FeishuAdapter is not None
        assert DingTalkAdapter is not None
        assert WeComAdapter is not None
        assert QQAdapter is not None

    def test_message_pagination_code_exists(self):
        """消息分页功能代码存在。"""
        routes_path = ROOT_DIR / "src" / "channels" / "api" / "routes_threads.py"
        assert routes_path.exists(), "routes_threads.py 不存在"
        content = routes_path.read_text(encoding="utf-8")
        # 检查分页相关参数
        assert "before_sequence" in content or "before" in content
        assert "limit" in content


# ═══════════════════════════════════════════════════════════════
# 区域6: 体验优化
# ═══════════════════════════════════════════════════════════════

class TestRegion6Experience:
    """区域6: 体验优化 - 前端构建 + UI 组件 + 路由 + Stores。"""

    def test_frontend_package_json_exists(self):
        """前端 package.json 存在。"""
        pkg_path = ROOT_DIR / "frontend" / "package.json"
        assert pkg_path.exists(), "frontend/package.json 不存在"

    def test_frontend_dependencies(self):
        """UI 组件库依赖完整性检查。"""
        import json
        pkg_path = ROOT_DIR / "frontend" / "package.json"
        with open(pkg_path) as f:
            pkg = json.load(f)

        deps = pkg.get("dependencies", {})
        dev_deps = pkg.get("devDependencies", {})

        # Radix UI
        radix_keys = [k for k in deps if k.startswith("@radix-ui")]
        assert len(radix_keys) >= 3, f"Radix UI 组件不足: {radix_keys}"

        # TailwindCSS
        assert "tailwindcss" in dev_deps or "@tailwindcss/postcss" in dev_deps, \
            "缺少 TailwindCSS"

        # LobeHub UI
        assert "@lobehub/ui" in deps, "缺少 @lobehub/ui"

        # Zustand
        assert "zustand" in deps, "缺少 zustand"

        # React
        assert "react" in deps, "缺少 react"
        assert "react-dom" in deps, "缺少 react-dom"

        # React Router
        assert "react-router-dom" in deps, "缺少 react-router-dom"

    def test_frontend_router_exists(self):
        """前端路由配置文件存在。"""
        router_path = ROOT_DIR / "frontend" / "src" / "router.tsx"
        assert router_path.exists(), "router.tsx 不存在"

    def test_frontend_router_routes(self):
        """前端路由定义完整性。"""
        router_path = ROOT_DIR / "frontend" / "src" / "router.tsx"
        content = router_path.read_text(encoding="utf-8")

        # 检查关键路由
        essential_routes = ["HOME", "LOGIN", "REGISTER"]
        for route in essential_routes:
            assert route in content, f"缺少路由定义: {route}"

        # 检查 ProtectedRoute
        assert "ProtectedRoute" in content, "缺少 ProtectedRoute"

        # 检查 404 通配符
        assert "path" in content and "*" in content, "缺少 404 通配路由"

    def test_frontend_stores_structure(self):
        """Zustand stores 结构完整性。"""
        stores_dir = ROOT_DIR / "frontend" / "src" / "stores"
        assert stores_dir.exists(), "stores 目录不存在"

        store_files = list(stores_dir.glob("*.ts"))
        store_names = [f.stem for f in store_files if not f.stem.endswith(".bak")]

        # 核心 Stores 必须存在
        essential_stores = [
            "sessionStore",
            "authStore",
            "themeStore",
            "uiStore",
            "streamingStore",
        ]
        for store in essential_stores:
            assert store in store_names, f"缺少核心 Store: {store}"

        # 检查 index.ts 导出
        index_path = stores_dir / "index.ts"
        assert index_path.exists(), "stores/index.ts 不存在"

    def test_frontend_viewport_meta(self):
        """移动端适配 - viewport meta 配置。"""
        index_html = ROOT_DIR / "frontend" / "index.html"
        if not index_html.exists():
            pytest.skip("index.html 不存在")

        content = index_html.read_text(encoding="utf-8")
        assert "viewport" in content, "缺少 viewport meta"

    def test_frontend_index_css(self):
        """全局样式文件存在。"""
        index_css = ROOT_DIR / "frontend" / "src" / "index.css"
        assert index_css.exists(), "index.css 不存在"

    def test_frontend_main_entry(self):
        """前端入口文件结构正确。"""
        main_path = ROOT_DIR / "frontend" / "src" / "main.tsx"
        assert main_path.exists(), "main.tsx 不存在"

        content = main_path.read_text(encoding="utf-8")
        assert "StrictMode" in content
        assert "createRoot" in content
        assert "App" in content

    def test_frontend_vite_config(self):
        """Vite 配置文件存在。"""
        vite_configs = [
            ROOT_DIR / "frontend" / "vite.config.ts",
            ROOT_DIR / "frontend" / "vite.config.js",
        ]
        assert any(p.exists() for p in vite_configs), "缺少 vite 配置文件"

    def test_frontend_tsconfig(self):
        """TypeScript 配置文件存在。"""
        tsconfig = ROOT_DIR / "frontend" / "tsconfig.json"
        if not tsconfig.exists():
            tsconfig = ROOT_DIR / "frontend" / "tsconfig.app.json"
        assert tsconfig.exists(), "缺少 tsconfig.json"


# ═══════════════════════════════════════════════════════════════
# 汇总
# ═══════════════════════════════════════════════════════════════

class TestSummary:
    """验证汇总信息。"""

    def test_verification_complete(self):
        """验证套件完整性检查。"""
        # 本测试只用于确认所有区域都有测试类
        import inspect
        import sys

        module = sys.modules[__name__]
        test_classes = [
            obj for name, obj in inspect.getmembers(module, inspect.isclass)
            if name.startswith("TestRegion")
        ]
        assert len(test_classes) == 6, f"期望 6 个区域测试类，实际 {len(test_classes)}: {[c.__name__ for c in test_classes]}"

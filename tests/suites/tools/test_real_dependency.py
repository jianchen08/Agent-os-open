"""
真实依赖工具测试 -- 不使用 Mock，而是把真实依赖接上去测试工具。

覆盖范围：
- 第一部分：MemoryTool -- 语义知识存储、情景记忆存储、检索、上下文统计、知识导入
- 第二部分：TaskSubmitTool -- 短期/长期任务提交、参数校验、事件发布
- 第三部分：TriggerSetupTool -- 延迟/定时/事件/条件触发器设置、上限校验
- 第四部分：ResourceSearchTool -- Agent 资源搜索
- 第五部分：ResourceMergeTool -- 基于 git 的资源合并与回滚
"""

from __future__ import annotations

import subprocess
import sys
import types as _types
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# =====================================================================
# 前置准备：注册 triggers.message_queue 模块
# trigger_setup.py 在模块顶层 import triggers.message_queue，
# 但 src/triggers/ 目录下不存在 message_queue.py，因此需要手动注册。
# =====================================================================


@dataclass
class _TriggerMessage:
    """触发器消息数据类。"""

    id: str = ""
    session_id: str = ""
    execution_id: str = ""
    content: str = ""
    priority: int = 0
    expires_at: datetime | None = None
    metadata: dict = field(default_factory=dict)


class _InMemoryTriggerQueue:
    """内存触发器消息队列，用于测试。"""

    def __init__(self) -> None:
        self._queues: dict[str, list[_TriggerMessage]] = defaultdict(list)

    async def push(self, msg: _TriggerMessage) -> None:
        """推送消息到队列。"""
        self._queues[msg.session_id].append(msg)

    async def size(self, session_id: str) -> int:
        """获取指定会话的队列大小。"""
        return len(self._queues.get(session_id, []))

    async def pop(self, session_id: str) -> _TriggerMessage | None:
        """弹出指定会话的最早一条消息。"""
        queue = self._queues.get(session_id, [])
        return queue.pop(0) if queue else None


# 全局单例
_trigger_queue_instance = _InMemoryTriggerQueue()


def _get_trigger_message_queue() -> _InMemoryTriggerQueue:
    """获取全局触发器消息队列实例。"""
    return _trigger_queue_instance


# 注册为 triggers.message_queue 模块
_mq = _types.ModuleType("triggers.message_queue")
_mq.TriggerMessage = _TriggerMessage
_mq.InMemoryTriggerQueue = _InMemoryTriggerQueue
_mq.get_trigger_message_queue = _get_trigger_message_queue
sys.modules.setdefault("triggers.message_queue", _mq)


# =====================================================================
# 安全导入业务模块（在 triggers.message_queue 已注册之后）
# =====================================================================

from memory.ports import IEpisodeStorage, ISemanticStorage
from memory.service import MemoryService
from memory.types import Episode, Knowledge
from tools.builtin.memory import MemoryTool

from pipeline.event_bus import EventBus
from tasks.service import TaskService
from tasks.storage import TaskStorage
from tools.builtin.task_submit import TaskSubmitTool

from tools.builtin.trigger_setup import TriggerSetupTool

from agents.registry import AgentRegistry
from agents.types import AgentConfig, AgentLevel, AgentType
from tools.builtin.resource_search import ResourceSearchTool

from tools.builtin.resource_merge import ResourceMergeTool


# =====================================================================
# 内存存储实现
# =====================================================================


class InMemoryEpisodeStorage(IEpisodeStorage):
    """内存情景记忆存储实现，用于测试。"""

    def __init__(self) -> None:
        self._store: dict[str, Episode] = {}

    async def save(self, episode: Episode) -> str:
        """保存情景记忆到内存字典。"""
        self._store[episode.id] = episode
        return episode.id

    async def get(self, episode_id: str) -> Episode | None:
        """按 ID 获取情景记忆。"""
        return self._store.get(episode_id)

    async def find_by_user(
        self, user_id: str, limit: int = 20, offset: int = 0,
    ) -> list[Episode]:
        """按用户 ID 查找情景记忆。"""
        episodes = [ep for ep in self._store.values() if ep.user_id == user_id]
        episodes.sort(key=lambda x: x.created_at, reverse=True)
        return episodes[offset : offset + limit]

    async def update(self, episode_id: str, **kwargs: Any) -> bool:
        """更新情景记忆字段。"""
        episode = self._store.get(episode_id)
        if not episode:
            return False
        for key, value in kwargs.items():
            if hasattr(episode, key):
                setattr(episode, key, value)
        return True

    async def delete(self, episode_id: str) -> bool:
        """删除情景记忆。"""
        if episode_id in self._store:
            del self._store[episode_id]
            return True
        return False

    async def count_by_user(self, user_id: str) -> int:
        """统计用户的情景记忆数量。"""
        return sum(1 for ep in self._store.values() if ep.user_id == user_id)


class InMemorySemanticStorage(ISemanticStorage):
    """内存语义知识存储实现，用于测试。"""

    def __init__(self) -> None:
        self._store: dict[str, Knowledge] = {}

    async def save(self, knowledge: Knowledge) -> str:
        """保存知识到内存字典。"""
        self._store[knowledge.id] = knowledge
        return knowledge.id

    async def get(self, knowledge_id: str) -> Knowledge | None:
        """按 ID 获取知识。"""
        return self._store.get(knowledge_id)

    async def find_by_user(self, user_id: str, limit: int = 20) -> list[Knowledge]:
        """按用户 ID 查找知识。"""
        items = [k for k in self._store.values() if k.user_id == user_id]
        items.sort(key=lambda x: x.created_at, reverse=True)
        return items[:limit]

    async def update_embedding(
        self, knowledge_id: str, embedding: list[float],
    ) -> bool:
        """更新知识的向量嵌入。"""
        k = self._store.get(knowledge_id)
        if not k:
            return False
        k.embedding = embedding
        return True

    async def delete(self, knowledge_id: str) -> bool:
        """删除知识。"""
        if knowledge_id in self._store:
            del self._store[knowledge_id]
            return True
        return False


# =====================================================================
# 知识导入器实现（鸭子类型接口）
# =====================================================================


@dataclass
class ImportResult:
    """知识导入结果。"""

    success: bool = True
    knowledge_id: str = ""
    file_path: str = ""
    error: str | None = None


class InMemoryKnowledgeImporter:
    """内存知识导入器，用于测试。"""

    def __init__(self, memory_service: MemoryService) -> None:
        self._memory_service = memory_service
        self._imported: dict[str, dict] = {}

    async def import_text(
        self,
        content: str,
        name: str,
        user_id: str,
        tags: list[str] | None = None,
    ) -> ImportResult:
        """导入文本知识。"""
        knowledge = Knowledge(
            user_id=user_id,
            content=content,
            source_type="text_import",
            extra_data={"name": name, "tags": tags or []},
        )
        kid = await self._memory_service.store_knowledge(knowledge)
        file_path = f"memory://text/{kid}"
        self._imported[file_path] = {"knowledge_id": kid, "name": name}
        return ImportResult(success=True, knowledge_id=kid, file_path=file_path)

    async def import_file(
        self,
        source_path: str,
        user_id: str,
        tags: list[str] | None = None,
    ) -> ImportResult:
        """导入文件知识。"""
        path = Path(source_path)
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        knowledge = Knowledge(
            user_id=user_id,
            content=content,
            source_type="file_import",
            extra_data={"file_path": source_path, "tags": tags or []},
        )
        kid = await self._memory_service.store_knowledge(knowledge)
        self._imported[source_path] = {"knowledge_id": kid}
        return ImportResult(success=True, knowledge_id=kid, file_path=source_path)

    async def update_knowledge(
        self,
        file_path: str,
        user_id: str,
        new_content: str | None = None,
        new_tags: list[str] | None = None,
    ) -> ImportResult:
        """更新知识。"""
        info = self._imported.get(file_path)
        if not info:
            return ImportResult(success=False, error=f"未找到: {file_path}")
        return ImportResult(
            success=True, knowledge_id=info["knowledge_id"], file_path=file_path,
        )

    async def delete_knowledge(
        self, file_path: str, user_id: str, delete_file: bool = True,
    ) -> bool:
        """删除知识。"""
        info = self._imported.pop(file_path, None)
        return info is not None


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture()
def memory_service() -> MemoryService:
    """创建真实的内存 MemoryService。"""
    episode_storage = InMemoryEpisodeStorage()
    semantic_storage = InMemorySemanticStorage()
    return MemoryService(
        episode_storage=episode_storage,
        semantic_storage=semantic_storage,
    )


@pytest.fixture()
def knowledge_importer(
    memory_service: MemoryService,
) -> InMemoryKnowledgeImporter:
    """创建真实的内存知识导入器。"""
    return InMemoryKnowledgeImporter(memory_service)


@pytest.fixture()
def memory_tool(
    memory_service: MemoryService,
    knowledge_importer: InMemoryKnowledgeImporter,
) -> MemoryTool:
    """创建注入了真实依赖的 MemoryTool（含 knowledge_importer）。"""
    tool = MemoryTool(knowledge_importer=knowledge_importer)
    tool._memory_service = memory_service
    return tool


@pytest.fixture()
def memory_tool_no_importer(
    memory_service: MemoryService,
) -> MemoryTool:
    """创建无 knowledge_importer 的 MemoryTool，用于测试降级路径。"""
    tool = MemoryTool()
    tool._memory_service = memory_service
    return tool


@pytest.fixture()
def task_setup() -> tuple[TaskSubmitTool, list[dict]]:
    """创建真实的任务提交环境，返回 (tool, events_received)。"""
    storage = TaskStorage(data_dir=None)
    task_service = TaskService(storage=storage)
    event_bus = EventBus()

    events_received: list[dict] = []

    async def on_task_submitted(data: dict) -> None:
        """记录 task.submitted 事件数据。"""
        events_received.append(data)

    event_bus.subscribe("task.submitted", on_task_submitted)

    tool = TaskSubmitTool()
    tool._task_service = task_service
    tool._event_bus = event_bus

    return tool, events_received


@pytest.fixture()
def trigger_queue() -> _InMemoryTriggerQueue:
    """创建全新的触发器消息队列（重置全局单例）。"""
    global _trigger_queue_instance
    _trigger_queue_instance = _InMemoryTriggerQueue()
    return _trigger_queue_instance


@pytest.fixture()
def trigger_tool(trigger_queue: _InMemoryTriggerQueue) -> TriggerSetupTool:
    """创建注入了真实队列的 TriggerSetupTool。"""
    tool = TriggerSetupTool()
    tool._queue = trigger_queue
    return tool


@pytest.fixture()
def agent_registry() -> AgentRegistry:
    """创建包含测试配置的 Agent 注册表。"""
    registry = AgentRegistry()
    config = AgentConfig(
        config_id="test_agent",
        name="TestAgent",
        description="测试用 Agent，用于验证资源搜索功能",
        agent_type=AgentType.SPECIALIZED,
        level=AgentLevel.L2_SUBTASK,
        tags=["test", "mock"],
    )
    registry.register(config)
    return registry


@pytest.fixture()
def resource_search_tool(agent_registry: AgentRegistry) -> ResourceSearchTool:
    """创建注入了真实注册表的 ResourceSearchTool。"""
    return ResourceSearchTool(agent_registry=agent_registry)


# =====================================================================
# 第一部分：MemoryTool 真实依赖测试
# =====================================================================


class TestMemoryToolReal:
    """MemoryTool 真实依赖测试 -- 使用 InMemory 存储和真实 MemoryService。"""

    async def test_store_semantic_knowledge_success(self, memory_tool: MemoryTool) -> None:
        """测试存储语义知识成功，验证返回 knowledge_id。"""
        result = await memory_tool.execute({
            "action": "store",
            "content": "Python 是一种解释型编程语言",
        })
        assert result.success
        assert result.output["success"] is True
        assert "knowledge_id" in result.output
        assert result.output["knowledge_id"]

    async def test_store_episode_success(self, memory_tool: MemoryTool) -> None:
        """测试存储情景记忆成功，验证返回 episode_id。"""
        result = await memory_tool.execute({
            "action": "store",
            "content": "用户请求创建登录功能",
            "memory_type": "episode",
            "tags": ["login", "auth"],
        })
        assert result.success
        assert result.output["success"] is True
        assert "episode_id" in result.output
        assert result.output["episode_id"]

    async def test_store_missing_content_fails(self, memory_tool: MemoryTool) -> None:
        """测试缺少 content 参数时存储失败。"""
        result = await memory_tool.execute({"action": "store"})
        assert not result.success

    async def test_retrieve_empty_results(self, memory_tool: MemoryTool) -> None:
        """测试检索返回空结果（无向量检索器，纯内存模式无匹配）。"""
        result = await memory_tool.execute({
            "action": "retrieve",
            "query": "Python",
            "inject_type": "retrieval",
        })
        assert result.success
        assert result.output["results"] == []

    async def test_get_context_stats(self, memory_tool: MemoryTool) -> None:
        """测试获取上下文统计信息，验证 episode_count 和 knowledge_count。"""
        # 先存储一些数据
        await memory_tool.execute({"action": "store", "content": "知识1"})
        await memory_tool.execute({
            "action": "store", "content": "记忆1", "memory_type": "episode",
        })

        result = await memory_tool.execute({"action": "get_context"})
        assert result.success
        assert "stats" in result.output
        stats = result.output["stats"]
        assert stats["total_count"] >= 2

    async def test_unknown_action_fails(self, memory_tool: MemoryTool) -> None:
        """测试未知操作返回失败。"""
        result = await memory_tool.execute({"action": "unknown_action"})
        assert not result.success

    async def test_no_memory_service_fails(self) -> None:
        """测试无 memory_service 时返回失败。

        需要阻止 _get_session 通过 infrastructure.db 的 get_current_session
        回退机制获取到协程对象，从而意外创建 MemoryService。
        """
        tool = MemoryTool()

        # 临时阻止 infrastructure.db 的导入，使 _get_session 返回 None
        saved_db = sys.modules.get("infrastructure.db")
        sys.modules["infrastructure.db"] = None  # type: ignore[assignment]
        try:
            result = await tool.execute({"action": "store", "content": "test"})
            assert not result.success
        finally:
            # 恢复原始模块
            if saved_db is not None:
                sys.modules["infrastructure.db"] = saved_db
            else:
                sys.modules.pop("infrastructure.db", None)

    async def test_import_text_success(self, memory_tool: MemoryTool) -> None:
        """测试导入文本知识成功，验证返回 knowledge_id 和 file_path。"""
        result = await memory_tool.execute({
            "action": "import_text",
            "content": "这是一段测试知识内容",
            "name": "测试知识",
        })
        assert result.success
        assert result.output["success"] is True
        assert "knowledge_id" in result.output
        assert "file_path" in result.output

    async def test_import_file_success(
        self, memory_tool: MemoryTool, tmp_path: Path,
    ) -> None:
        """测试导入文件知识成功，验证文件内容被读取并存储。"""
        test_file = tmp_path / "test_knowledge.txt"
        test_file.write_text("文件中的知识内容", encoding="utf-8")

        result = await memory_tool.execute({
            "action": "import_file",
            "file_path": str(test_file),
        })
        assert result.success
        assert result.output["success"] is True
        assert "knowledge_id" in result.output

    async def test_update_knowledge_success(self, memory_tool: MemoryTool) -> None:
        """测试更新知识成功，先导入再更新。"""
        import_result = await memory_tool.execute({
            "action": "import_text",
            "content": "原始内容",
            "name": "待更新知识",
        })
        file_path = import_result.output["file_path"]

        result = await memory_tool.execute({
            "action": "update",
            "file_path": file_path,
            "content": "更新后的内容",
        })
        assert result.success
        assert result.output["success"] is True

    async def test_delete_knowledge_success(self, memory_tool: MemoryTool) -> None:
        """测试删除知识成功，先导入再删除。"""
        import_result = await memory_tool.execute({
            "action": "import_text",
            "content": "待删除内容",
            "name": "待删除知识",
        })
        file_path = import_result.output["file_path"]

        result = await memory_tool.execute({
            "action": "delete",
            "file_path": file_path,
        })
        assert result.success
        assert result.output["success"] is True


class TestMemoryToolDegraded:
    """MemoryTool 降级路径测试 -- 无 knowledge_importer 时使用 MemoryService 降级。"""

    async def test_import_text_degraded(
        self, memory_tool_no_importer: MemoryTool,
    ) -> None:
        """无 importer 时 import_text 应通过 MemoryService 降级成功。"""
        result = await memory_tool_no_importer.execute({
            "action": "import_text",
            "content": "降级导入的文本内容",
            "name": "降级测试",
        })
        assert result.success
        assert result.output["success"] is True
        assert "knowledge_id" in result.output
        assert "file_path" in result.output

    async def test_update_degraded(
        self, memory_tool_no_importer: MemoryTool,
    ) -> None:
        """无 importer 时 update 应通过 MemoryService 降级成功。"""
        import_result = await memory_tool_no_importer.execute({
            "action": "import_text",
            "content": "原始内容",
            "name": "待更新",
        })
        file_path = import_result.output["file_path"]

        result = await memory_tool_no_importer.execute({
            "action": "update",
            "file_path": file_path,
            "content": "更新后内容",
        })
        assert result.success
        assert result.output["success"] is True

    async def test_delete_degraded(
        self, memory_tool_no_importer: MemoryTool,
    ) -> None:
        """无 importer 时 delete 应通过 MemoryService 降级成功。"""
        import_result = await memory_tool_no_importer.execute({
            "action": "import_text",
            "content": "待删除内容",
            "name": "待删除",
        })
        file_path = import_result.output["file_path"]

        result = await memory_tool_no_importer.execute({
            "action": "delete",
            "file_path": file_path,
        })
        assert result.success
        assert result.output["success"] is True

    async def test_import_file_degraded(
        self, memory_tool_no_importer: MemoryTool, tmp_path: Path,
    ) -> None:
        """无 importer 时 import_file 应通过 MemoryService 降级成功。"""
        test_file = tmp_path / "degraded_test.txt"
        test_file.write_text("降级文件内容", encoding="utf-8")

        result = await memory_tool_no_importer.execute({
            "action": "import_file",
            "file_path": str(test_file),
        })
        assert result.success
        assert result.output["success"] is True
        assert "knowledge_id" in result.output


# =====================================================================
# 第二部分：TaskSubmitTool 真实依赖测试
# =====================================================================


class TestTaskSubmitToolReal:
    """TaskSubmitTool 真实依赖测试 -- 使用真实 TaskStorage、TaskService 和 EventBus。"""

    async def test_short_term_task_submit_success(
        self, task_setup: tuple[TaskSubmitTool, list[dict]],
    ) -> None:
        """测试短期任务提交成功，验证 task_id 和事件接收。"""
        tool, events_received = task_setup

        result = await tool.execute({
            "goal": {"title": "实现用户登录"},
            "target_type": "agent",
            "target_id": "general_agent",
            "acceptance_criteria": {
                "file_check": {"input_params": {"path": "src/auth/login.py"}},
            },
            "priority": 5,
        })

        assert result.success
        assert "task_id" in result.output
        assert result.output["task_id"]
        assert result.output["status"] == "pending"
        assert result.output["submit_status"] == "submitted"
        # 验证事件总线已接收到 task.submitted 事件
        assert len(events_received) == 1
        assert events_received[0]["task_id"] == result.output["task_id"]

    async def test_long_term_task_submit_success(
        self, task_setup: tuple[TaskSubmitTool, list[dict]],
    ) -> None:
        """测试长期任务提交成功，验证 task_scope 为 long_term。"""
        tool, _ = task_setup

        result = await tool.execute({
            "goal": {"title": "重构认证系统"},
            "task_scope": "long_term",
            "parent_agent_level": 1,
        })

        assert result.success
        assert result.output["task_scope"] == "long_term"
        assert result.output["status"] == "pending"

    async def test_missing_goal_fails(
        self, task_setup: tuple[TaskSubmitTool, list[dict]],
    ) -> None:
        """测试缺少 goal 时提交失败。"""
        tool, _ = task_setup

        result = await tool.execute({
            "target_type": "agent",
            "target_id": "general_agent",
        })

        assert not result.success
        assert "GOAL" in result.error_code or "goal" in result.error.lower()

    async def test_missing_target_type_fails(
        self, task_setup: tuple[TaskSubmitTool, list[dict]],
    ) -> None:
        """测试缺少 target_type 时短期任务提交失败。"""
        tool, _ = task_setup

        result = await tool.execute({
            "goal": {"title": "测试任务"},
            "target_id": "general_agent",
            "acceptance_criteria": {
                "file_check": {"input_params": {"path": "test.py"}},
            },
        })

        assert not result.success
        assert "TARGET_TYPE" in result.error_code

    async def test_missing_acceptance_criteria_fails(
        self, task_setup: tuple[TaskSubmitTool, list[dict]],
    ) -> None:
        """测试缺少 acceptance_criteria 时短期任务提交失败。"""
        tool, _ = task_setup

        result = await tool.execute({
            "goal": {"title": "测试任务"},
            "target_type": "agent",
            "target_id": "general_agent",
        })

        assert not result.success
        assert "METRICS" in result.error_code

    async def test_l2_agent_cannot_submit_long_term(
        self, task_setup: tuple[TaskSubmitTool, list[dict]],
    ) -> None:
        """测试 L2 Agent 不能提交长期任务。"""
        tool, _ = task_setup

        result = await tool.execute({
            "goal": {"title": "长期任务"},
            "task_scope": "long_term",
            "parent_agent_level": 2,
        })

        assert not result.success
        assert "L2_CANNOT_SUBMIT_LONG_TERM" in result.error_code


# =====================================================================
# 第三部分：TriggerSetupTool 真实依赖测试
# =====================================================================


class TestTriggerSetupToolReal:
    """TriggerSetupTool 真实依赖测试 -- 使用 InMemoryTriggerQueue。"""

    async def test_delay_trigger_success(
        self,
        trigger_tool: TriggerSetupTool,
        trigger_queue: _InMemoryTriggerQueue,
    ) -> None:
        """测试延迟触发器设置成功，验证队列中有消息。"""
        result = await trigger_tool.execute({
            "trigger_type": "delay",
            "message": "请检查任务状态",
            "delay_seconds": 60,
            "session_id": "session-001",
            "execution_id": "exec-001",
        })

        assert result.success
        assert result.output["success"] is True
        assert "trigger_id" in result.output
        # 验证队列中确实有消息
        size = await trigger_queue.size("session-001")
        assert size == 1

    async def test_schedule_trigger_success(
        self,
        trigger_tool: TriggerSetupTool,
        trigger_queue: _InMemoryTriggerQueue,
    ) -> None:
        """测试定时触发器设置成功，验证消息已入队。"""
        future_time = (datetime.utcnow() + timedelta(hours=1)).isoformat()

        result = await trigger_tool.execute({
            "trigger_type": "schedule",
            "message": "下班前检查任务进度",
            "schedule_time": future_time,
            "session_id": "session-002",
        })

        assert result.success
        assert result.output["success"] is True
        size = await trigger_queue.size("session-002")
        assert size == 1

    async def test_event_trigger_success(
        self,
        trigger_tool: TriggerSetupTool,
        trigger_queue: _InMemoryTriggerQueue,
    ) -> None:
        """测试事件触发器设置成功，验证消息已入队。"""
        result = await trigger_tool.execute({
            "trigger_type": "event",
            "message": "任务完成通知",
            "event_type": "task_completed",
            "session_id": "session-003",
        })

        assert result.success
        assert result.output["success"] is True
        size = await trigger_queue.size("session-003")
        assert size == 1

    async def test_condition_trigger_success(
        self,
        trigger_tool: TriggerSetupTool,
        trigger_queue: _InMemoryTriggerQueue,
    ) -> None:
        """测试条件触发器设置成功，验证消息已入队。"""
        result = await trigger_tool.execute({
            "trigger_type": "condition",
            "message": "条件满足时触发",
            "condition": "task_status == 'pending'",
            "session_id": "session-004",
        })

        assert result.success
        assert result.output["success"] is True
        size = await trigger_queue.size("session-004")
        assert size == 1

    async def test_trigger_limit_exceeded(
        self,
        trigger_tool: TriggerSetupTool,
        trigger_queue: _InMemoryTriggerQueue,
    ) -> None:
        """测试触发器数量上限（10个），第11个应失败。"""
        session_id = "session-limit"

        # 设置 10 个触发器
        for i in range(10):
            res = await trigger_tool.execute({
                "trigger_type": "event",
                "message": f"触发器 {i}",
                "event_type": "test_event",
                "session_id": session_id,
            })
            assert res.success, f"第 {i + 1} 个触发器设置应成功"

        # 第 11 个应该失败
        result = await trigger_tool.execute({
            "trigger_type": "event",
            "message": "超限触发器",
            "event_type": "test_event",
            "session_id": session_id,
        })

        assert not result.success
        assert "TRIGGER_LIMIT" in result.error_code

    async def test_missing_params_fails(
        self,
        trigger_tool: TriggerSetupTool,
        trigger_queue: _InMemoryTriggerQueue,
    ) -> None:
        """测试缺少参数时触发器设置失败。"""
        # 缺少 message
        result = await trigger_tool.execute({
            "trigger_type": "delay",
            "delay_seconds": 60,
            "session_id": "session-005",
        })
        assert not result.success

        # 缺少 delay_seconds（delay 类型必需）
        result = await trigger_tool.execute({
            "trigger_type": "delay",
            "message": "测试",
            "session_id": "session-005",
        })
        assert not result.success

        # 缺少 session_id（注入参数）
        result = await trigger_tool.execute({
            "trigger_type": "delay",
            "message": "测试",
            "delay_seconds": 10,
        })
        assert not result.success


# =====================================================================
# 第四部分：ResourceSearchTool 真实依赖测试
# =====================================================================


class TestResourceSearchToolReal:
    """ResourceSearchTool 真实依赖测试 -- 使用真实 AgentRegistry。"""

    async def test_search_agent_success(
        self, resource_search_tool: ResourceSearchTool,
    ) -> None:
        """测试搜索 Agent 成功，验证返回结果包含已注册的 Agent。"""
        result = await resource_search_tool.execute({
            "resource_type": "agent",
            "query": "TestAgent",
        })

        assert result.success
        assert result.output.get("agent_c", 0) >= 1
        # 验证搜索结果包含 TestAgent
        agent_names = [row[1] for row in result.output.get("agent_d", [])]
        assert "TestAgent" in agent_names

    async def test_search_agent_no_results(
        self, resource_search_tool: ResourceSearchTool,
    ) -> None:
        """测试搜索不存在的 Agent 返回空结果。"""
        result = await resource_search_tool.execute({
            "resource_type": "agent",
            "query": "NonExistentAgentXYZ99999",
        })

        assert result.success
        assert result.output.get("agent_c", 0) == 0

    async def test_search_all_types(
        self, resource_search_tool: ResourceSearchTool,
    ) -> None:
        """测试搜索所有类型资源，验证返回结构正确。"""
        result = await resource_search_tool.execute({
            "resource_type": "all",
            "query": "Test",
        })

        assert result.success
        assert "mode" in result.output


# =====================================================================
# 第五部分：ResourceMergeTool 真实 git 测试
# =====================================================================


def _init_git_repo(path: Path) -> None:
    """初始化测试用 git 仓库，创建初始提交。"""
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), capture_output=True, check=True,
    )
    (path / "test.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path), capture_output=True, check=True,
    )


class TestResourceMergeToolReal:
    """ResourceMergeTool 真实 git 测试 -- 使用 tmp_path 创建临时仓库。"""

    async def test_prepare_creates_worktree(self, tmp_path: Path) -> None:
        """测试 prepare 创建 worktree 分支，验证分支名和目录存在。"""
        base = tmp_path / "repo"
        base.mkdir()
        _init_git_repo(base)

        tool = ResourceMergeTool(base_path=str(base))
        workspace = tmp_path / "workspace_prepare"

        result = await tool.execute({
            "action": "prepare",
            "workspace": str(workspace),
        })

        assert result.success
        assert result.output["branch_name"].startswith("task/")
        assert workspace.exists()
        # worktree 应包含基础仓库的文件
        assert (workspace / "test.txt").exists()

        # 清理 worktree
        await tool.execute({"action": "cleanup", "workspace": str(workspace)})

    async def test_git_status_shows_changes(self, tmp_path: Path) -> None:
        """测试 git_status 显示工作区变更。"""
        base = tmp_path / "repo"
        base.mkdir()
        _init_git_repo(base)

        tool = ResourceMergeTool(base_path=str(base))
        workspace = tmp_path / "workspace_status"

        # 先 prepare
        await tool.execute({"action": "prepare", "workspace": str(workspace)})

        # 在 worktree 中添加新文件（未跟踪）
        (workspace / "new_file.txt").write_text("新文件", encoding="utf-8")

        result = await tool.execute({
            "action": "git_status",
            "workspace": str(workspace),
        })

        assert result.success
        assert result.output["total_changes"] >= 1
        assert "new_file.txt" in result.output["untracked"]

        # 清理
        await tool.execute({"action": "cleanup", "workspace": str(workspace)})

    async def test_git_commit_creates_commit(self, tmp_path: Path) -> None:
        """测试 git_commit 创建提交，验证返回 commit_hash。"""
        base = tmp_path / "repo"
        base.mkdir()
        _init_git_repo(base)

        tool = ResourceMergeTool(base_path=str(base))
        workspace = tmp_path / "workspace_commit"

        await tool.execute({"action": "prepare", "workspace": str(workspace)})

        # 添加新文件
        (workspace / "new_file.txt").write_text("新内容", encoding="utf-8")

        result = await tool.execute({
            "action": "git_commit",
            "workspace": str(workspace),
            "message": "添加新文件",
        })

        assert result.success
        assert "commit_hash" in result.output
        assert result.output["commit_hash"] is not None

        # 清理
        await tool.execute({"action": "cleanup", "workspace": str(workspace)})

    async def test_git_diff_shows_changes(self, tmp_path: Path) -> None:
        """测试 git_diff 显示变更内容。"""
        base = tmp_path / "repo"
        base.mkdir()
        _init_git_repo(base)

        tool = ResourceMergeTool(base_path=str(base))
        workspace = tmp_path / "workspace_diff"

        await tool.execute({"action": "prepare", "workspace": str(workspace)})

        # 修改文件
        (workspace / "test.txt").write_text("修改后的内容", encoding="utf-8")

        result = await tool.execute({
            "action": "git_diff",
            "workspace": str(workspace),
        })

        assert result.success
        assert "diff" in result.output

        # 清理
        await tool.execute({"action": "cleanup", "workspace": str(workspace)})

    async def test_git_log_shows_history(self, tmp_path: Path) -> None:
        """测试 git_log 显示提交历史。"""
        base = tmp_path / "repo"
        base.mkdir()
        _init_git_repo(base)

        tool = ResourceMergeTool(base_path=str(base))
        workspace = tmp_path / "workspace_log"

        await tool.execute({"action": "prepare", "workspace": str(workspace)})

        result = await tool.execute({
            "action": "git_log",
            "workspace": str(workspace),
        })

        assert result.success
        assert result.output["count"] >= 1
        # 初始提交应该包含 "init" 消息
        messages = [c["message"] for c in result.output["commits"]]
        assert any("init" in m for m in messages)

        # 清理
        await tool.execute({"action": "cleanup", "workspace": str(workspace)})

    async def test_merge_copies_files(self, tmp_path: Path) -> None:
        """测试 merge 将 worktree 中的文件复制到目标目录。"""
        base = tmp_path / "repo"
        base.mkdir()
        _init_git_repo(base)

        tool = ResourceMergeTool(base_path=str(base))
        workspace = tmp_path / "workspace_merge"

        # 准备 worktree
        await tool.execute({"action": "prepare", "workspace": str(workspace)})

        # 在 worktree 中创建新文件
        (workspace / "new_feature.py").write_text("print('hello')", encoding="utf-8")

        # 创建目标目录
        target = tmp_path / "target"
        target.mkdir()

        # 合并（通过 target_files 指定要合并的文件）
        result = await tool.execute({
            "action": "merge",
            "workspace": str(workspace),
            "target_dir": str(target),
            "target_files": ["new_feature.py"],
        })

        assert result.success
        assert "new_feature.py" in result.output["merged_files"]
        assert (target / "new_feature.py").exists()
        assert (target / "new_feature.py").read_text(encoding="utf-8") == "print('hello')"

        # 清理
        await tool.execute({"action": "cleanup", "workspace": str(workspace)})

    async def test_rollback_restores_state(self, tmp_path: Path) -> None:
        """测试 rollback 恢复 worktree 到初始状态。

        在 worktree 中修改文件（不提交），回滚后应恢复到 HEAD 状态。
        """
        base = tmp_path / "repo"
        base.mkdir()
        _init_git_repo(base)

        tool = ResourceMergeTool(base_path=str(base))
        workspace = tmp_path / "workspace_rollback"

        # 准备 worktree
        await tool.execute({"action": "prepare", "workspace": str(workspace)})

        # 修改已有文件（不提交）
        (workspace / "test.txt").write_text("被修改的内容", encoding="utf-8")
        # 添加新文件（不提交）
        (workspace / "extra.txt").write_text("额外文件", encoding="utf-8")

        # 执行回滚（git checkout -- . + git clean -fd）
        result = await tool.execute({
            "action": "rollback",
            "workspace": str(workspace),
        })

        assert result.success
        # 验证已跟踪文件已恢复到初始内容
        content = (workspace / "test.txt").read_text(encoding="utf-8")
        assert content == "hello"
        # 验证未跟踪文件已被清除
        assert not (workspace / "extra.txt").exists()

        # 清理
        await tool.execute({"action": "cleanup", "workspace": str(workspace)})

"""
MemoryTool、TaskSubmitTool、HumanInteractionTool 全面单元测试

覆盖范围：
- MemoryTool: store/retrieve/import_text/import_file/update/delete/get_context 操作的成功/失败场景
- TaskSubmitTool: 短期/长期任务的参数校验、成功提交、服务不可用等场景
- HumanInteractionTool: choice/conversation 模式的成功、超时、取消、拒绝等场景
- 工具定义验证、参数校验、错误处理
"""

from unittest.mock import AsyncMock, MagicMock, patch


from tools.builtin.memory import MemoryTool
from tools.builtin.task_submit import TaskSubmitTool
from tools.builtin.human_interaction import HumanInteractionTool
from human_interaction.service import (
    InteractionCancelledError,
    InteractionDeniedError,
    InteractionTimeoutError,
)


# =====================================================================
# MemoryTool 测试
# =====================================================================


def _make_memory_tool(has_service=True, has_importer=False):
    """创建 MemoryTool 实例并注入 mock 依赖。

    Args:
        has_service: 是否注入 mock memory_service
        has_importer: 是否注入 mock knowledge_importer

    Returns:
        MemoryTool 实例
    """
    tool = MemoryTool()
    if has_service:
        tool._memory_service = AsyncMock()
    if has_importer:
        tool._knowledge_importer = AsyncMock()
    return tool


class TestMemoryToolDefinition:
    """MemoryTool 工具定义测试"""

    def test_tool_definition_name_is_memory(self):
        """验证工具定义名称为 memory"""
        tool_def = MemoryTool.get_tool_definition()
        assert tool_def.name == "memory"

    def test_tool_definition_actions_include_all_operations(self):
        """验证工具定义包含所有 7 种操作"""
        tool_def = MemoryTool.get_tool_definition()
        action_enum = tool_def.input_schema["properties"]["action"]["enum"]
        expected = {"store", "retrieve", "import_text", "import_file", "update", "delete", "get_context"}
        assert set(action_enum) == expected

    def test_tool_definition_required_action(self):
        """验证工具定义要求 action 为必填参数"""
        tool_def = MemoryTool.get_tool_definition()
        assert "action" in tool_def.input_schema["required"]

    def test_tool_definition_injected_params(self):
        """验证工具定义声明了注入参数"""
        tool_def = MemoryTool.get_tool_definition()
        assert "user_id" not in tool_def.injected_params
        assert "_memory_service" in tool_def.injected_params
        assert "_session" in tool_def.injected_params


class TestMemoryToolStore:
    """MemoryTool store 操作测试"""

    async def test_store_semantic_success(self):
        """测试存储语义知识成功，返回 knowledge_id"""
        tool = _make_memory_tool()
        tool._memory_service.store_knowledge = AsyncMock(return_value="k-001")

        result = await tool.execute({
            "action": "store",
            "content": "Python 是一种解释型语言",
            "tags": ["python", "编程"],
            "_memory_service": tool._memory_service,
        })

        assert result.success is True
        assert result.output["success"] is True
        assert result.output["knowledge_id"] == "k-001"

    async def test_store_episode_success(self):
        """测试存储情景记忆成功，返回 episode_id"""
        tool = _make_memory_tool()
        tool._memory_service.store_episode = AsyncMock(return_value="e-001")

        result = await tool.execute({
            "action": "store",
            "content": "用户请求生成登录页面",
            "memory_type": "episode",
            "tags": ["登录", "页面"],
            "_memory_service": tool._memory_service,
        })

        assert result.success is True
        assert result.output["success"] is True
        assert result.output["episode_id"] == "e-001"

    async def test_store_missing_content(self):
        """测试缺少 content 参数时返回错误"""
        tool = _make_memory_tool()

        result = await tool.execute({
            "action": "store",
            "_memory_service": tool._memory_service,
        })

        assert result.success is False
        assert "content" in result.error

    async def test_store_service_exception(self):
        """测试存储时服务异常返回失败结果"""
        tool = _make_memory_tool()
        tool._memory_service.store_knowledge = AsyncMock(side_effect=RuntimeError("DB error"))

        result = await tool.execute({
            "action": "store",
            "content": "测试内容",
            "_memory_service": tool._memory_service,
        })

        assert result.success is False
        assert "存储失败" in result.error


class TestMemoryToolRetrieve:
    """MemoryTool retrieve 操作测试"""

    async def test_retrieve_retrieval_mode_success(self):
        """测试 retrieval 注入方式检索成功，返回结果列表"""
        tool = _make_memory_tool()
        mock_result = MagicMock()
        mock_result.id = "r-001"
        mock_result.content = "检索到的内容"
        mock_result.score = 0.95
        mock_result.metadata = {"source": "test"}
        tool._memory_service.retrieve = AsyncMock(return_value=[mock_result])

        result = await tool.execute({
            "action": "retrieve",
            "query": "Python 编程",
            "inject_type": "retrieval",
            "retrieval_method": "vector",
            "top_k": 5,
            "_memory_service": tool._memory_service,
        })

        assert result.success is True
        assert result.output["success"] is True
        assert result.output["inject_type"] == "retrieval"
        assert len(result.output["results"]) == 1
        assert result.output["results"][0]["id"] == "r-001"

    async def test_retrieve_summary_mode_success(self):
        """测试 summary 注入方式检索成功，返回摘要"""
        tool = _make_memory_tool()
        mock_result = MagicMock()
        mock_result.content = "这是一段很长的内容"
        tool._memory_service.retrieve = AsyncMock(return_value=[mock_result])

        result = await tool.execute({
            "action": "retrieve",
            "query": "测试查询",
            "inject_type": "summary",
            "_memory_service": tool._memory_service,
        })

        assert result.success is True
        assert result.output["success"] is True
        assert "summary" in result.output
        assert result.output["source_count"] == 1

    async def test_retrieve_full_mode_success(self):
        """测试 full 注入方式检索成功，返回完整结果"""
        tool = _make_memory_tool()
        mock_result = MagicMock()
        mock_result.id = "r-002"
        mock_result.content = "完整内容"
        mock_result.score = 0.88
        mock_result.metadata = {}
        tool._memory_service.retrieve = AsyncMock(return_value=[mock_result])

        result = await tool.execute({
            "action": "retrieve",
            "query": "测试",
            "inject_type": "full",
            "_memory_service": tool._memory_service,
        })

        assert result.success is True
        assert result.output["inject_type"] == "full"
        assert result.output["results"][0]["content"] == "完整内容"

    async def test_retrieve_retrieval_mode_missing_query(self):
        """测试 retrieval 模式缺少 query 时返回错误"""
        tool = _make_memory_tool()

        result = await tool.execute({
            "action": "retrieve",
            "inject_type": "retrieval",
            "_memory_service": tool._memory_service,
        })

        assert result.success is False
        assert "query" in result.error

    async def test_retrieve_empty_results(self):
        """测试检索无结果时返回空列表"""
        tool = _make_memory_tool()
        tool._memory_service.retrieve = AsyncMock(return_value=[])

        result = await tool.execute({
            "action": "retrieve",
            "query": "不存在的查询",
            "_memory_service": tool._memory_service,
        })

        assert result.success is True
        assert result.output["results"] == []


class TestMemoryToolImportText:
    """MemoryTool import_text 操作测试"""

    async def test_import_text_success(self):
        """测试导入文本知识成功"""
        tool = _make_memory_tool(has_importer=True)
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.knowledge_id = "ki-001"
        mock_result.file_path = "/data/ki-001.txt"
        tool._knowledge_importer.import_text = AsyncMock(return_value=mock_result)

        result = await tool.execute({
            "action": "import_text",
            "content": "这是一段需要导入的知识文本",
            "name": "测试知识",
            "_memory_service": tool._memory_service,
        })

        assert result.success is True
        assert result.output["success"] is True
        assert result.output["knowledge_id"] == "ki-001"

    async def test_import_text_missing_content(self):
        """测试缺少 content 参数时返回错误"""
        tool = _make_memory_tool(has_importer=True)

        result = await tool.execute({
            "action": "import_text",
            "name": "测试",
            "_memory_service": tool._memory_service,
        })

        assert result.success is False
        assert "content" in result.error

    async def test_import_text_missing_name(self):
        """测试缺少 name 参数时返回错误"""
        tool = _make_memory_tool(has_importer=True)

        result = await tool.execute({
            "action": "import_text",
            "content": "内容",
            "_memory_service": tool._memory_service,
        })

        assert result.success is False
        assert "name" in result.error

    async def test_import_text_no_importer(self):
        """测试无知识导入器时返回错误"""
        tool = _make_memory_tool(has_importer=False)

        result = await tool.execute({
            "action": "import_text",
            "content": "内容",
            "name": "名称",
            "_memory_service": tool._memory_service,
        })

        assert result.success is False
        assert "导入器" in result.error


class TestMemoryToolImportFile:
    """MemoryTool import_file 操作测试"""

    async def test_import_file_missing_file_path(self):
        """测试缺少 file_path 参数时返回错误"""
        tool = _make_memory_tool(has_importer=True)

        result = await tool.execute({
            "action": "import_file",
            "_memory_service": tool._memory_service,
        })

        assert result.success is False
        assert "file_path" in result.error


class TestMemoryToolUpdateDelete:
    """MemoryTool update/delete 操作测试"""

    async def test_update_missing_file_path(self):
        """测试 update 操作缺少 file_path 时返回错误"""
        tool = _make_memory_tool(has_importer=True)

        result = await tool.execute({
            "action": "update",
            "content": "新内容",
            "_memory_service": tool._memory_service,
        })

        assert result.success is False
        assert "file_path" in result.error

    async def test_delete_missing_file_path(self):
        """测试 delete 操作缺少 file_path 时返回错误"""
        tool = _make_memory_tool(has_importer=True)

        result = await tool.execute({
            "action": "delete",
            "_memory_service": tool._memory_service,
        })

        assert result.success is False
        assert "file_path" in result.error


class TestMemoryToolGetContext:
    """MemoryTool get_context 操作测试"""

    async def test_get_context_success(self):
        """测试获取记忆统计信息成功"""
        tool = _make_memory_tool()
        tool._memory_service.get_stats = AsyncMock(return_value={"total": 100, "episodes": 30})

        result = await tool.execute({
            "action": "get_context",
            "_memory_service": tool._memory_service,
        })

        assert result.success is True
        assert result.output["success"] is True
        assert result.output["stats"]["total"] == 100


class TestMemoryToolErrorHandling:
    """MemoryTool 错误处理测试"""

    async def test_no_memory_service_returns_error(self):
        """测试无 memory_service 时返回 MEMORY_SERVICE_NOT_AVAILABLE 错误"""
        tool = MemoryTool()

        with patch.object(tool, "_get_memory_service", return_value=None):
            result = await tool.execute({"action": "store", "content": "test"})

        assert result.success is False
        assert result.error_code == "MEMORY_SERVICE_NOT_AVAILABLE"

    async def test_system_user_id_used_in_store(self):
        """测试 store 操作使用 SYSTEM_USER_ID"""
        tool = _make_memory_tool()
        tool._memory_service.store_knowledge = AsyncMock(return_value="k-sys")

        result = await tool.execute({
            "action": "store",
            "content": "系统级知识",
            "_memory_service": tool._memory_service,
        })

        assert result.success is True
        call_args = tool._memory_service.store_knowledge.call_args[0][0]
        assert call_args.user_id == "system"

    async def test_unknown_action_returns_error(self):
        """测试未知操作时返回错误"""
        tool = _make_memory_tool()

        result = await tool.execute({
            "action": "unknown_action",
            "_memory_service": tool._memory_service,
        })

        assert result.success is False
        assert "未知操作" in result.error

    async def test_system_user_id_used_in_retrieve(self):
        """测试 retrieve 操作使用 SYSTEM_USER_ID 而非 uuid.UUID 转换"""
        tool = _make_memory_tool()
        tool._memory_service.retrieve = AsyncMock(return_value=[])

        result = await tool.execute({
            "action": "retrieve",
            "query": "测试",
            "inject_type": "full",
            "_memory_service": tool._memory_service,
        })

        assert result.success is True
        call_kwargs = tool._memory_service.retrieve.call_args[1]
        assert call_kwargs["user_id"] == "system"


# =====================================================================
# TaskSubmitTool 测试
# =====================================================================


def _make_task_submit_tool(has_task_service=True, has_event_bus=True):
    """创建 TaskSubmitTool 实例并注入 mock 依赖。

    Args:
        has_task_service: 是否注入 mock TaskService
        has_event_bus: 是否注入 mock EventBus

    Returns:
        TaskSubmitTool 实例
    """
    tool = TaskSubmitTool()

    if has_task_service:
        mock_task = MagicMock()
        mock_task.id = "task-001"
        mock_task.title = "测试任务"
        mock_task.status = MagicMock(value="pending")

        mock_storage = MagicMock()
        mock_task_service = MagicMock()
        mock_task_service.create_task = MagicMock(return_value=mock_task)
        mock_task_service._storage = mock_storage
        tool._task_service = mock_task_service

    if has_event_bus:
        mock_event_bus = AsyncMock()
        mock_event_bus.emit = AsyncMock()
        mock_event_bus.has_subscribers = MagicMock(return_value=True)
        tool._event_bus = mock_event_bus

    return tool


def _short_term_inputs():
    """构造短期任务的基础输入参数。"""
    return {
        "goal": {"title": "实现用户登录"},
        "target_type": "agent",
        "target_id": "general_agent",
        "acceptance_criteria": {
            "file_check": {"input_params": {"path": "src/auth/login.py"}},
        },
        "description": "实现一个完整的用户登录功能",
        "parent_agent_level": 1,
    }


class TestTaskSubmitToolDefinition:
    """TaskSubmitTool 工具定义测试"""

    def test_tool_definition_name_is_task_submit(self):
        """验证工具定义名称为 task_submit"""
        tool_def = TaskSubmitTool.get_tool_definition()
        assert tool_def.name == "task_submit"

    def test_tool_definition_required_goal(self):
        """验证工具定义要求 goal 为必填参数"""
        tool_def = TaskSubmitTool.get_tool_definition()
        assert "goal" in tool_def.input_schema["required"]

    def test_tool_definition_has_priority_and_scope(self):
        """验证工具定义包含 priority 和 task_scope 参数"""
        tool_def = TaskSubmitTool.get_tool_definition()
        props = tool_def.input_schema["properties"]
        assert "priority" in props
        assert "task_scope" in props


class TestTaskSubmitToolValidation:
    """TaskSubmitTool 参数校验测试"""

    async def test_missing_goal_returns_missing_goal(self):
        """测试缺少 goal 参数时返回 MISSING_GOAL 错误"""
        tool = _make_task_submit_tool()

        result = await tool.execute({})

        assert result.success is False
        assert result.error_code == "MISSING_GOAL"

    async def test_goal_without_title_returns_missing_goal(self):
        """测试 goal 中缺少 title 时返回 MISSING_GOAL 错误"""
        tool = _make_task_submit_tool()

        result = await tool.execute({"goal": {"description": "无标题"}})

        assert result.success is False
        assert result.error_code == "MISSING_GOAL"

    async def test_short_term_missing_target_type(self):
        """测试短期任务缺少 target_type 时返回 MISSING_TARGET_TYPE 错误"""
        tool = _make_task_submit_tool()
        inputs = _short_term_inputs()
        del inputs["target_type"]

        result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "MISSING_TARGET_TYPE"

    async def test_short_term_missing_target_id(self):
        """测试短期任务缺少 target_id 时返回 MISSING_TARGET_ID 错误"""
        tool = _make_task_submit_tool()
        inputs = _short_term_inputs()
        del inputs["target_id"]

        result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "MISSING_TARGET_ID"

    async def test_short_term_missing_acceptance_criteria_not_blocked(self):
        """测试短期任务缺少 acceptance_criteria 时不再被 MISSING_METRICS 校验拦截"""
        tool = _make_task_submit_tool()
        inputs = _short_term_inputs()
        del inputs["acceptance_criteria"]

        result = await tool.execute(inputs)

        assert result.error_code != "MISSING_METRICS"


class TestTaskSubmitToolShortTerm:
    """TaskSubmitTool 短期任务测试"""

    async def test_short_term_submit_success(self):
        """测试短期任务成功提交，返回 task_id 和 submitted 状态"""
        tool = _make_task_submit_tool()
        inputs = _short_term_inputs()

        result = await tool.execute(inputs)

        assert result.success is True
        assert result.output["task_id"] == "task-001"
        assert result.output["submit_status"] == "submitted"
        assert result.output["target_type"] == "agent"
        assert result.output["target_id"] == "general_agent"
        # 验证事件发布被调用
        tool._event_bus.emit.assert_awaited_once()

    async def test_short_term_event_bus_unavailable_rollback(self):
        """测试 EventBus 不可用时回滚任务并返回错误"""
        tool = _make_task_submit_tool()
        inputs = _short_term_inputs()

        # mock _get_event_bus 返回 None，模拟 EventBus 不可用
        with patch.object(tool, "_get_event_bus", return_value=None):
            result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "EVENT_BUS_UNAVAILABLE"
        # 验证回滚（删除任务）被调用
        tool._task_service._storage.delete.assert_called_once_with("task-001")

    async def test_short_term_no_subscriber_rollback(self):
        """测试无订阅者时回滚任务并返回错误"""
        tool = _make_task_submit_tool()
        tool._event_bus.has_subscribers = MagicMock(return_value=False)
        inputs = _short_term_inputs()

        result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "NO_SUBSCRIBER"

    async def test_short_term_emit_error_rollback(self):
        """测试事件发布异常时回滚任务并返回错误"""
        tool = _make_task_submit_tool()
        tool._event_bus.emit = AsyncMock(side_effect=RuntimeError("emit failed"))
        inputs = _short_term_inputs()

        result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "EVENT_BUS_ERROR"

    async def test_short_term_task_create_failed(self):
        """测试 TaskService 创建任务失败时返回错误"""
        tool = _make_task_submit_tool()
        tool._task_service.create_task = MagicMock(side_effect=RuntimeError("DB error"))
        inputs = _short_term_inputs()

        result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "TASK_CREATE_FAILED"


class TestTaskSubmitToolLongTerm:
    """TaskSubmitTool 长期任务测试"""

    async def test_long_term_submit_success(self):
        """测试长期任务由 L1 Agent 成功提交"""
        tool = _make_task_submit_tool()
        inputs = {
            "goal": {"title": "大型系统重构"},
            "task_scope": "long_term",
            "parent_agent_level": 1,
        }

        result = await tool.execute(inputs)

        assert result.success is True
        assert result.output["task_id"] == "task-001"
        assert result.output["task_scope"] == "long_term"
        assert result.output["submit_status"] == "submitted"

    async def test_long_term_l2_agent_rejected(self):
        """测试 L2 Agent 提交长期任务被拒绝"""
        tool = _make_task_submit_tool()
        inputs = {
            "goal": {"title": "大型系统重构"},
            "task_scope": "long_term",
            "parent_agent_level": 2,
        }

        result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "L2_CANNOT_SUBMIT_LONG_TERM"


class TestTaskSubmitToolServiceUnavailable:
    """TaskSubmitTool 服务不可用测试"""

    async def test_task_service_unavailable(self):
        """测试 TaskService 不可用时返回 SERVICE_UNAVAILABLE 错误"""
        tool = _make_task_submit_tool(has_task_service=False, has_event_bus=False)
        inputs = _short_term_inputs()

        # _get_task_service 返回 None
        with patch.object(tool, "_get_task_service", return_value=None):
            result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "SERVICE_UNAVAILABLE"

    async def test_long_term_task_service_unavailable(self):
        """测试长期任务 TaskService 不可用时返回 SERVICE_UNAVAILABLE 错误"""
        tool = _make_task_submit_tool(has_task_service=False, has_event_bus=False)
        inputs = {
            "goal": {"title": "大型任务"},
            "task_scope": "long_term",
            "parent_agent_level": 1,
        }

        with patch.object(tool, "_get_task_service", return_value=None):
            result = await tool.execute(inputs)

        assert result.success is False
        assert result.error_code == "SERVICE_UNAVAILABLE"


class TestTaskSubmitToolGoalParsing:
    """TaskSubmitTool goal 参数解析测试"""

    async def test_goal_as_json_string(self):
        """测试 goal 为 JSON 字符串时正确解析"""
        tool = _make_task_submit_tool()
        inputs = _short_term_inputs()
        import json
        inputs["goal"] = json.dumps({"title": "JSON字符串任务"})

        # 动态设置 mock task 的 title 为传入的 goal title
        def _create_task(**kwargs):
            mock_task = MagicMock()
            mock_task.id = "task-001"
            mock_task.title = kwargs.get("title", "测试任务")
            mock_task.status = MagicMock(value="pending")
            return mock_task
        tool._task_service.create_task = MagicMock(side_effect=_create_task)

        result = await tool.execute(inputs)

        assert result.success is True
        assert result.output["title"] == "JSON字符串任务"

    async def test_goal_as_invalid_string(self):
        """测试 goal 为无效字符串时返回 MISSING_GOAL 错误"""
        tool = _make_task_submit_tool()

        result = await tool.execute({"goal": "not a json"})

        assert result.success is False
        assert result.error_code == "MISSING_GOAL"


# =====================================================================
# HumanInteractionTool 测试
# =====================================================================


def _make_human_interaction_tool(pipeline_id="pipeline-001"):
    """创建 HumanInteractionTool 实例。

    Args:
        pipeline_id: 管道 ID

    Returns:
        HumanInteractionTool 实例
    """
    return HumanInteractionTool(pipeline_id=pipeline_id)


def _mock_interaction_service():
    """创建 mock 的人类交互服务。

    Returns:
        AsyncMock 实例
    """
    service = AsyncMock()
    service.create_choice_request = AsyncMock(return_value="req-001")
    service.wait_for_choice = AsyncMock(return_value={
        "response_type": "option_selected",
        "selected_option": {"id": "opt-1", "label": "确认"},
    })
    service.create_conversation_request = AsyncMock(return_value="req-002")
    service.send_notification = AsyncMock(return_value="req-003")
    service.cancel_request = AsyncMock()
    return service


class TestHumanInteractionToolDefinition:
    """HumanInteractionTool 工具定义测试"""

    def test_tool_definition_name_is_human_interaction(self):
        """验证工具定义名称为 human_interaction"""
        tool_def = HumanInteractionTool.get_tool_definition()
        assert tool_def.name == "human_interaction"

    def test_tool_definition_required_mode_and_title(self):
        """验证工具定义要求 mode 和 title 为必填参数"""
        tool_def = HumanInteractionTool.get_tool_definition()
        required = tool_def.input_schema["required"]
        assert "mode" in required
        assert "title" in required

    def test_tool_definition_modes_include_choice_and_conversation(self):
        """验证工具定义包含 choice 和 conversation 两种模式"""
        tool_def = HumanInteractionTool.get_tool_definition()
        mode_enum = tool_def.input_schema["properties"]["mode"]["enum"]
        assert "choice" in mode_enum
        assert "conversation" in mode_enum


class TestHumanInteractionToolValidation:
    """HumanInteractionTool 参数校验测试"""

    async def test_missing_mode_returns_error(self):
        """测试缺少 mode 参数时返回错误（因 title 也缺失或 mode 校验失败）"""
        tool = _make_human_interaction_tool()
        service = _mock_interaction_service()

        with patch("tools.builtin.human_interaction.tool.get_human_interaction_service", return_value=service):
            result = await tool.execute({"title": "测试"})

        assert result.success is False
        assert "不支持" in result.error or "mode" in result.error.lower()

    async def test_missing_title_no_effect_but_executes(self):
        """测试有 mode 但无 title 时仍可执行（title 有默认空字符串）"""
        tool = _make_human_interaction_tool()
        service = _mock_interaction_service()

        with patch("tools.builtin.human_interaction.tool.get_human_interaction_service", return_value=service):
            result = await tool.execute({"mode": "choice"})

        # title 默认为空字符串，不会阻止执行
        assert result.success is True

    async def test_missing_context_info_returns_error(self):
        """测试缺少 pipeline_id 时返回缺少上下文错误"""
        tool = HumanInteractionTool()

        result = await tool.execute({
            "mode": "choice",
            "title": "确认操作",
        })

        assert result.success is False
        assert "pipeline_id" in result.error

    async def test_unsupported_mode_returns_error(self):
        """测试不支持的 mode 时返回错误"""
        tool = _make_human_interaction_tool()
        service = _mock_interaction_service()

        with patch("tools.builtin.human_interaction.tool.get_human_interaction_service", return_value=service):
            result = await tool.execute({"mode": "unsupported_mode", "title": "测试"})

        assert result.success is False
        assert "不支持" in result.error


class TestHumanInteractionToolChoiceMode:
    """HumanInteractionTool choice 模式测试"""

    async def test_choice_mode_success(self):
        """测试 choice 模式成功返回用户选择"""
        tool = _make_human_interaction_tool()
        service = _mock_interaction_service()

        with patch("tools.builtin.human_interaction.tool.get_human_interaction_service", return_value=service):
            result = await tool.execute({
                "mode": "choice",
                "title": "确认操作",
                "options": [
                    {"id": "opt-1", "label": "确认"},
                    {"id": "opt-2", "label": "取消"},
                ],
            })

        assert result.success is True
        assert result.output["status"] == "completed"
        assert result.output["selected_option"]["id"] == "opt-1"

    async def test_choice_mode_timeout(self):
        """测试 choice 模式超时返回 INTERACTION_TIMEOUT 错误"""
        tool = _make_human_interaction_tool()
        service = _mock_interaction_service()
        service.wait_for_choice = AsyncMock(
            side_effect=InteractionTimeoutError(request_id="req-timeout", timeout=300)
        )

        with patch("tools.builtin.human_interaction.tool.get_human_interaction_service", return_value=service):
            result = await tool.execute({
                "mode": "choice",
                "title": "超时测试",
                "timeout_seconds": 300,
            })

        assert result.success is False
        assert result.error_code == "INTERACTION_TIMEOUT"
        assert "超时" in result.error

    async def test_choice_mode_cancelled(self):
        """测试 choice 模式取消返回 INTERACTION_CANCELLED 错误"""
        tool = _make_human_interaction_tool()
        service = _mock_interaction_service()
        service.wait_for_choice = AsyncMock(
            side_effect=InteractionCancelledError(request_id="req-cancel", reason="用户取消")
        )

        with patch("tools.builtin.human_interaction.tool.get_human_interaction_service", return_value=service):
            result = await tool.execute({
                "mode": "choice",
                "title": "取消测试",
            })

        assert result.success is False
        assert result.error_code == "INTERACTION_CANCELLED"
        assert "取消" in result.error

    async def test_choice_mode_denied(self):
        """测试 choice 模式拒绝返回 denied 状态（成功结果）"""
        tool = _make_human_interaction_tool()
        service = _mock_interaction_service()
        service.wait_for_choice = AsyncMock(
            side_effect=InteractionDeniedError(request_id="req-deny", reason="用户拒绝")
        )

        with patch("tools.builtin.human_interaction.tool.get_human_interaction_service", return_value=service):
            result = await tool.execute({
                "mode": "choice",
                "title": "拒绝测试",
            })

        # 注意：InteractionDeniedError 返回的是 success=True，status=denied
        assert result.success is True
        assert result.output["status"] == "denied"
        assert "拒绝" in result.output["reason"]


class TestHumanInteractionToolConversationMode:
    """HumanInteractionTool conversation 模式测试"""

    async def test_conversation_mode_success(self):
        """测试 conversation 模式成功返回到达信息"""
        tool = _make_human_interaction_tool()
        service = _mock_interaction_service()
        service.wait_for_choice = AsyncMock(return_value={
            "response_type": "approved",
            "feedback": "",
        })

        with patch("tools.builtin.human_interaction.tool.get_human_interaction_service", return_value=service):
            result = await tool.execute({
                "mode": "conversation",
                "title": "技术讨论",
                "initial_message": "让我们讨论一下架构方案",
                "suggestions": ["方案A", "方案B"],
            })

        assert result.success is True
        assert result.output["status"] == "user_arrived"
        # 验证 service 方法被正确调用
        service.create_conversation_request.assert_awaited_once()
        service.wait_for_choice.assert_awaited_once()

    async def test_conversation_mode_exception(self):
        """测试 conversation 模式异常返回失败结果"""
        tool = _make_human_interaction_tool()
        service = _mock_interaction_service()
        service.wait_for_choice = AsyncMock(
            side_effect=RuntimeError("连接失败")
        )

        with patch("tools.builtin.human_interaction.tool.get_human_interaction_service", return_value=service):
            result = await tool.execute({
                "mode": "conversation",
                "title": "异常测试",
            })

        assert result.success is False
        assert "人类交互执行失败" in result.error


class TestHumanInteractionToolContextFallback:
    """HumanInteractionTool 上下文回退测试"""

    async def test_context_from_inputs_fallback(self):
        """测试构造函数无 pipeline_id 时从 inputs 参数获取"""
        tool = HumanInteractionTool()
        service = _mock_interaction_service()

        with patch("tools.builtin.human_interaction.tool.get_human_interaction_service", return_value=service):
            result = await tool.execute({
                "mode": "choice",
                "title": "从 inputs 获取上下文",
                "pipeline_id": "pipeline-from-inputs",
            })

        assert result.success is True
        assert result.output["status"] == "completed"

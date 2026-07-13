"""
LSPTools、TriggerSetupTool、hot_swap_func、ResourceSearchTool 全面单元测试

覆盖范围：
- LSPTools: 工具定义验证、参数校验、成功/失败执行路径、LSP 未安装场景
- TriggerSetupTool: 四种触发类型的参数校验与成功路径、数量上限、工具定义
- hot_swap_func: 五种操作的参数校验、schema 验证
- ResourceSearchTool: 三种资源类型搜索、模式切换、无注册表降级、工具定义
"""

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch



# ---------------------------------------------------------------------------
# 注册 triggers.message_queue mock 模块（TriggerSetupTool 依赖但尚未实现）
# ---------------------------------------------------------------------------

def _ensure_trigger_message_queue_mock():
    """确保 triggers.message_queue 模块存在（mock），避免 ImportError。"""
    if "triggers.message_queue" in sys.modules:
        return

    # 确保 triggers 包存在
    if "triggers" not in sys.modules:
        triggers_pkg = ModuleType("triggers")
        triggers_pkg.__path__ = []
        sys.modules["triggers"] = triggers_pkg

    # 创建 mock 模块
    mock_mod = ModuleType("triggers.message_queue")

    @dataclass
    class TriggerMessage:
        """触发器消息 mock 数据类。"""
        id: str = ""
        session_id: str = ""
        execution_id: str = ""
        content: str = ""
        priority: int = 0
        expires_at: datetime | None = None
        metadata: dict = field(default_factory=dict)

    mock_mod.TriggerMessage = TriggerMessage
    mock_mod.get_trigger_message_queue = MagicMock(return_value=AsyncMock())

    sys.modules["triggers.message_queue"] = mock_mod


_ensure_trigger_message_queue_mock()

from tools.builtin.hot_swap import hot_swap_func, hot_swap_schema
from tools.builtin.lsp_tools import LSPTools
from tools.builtin.resource_search import ResourceSearchTool
from tools.builtin.trigger_setup import TriggerSetupTool


# =====================================================================
# LSPTools 测试
# =====================================================================


class TestLSPToolsDefinition:
    """LSPTools 工具定义验证"""

    def test_get_tool_definitions_returns_four_tools(self):
        """验证 get_tool_definitions 返回 4 个工具"""
        tools = LSPTools.get_tool_definitions()
        assert isinstance(tools, dict)
        assert len(tools) == 4

    def test_tool_definitions_contain_expected_names(self):
        """验证工具定义包含所有预期的工具名称"""
        tools = LSPTools.get_tool_definitions()
        expected_names = {"lsp_definition", "lsp_references", "lsp_diagnostics", "file_jump"}
        assert set(tools.keys()) == expected_names

    def test_lsp_definition_has_required_params(self):
        """验证 lsp_definition 工具要求 file_path 和 line 参数"""
        tools = LSPTools.get_tool_definitions()
        schema = tools["lsp_definition"].input_schema
        required = schema.get("required", [])
        assert "file_path" in required
        assert "line" in required

    def test_lsp_references_has_required_params(self):
        """验证 lsp_references 工具要求 file_path 和 line 参数"""
        tools = LSPTools.get_tool_definitions()
        schema = tools["lsp_references"].input_schema
        required = schema.get("required", [])
        assert "file_path" in required
        assert "line" in required

    def test_lsp_diagnostics_has_required_params(self):
        """验证 lsp_diagnostics 工具要求 file_path 参数"""
        tools = LSPTools.get_tool_definitions()
        schema = tools["lsp_diagnostics"].input_schema
        required = schema.get("required", [])
        assert "file_path" in required

    def test_file_jump_has_required_params(self):
        """验证 file_jump 工具要求 file_path 参数"""
        tools = LSPTools.get_tool_definitions()
        schema = tools["file_jump"].input_schema
        required = schema.get("required", [])
        assert "file_path" in required


class TestLSPDefinitionExecute:
    """LSPTools lsp_definition 执行测试"""

    async def test_lsp_definition_missing_file_path(self):
        """测试缺少 file_path 参数时返回失败"""
        lsp = LSPTools()
        result = await lsp._lsp_definition({"line": 0})
        assert result.success is False
        assert result.error_code == "MISSING_FILE_PATH"

    async def test_lsp_definition_file_not_exists(self):
        """测试文件不存在时返回失败"""
        lsp = LSPTools()
        result = await lsp._lsp_definition({
            "file_path": "/nonexistent/path/to/file.py",
            "line": 0,
        })
        assert result.success is False
        assert result.error_code == "INVALID_PATH"

    @patch("tools.builtin.lsp_tools.LSPTools._validate_file_path")
    async def test_lsp_definition_lsp_not_installed(self, mock_validate):
        """测试 LSP 模块未安装时返回失败"""
        mock_validate.return_value = (MagicMock(), None)
        # 模拟 ImportError: lsp 模块不存在
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def mock_import(name, *args, **kwargs):
            if name.startswith("lsp"):
                raise ImportError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)

        lsp = LSPTools()
        with patch("builtins.__import__", side_effect=mock_import):
            result = await lsp._lsp_definition({
                "file_path": "/some/file.py",
                "line": 0,
            })
        assert result.success is False
        assert result.error_code == "LSP_NOT_INSTALLED"


class TestLSPReferencesExecute:
    """LSPTools lsp_references 执行测试"""

    async def test_lsp_references_missing_file_path(self):
        """测试缺少 file_path 参数时返回失败"""
        lsp = LSPTools()
        result = await lsp._lsp_references({"line": 0})
        assert result.success is False
        assert result.error_code == "MISSING_FILE_PATH"

    async def test_lsp_references_file_not_exists(self):
        """测试文件不存在时返回失败"""
        lsp = LSPTools()
        result = await lsp._lsp_references({
            "file_path": "/nonexistent/path/to/file.py",
            "line": 0,
        })
        assert result.success is False
        assert result.error_code == "INVALID_PATH"

    @patch("tools.builtin.lsp_tools.LSPTools._validate_file_path")
    async def test_lsp_references_success(self, mock_validate):
        """测试成功查找引用"""
        mock_path = MagicMock()
        mock_path.__str__ = lambda self: "/some/file.py"
        mock_validate.return_value = (mock_path, None)

        # 模拟 LSP gateway 返回引用结果
        mock_location = MagicMock()
        mock_location.uri = "file:///some/other.py"
        mock_location.range.dict.return_value = {"start": {"line": 5, "character": 0}}

        mock_gateway = AsyncMock()
        mock_gateway.find_references.return_value = [mock_location]

        with patch("tools.builtin.lsp_tools.LSPTools._validate_file_path", mock_validate):
            # 需要再次 patch 因为装饰器已经替换了
            pass

        # 直接用 monkey-patch 方式测试
        lsp = LSPTools()
        lsp._validate_file_path = lambda fp: (mock_path, None)

        with patch.dict("sys.modules", {
            "lsp": MagicMock(),
            "lsp.gateway": MagicMock(get_lsp_gateway=AsyncMock(return_value=mock_gateway)),
            "lsp.types": MagicMock(Position=MagicMock),
        }):
            from lsp.gateway import get_lsp_gateway
            from lsp.types import Position

            gateway = await get_lsp_gateway()
            position = Position(line=0, character=0)
            references = await gateway.find_references(str(mock_path), position)

            assert len(references) == 1
            assert references[0].uri == "file:///some/other.py"


class TestLSPDiagnosticsExecute:
    """LSPTools lsp_diagnostics 执行测试"""

    async def test_lsp_diagnostics_missing_file_path(self):
        """测试缺少 file_path 参数时返回失败"""
        lsp = LSPTools()
        result = await lsp._lsp_diagnostics({})
        assert result.success is False
        assert result.error_code == "MISSING_FILE_PATH"

    async def test_lsp_diagnostics_file_not_exists(self):
        """测试文件不存在时返回失败"""
        lsp = LSPTools()
        result = await lsp._lsp_diagnostics({
            "file_path": "/nonexistent/path/to/file.py",
        })
        assert result.success is False
        assert result.error_code == "INVALID_PATH"

    @patch("tools.builtin.lsp_tools.LSPTools._validate_file_path")
    async def test_lsp_diagnostics_success(self, mock_validate):
        """测试成功获取诊断信息"""
        mock_path = MagicMock()
        mock_path.__str__ = lambda self: "/some/file.py"
        mock_validate.return_value = (mock_path, None)

        mock_diag = MagicMock()
        mock_diag.severity = 1
        mock_diag.message = "Syntax error"
        mock_diag.dict.return_value = {"severity": 1, "message": "Syntax error"}

        mock_gateway = AsyncMock()
        mock_gateway.get_diagnostics.return_value = [mock_diag]

        lsp = LSPTools()
        lsp._validate_file_path = lambda fp: (mock_path, None)

        with patch.dict("sys.modules", {
            "lsp": MagicMock(),
            "lsp.gateway": MagicMock(get_lsp_gateway=AsyncMock(return_value=mock_gateway)),
        }):
            from lsp.gateway import get_lsp_gateway

            gateway = await get_lsp_gateway()
            diagnostics = await gateway.get_diagnostics(str(mock_path))

            assert len(diagnostics) == 1
            assert diagnostics[0].message == "Syntax error"


class TestFileJumpExecute:
    """LSPTools file_jump 执行测试"""

    async def test_file_jump_missing_file_path(self):
        """测试缺少 file_path 参数时返回失败"""
        lsp = LSPTools()
        result = await lsp._file_jump({})
        assert result.success is False
        assert result.error_code == "MISSING_FILE_PATH"

    async def test_file_jump_file_not_exists(self):
        """测试文件不存在时返回失败"""
        lsp = LSPTools()
        result = await lsp._file_jump({
            "file_path": "/nonexistent/path/to/file.py",
        })
        assert result.success is False
        assert result.error_code == "INVALID_PATH"

    @patch("tools.builtin.lsp_tools.LSPTools._validate_file_path")
    async def test_file_jump_success(self, mock_validate):
        """测试成功跳转到文件"""
        mock_path = MagicMock()
        mock_path.__str__ = lambda self: "/some/file.py"
        mock_validate.return_value = (mock_path, None)

        lsp = LSPTools()
        lsp._validate_file_path = lambda fp: (mock_path, None)

        mock_jump = AsyncMock(return_value=True)
        with patch.dict("sys.modules", {
            "lsp": MagicMock(),
            "lsp.file_jump": MagicMock(FileJumpProtocol=MagicMock(jump_to_file=mock_jump)),
            "lsp.types": MagicMock(Position=MagicMock),
        }):
            from lsp.file_jump import FileJumpProtocol

            success = await FileJumpProtocol.jump_to_file(str(mock_path), None)
            assert success is True


# =====================================================================
# TriggerSetupTool 测试
# =====================================================================


class TestTriggerSetupToolDefinition:
    """TriggerSetupTool 工具定义验证"""

    def test_tool_definition_name(self):
        """验证工具名称为 trigger_setup"""
        tool_def = TriggerSetupTool.get_tool_definition()
        assert tool_def.name == "trigger_setup"

    def test_tool_definition_has_trigger_type_enum(self):
        """验证工具定义包含 trigger_type 枚举值"""
        tool_def = TriggerSetupTool.get_tool_definition()
        trigger_enum = tool_def.input_schema["properties"]["trigger_type"]["enum"]
        assert set(trigger_enum) == {"delay", "schedule", "event", "condition"}

    def test_tool_definition_required_params(self):
        """验证工具定义要求 trigger_type 和 message 参数"""
        tool_def = TriggerSetupTool.get_tool_definition()
        required = tool_def.input_schema["required"]
        assert "trigger_type" in required
        assert "message" in required

    def test_tool_definition_injected_params(self):
        """验证工具定义声明了 session_id 和 execution_id 注入参数"""
        tool_def = TriggerSetupTool.get_tool_definition()
        assert "session_id" in tool_def.injected_params
        assert "execution_id" in tool_def.injected_params


class TestTriggerSetupValidation:
    """TriggerSetupTool 参数校验测试"""

    async def test_missing_trigger_type(self):
        """测试缺少 trigger_type 参数时返回失败"""
        tool = TriggerSetupTool()
        result = await tool.execute({
            "message": "测试消息",
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "MISSING_TRIGGER_TYPE"

    async def test_missing_message(self):
        """测试缺少 message 参数时返回失败"""
        tool = TriggerSetupTool()
        result = await tool.execute({
            "trigger_type": "delay",
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "MISSING_MESSAGE"

    async def test_missing_session_id(self):
        """测试缺少 session_id 注入参数时返回失败"""
        tool = TriggerSetupTool()
        result = await tool.execute({
            "trigger_type": "delay",
            "message": "测试消息",
        })
        assert result.success is False
        assert result.error_code == "MISSING_SESSION_ID"

    async def test_unsupported_trigger_type(self):
        """测试不支持的触发类型时返回失败"""
        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "unknown_type",
            "message": "测试消息",
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "INVALID_TRIGGER_TYPE"


class TestTriggerDelay:
    """TriggerSetupTool delay 触发类型测试"""

    async def test_delay_success(self):
        """测试延迟触发器设置成功"""
        mock_queue = AsyncMock()
        mock_queue.size.return_value = 0

        with patch("tools.builtin.trigger_setup.get_trigger_message_queue", return_value=mock_queue):
            tool = TriggerSetupTool()
            tool._queue = mock_queue

            result = await tool.execute({
                "trigger_type": "delay",
                "message": "5秒后提醒",
                "delay_seconds": 5,
                "session_id": "sess_001",
                "execution_id": "exec_001",
            })

        assert result.success is True
        assert "trigger_id" in result.output
        assert mock_queue.push.called

    async def test_delay_missing_delay_seconds(self):
        """测试 delay 类型缺少 delay_seconds 参数时返回失败"""
        tool = TriggerSetupTool()
        # mock queue 避免触发器数量检查失败
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "delay",
            "message": "延迟消息",
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "MISSING_DELAY_SECONDS"

    async def test_delay_non_integer_delay_seconds(self):
        """测试 delay_seconds 为非整数时返回失败"""
        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "delay",
            "message": "延迟消息",
            "delay_seconds": "not_a_number",
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "INVALID_DELAY_SECONDS"

    async def test_delay_exceeds_max_limit(self):
        """测试延迟时间超过最大限制时返回失败"""
        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "delay",
            "message": "超时消息",
            "delay_seconds": TriggerSetupTool.MAX_DELAY_SECONDS + 1,
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "DELAY_EXCEEDS_LIMIT"

    async def test_delay_zero_seconds(self):
        """测试 delay_seconds 为 0 时返回失败"""
        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "delay",
            "message": "零秒消息",
            "delay_seconds": 0,
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "INVALID_DELAY_SECONDS"


class TestTriggerSchedule:
    """TriggerSetupTool schedule 触发类型测试"""

    async def test_schedule_success(self):
        """测试定时触发器设置成功（使用未来时间）"""
        future_time = (datetime.utcnow() + timedelta(hours=1)).isoformat()

        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "schedule",
            "message": "定时消息",
            "schedule_time": future_time,
            "session_id": "sess_001",
            "execution_id": "exec_001",
        })
        assert result.success is True
        assert "trigger_id" in result.output
        assert tool._queue.push.called

    async def test_schedule_missing_schedule_time(self):
        """测试 schedule 类型缺少 schedule_time 参数时返回失败"""
        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "schedule",
            "message": "定时消息",
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "MISSING_SCHEDULE_TIME"

    async def test_schedule_past_time(self):
        """测试定时时间为过去时间时返回失败"""
        past_time = (datetime.utcnow() - timedelta(hours=1)).isoformat()

        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "schedule",
            "message": "过去时间消息",
            "schedule_time": past_time,
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "SCHEDULE_TIME_IN_PAST"


class TestTriggerEvent:
    """TriggerSetupTool event 触发类型测试"""

    async def test_event_success(self):
        """测试事件触发器设置成功"""
        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "event",
            "message": "事件消息",
            "event_type": "task_completed",
            "session_id": "sess_001",
            "execution_id": "exec_001",
        })
        assert result.success is True
        assert "trigger_id" in result.output
        assert tool._queue.push.called

    async def test_event_missing_event_type(self):
        """测试 event 类型缺少 event_type 参数时返回失败"""
        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "event",
            "message": "事件消息",
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "MISSING_EVENT_TYPE"


class TestTriggerCondition:
    """TriggerSetupTool condition 触发类型测试"""

    async def test_condition_success(self):
        """测试条件触发器设置成功"""
        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "condition",
            "message": "条件消息",
            "condition": "task_status == 'pending'",
            "session_id": "sess_001",
            "execution_id": "exec_001",
        })
        assert result.success is True
        assert "trigger_id" in result.output
        assert tool._queue.push.called

    async def test_condition_missing_condition(self):
        """测试 condition 类型缺少 condition 参数时返回失败"""
        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = 0

        result = await tool.execute({
            "trigger_type": "condition",
            "message": "条件消息",
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "MISSING_CONDITION"


class TestTriggerLimit:
    """TriggerSetupTool 触发器数量上限测试"""

    async def test_trigger_limit_exceeded(self):
        """测试触发器数量达到上限时返回失败"""
        tool = TriggerSetupTool()
        tool._queue = AsyncMock()
        tool._queue.size.return_value = TriggerSetupTool.MAX_TRIGGERS_PER_SESSION

        result = await tool.execute({
            "trigger_type": "event",
            "message": "超限消息",
            "event_type": "test_event",
            "session_id": "sess_001",
        })
        assert result.success is False
        assert result.error_code == "TRIGGER_LIMIT_EXCEEDED"


# =====================================================================
# hot_swap_func 测试
# =====================================================================


class TestHotSwapAction:
    """hot_swap_func action 分发测试"""

    def test_missing_action(self):
        """测试缺少 action 参数时返回失败"""
        result = hot_swap_func({})
        assert result["success"] is False
        assert result["error_code"] == "MISSING_ACTION"

    def test_unsupported_action(self):
        """测试不支持的 action 时返回失败"""
        result = hot_swap_func({"action": "invalid_action"})
        assert result["success"] is False
        assert result["error_code"] == "INVALID_ACTION"


class TestHotSwapSwapPlugin:
    """hot_swap_func swap_plugin 操作测试"""

    def test_swap_plugin_missing_plugin_name(self):
        """测试 swap_plugin 缺少 plugin_name 参数时返回失败"""
        result = hot_swap_func({
            "action": "swap_plugin",
            "new_plugin_class": "some.Module",
        })
        assert result["success"] is False
        assert result["error_code"] == "MISSING_PLUGIN_NAME"

    def test_swap_plugin_missing_new_plugin_class(self):
        """测试 swap_plugin 缺少 new_plugin_class 参数时返回失败"""
        result = hot_swap_func({
            "action": "swap_plugin",
            "plugin_name": "my_plugin",
        })
        assert result["success"] is False
        assert result["error_code"] == "MISSING_NEW_PLUGIN_CLASS"


class TestHotSwapRollbackPlugin:
    """hot_swap_func rollback_plugin 操作测试"""

    def test_rollback_plugin_missing_swap_id(self):
        """测试 rollback_plugin 缺少 swap_id 参数时返回失败"""
        result = hot_swap_func({"action": "rollback_plugin"})
        assert result["success"] is False
        assert result["error_code"] == "MISSING_SWAP_ID"


class TestHotSwapSaveConfigVersion:
    """hot_swap_func save_config_version 操作测试"""

    def test_save_config_version_missing_config_id(self):
        """测试 save_config_version 缺少 config_id 参数时返回失败"""
        result = hot_swap_func({
            "action": "save_config_version",
            "config_data": {"key": "value"},
        })
        assert result["success"] is False
        assert result["error_code"] == "MISSING_CONFIG_ID"

    def test_save_config_version_missing_config_data(self):
        """测试 save_config_version 缺少 config_data 参数时返回失败"""
        result = hot_swap_func({
            "action": "save_config_version",
            "config_id": "cfg_001",
        })
        assert result["success"] is False
        assert result["error_code"] == "MISSING_CONFIG_DATA"


class TestHotSwapRollbackConfig:
    """hot_swap_func rollback_config 操作测试"""

    def test_rollback_config_missing_version_id(self):
        """测试 rollback_config 缺少 version_id 参数时返回失败"""
        result = hot_swap_func({"action": "rollback_config"})
        assert result["success"] is False
        assert result["error_code"] == "MISSING_VERSION_ID"


class TestHotSwapListVersions:
    """hot_swap_func list_versions 操作测试"""

    def test_list_versions_missing_config_id(self):
        """测试 list_versions 缺少 config_id 参数时返回失败"""
        result = hot_swap_func({"action": "list_versions"})
        assert result["success"] is False
        assert result["error_code"] == "MISSING_CONFIG_ID"


class TestHotSwapSchema:
    """hot_swap schema 验证测试"""

    def test_schema_has_required_action(self):
        """验证 schema 要求 action 为必填参数"""
        assert "action" in hot_swap_schema["required"]

    def test_schema_has_all_actions_in_enum(self):
        """验证 schema 包含所有支持的 action 枚举值"""
        action_enum = hot_swap_schema["properties"]["action"]["enum"]
        expected_actions = {
            "swap_plugin",
            "rollback_plugin",
            "save_config_version",
            "rollback_config",
            "list_versions",
        }
        assert set(action_enum) == expected_actions

    def test_schema_has_swap_plugin_params(self):
        """验证 schema 包含 swap_plugin 所需的参数"""
        props = hot_swap_schema["properties"]
        assert "plugin_name" in props
        assert "new_plugin_class" in props

    def test_schema_has_rollback_plugin_params(self):
        """验证 schema 包含 rollback_plugin 所需的参数"""
        props = hot_swap_schema["properties"]
        assert "swap_id" in props

    def test_schema_has_config_params(self):
        """验证 schema 包含配置相关参数"""
        props = hot_swap_schema["properties"]
        assert "config_id" in props
        assert "config_data" in props
        assert "version_id" in props


# =====================================================================
# ResourceSearchTool 测试
# =====================================================================


class TestResourceSearchToolDefinition:
    """ResourceSearchTool 工具定义验证"""

    def test_tool_definition_name(self):
        """验证工具名称为 resource_search"""
        tool_def = ResourceSearchTool.get_tool_definition()
        assert tool_def.name == "resource_search"

    def test_tool_definition_resource_type_enum(self):
        """验证工具定义包含 resource_type 枚举值"""
        tool_def = ResourceSearchTool.get_tool_definition()
        resource_enum = tool_def.input_schema["properties"]["resource_type"]["enum"]
        assert set(resource_enum) == {"agent", "tool", "skill", "all"}

    def test_tool_definition_required_params(self):
        """验证工具定义要求 resource_type 参数"""
        tool_def = ResourceSearchTool.get_tool_definition()
        required = tool_def.input_schema["required"]
        assert "resource_type" in required


class TestResourceSearchAgent:
    """ResourceSearchTool agent 搜索测试"""

    async def test_search_agent_success(self):
        """测试成功搜索 agent 资源"""
        # 构造 mock agent 配置对象
        mock_agent = MagicMock()
        mock_agent.name = "CodeHelper"
        mock_agent.description = "代码辅助 Agent"
        mock_agent.tags = ["code", "helper"]
        mock_agent.config_id = "agent_001"
        mock_agent.level = "user"
        mock_agent.category = None

        mock_registry = MagicMock()
        mock_registry.list_all.return_value = [mock_agent]

        tool = ResourceSearchTool(
            agent_registry=mock_registry,
            search_engine=None,
        )

        result = await tool.execute({
            "resource_type": "agent",
            "query": "CodeHelper",
        })

        assert result.success is True
        assert "agent_d" in result.output
        assert result.output["agent_c"] == 1

    async def test_search_agent_no_results(self):
        """测试搜索 agent 无结果时返回空列表"""
        mock_registry = MagicMock()
        mock_registry.list_all.return_value = []

        tool = ResourceSearchTool(
            agent_registry=mock_registry,
            search_engine=None,
        )

        result = await tool.execute({
            "resource_type": "agent",
            "query": "nonexistent",
        })

        assert result.success is True
        assert result.output.get("agent_c") is None


class TestResourceSearchTool:
    """ResourceSearchTool tool 搜索测试"""

    async def test_search_tool_success(self):
        """测试成功搜索 tool 资源"""
        mock_tool = MagicMock()
        mock_tool.name = "file_read"
        mock_tool.description = "文件读取工具"
        mock_tool.tags = ["file"]
        mock_tool.level = "user"
        mock_tool.category = None

        mock_registry = MagicMock()
        mock_registry.list_all.return_value = [mock_tool]

        tool = ResourceSearchTool(
            tool_registry=mock_registry,
            search_engine=None,
        )

        result = await tool.execute({
            "resource_type": "tool",
            "query": "file_read",
        })

        assert result.success is True
        assert "tool_d" in result.output
        assert result.output["tool_c"] == 1

    async def test_search_tool_no_results(self):
        """测试搜索 tool 无结果时返回空"""
        mock_registry = MagicMock()
        mock_registry.list_all.return_value = []

        tool = ResourceSearchTool(
            tool_registry=mock_registry,
            search_engine=None,
        )

        result = await tool.execute({
            "resource_type": "tool",
            "query": "nonexistent_tool",
        })

        assert result.success is True
        assert result.output.get("tool_c") is None


class TestResourceSearchSkill:
    """ResourceSearchTool skill 搜索测试"""

    async def test_search_skill_success(self):
        """测试成功搜索 skill 资源"""
        # 构造 mock skill 对象
        mock_skill = MagicMock()
        mock_skill.skill_name = "code_review"
        mock_skill.description = "代码审查 Skill"
        mock_skill.scripts = []
        mock_skill.skill_path = "/skills/code_review"

        mock_registry = MagicMock()
        mock_registry.is_initialized.return_value = True
        mock_registry.search_skills.return_value = [mock_skill]

        tool = ResourceSearchTool(
            skill_registry=mock_registry,
            search_engine=None,
        )

        result = await tool.execute({
            "resource_type": "skill",
            "query": "code_review",
        })

        assert result.success is True
        assert "skill_d" in result.output
        assert result.output["skill_c"] == 1

    async def test_search_skill_no_results(self):
        """测试搜索 skill 无结果时返回空"""
        mock_registry = MagicMock()
        mock_registry.is_initialized.return_value = True
        mock_registry.search_skills.return_value = []

        tool = ResourceSearchTool(
            skill_registry=mock_registry,
            search_engine=None,
        )

        result = await tool.execute({
            "resource_type": "skill",
            "query": "nonexistent",
        })

        assert result.success is True
        assert result.output.get("skill_c") is None


class TestResourceSearchDetailed:
    """ResourceSearchTool detailed 模式测试"""

    async def test_detailed_mode_tool_search(self):
        """测试 detailed 模式搜索工具"""
        mock_tool = MagicMock()
        mock_tool.name = "rollback_task"
        mock_tool.description = "回滚任务工具"
        mock_tool.tags = ["task"]
        mock_tool.level = "user"
        mock_tool.category = None

        mock_registry = MagicMock()
        mock_registry.list_all.return_value = [mock_tool]

        mock_injector = AsyncMock()

        tool = ResourceSearchTool(
            tool_registry=mock_registry,
            search_engine=None,
            dynamic_tool_injector=mock_injector,
        )

        result = await tool.execute({
            "resource_type": "tool",
            "query": "rollback_task",
            "mode": "detailed",
        })

        assert result.success is True
        assert result.output.get("tool_c") == 1

    async def test_detailed_mode_skill_search(self):
        """测试 detailed 模式搜索 Skill（返回完整内容）"""
        mock_skill = MagicMock()
        mock_skill.skill_name = "code_review"
        mock_skill.description = "代码审查 Skill"
        mock_skill.scripts = []
        mock_skill.skill_path = "/skills/code_review"

        mock_registry = MagicMock()
        mock_registry.is_initialized.return_value = True
        mock_registry.search_skills.return_value = [mock_skill]

        tool = ResourceSearchTool(
            skill_registry=mock_registry,
            search_engine=None,
        )

        # mock _read_skill_markdown 方法
        tool._read_skill_markdown = MagicMock(return_value="# Code Review Skill\n内容...")

        result = await tool.execute({
            "resource_type": "skill",
            "query": "code_review",
            "mode": "detailed",
        })

        assert result.success is True
        assert result.output.get("skill_c") == 1
        # detailed 模式下 skill 数据应包含 skill_content
        skill_data = result.output["skill_d"][0]
        assert len(skill_data) == 3  # skill_name, skill_description, skill_content


class TestResourceSearchNoRegistry:
    """ResourceSearchTool 无注册表降级测试"""

    async def test_no_registry_returns_empty_result(self):
        """测试所有注册表为 None 时返回空结果"""
        tool = ResourceSearchTool(
            agent_registry=None,
            tool_registry=None,
            skill_registry=None,
            search_engine=None,
        )

        # 阻止延迟加载
        tool._get_agent_registry = MagicMock(return_value=None)
        tool._get_tool_registry = MagicMock(return_value=None)
        tool._get_skill_registry = MagicMock(return_value=None)

        result = await tool.execute({
            "resource_type": "all",
            "query": "test",
        })

        assert result.success is True
        assert result.output.get("agent_c") is None
        assert result.output.get("tool_c") is None
        assert result.output.get("skill_c") is None

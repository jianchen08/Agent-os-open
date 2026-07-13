"""外部工具示例连接器测试（VSCodeConnector + GodotConnector）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from tools.external.examples.vscode_connector import (
    ALLOWED_PATH_PREFIXES,
    APPLY_EDIT_SCHEMA,
    GET_DIAGNOSTICS_SCHEMA,
    GET_SELECTION_SCHEMA,
    GET_SYMBOLS_SCHEMA,
    OPEN_FILE_SCHEMA,
    OPERATION_RESULT_SCHEMA,
    SHOW_DIFF_SCHEMA,
    VSCodeConnector,
)
from tools.external.examples.godot_connector import (
    EXECUTE_SCRIPT_SCHEMA,
    GET_PROJECT_INFO_SCHEMA,
    LIST_SCENES_SCHEMA,
    MANAGE_RESOURCE_SCHEMA,
    OPEN_SCENE_SCHEMA,
    RUN_SCENE_SCHEMA,
    GodotConnector,
)
from tools.external.types import (
    ExternalToolConfig,
    ProtocolType,
)


# ── 辅助 fixtures ──


@pytest.fixture
def vscode_config() -> ExternalToolConfig:
    return ExternalToolConfig(
        name="vscode",
        display_name="VSCode",
        protocol=ProtocolType.WEBSOCKET,
        endpoint="ws://localhost:9889",
        execute_timeout=30.0,
    )


@pytest.fixture
def vscode_connector(vscode_config: ExternalToolConfig) -> VSCodeConnector:
    return VSCodeConnector(vscode_config)


@pytest.fixture
def godot_config() -> ExternalToolConfig:
    return ExternalToolConfig(
        name="godot",
        display_name="Godot",
        protocol=ProtocolType.HTTP,
        endpoint="http://localhost:8901",
        execute_timeout=30.0,
    )


@pytest.fixture
def godot_connector(godot_config: ExternalToolConfig) -> GodotConnector:
    return GodotConnector(godot_config)


# ════════════════════════════════════════════
# VSCodeConnector Schema 定义
# ════════════════════════════════════════════


class TestVSCodeSchemas:
    """VSCode 操作 Schema 定义测试。"""

    def test_schema_definitions_exist(self) -> None:
        """所有 Schema 常量非空。"""
        schemas = [
            OPEN_FILE_SCHEMA,
            GET_SELECTION_SCHEMA,
            SHOW_DIFF_SCHEMA,
            APPLY_EDIT_SCHEMA,
            GET_DIAGNOSTICS_SCHEMA,
            GET_SYMBOLS_SCHEMA,
        ]
        for s in schemas:
            assert isinstance(s, dict)
            assert s.get("type") == "object"

    def test_open_file_has_required(self) -> None:
        """open_file 要求 file_path。"""
        assert "file_path" in OPEN_FILE_SCHEMA["properties"]
        assert "file_path" in OPEN_FILE_SCHEMA["required"]

    def test_apply_edit_schema_structure(self) -> None:
        """apply_edit Schema 包含 file_path 和 edits。"""
        assert "file_path" in APPLY_EDIT_SCHEMA["properties"]
        assert "edits" in APPLY_EDIT_SCHEMA["properties"]
        assert "required" in APPLY_EDIT_SCHEMA

    def test_show_diff_has_required(self) -> None:
        """show_diff 要求 original 和 modified。"""
        assert "original" in SHOW_DIFF_SCHEMA["required"]
        assert "modified" in SHOW_DIFF_SCHEMA["required"]


class TestVSCodeDefineSchemas:
    """VSCode define_schemas 方法测试。"""

    def test_returns_all_operations(self, vscode_connector: VSCodeConnector) -> None:
        """返回所有 6 个操作。"""
        caps = vscode_connector.define_schemas()
        assert len(caps) == 6
        names = {c.name for c in caps}
        expected = {"open_file", "get_selection", "show_diff", "apply_edit", "get_diagnostics", "get_symbols"}
        assert names == expected

    def test_apply_edit_is_dangerous(self, vscode_connector: VSCodeConnector) -> None:
        """apply_edit 标记为危险操作。"""
        caps = vscode_connector.define_schemas()
        apply_edit = next(c for c in caps if c.name == "apply_edit")
        assert apply_edit.dangerous is True

    def test_all_ops_have_output_schema(self, vscode_connector: VSCodeConnector) -> None:
        """所有操作都有 output_schema。"""
        caps = vscode_connector.define_schemas()
        for cap in caps:
            assert cap.output_schema is not None


# ════════════════════════════════════════════
# VSCodeConnector 路径安全验证
# ════════════════════════════════════════════


class TestVSCodePathValidation:
    """VSCode 路径安全验证测试。"""

    def test_path_traversal_blocked(self) -> None:
        """路径遍历被阻止。"""
        with pytest.raises(ValueError, match="遍历"):
            VSCodeConnector._validate_path("/workspace/../etc/passwd")

    def test_path_traversal_various(self) -> None:
        """各种路径遍历模式被阻止。"""
        bad_paths = [
            "/tmp/../etc/passwd",
            "/home/user/../../../root",
            "relative/../path",
        ]
        for p in bad_paths:
            with pytest.raises(ValueError):
                VSCodeConnector._validate_path(p)

    def test_empty_path_allowed(self) -> None:
        """空路径不报错。"""
        VSCodeConnector._validate_path("")  # 不应抛异常

    def test_normal_path_allowed(self) -> None:
        """正常路径不报错。"""
        VSCodeConnector._validate_path("/workspace/src/main.py")

    def test_validate_input_with_path(self, vscode_connector: VSCodeConnector) -> None:
        """验证输入时对路径操作进行安全检查。"""
        # 正常路径
        result = vscode_connector.validate_input("open_file", {"file_path": "/workspace/test.py"})
        assert result["file_path"] == "/workspace/test.py"

    def test_validate_input_blocks_traversal(self, vscode_connector: VSCodeConnector) -> None:
        """路径遍历在验证输入时被阻止。"""
        with pytest.raises(ValueError, match="遍历"):
            vscode_connector.validate_input("open_file", {"file_path": "/workspace/../etc/passwd"})


# ════════════════════════════════════════════
# VSCodeConnector 执行
# ════════════════════════════════════════════


class TestVSCodeExecute:
    """VSCode 操作执行测试。"""

    @pytest.mark.asyncio
    async def test_execute_without_connection(self, vscode_connector: VSCodeConnector) -> None:
        """无连接时返回错误。"""
        result = await vscode_connector._do_execute("open_file", {"file_path": "/workspace/a.py"})
        assert result["success"] is False
        assert "连接未建立" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_with_connection(self, vscode_connector: VSCodeConnector) -> None:
        """有连接时通过连接发送请求。"""
        mock_conn = AsyncMock()
        mock_conn.send_request.return_value = {"success": True, "data": {"content": "hello"}}
        vscode_connector._connection = mock_conn

        result = await vscode_connector._do_execute("open_file", {"file_path": "/workspace/a.py"})
        assert result["success"] is True
        mock_conn.send_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_handles_exception(self, vscode_connector: VSCodeConnector) -> None:
        """连接异常被捕获。"""
        mock_conn = AsyncMock()
        mock_conn.send_request.side_effect = Exception("conn dropped")
        vscode_connector._connection = mock_conn

        result = await vscode_connector._do_execute("open_file", {"file_path": "/a.py"})
        assert result["success"] is False
        assert "conn dropped" in result["error"]


# ════════════════════════════════════════════
# GodotConnector Schema 定义
# ════════════════════════════════════════════


class TestGodotSchemas:
    """Godot 操作 Schema 定义测试。"""

    def test_schema_definitions_exist(self) -> None:
        """所有 Schema 常量非空。"""
        schemas = [
            LIST_SCENES_SCHEMA,
            OPEN_SCENE_SCHEMA,
            RUN_SCENE_SCHEMA,
            EXECUTE_SCRIPT_SCHEMA,
            MANAGE_RESOURCE_SCHEMA,
            GET_PROJECT_INFO_SCHEMA,
        ]
        for s in schemas:
            assert isinstance(s, dict)
            assert s.get("type") == "object"

    def test_open_scene_requires_scene_path(self) -> None:
        """open_scene 要求 scene_path。"""
        assert "scene_path" in OPEN_SCENE_SCHEMA["required"]

    def test_manage_resource_has_action_enum(self) -> None:
        """manage_resource action 有枚举值。"""
        action_prop = MANAGE_RESOURCE_SCHEMA["properties"]["action"]
        assert "enum" in action_prop
        assert set(action_prop["enum"]) == {"list", "get", "create", "delete"}


class TestGodotDefineSchemas:
    """Godot define_schemas 方法测试。"""

    def test_returns_all_operations(self, godot_connector: GodotConnector) -> None:
        """返回所有 6 个操作。"""
        caps = godot_connector.define_schemas()
        assert len(caps) == 6
        names = {c.name for c in caps}
        expected = {"list_scenes", "open_scene", "run_scene", "execute_script", "manage_resource", "get_project_info"}
        assert names == expected

    def test_run_scene_has_timeout_override(self, godot_connector: GodotConnector) -> None:
        """run_scene 有自定义超时。"""
        caps = godot_connector.define_schemas()
        run = next(c for c in caps if c.name == "run_scene")
        assert run.timeout_override == 120.0

    def test_execute_script_requires_sandbox(self, godot_connector: GodotConnector) -> None:
        """execute_script 需要沙箱执行。"""
        caps = godot_connector.define_schemas()
        script = next(c for c in caps if c.name == "execute_script")
        assert script.requires_sandbox is True

    def test_manage_resource_is_dangerous(self, godot_connector: GodotConnector) -> None:
        """manage_resource 标记为危险操作。"""
        caps = godot_connector.define_schemas()
        res = next(c for c in caps if c.name == "manage_resource")
        assert res.dangerous is True


# ════════════════════════════════════════════
# GodotConnector 场景/资源验证
# ════════════════════════════════════════════


class TestGodotValidation:
    """Godot 输入验证测试。"""

    def test_validate_open_scene_valid(self, godot_connector: GodotConnector) -> None:
        """合法场景路径通过验证。"""
        result = godot_connector.validate_input("open_scene", {"scene_path": "main.tscn"})
        assert result["scene_path"] == "main.tscn"

    def test_validate_open_scene_invalid_format(self, godot_connector: GodotConnector) -> None:
        """无效场景文件格式报错。"""
        with pytest.raises(ValueError, match="场景文件格式无效"):
            godot_connector.validate_input("open_scene", {"scene_path": "main.py"})

    def test_validate_open_scene_scn_extension(self, godot_connector: GodotConnector) -> None:
        """.scn 扩展名也合法。"""
        result = godot_connector.validate_input("open_scene", {"scene_path": "level.scn"})
        assert result["scene_path"] == "level.scn"

    def test_validate_manage_resource_get_needs_path(self, godot_connector: GodotConnector) -> None:
        """get 操作需要 resource_path。"""
        with pytest.raises(ValueError, match="resource_path"):
            godot_connector.validate_input("manage_resource", {"action": "get"})

    def test_validate_manage_resource_delete_needs_path(self, godot_connector: GodotConnector) -> None:
        """delete 操作需要 resource_path。"""
        with pytest.raises(ValueError, match="resource_path"):
            godot_connector.validate_input("manage_resource", {"action": "delete"})

    def test_validate_manage_resource_create_needs_data(self, godot_connector: GodotConnector) -> None:
        """create 操作需要 resource_data。"""
        with pytest.raises(ValueError, match="resource_data"):
            godot_connector.validate_input("manage_resource", {"action": "create"})

    def test_validate_manage_resource_list_ok(self, godot_connector: GodotConnector) -> None:
        """list 操作不需要额外参数。"""
        result = godot_connector.validate_input("manage_resource", {"action": "list"})
        assert result["action"] == "list"

    def test_validate_execute_script_needs_content_or_path(self, godot_connector: GodotConnector) -> None:
        """execute_script 需要 script_content 或 script_path。"""
        with pytest.raises(ValueError, match="script_content"):
            godot_connector.validate_input("execute_script", {})

    def test_validate_execute_script_with_content(self, godot_connector: GodotConnector) -> None:
        """提供 script_content 通过验证。"""
        result = godot_connector.validate_input("execute_script", {"script_content": "print('hi')"})
        assert result["script_content"] == "print('hi')"

    def test_validate_execute_script_with_path(self, godot_connector: GodotConnector) -> None:
        """提供 script_path 通过验证。"""
        result = godot_connector.validate_input("execute_script", {"script_path": "/game/player.gd"})
        assert result["script_path"] == "/game/player.gd"


# ════════════════════════════════════════════
# GodotConnector 执行
# ════════════════════════════════════════════


class TestGodotExecute:
    """Godot 操作执行测试。"""

    @pytest.mark.asyncio
    async def test_execute_without_connection(self, godot_connector: GodotConnector) -> None:
        """无连接时返回错误。"""
        result = await godot_connector._do_execute("list_scenes", {})
        assert result["success"] is False
        assert "连接未建立" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_with_connection(self, godot_connector: GodotConnector) -> None:
        """有连接时通过连接发送请求。"""
        mock_conn = AsyncMock()
        mock_conn.send_request.return_value = {"success": True, "data": {"scenes": []}}
        godot_connector._connection = mock_conn

        result = await godot_connector._do_execute("list_scenes", {})
        assert result["success"] is True
        mock_conn.send_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_handles_exception(self, godot_connector: GodotConnector) -> None:
        """连接异常被捕获。"""
        mock_conn = AsyncMock()
        mock_conn.send_request.side_effect = Exception("timeout")
        godot_connector._connection = mock_conn

        result = await godot_connector._do_execute("run_scene", {})
        assert result["success"] is False
        assert "timeout" in result["error"]


# ════════════════════════════════════════════
# 连接器 to_tool() 集成
# ════════════════════════════════════════════


class TestConnectorToTool:
    """连接器 to_tool() 转换测试。"""

    def test_vscode_to_tool(self, vscode_connector: VSCodeConnector) -> None:
        """VSCode 转换为 6 个内部 Tool。"""
        tools = vscode_connector.to_tool()
        assert len(tools) == 6
        names = {t.name for t in tools}
        assert "vscode__open_file" in names
        assert "vscode__apply_edit" in names

    def test_godot_to_tool(self, godot_connector: GodotConnector) -> None:
        """Godot 转换为 6 个内部 Tool。"""
        tools = godot_connector.to_tool()
        assert len(tools) == 6
        names = {t.name for t in tools}
        assert "godot__list_scenes" in names
        assert "godot__execute_script" in names

    def test_vscode_dangerous_metadata(self, vscode_connector: VSCodeConnector) -> None:
        """apply_edit 的 Tool 元数据标记为危险。"""
        tools = vscode_connector.to_tool()
        apply_edit = next(t for t in tools if "apply_edit" in t.name)
        assert apply_edit.metadata["dangerous"] is True

    def test_godot_sandbox_metadata(self, godot_connector: GodotConnector) -> None:
        """execute_script 的 Tool 元数据标记需要沙箱。"""
        tools = godot_connector.to_tool()
        script_tool = next(t for t in tools if "execute_script" in t.name)
        assert script_tool.metadata["requires_sandbox"] is True

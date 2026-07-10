"""
Bug 修复验证测试

Bug1: copy_file / move_file / delete_file 三个工具因缺失 ToolResult 导入
      导致 DynamicToolLoader 无法发现和注册。

Bug3: resolve_task_workspace 对子任务无条件拼接 task.id，
      但 ws_meta.path 已是正确路径，导致路径指向不存在的目录。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ════════════════════════════════════════════════════════════════
# Bug1: 工具注册缺失
# ════════════════════════════════════════════════════════════════


class TestBug1ToolRegistration:
    """验证 copy_file / move_file / delete_file 能被 DynamicToolLoader 正确发现和加载。"""

    EXPECTED_TOOLS = {"copy_file", "move_file", "delete_file"}

    def test_tools_discovered_by_dynamic_loader(self):
        """Bug1 回归：DynamicToolLoader._discover_tools 应发现这三个工具。"""
        from tools.loader import DynamicToolLoader
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        loader = DynamicToolLoader(registry)
        loader._discover_tools()

        for tool_name in self.EXPECTED_TOOLS:
            assert tool_name in loader._tool_classes, (
                f"工具 '{tool_name}' 未被 DynamicToolLoader 发现。"
                f"已发现工具: {sorted(loader._tool_classes.keys())}"
            )

    def test_tools_instantiate_and_get_definition(self):
        """Bug1 回归：三个工具类可正常实例化并返回工具定义。"""
        from tools.loader import DynamicToolLoader
        from tools.registry import ToolRegistry
        import importlib

        registry = ToolRegistry()
        loader = DynamicToolLoader(registry)
        loader._discover_tools()

        for tool_name in self.EXPECTED_TOOLS:
            module_path, class_name = loader._tool_classes[tool_name]
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)

            # 实例化（无参构造）
            instance = cls()
            tool_def = instance.get_tool_definition()

            assert tool_def.name == tool_name, (
                f"工具定义名称不匹配: 期望 '{tool_name}'，实际 '{tool_def.name}'"
            )

    def test_tools_available_via_get_available_tools(self):
        """Bug1 回归：三个工具出现在 get_available_tools 列表中。"""
        from tools.loader import DynamicToolLoader
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        loader = DynamicToolLoader(registry)

        available = loader.get_available_tools()

        for tool_name in self.EXPECTED_TOOLS:
            assert tool_name in available, (
                f"工具 '{tool_name}' 不在可用工具列表中"
            )

    def test_copy_file_execute_basic(self, tmp_path):
        """Bug1 回归：CopyFileTool 可正常执行文件复制。"""
        from tools.builtin.copy_file.tool import CopyFileTool

        # 准备测试文件
        src = tmp_path / "source.txt"
        src.write_text("hello world", encoding="utf-8")
        dest = tmp_path / "dest.txt"

        tool = CopyFileTool(base_path=str(tmp_path))
        result = asyncio.get_event_loop().run_until_complete(
            tool.execute({
                "source": str(src),
                "destination": str(dest),
            })
        )

        assert result.success, f"copy_file 执行失败: {result.error}"
        assert dest.read_text(encoding="utf-8") == "hello world"

    def test_move_file_execute_basic(self, tmp_path):
        """Bug1 回归：MoveFileTool 可正常执行文件移动。"""
        from tools.builtin.move_file.tool import MoveFileTool

        src = tmp_path / "source.txt"
        src.write_text("move me", encoding="utf-8")
        dest = tmp_path / "dest.txt"

        tool = MoveFileTool(base_path=str(tmp_path))
        result = asyncio.get_event_loop().run_until_complete(
            tool.execute({
                "source": str(src),
                "destination": str(dest),
            })
        )

        assert result.success, f"move_file 执行失败: {result.error}"
        assert dest.read_text(encoding="utf-8") == "move me"
        assert not src.exists()

    def test_delete_file_execute_basic(self, tmp_path):
        """Bug1 回归：DeleteFileTool 可正常执行文件删除。"""
        from tools.builtin.delete_file.tool import DeleteFileTool

        target = tmp_path / "to_delete.txt"
        target.write_text("delete me", encoding="utf-8")

        tool = DeleteFileTool(base_path=str(tmp_path))
        result = asyncio.get_event_loop().run_until_complete(
            tool.execute({"path": str(target)})
        )

        assert result.success, f"delete_file 执行失败: {result.error}"
        assert not target.exists()


# ════════════════════════════════════════════════════════════════
# Bug3: 子任务工作空间路径拼接异常
# ════════════════════════════════════════════════════════════════


@dataclass
class _FakeTask:
    """轻量任务模型，模拟 TaskModel 的关键属性。"""
    id: str = "sub123"
    parent_task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class TestBug3WorkspacePathConcatenation:
    """验证 resolve_task_workspace 不再错误拼接 task.id 到子任务路径。"""

    def test_subtask_path_not_appended_with_task_id(self):
        """Bug3 回归：子任务的 workspace 路径不应拼接 task.id。

        场景：子任务 ws_meta.path 指向 /workspace/container_xxx（shared 模式），
        resolve_task_workspace 应返回该路径本身，不额外追加 task.id。
        """
        from tasks.workspace import resolve_task_workspace

        ws_path = "/workspace/container_xxx"
        task = _FakeTask(
            id="sub_task_001",
            parent_task_id="parent_task_002",
            metadata={
                "ws_meta": {
                    "mode": "shared",
                    "path": ws_path,
                },
            },
        )

        result = resolve_task_workspace(task)

        assert result is not None
        # 关键断言：路径不应包含 task.id
        assert result == ws_path, (
            f"子任务路径不应拼接 task.id。期望 '{ws_path}'，实际 '{result}'"
        )
        assert "sub_task_001" not in result, (
            f"路径中不应包含子任务 ID 'sub_task_001'，实际路径: '{result}'"
        )

    def test_root_task_path_unchanged(self):
        """根任务路径不受影响。"""
        from tasks.workspace import resolve_task_workspace

        ws_path = "/workspace/root_project"
        task = _FakeTask(
            id="root_task_001",
            parent_task_id=None,
            metadata={
                "ws_meta": {
                    "mode": "worktree",
                    "path": ws_path,
                },
            },
        )

        result = resolve_task_workspace(task)

        assert result is not None
        assert result == ws_path

    def test_subtask_worktree_mode_path_unchanged(self):
        """子任务在 worktree 模式下路径也不应被修改。

        ws_meta.path 由 WorkspaceLifecycleManager 正确设置为 worktree 目录，
        resolve_task_workspace 不应再拼接 task.id。
        """
        from tasks.workspace import resolve_task_workspace

        ws_path = "/workspace/.ai_workspaces/container_xxx__wt_sub456"
        task = _FakeTask(
            id="sub456",
            parent_task_id="parent789",
            metadata={
                "ws_meta": {
                    "mode": "worktree",
                    "path": ws_path,
                    "branch": "task/sub456",
                },
            },
        )

        result = resolve_task_workspace(task)

        assert result is not None
        assert result == ws_path
        # 路径中不应出现重复的 task.id
        assert result.count("sub456") <= 1, (
            f"路径中 task.id 出现超过 1 次，可能被重复拼接: '{result}'"
        )

    def test_relative_path_converted_to_absolute(self):
        """相对路径应转为绝对路径，不拼接 task.id。"""
        from tasks.workspace import resolve_task_workspace

        task = _FakeTask(
            id="sub_relat",
            parent_task_id="parent_relat",
            metadata={
                "ws_meta": {
                    "mode": "shared",
                    "path": "workspace/relative_dir",
                },
            },
        )

        result = resolve_task_workspace(task)

        assert result is not None
        result_path = Path(result)
        assert result_path.is_absolute(), f"相对路径应转为绝对路径: '{result}'"
        # 不应包含 task.id
        assert "sub_relat" not in result

    def test_no_ws_meta_returns_none(self):
        """无 ws_meta 时返回 None。"""
        from tasks.workspace import resolve_task_workspace

        task = _FakeTask(
            id="no_meta",
            parent_task_id="parent_id",
            metadata={},
        )

        result = resolve_task_workspace(task)
        assert result is None

    def test_empty_ws_meta_path_returns_none(self):
        """ws_meta.path 为空时返回 None。"""
        from tasks.workspace import resolve_task_workspace

        task = _FakeTask(
            id="empty_path",
            parent_task_id="parent_id",
            metadata={"ws_meta": {"mode": "shared", "path": ""}},
        )

        result = resolve_task_workspace(task)
        assert result is None


# ════════════════════════════════════════════════════════════════
# Bug2: LSP 服务安装验证
# ════════════════════════════════════════════════════════════════


class TestBug2LspInstallation:
    """验证 python-lsp-server 已安装且 LSP 工具模块可正常导入和初始化。"""

    def test_pylsp_module_importable(self):
        """Bug2 回归：python-lsp-server (pylsp) 模块可导入。"""
        import importlib

        spec = importlib.util.find_spec("pylsp")
        assert spec is not None, "python-lsp-server (pylsp) 模块未安装"

    def test_pylsp_version_available(self):
        """Bug2 回归：pylsp 版本信息可获取。"""
        import pylsp

        version = getattr(pylsp, "__version__", None)
        assert version is not None, "pylsp 模块无 __version__ 属性"

    def test_lsp_tools_importable_and_definitions(self):
        """Bug2 回归：LSPTools 可导入且 get_tool_definitions 返回有效工具列表。"""
        from tools.builtin.lsp_tools.tool import LSPTools

        # 无参实例化
        instance = LSPTools()
        assert instance is not None

        # 获取工具定义
        defs = LSPTools.get_tool_definitions()
        assert isinstance(defs, dict), f"get_tool_definitions 应返回 dict，实际返回 {type(defs)}"
        assert len(defs) > 0, "LSPTools.get_tool_definitions 返回空字典"

        expected_tools = {"lsp_definition", "lsp_references", "lsp_diagnostics", "file_jump"}
        actual_tools = set(defs.keys())
        assert expected_tools.issubset(actual_tools), (
            f"期望 LSP 工具 {expected_tools} ⊆ 实际 {actual_tools}"
        )

    def test_lsp_tools_individual_definitions_valid(self):
        """Bug2 回归：每个 LSP 工具定义包含必要字段。"""
        from tools.builtin.lsp_tools.tool import LSPTools

        defs = LSPTools.get_tool_definitions()
        for tool_name, tool_def in defs.items():
            assert hasattr(tool_def, "name"), f"工具 {tool_name} 缺少 name 属性"
            assert tool_def.name == tool_name, (
                f"工具定义名称不匹配: 期望 '{tool_name}'，实际 '{tool_def.name}'"
            )
            assert hasattr(tool_def, "description"), f"工具 {tool_name} 缺少 description"
            assert len(tool_def.description) > 0, f"工具 {tool_name} 描述为空"

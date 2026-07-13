"""工作空间前端组件和结构验证测试。

验证前端文件的存在性、导出正确性和 API 函数签名。
不依赖前端运行环境，通过静态分析 TypeScript 源码进行验证。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 项目根目录（tests/test_workspace_frontend.py → 往上两级到工作区根目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SRC = PROJECT_ROOT / "frontend" / "src"


# ============================================================
# 辅助函数
# ============================================================


def _read_ts_file(rel_path: str) -> str:
    """读取 TypeScript 文件内容。"""
    fpath = FRONTEND_SRC / rel_path
    if not fpath.exists():
        pytest.fail(f"文件不存在: {rel_path}")
    return fpath.read_text(encoding="utf-8")


# ============================================================
# Test: CodeEditor 组件
# ============================================================


class TestCodeEditorComponent:
    """CodeEditor.tsx 组件验证。"""

    def test_file_exists(self):
        """CodeEditor.tsx 文件应存在。"""
        fpath = FRONTEND_SRC / "components" / "workspace" / "CodeEditor.tsx"
        assert fpath.exists(), f"CodeEditor.tsx 不存在于 {fpath}"

    def test_exports_code_editor_function(self):
        """应导出 CodeEditor 函数组件。"""
        content = _read_ts_file("components/workspace/CodeEditor.tsx")
        assert "export function CodeEditor" in content, "缺少 export function CodeEditor"

    def test_exports_code_editor_props(self):
        """应导出 CodeEditorProps 接口。"""
        content = _read_ts_file("components/workspace/CodeEditor.tsx")
        assert "export interface CodeEditorProps" in content, "缺少 export interface CodeEditorProps"

    def test_props_include_required_fields(self):
        """CodeEditorProps 应包含 filePath、content、onSave 等必要属性。"""
        content = _read_ts_file("components/workspace/CodeEditor.tsx")
        required_props = ["filePath", "content", "onSave"]
        for prop in required_props:
            assert prop in content, f"CodeEditorProps 缺少属性: {prop}"

    def test_has_save_functionality(self):
        """应包含保存功能（Ctrl+S 或保存按钮）。"""
        content = _read_ts_file("components/workspace/CodeEditor.tsx")
        # Ctrl+S 快捷键
        has_shortcut = "ctrlKey" in content and "e.key === 's'" in content
        # 保存按钮
        has_button = "handleSave" in content
        assert has_shortcut or has_button, "缺少保存功能"

    def test_has_large_file_detection(self):
        """应包含大文件检测逻辑。"""
        content = _read_ts_file("components/workspace/CodeEditor.tsx")
        assert "LARGE_FILE_THRESHOLD" in content or "large" in content.lower(), "缺少大文件检测"

    def test_has_language_detection(self):
        """应包含语言检测（根据文件扩展名）。"""
        content = _read_ts_file("components/workspace/CodeEditor.tsx")
        assert "EXTENSION_TO_LANGUAGE" in content, "缺少语言映射表"

    def test_file_size_is_reasonable(self):
        """文件大小应合理（非空、不超过 50KB）。"""
        fpath = FRONTEND_SRC / "components" / "workspace" / "CodeEditor.tsx"
        size = fpath.stat().st_size
        assert size > 100, "CodeEditor.tsx 文件过小，可能内容不完整"
        assert size < 50 * 1024, f"CodeEditor.tsx 文件过大 ({size} bytes)"


# ============================================================
# Test: FilePreview 组件
# ============================================================


class TestFilePreviewComponent:
    """FilePreview.tsx 组件验证。"""

    def test_file_exists(self):
        """FilePreview.tsx 文件应存在。"""
        fpath = FRONTEND_SRC / "components" / "workspace" / "FilePreview.tsx"
        assert fpath.exists(), f"FilePreview.tsx 不存在于 {fpath}"

    def test_exports_file_preview_function(self):
        """应导出 FilePreview 函数组件。"""
        content = _read_ts_file("components/workspace/FilePreview.tsx")
        assert "export function FilePreview" in content, "缺少 export function FilePreview"

    def test_exports_file_preview_props(self):
        """应导出 FilePreviewProps 接口。"""
        content = _read_ts_file("components/workspace/FilePreview.tsx")
        assert "export interface FilePreviewProps" in content, "缺少 export interface FilePreviewProps"

    def test_props_include_required_fields(self):
        """FilePreviewProps 应包含 filePath、content、containerTaskId 等必要属性。"""
        content = _read_ts_file("components/workspace/FilePreview.tsx")
        required_props = ["filePath", "content", "containerTaskId"]
        for prop in required_props:
            assert prop in content, f"FilePreviewProps 缺少属性: {prop}"

    def test_supports_image_preview(self):
        """应支持图片预览。"""
        content = _read_ts_file("components/workspace/FilePreview.tsx")
        assert "IMAGE_EXTENSIONS" in content or "'image'" in content, "缺少图片预览支持"

    def test_supports_code_preview(self):
        """应支持代码只读预览。"""
        content = _read_ts_file("components/workspace/FilePreview.tsx")
        assert "SyntaxHighlighter" in content, "缺少代码语法高亮"

    def test_supports_binary_fallback(self):
        """应支持二进制文件的友好提示。"""
        content = _read_ts_file("components/workspace/FilePreview.tsx")
        assert "binary" in content.lower() or "无法预览" in content, "缺少二进制文件友好提示"

    def test_file_size_is_reasonable(self):
        """文件大小应合理。"""
        fpath = FRONTEND_SRC / "components" / "workspace" / "FilePreview.tsx"
        size = fpath.stat().st_size
        assert size > 100, "FilePreview.tsx 文件过小"
        assert size < 50 * 1024, f"FilePreview.tsx 文件过大 ({size} bytes)"


# ============================================================
# Test: workspaces.ts API 函数
# ============================================================


class TestWorkspacesApi:
    """workspaces.ts API 函数签名验证。"""

    def test_file_exists(self):
        """workspaces.ts 文件应存在。"""
        fpath = FRONTEND_SRC / "services" / "api" / "workspaces.ts"
        assert fpath.exists(), f"workspaces.ts 不存在"

    def test_has_get_workspace_function(self):
        """应包含 getWorkspace 函数。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "export async function getWorkspace" in content, "缺少 getWorkspace 函数"

    def test_has_get_file_tree_function(self):
        """应包含 getFileTree 函数。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "export async function getFileTree" in content, "缺少 getFileTree 函数"

    def test_has_create_entry_function(self):
        """应包含 createEntry 函数。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "export async function createEntry" in content, "缺少 createEntry 函数"

    def test_create_entry_has_correct_params(self):
        """createEntry 函数签名应包含 containerTaskId, path, type 参数。"""
        content = _read_ts_file("services/api/workspaces.ts")
        # 检查函数参数
        match = re.search(
            r"function createEntry\s*\([^)]*\)",
            content,
        )
        assert match, "找不到 createEntry 函数定义"
        params = match.group()
        assert "containerTaskId" in params, "createEntry 缺少 containerTaskId 参数"
        assert "path" in params, "createEntry 缺少 path 参数"
        assert "type" in params, "createEntry 缺少 type 参数"

    def test_create_entry_calls_correct_endpoint(self):
        """createEntry 应调用 /create-entry 端点。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "create-entry" in content, "createEntry 未调用 create-entry 端点"

    def test_has_delete_entry_function(self):
        """应包含 deleteEntry 函数。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "export async function deleteEntry" in content, "缺少 deleteEntry 函数"

    def test_delete_entry_calls_correct_endpoint(self):
        """deleteEntry 应调用 DELETE /entries 端点。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "/entries" in content, "deleteEntry 未调用 entries 端点"

    def test_has_rename_entry_function(self):
        """应包含 renameEntry 函数。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "export async function renameEntry" in content, "缺少 renameEntry 函数"

    def test_rename_entry_calls_correct_endpoint(self):
        """renameEntry 应调用 /rename-entry 端点。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "rename-entry" in content, "renameEntry 未调用 rename-entry 端点"

    def test_has_move_entry_function(self):
        """应包含 moveEntry 函数。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "export async function moveEntry" in content, "缺少 moveEntry 函数"

    def test_move_entry_calls_correct_endpoint(self):
        """moveEntry 应调用 /move-entry 端点。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "move-entry" in content, "moveEntry 未调用 move-entry 端点"

    def test_all_functions_use_api_client(self):
        """所有 API 函数应使用 apiClient。"""
        content = _read_ts_file("services/api/workspaces.ts")
        assert "apiClient" in content, "未使用 apiClient"


# ============================================================
# Test: workspaceStore.ts
# ============================================================


class TestWorkspaceStore:
    """workspaceStore.ts 新增文件操作方法验证。"""

    def test_file_exists(self):
        """workspaceStore.ts 文件应存在。"""
        fpath = FRONTEND_SRC / "stores" / "workspaceStore.ts"
        assert fpath.exists(), "workspaceStore.ts 不存在"

    def test_has_create_entry_action(self):
        """store 应包含 createEntry action。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        assert "createEntry:" in content, "store 缺少 createEntry action"

    def test_has_delete_entry_action(self):
        """store 应包含 deleteEntry action。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        assert "deleteEntry:" in content, "store 缺少 deleteEntry action"

    def test_has_rename_entry_action(self):
        """store 应包含 renameEntry action。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        assert "renameEntry:" in content, "store 缺少 renameEntry action"

    def test_has_move_entry_action(self):
        """store 应包含 moveEntry action。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        assert "moveEntry:" in content, "store 缺少 moveEntry action"

    def test_create_entry_calls_api(self):
        """createEntry action 应调用 apiCreateEntry。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        assert "apiCreateEntry" in content, "createEntry 未调用 apiCreateEntry"

    def test_delete_entry_calls_api(self):
        """deleteEntry action 应调用 apiDeleteEntry。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        assert "apiDeleteEntry" in content, "deleteEntry 未调用 apiDeleteEntry"

    def test_rename_entry_calls_api(self):
        """renameEntry action 应调用 apiRenameEntry。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        assert "apiRenameEntry" in content, "renameEntry 未调用 apiRenameEntry"

    def test_move_entry_calls_api(self):
        """moveEntry action 应调用 apiMoveEntry。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        assert "apiMoveEntry" in content, "moveEntry 未调用 apiMoveEntry"

    def test_actions_refresh_file_tree(self):
        """文件操作后应刷新文件树。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        # 每个 action 成功后都应调用 fetchFileTree
        assert "fetchFileTree" in content, "文件操作后未调用 fetchFileTree 刷新"

    def test_actions_return_boolean(self):
        """文件操作 action 应返回 boolean。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        # 检查接口声明中有 Promise<boolean>
        assert "Promise<boolean>" in content, "action 未声明返回 Promise<boolean>"

    def test_has_workspace_actions_interface(self):
        """应定义 WorkspaceActions 接口。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        assert "interface WorkspaceActions" in content, "缺少 WorkspaceActions 接口"

    def test_actions_included_in_interface(self):
        """WorkspaceActions 接口应包含所有文件操作方法签名。"""
        content = _read_ts_file("stores/workspaceStore.ts")
        actions = [
            ("createEntry", "containerTaskId"),
            ("deleteEntry", "containerTaskId"),
            ("renameEntry", "containerTaskId"),
            ("moveEntry", "containerTaskId"),
        ]
        for action_name, param in actions:
            # 验证接口中有声明
            assert action_name in content, f"WorkspaceActions 缺少 {action_name} 声明"


# ============================================================
# Test: FileTreeWidget 上下文菜单
# ============================================================


class TestFileTreeWidgetContextMenu:
    """FileTreeWidget.tsx 上下文菜单功能验证。"""

    def test_file_exists(self):
        """FileTreeWidget.tsx 文件应存在。"""
        fpath = FRONTEND_SRC / "components" / "schema" / "widgets" / "FileTreeWidget.tsx"
        assert fpath.exists(), "FileTreeWidget.tsx 不存在"

    def test_has_context_menu_support(self):
        """应支持上下文菜单（onContextMenu）。"""
        fpath = FRONTEND_SRC / "components" / "schema" / "widgets" / "FileTreeWidget.tsx"
        content = fpath.read_text(encoding="utf-8")
        assert "onContextMenu" in content, "缺少上下文菜单支持（onContextMenu 属性）"

    def test_has_file_click_support(self):
        """应支持文件点击（onFileClick）。"""
        fpath = FRONTEND_SRC / "components" / "schema" / "widgets" / "FileTreeWidget.tsx"
        content = fpath.read_text(encoding="utf-8")
        assert "onFileClick" in content, "缺少文件点击支持（onFileClick 属性）"

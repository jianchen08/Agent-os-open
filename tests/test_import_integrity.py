"""大文件拆分和代码清理后 import 完整性验证。

验证内容：
1. 已删除文件确认（start_server.py、重复/过期文件）
2. app_factory.py → stream_handler → ws_handler → static_files import 链
3. src/isolation/ 拆分后的 import 链
4. src/services/tool_marketplace.py（删除重复后）
5. Dockerfile 引用的文件都存在
6. tests/ 中测试文件的 import 路径不受影响
"""

from __future__ import annotations

import ast
import sys
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "src"


# ---------------------------------------------------------------------------
# 辅助：确保 src 在 sys.path 中
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _ensure_src_path():
    """确保 src 目录在 sys.path 中。"""
    src_str = str(_SRC_DIR)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def _get_import_names(source: str) -> list[str]:
    """从 Python 源码中提取所有 import 的模块名（不含 from 的子模块）。"""
    names: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


# ===================================================================
# 1. 已删除文件确认
# ===================================================================

class TestDeletedFiles:
    """验证已删除文件确实已不存在。"""

    @pytest.mark.parametrize(
        "deleted_file",
        [
            "start_server.py",
        ],
        ids=["start_server.py"],
    )
    def test_deleted_file_not_exists(self, deleted_file: str):
        """已删除文件不应再存在。"""
        path = _PROJECT_ROOT / deleted_file
        assert not path.exists(), f"已删除文件仍存在: {deleted_file}"

    @pytest.mark.parametrize(
        "deleted_file",
        [
            "debug_page_structure.py",
            "diag_ws.py",
            "hello.txt",
            "identical_files.csv",
        ],
        ids=["debug_page_structure", "diag_ws", "hello.txt", "identical_files"],
    )
    def test_cleanup_files_not_exists(self, deleted_file: str):
        """过期/清理文件不应存在。"""
        for parent in [_PROJECT_ROOT, _PROJECT_ROOT / "scripts"]:
            path = parent / deleted_file
            if path.exists():
                pytest.fail(f"过期文件仍存在: {path}")

    def test_tool_marketplace_service_not_exists(self):
        """重复文件 tool_marketplace_service.py 不应存在。"""
        path = _SRC_DIR / "services" / "tool_marketplace_service.py"
        assert not path.exists(), "tool_marketplace_service.py 应已被删除"

    def test_src_errors_unused(self):
        """src/errors.py 如仍存在，应无其他模块引用它。"""
        errors_path = _SRC_DIR / "errors.py"
        if not errors_path.exists():
            pytest.skip("src/errors.py 已被删除，无需检查")
        # 扫描 src/ 下所有 .py 文件，确认无代码 import src.errors
        for py_file in _SRC_DIR.rglob("*.py"):
            if py_file.name == "errors.py":
                continue
            content = py_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                assert "from src.errors" not in stripped, (
                    f"{py_file.relative_to(_PROJECT_ROOT)} 仍引用 src.errors"
                )
                assert "import src.errors" not in stripped, (
                    f"{py_file.relative_to(_PROJECT_ROOT)} 仍引用 src.errors"
                )


# ===================================================================
# 2. app_factory 拆分后的 import 链
# ===================================================================

class TestAppFactoryImportChain:
    """验证 app_factory.py 拆分后的模块可正确导入。"""

    def test_app_factory_file_exists(self):
        """app_factory.py 文件存在。"""
        assert (_PROJECT_ROOT / "app_factory.py").exists()

    def test_stream_handler_file_exists(self):
        """stream_handler.py 文件存在。"""
        assert (_PROJECT_ROOT / "stream_handler.py").exists()

    def test_ws_handler_file_exists(self):
        """ws_handler.py 文件存在。"""
        assert (_PROJECT_ROOT / "ws_handler.py").exists()

    def test_static_files_file_exists(self):
        """static_files.py 文件存在。"""
        assert (_PROJECT_ROOT / "static_files.py").exists()

    def test_ws_handler_importable(self):
        """ws_handler.py 可被导入（无外部依赖问题）。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ws_handler", _PROJECT_ROOT / "ws_handler.py"
        )
        assert spec is not None, "ws_handler.py 无法被定位"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "WebSocketInteractionNotifier")

    def test_static_files_importable(self):
        """static_files.py 可被导入。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "static_files", _PROJECT_ROOT / "static_files.py"
        )
        assert spec is not None, "static_files.py 无法被定位"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "mount_media_static_files")

    def test_app_factory_imports_stream_handler(self):
        """app_factory.py 中导入了 stream_handler。"""
        content = (_PROJECT_ROOT / "app_factory.py").read_text(encoding="utf-8")
        imports = _get_import_names(content)
        assert "stream_handler" in imports, "app_factory.py 未导入 stream_handler"

    def test_app_factory_imports_ws_handler(self):
        """app_factory.py 中导入了 ws_handler。"""
        content = (_PROJECT_ROOT / "app_factory.py").read_text(encoding="utf-8")
        imports = _get_import_names(content)
        assert "ws_handler" in imports, "app_factory.py 未导入 ws_handler"

    def test_app_factory_imports_static_files(self):
        """app_factory.py 中导入了 static_files。"""
        content = (_PROJECT_ROOT / "app_factory.py").read_text(encoding="utf-8")
        imports = _get_import_names(content)
        assert "static_files" in imports, "app_factory.py 未导入 static_files"

    def test_stream_handler_imports_ws_handler(self):
        """stream_handler.py 中导入了 ws_handler。"""
        content = (_PROJECT_ROOT / "stream_handler.py").read_text(encoding="utf-8")
        imports = _get_import_names(content)
        assert "ws_handler" in imports, "stream_handler.py 未导入 ws_handler"

    def test_app_factory_no_import_start_server(self):
        """app_factory.py 不应 import start_server（代码行）。"""
        content = (_PROJECT_ROOT / "app_factory.py").read_text(encoding="utf-8")
        imports = _get_import_names(content)
        assert "start_server" not in imports, (
            "app_factory.py 仍 import start_server"
        )

    def test_stream_handler_no_import_start_server(self):
        """stream_handler.py 不应 import start_server（代码行）。"""
        content = (_PROJECT_ROOT / "stream_handler.py").read_text(encoding="utf-8")
        imports = _get_import_names(content)
        assert "start_server" not in imports, (
            "stream_handler.py 仍 import start_server"
        )


# ===================================================================
# 3. workspace_lifecycle 拆分后的 import 链
# ===================================================================

class TestWorkspaceLifecycleSplit:
    """验证 workspace_lifecycle.py 拆分后的 import 链。"""

    def test_workspace_lifecycle_file_exists(self):
        """workspace_lifecycle.py 文件存在。"""
        assert (_SRC_DIR / "isolation" / "workspace_lifecycle.py").exists()

    def test_git_ops_file_exists(self):
        """_workspace_git_ops.py 文件存在。"""
        assert (_SRC_DIR / "isolation" / "_workspace_git_ops.py").exists()

    def test_merge_ops_file_exists(self):
        """_workspace_merge_ops.py 文件存在。"""
        assert (_SRC_DIR / "isolation" / "_workspace_merge_ops.py").exists()

    def test_workspace_lifecycle_imports_git_ops(self):
        """workspace_lifecycle.py 正确导入 _workspace_git_ops。"""
        content = (_SRC_DIR / "isolation" / "workspace_lifecycle.py").read_text(
            encoding="utf-8"
        )
        assert "from isolation._workspace_git_ops import" in content

    def test_workspace_lifecycle_imports_merge_ops(self):
        """workspace_lifecycle.py 正确导入 _workspace_merge_ops。"""
        content = (_SRC_DIR / "isolation" / "workspace_lifecycle.py").read_text(
            encoding="utf-8"
        )
        assert "from isolation._workspace_merge_ops import" in content

    def test_git_ops_ast_valid(self):
        """_workspace_git_ops.py 语法正确。"""
        content = (_SRC_DIR / "isolation" / "_workspace_git_ops.py").read_text(
            encoding="utf-8"
        )
        ast.parse(content)

    def test_merge_ops_ast_valid(self):
        """_workspace_merge_ops.py 语法正确。"""
        content = (_SRC_DIR / "isolation" / "_workspace_merge_ops.py").read_text(
            encoding="utf-8"
        )
        ast.parse(content)

    def test_isolation_package_importable(self):
        """isolation 包可被整体导入。"""
        import isolation

        assert hasattr(isolation, "IsolationManager")
        assert hasattr(isolation, "IsolationDecider")

    def test_workspace_lifecycle_importable(self):
        """workspace_lifecycle 模块可被导入。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager

        assert WorkspaceLifecycleManager is not None

    def test_git_ops_exports_mixins(self):
        """_workspace_git_ops 导出 _GitOpsMixin。"""
        from isolation._workspace_git_ops import _GitOpsMixin

        assert _GitOpsMixin is not None

    def test_merge_ops_exports_mixins(self):
        """_workspace_merge_ops 导出 _MergeOpsMixin。"""
        from isolation._workspace_merge_ops import _MergeOpsMixin

        assert _MergeOpsMixin is not None

    def test_lifecycle_manager_inherits_mixins(self):
        """WorkspaceLifecycleManager 继承两个 Mixin。"""
        from isolation.workspace_lifecycle import WorkspaceLifecycleManager
        from isolation._workspace_git_ops import _GitOpsMixin
        from isolation._workspace_merge_ops import _MergeOpsMixin

        assert issubclass(WorkspaceLifecycleManager, _GitOpsMixin)
        assert issubclass(WorkspaceLifecycleManager, _MergeOpsMixin)


# ===================================================================
# 4. tool_marketplace.py 完整性
# ===================================================================

class TestToolMarketplace:
    """验证 tool_marketplace.py 在删除重复文件后仍完整。"""

    def test_tool_marketplace_file_exists(self):
        """tool_marketplace.py 文件存在。"""
        assert (_SRC_DIR / "services" / "tool_marketplace.py").exists()

    def test_tool_marketplace_service_not_exists(self):
        """tool_marketplace_service.py 已被删除。"""
        assert not (_SRC_DIR / "services" / "tool_marketplace_service.py").exists()

    def test_tool_marketplace_ast_valid(self):
        """tool_marketplace.py 语法正确，无 import 断链。"""
        content = (_SRC_DIR / "services" / "tool_marketplace.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(content)
        # 检查所有 import 的模块名
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "tool_marketplace_service" not in node.module, (
                    "tool_marketplace.py 不应引用已删除的 tool_marketplace_service"
                )

    def test_tool_marketplace_has_key_classes(self):
        """tool_marketplace.py 包含关键类定义。"""
        content = (_SRC_DIR / "services" / "tool_marketplace.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(content)
        class_names = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        ]
        assert "ToolCategory" in class_names, "缺少 ToolCategory 类"


# ===================================================================
# 5. Dockerfile 引用的文件都存在
# ===================================================================

class TestDockerfileReferences:
    """验证 Dockerfile 中 COPY 和 CMD 引用的文件都存在。"""

    def test_dockerfile_exists(self):
        """Dockerfile 存在。"""
        assert (_PROJECT_ROOT / "Dockerfile").exists()

    def test_dockerfile_copy_src_exists(self):
        """Dockerfile COPY src/ ./src/ — src 目录存在。"""
        assert _SRC_DIR.is_dir()

    def test_dockerfile_copy_config_exists(self):
        """Dockerfile COPY config/ ./config/ — config 目录存在。"""
        assert (_PROJECT_ROOT / "config").is_dir()

    def test_dockerfile_copy_conftest_exists(self):
        """Dockerfile COPY conftest.py ./ — conftest.py 存在。"""
        assert (_PROJECT_ROOT / "conftest.py").exists()

    def test_dockerfile_copy_app_factory_exists(self):
        """Dockerfile COPY app_factory.py — 存在。"""
        assert (_PROJECT_ROOT / "app_factory.py").exists()

    def test_dockerfile_copy_stream_handler_exists(self):
        """Dockerfile COPY stream_handler.py — 存在。"""
        assert (_PROJECT_ROOT / "stream_handler.py").exists()

    def test_dockerfile_copy_ws_handler_exists(self):
        """Dockerfile COPY ws_handler.py — 存在。"""
        assert (_PROJECT_ROOT / "ws_handler.py").exists()

    def test_dockerfile_copy_static_files_exists(self):
        """Dockerfile COPY static_files.py — 存在。"""
        assert (_PROJECT_ROOT / "static_files.py").exists()

    def test_dockerfile_copy_run_exists(self):
        """Dockerfile COPY run.py — 存在。"""
        assert (_PROJECT_ROOT / "run.py").exists()

    def test_dockerfile_copy_entrypoint_exists(self):
        """Dockerfile COPY docker-entrypoint.sh — 存在。"""
        assert (_PROJECT_ROOT / "docker-entrypoint.sh").exists()

    def test_dockerfile_cmd_references_app_factory(self):
        """Dockerfile CMD 使用 app_factory.py。"""
        content = (_PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "app_factory.py" in content, "Dockerfile CMD 未引用 app_factory.py"

    def test_dockerfile_no_import_start_server(self):
        """Dockerfile 不应引用已删除的 start_server.py。"""
        content = (_PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        # 仅检查 COPY/CMD 行，不检查注释
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("COPY") or stripped.startswith("CMD"):
                assert "start_server" not in stripped, (
                    f"Dockerfile 仍引用 start_server: {stripped}"
                )


# ===================================================================
# 6. tests/ 中 import 路径不受影响
# ===================================================================

class TestTestImports:
    """验证测试文件的 import 路径不受清理影响。"""

    @pytest.mark.parametrize(
        "test_file",
        [
            "test_start_server_refactor.py",
            "test_step3_gateway_and_globals.py",
        ],
        ids=["test_start_server_refactor", "test_step3_gateway_and_globals"],
    )
    def test_file_syntax_valid(self, test_file: str):
        """测试文件语法正确（AST 可解析）。"""
        path = _PROJECT_ROOT / "tests" / test_file
        if not path.exists():
            pytest.skip(f"{test_file} 不存在")
        content = path.read_text(encoding="utf-8")
        ast.parse(content)

    def test_no_import_of_deleted_errors_module(self):
        """如果 src.errors 已被删除，则没有测试文件导入它。"""
        errors_path = _SRC_DIR / "errors.py"
        if errors_path.exists():
            pytest.skip("src/errors.py 仍存在，无需检查残留引用")
        tests_dir = _PROJECT_ROOT / "tests"
        if not tests_dir.is_dir():
            pytest.skip("tests 目录不存在")
        this_file = Path(__file__).resolve()
        for py_file in tests_dir.glob("test_*.py"):
            if py_file.resolve() == this_file:
                continue
            content = py_file.read_text(encoding="utf-8")
            imports = _get_import_names(content)
            assert "src.errors" not in imports, (
                f"{py_file.name} 导入了已删除的 src.errors"
            )

    def test_no_import_of_deleted_start_server(self):
        """没有测试文件 import start_server 模块。"""
        tests_dir = _PROJECT_ROOT / "tests"
        if not tests_dir.is_dir():
            pytest.skip("tests 目录不存在")
        this_file = Path(__file__).resolve()
        for py_file in tests_dir.glob("test_*.py"):
            if py_file.resolve() == this_file:
                continue
            content = py_file.read_text(encoding="utf-8")
            imports = _get_import_names(content)
            assert "start_server" not in imports, (
                f"{py_file.name} 导入了已删除的 start_server"
            )

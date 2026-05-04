"""测试 start_server.py 改造为 Application 服务注入。

验证验收标准：
AC-1: start_server.py 不再包含 _build_services 函数定义
AC-2: start_server.py 通过 import 使用 Application 类
AC-3: PipelineEngine、TaskWorker 不再在 start_server.py 中直接实例化
AC-4: 服务器入口函数仍存在且可调用
AC-5: 与 CLI 通道的改造方式保持一致
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_START_SERVER_PATH = _PROJECT_ROOT / "start_server.py"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _read_start_server() -> str:
    """读取 start_server.py 的全部内容。"""
    return _START_SERVER_PATH.read_text(encoding="utf-8")


def _parse_module(source: str) -> ast.Module:
    """将 Python 源码解析为 AST。"""
    return ast.parse(source)


def _get_top_level_functions(source: str) -> set[str]:
    """获取模块顶层定义的函数名集合。"""
    tree = _parse_module(source)
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _get_top_level_imports(source: str) -> dict[str, str]:
    """获取模块顶层 import 的名称到模块路径的映射。

    Returns:
        dict: {别名: 模块路径或导入名}
    """
    tree = _parse_module(source)
    imports: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                imports[name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                imports[name] = f"{node.module}.{alias.name}" if node.module else alias.name
    return imports


def _get_direct_instantiations(source: str, class_name: str) -> list[str]:
    """查找源码中直接实例化指定类的行（排除注释和字符串）。

    Returns:
        匹配的源码行列表
    """
    lines = source.splitlines()
    results: list[str] = []
    for line in lines:
        stripped = line.strip()
        # 跳过注释和空行
        if stripped.startswith("#") or not stripped:
            continue
        # 检查是否有直接实例化 ClassName(
        if f"{class_name}(" in stripped:
            results.append(stripped)
    return results


# ---------------------------------------------------------------------------
# AC-1: _build_services 函数定义已删除
# ---------------------------------------------------------------------------


class TestAC1NoBuildServices:
    """AC-1: start_server.py 不再包含 _build_services 函数定义。"""

    def test_no_build_services_function_definition(self) -> None:
        """模块顶层不应存在 _build_services 函数定义。"""
        source = _read_start_server()
        top_funcs = _get_top_level_functions(source)
        assert "_build_services" not in top_funcs, (
            "_build_services 函数定义仍存在于 start_server.py 中，应该已删除"
        )

    def test_no_build_services_definition_in_source(self) -> None:
        """源码中不应有 'def _build_services' 的函数定义。"""
        source = _read_start_server()
        assert "def _build_services" not in source, (
            "start_server.py 中仍包含 'def _build_services' 定义"
        )

    def test_no_register_basic_tools_function_definition(self) -> None:
        """_register_basic_tools 也应被删除（它是 _build_services 的辅助函数）。"""
        source = _read_start_server()
        top_funcs = _get_top_level_functions(source)
        assert "_register_basic_tools" not in top_funcs, (
            "_register_basic_tools 函数定义仍存在，应随 _build_services 一起删除"
        )


# ---------------------------------------------------------------------------
# AC-2: 通过 import 使用 Application 类
# ---------------------------------------------------------------------------


class TestAC2ApplicationImport:
    """AC-2: start_server.py 通过 import 使用 Application 类。"""

    def test_application_imported(self) -> None:
        """start_server.py 应从 src.application 导入 Application 类。"""
        source = _read_start_server()
        imports = _get_top_level_imports(source)
        assert "Application" in imports, (
            "Application 类未在 start_server.py 中导入"
        )
        # 验证导入来源正确
        assert "application" in imports["Application"].lower(), (
            f"Application 应从 application 模块导入，实际导入路径: {imports['Application']}"
        )

    def test_application_used_in_init_pipeline(self) -> None:
        """_init_pipeline_context 应使用 Application 实例来构建服务。"""
        source = _read_start_server()
        # 应存在 Application( 的实例化调用
        assert "Application(" in source, (
            "start_server.py 中未找到 Application 类的实例化调用"
        )


# ---------------------------------------------------------------------------
# AC-3: PipelineEngine、TaskWorker 不再直接实例化
# ---------------------------------------------------------------------------


class TestAC3NoDirectInstantiation:
    """AC-3: PipelineEngine、TaskWorker 不再在 start_server.py 中直接实例化。"""

    def test_pipeline_engine_not_directly_instantiated(self) -> None:
        """不应存在 PipelineEngine( 的直接实例化（在 _init_pipeline_context 内）。"""
        source = _read_start_server()
        # 查找直接实例化
        direct_calls = _get_direct_instantiations(source, "PipelineEngine")
        # 过滤掉合法的引用（如类型注解、注释、文档字符串中的引用）
        # 在 _init_pipeline_context 中不应有 PipelineEngine( 调用
        # 但在 _stream_engine_response 等函数中也不会有
        # 直接创建 PipelineEngine 的代码应该已移除
        illegitimate = [
            line for line in direct_calls
            # 保留：如果这是通过 _app.create_pipeline_factory 返回的 lambda 中的调用
            # 那就不应该出现在 start_server.py 中了
            if "return PipelineEngine(" not in line
            or "def " in line  # 如果在函数定义行中出现，可能是误匹配
        ]
        # 改造后，PipelineEngine 的创建应该只通过 Application.create_pipeline_engine
        # _eval_pipeline_factory 也应通过 Application.create_pipeline_factory
        # 所以不应有直接 return PipelineEngine( 的情况
        assert len(direct_calls) == 0, (
            f"start_server.py 中仍存在 PipelineEngine 直接实例化: {direct_calls}"
        )

    def test_task_worker_not_directly_instantiated(self) -> None:
        """不应存在 TaskWorker( 的直接实例化。"""
        source = _read_start_server()
        direct_calls = _get_direct_instantiations(source, "TaskWorker")
        # 过滤注释
        illegitimate = [
            line for line in direct_calls
            if not line.startswith("#")
        ]
        assert len(illegitimate) == 0, (
            f"start_server.py 中仍存在 TaskWorker 直接实例化: {illegitimate}"
        )

    def test_uses_app_create_pipeline_engine(self) -> None:
        """应使用 _app.create_pipeline_engine() 来创建引擎。"""
        source = _read_start_server()
        assert "_app.create_pipeline_engine(" in source, (
            "未使用 _app.create_pipeline_engine() 创建 PipelineEngine"
        )

    def test_uses_app_create_task_worker(self) -> None:
        """应使用 _app.create_task_worker() 来创建 TaskWorker。"""
        source = _read_start_server()
        assert "_app.create_task_worker(" in source, (
            "未使用 _app.create_task_worker() 创建 TaskWorker"
        )

    def test_uses_app_create_pipeline_factory(self) -> None:
        """应使用 _app.create_pipeline_factory() 来创建 pipeline 工厂。"""
        source = _read_start_server()
        assert "_app.create_pipeline_factory(" in source, (
            "未使用 _app.create_pipeline_factory() 创建 pipeline 工厂"
        )

    def test_no_task_worker_import(self) -> None:
        """不应再有 'from infrastructure.task_worker import TaskWorker' 导入。"""
        source = _read_start_server()
        lines = source.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("from infrastructure.task_worker import"):
                assert "TaskWorker" not in stripped, (
                    f"第 {i} 行仍有 TaskWorker 直接导入: {stripped}"
                )

    def test_no_pipeline_engine_import(self) -> None:
        """不应再有 'from pipeline.engine import PipelineEngine' 导入。"""
        source = _read_start_server()
        lines = source.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("from pipeline.engine import"):
                assert "PipelineEngine" not in stripped, (
                    f"第 {i} 行仍有 PipelineEngine 直接导入: {stripped}"
                )


# ---------------------------------------------------------------------------
# AC-4: 服务器入口函数仍存在且可调用
# ---------------------------------------------------------------------------


class TestAC4EntryFunctionsExist:
    """AC-4: 服务器入口函数仍存在且可调用。"""

    def test_main_function_exists(self) -> None:
        """main() 函数应仍然存在。"""
        source = _read_start_server()
        top_funcs = _get_top_level_functions(source)
        assert "main" in top_funcs, "main() 函数不存在"

    def test_create_combined_app_exists(self) -> None:
        """create_combined_app() 函数应仍然存在。"""
        source = _read_start_server()
        top_funcs = _get_top_level_functions(source)
        assert "create_combined_app" in top_funcs, "create_combined_app() 函数不存在"

    def test_init_pipeline_context_exists(self) -> None:
        """_init_pipeline_context() 函数应仍然存在。"""
        source = _read_start_server()
        top_funcs = _get_top_level_functions(source)
        assert "_init_pipeline_context" in top_funcs, "_init_pipeline_context() 函数不存在"

    def test_pipeline_context_class_exists(self) -> None:
        """PipelineContext 类应仍然存在。"""
        source = _read_start_server()
        tree = _parse_module(source)
        class_names: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_names.add(node.name)
        assert "PipelineContext" in class_names, "PipelineContext 类不存在"

    def test_websocket_handler_functions_exist(self) -> None:
        """WebSocket 交互通知器和流式处理函数应保留。"""
        source = _read_start_server()
        top_funcs = _get_top_level_functions(source)
        # 关键函数应保留
        assert "_stream_engine_response" in top_funcs, (
            "_stream_engine_response 函数不存在"
        )
        assert "_stream_simulated_response" in top_funcs, (
            "_stream_simulated_response 函数不存在"
        )
        assert "_generate_simulated_reply" in top_funcs, (
            "_generate_simulated_reply 函数不存在"
        )


# ---------------------------------------------------------------------------
# AC-5: 与 CLI 通道的改造方式保持一致
# ---------------------------------------------------------------------------


class TestAC5ConsistentWithCLI:
    """AC-5: 与 CLI 通道的改造方式保持一致。"""

    def test_uses_application_build_services(self) -> None:
        """应通过 Application.build_services() 构建服务（与 CLI 一致）。"""
        source = _read_start_server()
        assert "_app.build_services(" in source, (
            "未使用 _app.build_services() 构建服务（与 CLI 改造方式不一致）"
        )

    def test_uses_application_create_pipeline_engine(self) -> None:
        """应通过 Application.create_pipeline_engine() 创建引擎（与 CLI 一致）。"""
        source = _read_start_server()
        assert "_app.create_pipeline_engine(" in source, (
            "未使用 _app.create_pipeline_engine()（与 CLI 改造方式不一致）"
        )

    def test_no_standalone_build_services(self) -> None:
        """不应有独立的服务构建逻辑（应全部委托给 Application）。"""
        source = _read_start_server()
        # 不应有手动创建 ToolRegistry 的代码（非注释、非文档字符串）
        lines = source.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if "ToolRegistry()" in stripped and "def " not in stripped:
                # 如果不是在 _build_services 或 _register_basic_tools 中
                # （这两个函数应该已被删除）
                pytest.fail(
                    f"第 {i} 行仍有直接创建 ToolRegistry() 的代码: {stripped}"
                )

"""测试 start_server.py 改造为 Application 服务注入（拆分后版本）。

验证验收标准：
AC-1: 拆分后模块不包含 _build_services 函数定义
AC-2: 子模块通过 import 使用 Application 类
AC-3: PipelineEngine、TaskWorker 不再直接实例化
AC-4: 服务器入口函数仍存在且可调用
AC-5: 与 CLI 通道的改造方式保持一致

拆分后文件关系（start_server.py 已删除）：
- app_factory.py: FastAPI 应用工厂和服务器入口
- stream_handler.py: 管道上下文和流式响应处理
- ws_handler.py: WebSocket 人类交互通知器
- static_files.py: 媒体静态文件挂载
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_FACTORY_PATH = _PROJECT_ROOT / "app_factory.py"
_STREAM_HANDLER_PATH = _PROJECT_ROOT / "stream_handler.py"

# 拆分后需要检查的源文件集合
_SPLIT_MODULES = [_APP_FACTORY_PATH, _STREAM_HANDLER_PATH]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _read_file(path: Path) -> str:
    """读取文件的全部内容。"""
    return path.read_text(encoding="utf-8")


def _read_app_factory() -> str:
    """读取 app_factory.py 的全部内容。"""
    return _read_file(_APP_FACTORY_PATH)


def _read_all_split_sources() -> str:
    """读取所有拆分子模块的源码合并。"""
    parts = []
    for path in _SPLIT_MODULES:
        if path.exists():
            parts.append(_read_file(path))
    return "\n".join(parts)


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


def _get_all_imports(source: str) -> dict[str, str]:
    """获取模块中所有 import（包括函数内部的导入）的名称到模块路径的映射。

    Returns:
        dict: {别名: 模块路径或导入名}
    """
    tree = _parse_module(source)
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
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
    """AC-1: 拆分后模块不包含 _build_services 函数定义。"""

    def test_no_build_services_in_app_factory(self) -> None:
        """app_factory.py 不应存在 _build_services 函数定义。"""
        source = _read_app_factory()
        top_funcs = _get_top_level_functions(source)
        assert "_build_services" not in top_funcs, (
            "_build_services 函数定义仍存在于 app_factory.py 中，应该已删除"
        )

    def test_no_build_services_definition_in_source(self) -> None:
        """源码中不应有 'def _build_services' 的函数定义。"""
        combined = _read_all_split_sources()
        assert "def _build_services" not in combined, (
            "拆分模块中仍包含 'def _build_services' 定义"
        )

    def test_no_register_basic_tools_function_definition(self) -> None:
        """_register_basic_tools 也应被删除（它是 _build_services 的辅助函数）。"""
        source = _read_app_factory()
        top_funcs = _get_top_level_functions(source)
        assert "_register_basic_tools" not in top_funcs, (
            "_register_basic_tools 函数定义仍存在，应随 _build_services 一起删除"
        )

    def test_split_modules_no_build_services(self) -> None:
        """拆分后的子模块也不应包含 _build_services 函数定义。"""
        for path in _SPLIT_MODULES:
            if path.exists():
                source = _read_file(path)
                assert "def _build_services" not in source, (
                    f"{path.name} 中仍包含 'def _build_services' 定义"
                )


# ---------------------------------------------------------------------------
# AC-2: 通过 import 使用 Application 类
# ---------------------------------------------------------------------------


class TestAC2ApplicationImport:
    """AC-2: 子模块通过 import 使用 Application 类。"""

    def test_application_imported(self) -> None:
        """stream_handler.py 应导入 Application 类。"""
        source = _read_file(_STREAM_HANDLER_PATH)
        imports = _get_all_imports(source)
        assert "Application" in imports, (
            "Application 类未在 stream_handler.py 中导入"
        )
        assert "application" in imports["Application"].lower(), (
            f"Application 应从 application 模块导入，实际导入路径: {imports['Application']}"
        )

    def test_application_used_in_init_pipeline(self) -> None:
        """stream_handler.py 中应使用 Application 实例来构建服务。"""
        source = _read_file(_STREAM_HANDLER_PATH)
        # 应存在 Application( 的实例化调用
        assert "Application(" in source, (
            "stream_handler.py 中未找到 Application 类的实例化调用"
        )


# ---------------------------------------------------------------------------
# AC-3: PipelineEngine、TaskWorker 不再直接实例化
# ---------------------------------------------------------------------------


class TestAC3NoDirectInstantiation:
    """AC-3: PipelineEngine、TaskWorker 不再直接实例化（应通过 Application 委托）。"""

    def test_pipeline_engine_not_directly_instantiated(self) -> None:
        """不应存在 PipelineEngine( 的直接实例化。"""
        combined = _read_all_split_sources()
        direct_calls = _get_direct_instantiations(combined, "PipelineEngine")
        # 过滤掉合法的引用（如注释、文档字符串、类型注解中的引用）
        illegitimate = [
            line for line in direct_calls
            if "return PipelineEngine(" not in line
            or "def " in line
        ]
        assert len(illegitimate) == 0, (
            f"拆分模块中仍存在 PipelineEngine 直接实例化: {illegitimate}"
        )

    def test_task_worker_not_directly_instantiated(self) -> None:
        """不应存在 TaskWorker( 的直接实例化。"""
        combined = _read_all_split_sources()
        direct_calls = _get_direct_instantiations(combined, "TaskWorker")
        # 过滤注释和日志消息
        illegitimate = [
            line for line in direct_calls
            if not line.startswith("#")
            and "logger" not in line
            and '"' not in line.split("TaskWorker")[0]
        ]
        assert len(illegitimate) == 0, (
            f"拆分模块中仍存在 TaskWorker 直接实例化: {illegitimate}"
        )

    def test_uses_app_create_pipeline_engine(self) -> None:
        """应使用 _app.create_pipeline_engine() 来创建引擎。"""
        source = _read_file(_STREAM_HANDLER_PATH)
        assert "_app.create_pipeline_engine(" in source, (
            "未使用 _app.create_pipeline_engine() 创建 PipelineEngine"
        )

    def test_uses_app_create_task_worker(self) -> None:
        """应使用 _app.create_task_worker() 来创建 TaskWorker。"""
        source = _read_file(_STREAM_HANDLER_PATH)
        assert "_app.create_task_worker(" in source, (
            "未使用 _app.create_task_worker() 创建 TaskWorker"
        )

    def test_uses_app_create_pipeline_factory(self) -> None:
        """应使用 _app.create_pipeline_factory() 来创建 pipeline 工厂。"""
        source = _read_file(_STREAM_HANDLER_PATH)
        assert "_app.create_pipeline_factory(" in source, (
            "未使用 _app.create_pipeline_factory() 创建 pipeline 工厂"
        )

    def test_no_task_worker_import(self) -> None:
        """app_factory.py 不应再有 'from infrastructure.task_worker import TaskWorker' 导入。"""
        source = _read_app_factory()
        lines = source.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("from infrastructure.task_worker import"):
                assert "TaskWorker" not in stripped, (
                    f"第 {i} 行仍有 TaskWorker 直接导入: {stripped}"
                )

    def test_no_pipeline_engine_import(self) -> None:
        """app_factory.py 不应再有 'from pipeline.engine import PipelineEngine' 导入。"""
        source = _read_app_factory()
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
        """main() 函数应存在于 app_factory.py 中。"""
        source = _read_file(_APP_FACTORY_PATH)
        top_funcs = _get_top_level_functions(source)
        assert "main" in top_funcs, "main() 函数不存在于 app_factory.py"

    def test_create_combined_app_exists(self) -> None:
        """create_combined_app() 函数应存在于 app_factory.py 中。"""
        source = _read_file(_APP_FACTORY_PATH)
        top_funcs = _get_top_level_functions(source)
        assert "create_combined_app" in top_funcs, "create_combined_app() 函数不存在于 app_factory.py"

    def test_init_pipeline_context_exists(self) -> None:
        """_init_pipeline_context() 函数应存在于 stream_handler.py 中。"""
        source = _read_file(_STREAM_HANDLER_PATH)
        top_funcs = _get_top_level_functions(source)
        assert "_init_pipeline_context" in top_funcs, (
            "_init_pipeline_context() 函数不存在于 stream_handler.py"
        )

    def test_pipeline_context_class_exists(self) -> None:
        """PipelineContext 类应存在于 stream_handler.py 中。"""
        source = _read_file(_STREAM_HANDLER_PATH)
        tree = _parse_module(source)
        class_names: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_names.add(node.name)
        assert "PipelineContext" in class_names, "PipelineContext 类不存在于 stream_handler.py"

    def test_websocket_handler_functions_exist(self) -> None:
        """WebSocket 交互通知器和流式处理函数应保留在子模块中。"""
        stream_source = _read_file(_STREAM_HANDLER_PATH)
        stream_funcs = _get_top_level_functions(stream_source)
        assert "handle_stream_request" in stream_funcs, (
            "handle_stream_request 函数不存在于 stream_handler.py"
        )
        tree = _parse_module(stream_source)
        class_names: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_names.add(node.name)
        assert "StreamContext" in class_names, (
            "StreamContext 类不存在于 stream_handler.py"
        )


# ---------------------------------------------------------------------------
# AC-5: 与 CLI 通道的改造方式保持一致
# ---------------------------------------------------------------------------


class TestAC5ConsistentWithCLI:
    """AC-5: 与 CLI 通道的改造方式保持一致。"""

    def test_uses_application_build_services(self) -> None:
        """应通过 Application.build_services() 构建服务（与 CLI 一致）。"""
        source = _read_file(_STREAM_HANDLER_PATH)
        assert "_app.build_services(" in source, (
            "未使用 _app.build_services() 构建服务（与 CLI 改造方式不一致）"
        )

    def test_uses_application_create_pipeline_engine(self) -> None:
        """应通过 Application.create_pipeline_engine() 创建引擎（与 CLI 一致）。"""
        source = _read_file(_STREAM_HANDLER_PATH)
        assert "_app.create_pipeline_engine(" in source, (
            "未使用 _app.create_pipeline_engine()（与 CLI 改造方式不一致）"
        )

    def test_no_standalone_build_services(self) -> None:
        """不应有独立的服务构建逻辑（应全部委托给 Application）。"""
        for path in _SPLIT_MODULES:
            if not path.exists():
                continue
            source = _read_file(path)
            lines = source.splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                if "ToolRegistry()" in stripped and "def " not in stripped:
                    pytest.fail(
                        f"{path.name} 第 {i} 行仍有直接创建 ToolRegistry() 的代码: {stripped}"
                    )

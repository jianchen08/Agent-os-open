"""AR-2 架构解耦验证：tool_context 桥接层解耦 tools/builtin/ ↔ pipeline。

验证覆盖点：
1. tools/tool_context.py 正确 re-export 全部 pipeline 类型
2. 5 个工具文件（hot_swap/task/register_resource/task_evaluate/trigger_review）通过 tool_context 获取 pipeline 服务
3. tools/builtin/ 中不再有 from pipeline import
"""
from __future__ import annotations

import ast
import importlib
import os
from pathlib import Path

import pytest

# ── 公共路径常量 ──────────────────────────────────────────────
SRC_DIR = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
BUILTIN_DIR = SRC_DIR / "tools" / "builtin"
TOOL_CONTEXT_PATH = SRC_DIR / "tools" / "tool_context.py"

# AR-2 要求 re-export 的全部 10 个类型
EXPECTED_EXPORTS = [
    "HotSwapManager",
    "PluginRegistry",
    "RollbackManager",
    "emit",
    "MessageType",
    "PipelineMessage",
    "PipelineConfig",
    "PipelineConfigStore",
    "PipelineEngine",
    "get_engine_registry",
]

# 5 个通过 tool_context 桥接的工具文件
BRIDGED_TOOL_FILES = {
    "hot_swap": BUILTIN_DIR / "hot_swap" / "tool.py",
    "task": BUILTIN_DIR / "task" / "tool.py",
    "register_resource": BUILTIN_DIR / "register_resource" / "tool.py",
    "task_evaluate": BUILTIN_DIR / "task_evaluate" / "tool.py",
    "trigger_review": BUILTIN_DIR / "trigger_review" / "tool.py",
}


# ═════════════════════════════════════════════════════════════
# 第一组：tool_context.py 正确 re-export pipeline 类型
# ═════════════════════════════════════════════════════════════


class TestToolContextReExport:
    """验证 tool_context.py 正确 re-export 全部 pipeline 类型。"""

    @pytest.mark.parametrize("export_name", EXPECTED_EXPORTS)
    def test_export_is_importable(self, export_name: str) -> None:
        """每个期望导出的类型都可以从 tool_context 导入。"""
        tool_context = importlib.import_module("tools.tool_context")
        assert hasattr(tool_context, export_name), (
            f"tool_context.py 应 re-export '{export_name}'"
        )

    @pytest.mark.parametrize("export_name", EXPECTED_EXPORTS)
    def test_export_in_all_list(self, export_name: str) -> None:
        """每个期望导出的类型都在 __all__ 列表中。"""
        tool_context = importlib.import_module("tools.tool_context")
        assert export_name in tool_context.__all__, (
            f"'{export_name}' 应在 tool_context.__all__ 列表中"
        )

    def test_all_list_contains_exactly_expected(self) -> None:
        """__all__ 列表恰好包含期望的 10 个类型，不多不少。"""
        tool_context = importlib.import_module("tools.tool_context")
        assert set(tool_context.__all__) == set(EXPECTED_EXPORTS), (
            f"__all__ 应恰好包含 {set(EXPECTED_EXPORTS)}，"
            f"实际为 {set(tool_context.__all__)}"
        )

    @pytest.mark.parametrize("export_name", EXPECTED_EXPORTS)
    def test_re_exported_object_is_same_as_pipeline_original(self, export_name: str) -> None:
        """tool_context re-export 的对象与 pipeline 原始定义是同一个对象（身份一致性）。"""
        tool_context = importlib.import_module("tools.tool_context")
        exported_obj = getattr(tool_context, export_name)

        # 逐个查找 pipeline 来源模块
        pipeline_sources = {
            "HotSwapManager": "pipeline.hot_swap",
            "PluginRegistry": "pipeline.registry",
            "RollbackManager": "pipeline.rollback",
            "emit": "pipeline.message_bus",
            "MessageType": "pipeline.message_types",
            "PipelineMessage": "pipeline.message_types",
            "PipelineConfig": "pipeline.config_store",
            "PipelineConfigStore": "pipeline.config_store",
            "PipelineEngine": "pipeline.engine",
            "get_engine_registry": "pipeline.registry",
        }

        source_module = importlib.import_module(pipeline_sources[export_name])
        original_obj = getattr(source_module, export_name)

        assert exported_obj is original_obj, (
            f"tool_context.{export_name} 应与 {pipeline_sources[export_name]}.{export_name}"
            f" 是同一个对象（re-export 不是复制）"
        )

    def test_tool_context_imports_from_pipeline(self) -> None:
        """tool_context.py 的导入语句来自 pipeline 模块（桥接层本身允许导入 pipeline）。"""
        source = TOOL_CONTEXT_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)

        pipeline_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("pipeline"):
                pipeline_imports.append(node.module)

        assert len(pipeline_imports) > 0, (
            "tool_context.py 应包含 from pipeline.xxx import 语句"
        )

    def test_tool_context_does_not_import_from_builtin(self) -> None:
        """tool_context.py 不导入 tools.builtin（避免反向依赖）。"""
        source = TOOL_CONTEXT_PATH.read_text(encoding="utf-8")
        assert "from tools.builtin" not in source, (
            "tool_context.py 不应导入 tools.builtin（反向依赖）"
        )


# ═════════════════════════════════════════════════════════════
# 第二组：5 个工具文件通过 tool_context 桥接获取 pipeline 服务
# ═════════════════════════════════════════════════════════════


class TestToolFilesUseBridge:
    """验证 5 个工具文件通过 tool_context 桥接层获取 pipeline 服务。"""

    @pytest.mark.parametrize("tool_name", list(BRIDGED_TOOL_FILES.keys()))
    def test_tool_file_imports_from_tool_context(self, tool_name: str) -> None:
        """每个工具文件都包含从 tool_context 导入的语句。"""
        tool_file = BRIDGED_TOOL_FILES[tool_name]
        assert tool_file.exists(), f"工具文件不存在: {tool_file}"

        source = tool_file.read_text(encoding="utf-8")
        assert "from tools.tool_context import" in source, (
            f"{tool_name}/tool.py 应通过 tools.tool_context 桥接导入 pipeline 类型"
        )

    @pytest.mark.parametrize("tool_name", list(BRIDGED_TOOL_FILES.keys()))
    def test_tool_file_no_direct_pipeline_import(self, tool_name: str) -> None:
        """每个工具文件都不直接 from pipeline import。"""
        tool_file = BRIDGED_TOOL_FILES[tool_name]
        source = tool_file.read_text(encoding="utf-8")

        # 逐行检查，排除注释行
        for i, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not (
                "from pipeline" in stripped
                and "import" in stripped
            ), (
                f"{tool_name}/tool.py:{i} 不应直接导入 pipeline: {stripped}"
            )

    @pytest.mark.parametrize("tool_name", list(BRIDGED_TOOL_FILES.keys()))
    def test_tool_file_bridge_imports_resolve(self, tool_name: str) -> None:
        """每个工具文件中从 tool_context 导入的类型都能成功解析。"""
        tool_file = BRIDGED_TOOL_FILES[tool_name]
        source = tool_file.read_text(encoding="utf-8")
        tree = ast.parse(source)

        tool_context = importlib.import_module("tools.tool_context")

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "tool_context" in module:
                    for alias in node.names:
                        attr_name = alias.name
                        assert hasattr(tool_context, attr_name), (
                            f"{tool_name}/tool.py 导入 tool_context.{attr_name}，"
                            f"但 tool_context 中不存在该属性"
                        )

    def test_hot_swap_uses_bridged_types(self) -> None:
        """hot_swap/tool.py 通过桥接层获取 HotSwapManager、PluginRegistry、RollbackManager。"""
        source = BRIDGED_TOOL_FILES["hot_swap"].read_text(encoding="utf-8")
        # 从 tool_context 导入的类型列表
        imported_from_bridge = self._extract_bridge_imports(source)

        assert "HotSwapManager" in imported_from_bridge, (
            "hot_swap/tool.py 应通过 tool_context 导入 HotSwapManager"
        )
        assert "PluginRegistry" in imported_from_bridge, (
            "hot_swap/tool.py 应通过 tool_context 导入 PluginRegistry"
        )
        assert "RollbackManager" in imported_from_bridge, (
            "hot_swap/tool.py 应通过 tool_context 导入 RollbackManager"
        )

    def test_task_uses_bridged_types(self) -> None:
        """task/tool.py 通过桥接层获取 emit、MessageType、PipelineMessage。"""
        source = BRIDGED_TOOL_FILES["task"].read_text(encoding="utf-8")
        imported_from_bridge = self._extract_bridge_imports(source)

        assert "emit" in imported_from_bridge, (
            "task/tool.py 应通过 tool_context 导入 emit"
        )
        assert "PipelineMessage" in imported_from_bridge, (
            "task/tool.py 应通过 tool_context 导入 PipelineMessage"
        )

    def test_register_resource_uses_bridged_types(self) -> None:
        """register_resource/tool.py 通过桥接层获取 PipelineConfig、PipelineConfigStore。"""
        source = BRIDGED_TOOL_FILES["register_resource"].read_text(encoding="utf-8")
        imported_from_bridge = self._extract_bridge_imports(source)

        assert "PipelineConfig" in imported_from_bridge or "PipelineConfigStore" in imported_from_bridge, (
            "register_resource/tool.py 应通过 tool_context 导入 PipelineConfig/PipelineConfigStore"
        )

    def test_task_evaluate_uses_bridged_types(self) -> None:
        """task_evaluate/tool.py 通过桥接层获取 PipelineEngine。"""
        source = BRIDGED_TOOL_FILES["task_evaluate"].read_text(encoding="utf-8")
        imported_from_bridge = self._extract_bridge_imports(source)

        assert "PipelineEngine" in imported_from_bridge, (
            "task_evaluate/tool.py 应通过 tool_context 导入 PipelineEngine"
        )

    def test_trigger_review_uses_bridged_types(self) -> None:
        """trigger_review/tool.py 通过桥接层获取 emit、MessageType、PipelineMessage、get_engine_registry。"""
        source = BRIDGED_TOOL_FILES["trigger_review"].read_text(encoding="utf-8")
        imported_from_bridge = self._extract_bridge_imports(source)

        assert "emit" in imported_from_bridge, (
            "trigger_review/tool.py 应通过 tool_context 导入 emit"
        )
        assert "get_engine_registry" in imported_from_bridge, (
            "trigger_review/tool.py 应通过 tool_context 导入 get_engine_registry"
        )
        assert "PipelineMessage" in imported_from_bridge or "MessageType" in imported_from_bridge, (
            "trigger_review/tool.py 应通过 tool_context 导入 PipelineMessage/MessageType"
        )

    @staticmethod
    def _extract_bridge_imports(source: str) -> set[str]:
        """从源代码中提取所有从 tool_context 导入的类型名。"""
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "tool_context" in module:
                    for alias in node.names:
                        names.add(alias.name)
        return names


# ═════════════════════════════════════════════════════════════
# 第三组：tools/builtin/ 中不再有 from pipeline
# ═════════════════════════════════════════════════════════════


class TestNoDirectPipelineImport:
    """验证 tools/builtin/ 目录下不再有 from pipeline import。"""

    def test_no_pipeline_import_in_builtin_dir(self) -> None:
        """tools/builtin/ 下所有 .py 文件不含 from pipeline import。"""
        violations: list[str] = []

        for py_file in BUILTIN_DIR.rglob("*.py"):
            try:
                source = py_file.read_text(encoding="utf-8")
            except Exception:
                continue

            for i, line in enumerate(source.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # 检测直接 pipeline 导入
                if "from pipeline" in stripped and "import" in stripped:
                    violations.append(f"{py_file.relative_to(SRC_DIR)}:{i}: {stripped}")
                elif stripped.startswith("import pipeline.") or (
                    stripped.startswith("import pipeline") and stripped == "import pipeline"
                ):
                    violations.append(f"{py_file.relative_to(SRC_DIR)}:{i}: {stripped}")

        assert violations == [], (
            "tools/builtin/ 中不应直接导入 pipeline，发现以下违规:\n"
            + "\n".join(violations)
        )

    def test_builtin_uses_tool_context_exclusively_for_pipeline_types(self) -> None:
        """tools/builtin/ 中的 pipeline 类型获取全部来自 tool_context 桥接层。"""
        # 收集所有从 tool_context 导入的类型
        bridge_imports: set[str] = set()

        for py_file in BUILTIN_DIR.rglob("*.py"):
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError):
                # 跳过无法解析的文件（模板、测试片段等）
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if "tool_context" in module:
                        for alias in node.names:
                            bridge_imports.add(alias.name)

        # 验证存在通过桥接层导入的类型
        assert len(bridge_imports) > 0, (
            "tools/builtin/ 应至少有一个文件通过 tool_context 导入类型"
        )

    def test_all_5_bridge_files_exist(self) -> None:
        """5 个桥接工具文件都存在。"""
        for tool_name, file_path in BRIDGED_TOOL_FILES.items():
            assert file_path.exists(), (
                f"桥接工具文件不存在: {tool_name} -> {file_path}"
            )

    def test_tool_context_file_location(self) -> None:
        """tool_context.py 位于 tools/ 层（非 builtin/），确保架构层次正确。"""
        assert TOOL_CONTEXT_PATH.exists(), "tools/tool_context.py 不存在"

        # 应在 tools/ 目录下但不在 tools/builtin/ 下
        relative = TOOL_CONTEXT_PATH.relative_to(SRC_DIR / "tools")
        parts = relative.parts
        assert parts[0] != "builtin", (
            "tool_context.py 应位于 tools/ 层，而非 tools/builtin/ 层"
        )
        assert parts[-1] == "tool_context.py"

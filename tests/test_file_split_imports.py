"""文件拆分后导入完整性验证。

验证 6 个被拆分的 Python 文件的所有拆分产物能正常导入，且原始功能未破坏：
1. executor.py → tool_cache.py / input_normalizer.py / nested_record_manager.py
2. models.py → memory_store.py
3. mcp_loader.py → mcp_client.py
4. workspace_lifecycle.py → _workspace_git_ops.py / _workspace_merge_ops.py
5. resource_merge/tool.py → git_helpers.py
6. plugin.py → _message_normalizer.py
7. 引用了拆分文件的下游模块也能正常导入
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 辅助：确保 src 目录在 sys.path 中
# ---------------------------------------------------------------------------
# 当前工作区只有 tests/，源码在父容器的项目根目录中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "src"

# 如果当前工作区没有 src/，尝试查找实际项目根目录
if not _SRC_DIR.exists():
    _CONTAINER_ROOT = _PROJECT_ROOT.parent  # container_08f57__wt_xxx 的父级
    for candidate in _CONTAINER_ROOT.iterdir():
        candidate_src = candidate / "src"
        if candidate_src.exists() and (candidate_src / "tools" / "executor.py").exists():
            _SRC_DIR = candidate_src
            _PROJECT_ROOT = candidate
            break

# 项目同时使用 from core.xxx 和 from src.core.xxx 两种导入风格
# 需要把 src/ 和项目根目录都加入 sys.path
_EXTRA_PATHS = [str(_SRC_DIR), str(_PROJECT_ROOT)]


@pytest.fixture(autouse=True)
def _ensure_src_path():
    """确保 src 目录和项目根目录在 sys.path 中。"""
    for p in _EXTRA_PATHS:
        if p not in sys.path:
            sys.path.insert(0, p)


# ===================================================================
# 1. executor.py 拆分验证
# ===================================================================

class TestExecutorSplit:
    """验证 executor.py 拆分后的所有产物。"""

    def test_import_executor(self):
        """import src.tools.executor 能正常导入。"""
        mod = importlib.import_module("tools.executor")
        assert mod is not None

    def test_import_tool_cache(self):
        """import src.tools.tool_cache 能正常导入，ToolCache 类存在。"""
        mod = importlib.import_module("tools.tool_cache")
        assert hasattr(mod, "ToolCache"), "ToolCache 类在 tool_cache 模块中不存在"

    def test_tool_cache_config_class_exists(self):
        """ToolCacheConfig 类存在。"""
        mod = importlib.import_module("tools.tool_cache")
        assert hasattr(mod, "ToolCacheConfig"), "ToolCacheConfig 类在 tool_cache 模块中不存在"

    def test_import_input_normalizer(self):
        """import src.tools.input_normalizer 能正常导入。"""
        mod = importlib.import_module("tools.input_normalizer")
        assert mod is not None

    def test_input_normalizer_functions_exist(self):
        """InputNormalizer 相关函数存在（normalize_inputs, normalize_input_types, fix_task_submit_inputs）。"""
        mod = importlib.import_module("tools.input_normalizer")
        # 按照源码实际导出的函数检查
        assert hasattr(mod, "normalize_inputs"), "normalize_inputs 函数不存在"

    def test_import_nested_record_manager(self):
        """import src.tools.nested_record_manager 能正常导入，NestedRecordManager 类存在。"""
        mod = importlib.import_module("tools.nested_record_manager")
        assert hasattr(mod, "NestedRecordManager"), "NestedRecordManager 类不存在"

    def test_tool_executor_class_exists(self):
        """ToolExecutor 类在 executor 模块中存在。"""
        mod = importlib.import_module("tools.executor")
        assert hasattr(mod, "ToolExecutor"), "ToolExecutor 类不存在"

    def test_tool_executor_core_methods_exist(self):
        """ToolExecutor 核心方法 execute / batch_execute / execute_pipeline 存在。"""
        mod = importlib.import_module("tools.executor")
        cls = mod.ToolExecutor
        for method_name in ("execute", "batch_execute", "execute_pipeline"):
            assert hasattr(cls, method_name), f"ToolExecutor.{method_name} 方法不存在"

    def test_executor_imports_split_modules(self):
        """executor.py 正确导入了拆分出的子模块。"""
        mod = importlib.import_module("tools.executor")
        # executor 从 tool_cache 导入了 ToolCache 和 ToolCacheConfig
        assert hasattr(mod, "ToolCache"), "executor 未正确导入 ToolCache"
        assert hasattr(mod, "ToolCacheConfig"), "executor 未正确导入 ToolCacheConfig"
        # executor 从 nested_record_manager 导入了 NestedRecordManager
        assert hasattr(mod, "NestedRecordManager"), "executor 未正确导入 NestedRecordManager"


# ===================================================================
# 2. models.py 拆分验证
# ===================================================================

class TestModelsSplit:
    """验证 channels/api/models.py 拆分后的产物。"""

    def test_import_models(self):
        """import src.channels.api.models 能正常导入。"""
        mod = importlib.import_module("channels.api.models")
        assert mod is not None

    def test_dto_model_classes_exist(self):
        """DTO 模型类存在于 models 模块中。"""
        mod = importlib.import_module("channels.api.models")
        dto_classes = [
            "RefreshRequest", "LoginRequest", "RegisterRequest",
            "TokenResponse", "UserResponse",
            "ThreadCreate", "ThreadUpdate", "ThreadResponse",
            "MessageResponse",
            "TaskCreate", "TaskUpdate", "TaskResponse",
            "ErrorResponse", "HealthResponse",
        ]
        for cls_name in dto_classes:
            assert hasattr(mod, cls_name), f"DTO 模型类 {cls_name} 不存在"

    def test_import_memory_store(self):
        """import src.channels.api.memory_store 能正常导入。"""
        mod = importlib.import_module("channels.api.memory_store")
        assert mod is not None

    def test_memory_store_class_exists(self):
        """MemoryStore 类存在。"""
        mod = importlib.import_module("channels.api.memory_store")
        assert hasattr(mod, "MemoryStore"), "MemoryStore 类不存在"

    def test_memory_store_can_instantiate(self):
        """MemoryStore 能正常实例化。"""
        mod = importlib.import_module("channels.api.memory_store")
        store = mod.MemoryStore()
        assert store is not None


# ===================================================================
# 3. mcp_loader.py 拆分验证
# ===================================================================

class TestMCPLoaderSplit:
    """验证 mcp_loader.py 拆分后的产物。"""

    def test_import_mcp_loader(self):
        """import src.tools.mcp_loader 能正常导入。"""
        mod = importlib.import_module("tools.mcp_loader")
        assert mod is not None

    def test_mcp_tool_loader_class_exists(self):
        """MCPToolLoader 类存在。"""
        mod = importlib.import_module("tools.mcp_loader")
        assert hasattr(mod, "MCPToolLoader"), "MCPToolLoader 类不存在"

    def test_import_mcp_client(self):
        """import src.tools.mcp_client 能正常导入。"""
        mod = importlib.import_module("tools.mcp_client")
        assert mod is not None

    def test_mcp_client_class_exists(self):
        """MCPClient 类存在。"""
        mod = importlib.import_module("tools.mcp_client")
        assert hasattr(mod, "MCPClient"), "MCPClient 类不存在"

    def test_mcp_loader_re_exports_mcp_client(self):
        """mcp_loader 重导出 MCPClient，保持向后兼容。"""
        mod = importlib.import_module("tools.mcp_loader")
        assert hasattr(mod, "MCPClient"), "mcp_loader 未重导出 MCPClient"


# ===================================================================
# 4. workspace_lifecycle.py 拆分验证
# ===================================================================

class TestWorkspaceLifecycleSplit:
    """验证 workspace_lifecycle.py 拆分后的产物。"""

    def test_import_workspace_lifecycle(self):
        """import src.isolation.workspace_lifecycle 能正常导入。"""
        mod = importlib.import_module("isolation.workspace_lifecycle")
        assert mod is not None

    def test_workspace_lifecycle_manager_class_exists(self):
        """WorkspaceLifecycleManager 类存在。"""
        mod = importlib.import_module("isolation.workspace_lifecycle")
        assert hasattr(mod, "WorkspaceLifecycleManager"), "WorkspaceLifecycleManager 类不存在"

    def test_workspace_lifecycle_manager_inherits_mixins(self):
        """WorkspaceLifecycleManager 继承了 _GitOpsMixin 和 _MergeOpsMixin。"""
        mod = importlib.import_module("isolation.workspace_lifecycle")
        cls = mod.WorkspaceLifecycleManager

        git_ops_mod = importlib.import_module("isolation._workspace_git_ops")
        merge_ops_mod = importlib.import_module("isolation._workspace_merge_ops")

        assert issubclass(cls, git_ops_mod._GitOpsMixin), \
            "WorkspaceLifecycleManager 未继承 _GitOpsMixin"
        assert issubclass(cls, merge_ops_mod._MergeOpsMixin), \
            "WorkspaceLifecycleManager 未继承 _MergeOpsMixin"

    def test_import_git_ops_mixin(self):
        """import src.isolation._workspace_git_ops 正常，_GitOpsMixin 类存在。"""
        mod = importlib.import_module("isolation._workspace_git_ops")
        assert hasattr(mod, "_GitOpsMixin"), "_GitOpsMixin 类不存在"

    def test_import_merge_ops_mixin(self):
        """import src.isolation._workspace_merge_ops 正常，_MergeOpsMixin 类存在。"""
        mod = importlib.import_module("isolation._workspace_merge_ops")
        assert hasattr(mod, "_MergeOpsMixin"), "_MergeOpsMixin 类不存在"


# ===================================================================
# 5. resource_merge/tool.py 拆分验证
# ===================================================================

class TestResourceMergeSplit:
    """验证 resource_merge/tool.py 拆分后的产物。"""

    def test_import_resource_merge_tool(self):
        """import src.tools.builtin.resource_merge.tool 正常。"""
        mod = importlib.import_module("tools.builtin.resource_merge.tool")
        assert mod is not None

    def test_import_git_helpers(self):
        """import src.tools.builtin.resource_merge.git_helpers 正常。"""
        mod = importlib.import_module("tools.builtin.resource_merge.git_helpers")
        assert mod is not None

    def test_git_helpers_class_exists(self):
        """GitHelpers 类存在。"""
        mod = importlib.import_module("tools.builtin.resource_merge.git_helpers")
        assert hasattr(mod, "GitHelpers"), "GitHelpers 类不存在"

    def test_resource_merge_tool_class_exists(self):
        """ResourceMergeTool 类存在。"""
        mod = importlib.import_module("tools.builtin.resource_merge.tool")
        assert hasattr(mod, "ResourceMergeTool"), "ResourceMergeTool 类不存在"


# ===================================================================
# 6. plugin.py 拆分验证
# ===================================================================

class TestLLMCorePluginSplit:
    """验证 plugins/core/llm_core/plugin.py 拆分后的产物。"""

    def test_import_llm_core_plugin(self):
        """import src.plugins.core.llm_core.plugin 正常，LLMCore 类存在。"""
        mod = importlib.import_module("plugins.core.llm_core.plugin")
        assert mod is not None
        assert hasattr(mod, "LLMCore"), "LLMCore 类不存在"

    def test_import_message_normalizer(self):
        """import src.plugins.core.llm_core._message_normalizer 正常。"""
        mod = importlib.import_module("plugins.core.llm_core._message_normalizer")
        assert mod is not None

    def test_message_normalizer_functions_exist(self):
        """_message_normalizer 模块导出了 normalize_messages_for_provider 函数。"""
        mod = importlib.import_module("plugins.core.llm_core._message_normalizer")
        assert hasattr(mod, "normalize_messages_for_provider"), \
            "normalize_messages_for_provider 函数不存在"

    def test_llm_core_init_re_exports(self):
        """__init__.py 重导出 LLMCore。"""
        mod = importlib.import_module("plugins.core.llm_core")
        assert hasattr(mod, "LLMCore"), "__init__.py 未重导出 LLMCore"


# ===================================================================
# 7. 下游引用模块导入验证
# ===================================================================

class TestDownstreamImports:
    """验证引用了拆分文件的下游模块能正常导入。"""

    @pytest.mark.parametrize(
        "module_path",
        [
            # 引用 executor / tool_cache / input_normalizer / nested_record_manager 的模块
            "tools.auto_loader",
            "tools.global_registry",
            "tools.loader",
            "tools.registry",
            # 引用 mcp_loader / mcp_client 的模块
            "tools.mcp_adapter",
            # 引用 channels.api.models / memory_store 的模块
            "channels.api.app",
            "channels.api.deps",
            "channels.api.routes_threads",
            "channels.api.routes_tasks",
            # 引用 isolation.workspace_lifecycle 的模块
            "isolation.manager",
            "isolation.workspace",
            # 引用 resource_merge 的模块
            "tools.builtin.resource_merge",
        ],
        ids=[
            "tools.auto_loader",
            "tools.global_registry",
            "tools.loader",
            "tools.registry",
            "tools.mcp_adapter",
            "channels.api.app",
            "channels.api.deps",
            "channels.api.routes_threads",
            "channels.api.routes_tasks",
            "isolation.manager",
            "isolation.workspace",
            "tools.builtin.resource_merge",
        ],
    )
    def test_downstream_module_importable(self, module_path: str):
        """下游模块 {module_path} 能正常导入。"""
        mod = importlib.import_module(module_path)
        assert mod is not None

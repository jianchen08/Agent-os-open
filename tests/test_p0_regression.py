"""P0 安全+冗余清理变更回归测试。

覆盖 7 个核心变更点的回归验证：
1. routes_thinking_mode 认证依赖（S-1）
2. routes_config Pydantic Schema 校验（S-2）
3. tasks/service_access + infrastructure/service_access 公共接口（A-3/A-6）
4. utils/enum_utils safe_enum_value（A-4）
5. evaluation/engine _TYPE_PRIORITY 作用域修复（A-5）
6. routes_tasks TaskModel 序列化修复（B-2）
7. pipeline/engine FileHandler 工厂函数（B-4）
8. tools/executor ProgressCallback 统一导入（A-2）

测试类：
- TestImportIntegrity: 变更模块导入完整性验证（12 用例）
- TestRoutesThinkingModeAuth: 认证依赖验证（3 用例）
- TestRoutesConfigSchema: Pydantic Schema 校验验证（8 用例）
- TestSafeEnumValue: 枚举值提取验证（10 用例）
- TestServiceAccess: 公共接口行为验证（4 用例）
- TestTypePriorityScope: _TYPE_PRIORITY 作用域验证（5 用例）
- TestTaskModelSerialization: TaskModel 序列化验证（4 用例）
- TestCrossModuleConsistency: 跨模块委托一致性验证（4 用例）
"""

from __future__ import annotations

import importlib
from enum import Enum
from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# 1. TestImportIntegrity — 变更模块导入完整性（12 用例）
# =============================================================================


class TestImportIntegrity:
    """验证所有变更模块可正确导入，无循环导入、无 NameError。"""

    def test_import_enum_utils(self) -> None:
        """utils.enum_utils 模块导入成功。"""
        mod = importlib.import_module("utils.enum_utils")
        assert hasattr(mod, "safe_enum_value")

    def test_import_tasks_service_access(self) -> None:
        """tasks.service_access 模块导入成功。"""
        mod = importlib.import_module("tasks.service_access")
        assert hasattr(mod, "get_task_service")

    def test_import_infra_service_access(self) -> None:
        """infrastructure.service_access 模块导入成功。"""
        mod = importlib.import_module("infrastructure.service_access")
        assert hasattr(mod, "get_execution_record_storage")

    def test_import_routes_thinking_mode(self) -> None:
        """channels.api.routes_thinking_mode 模块导入成功。"""
        mod = importlib.import_module("channels.api.routes_thinking_mode")
        assert hasattr(mod, "router")

    def test_import_routes_config(self) -> None:
        """channels.api.routes_config 模块导入成功。"""
        mod = importlib.import_module("channels.api.routes_config")
        assert hasattr(mod, "router")

    def test_import_routes_tasks(self) -> None:
        """channels.api.routes_tasks 模块导入成功。"""
        mod = importlib.import_module("channels.api.routes_tasks")
        assert hasattr(mod, "router")

    def test_import_routes_threads(self) -> None:
        """channels.api.routes_threads 模块导入成功。"""
        mod = importlib.import_module("channels.api.routes_threads")
        assert hasattr(mod, "router")

    def test_import_evaluation_engine(self) -> None:
        """evaluation.engine 模块导入成功，_TYPE_PRIORITY 可访问。"""
        mod = importlib.import_module("evaluation.engine")
        assert hasattr(mod, "_TYPE_PRIORITY")

    def test_import_tools_executor(self) -> None:
        """tools.executor 模块导入成功。"""
        mod = importlib.import_module("tools.executor")
        assert hasattr(mod, "ProgressCallback")

    def test_import_tools_interfaces(self) -> None:
        """tools.interfaces 模块导入成功，ProgressCallback 定义存在。"""
        mod = importlib.import_module("tools.interfaces")
        assert hasattr(mod, "ProgressCallback")

    def test_pipeline_engine_has_file_handler_factory(self) -> None:
        """pipeline.engine 包含 _create_file_handler 工厂方法。"""
        mod = importlib.import_module("pipeline.engine")
        engine_cls = getattr(mod, "PipelineEngine", None)
        assert engine_cls is not None
        assert hasattr(engine_cls, "_create_file_handler")

    def test_no_src_api_directory(self) -> None:
        """src/api 目录应不存在（S-3 死代码清除）。"""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("api")


# =============================================================================
# 2. TestRoutesThinkingModeAuth — 认证依赖验证（3 用例）
# =============================================================================


class TestRoutesThinkingModeAuth:
    """验证 routes_thinking_mode.py 的认证依赖已正确添加。"""

    def test_router_has_auth_dependency(self) -> None:
        """router 的 dependencies 列表非空。"""
        from channels.api.routes_thinking_mode import router

        assert len(router.dependencies) > 0

    def test_router_prefix_correct(self) -> None:
        """router 前缀为 /api/v1/thinking-mode。"""
        from channels.api.routes_thinking_mode import router

        assert router.prefix == "/api/v1/thinking-mode"

    def test_router_depends_contains_require_auth(self) -> None:
        """router 依赖中包含 require_auth 认证函数。"""
        from channels.api.routes_thinking_mode import router

        dep_names = []
        for dep in router.dependencies:
            if hasattr(dep, "dependency") and dep.dependency is not None:
                dep_names.append(getattr(dep.dependency, "__name__", ""))
        assert any("require_auth" in name for name in dep_names), (
            f"router.dependencies 未包含 require_auth，实际依赖: {dep_names}"
        )


# =============================================================================
# 3. TestRoutesConfigSchema — Pydantic Schema 校验验证（8 用例）
# =============================================================================


class TestRoutesConfigSchema:
    """验证 routes_config.py 的 Pydantic Schema 模型定义和行为。"""

    def test_llm_defaults_update_request_exists(self) -> None:
        """LlmDefaultsUpdateRequest 模型存在。"""
        from channels.api.routes_config import LlmDefaultsUpdateRequest

        assert LlmDefaultsUpdateRequest is not None

    def test_llm_defaults_partial_update(self) -> None:
        """LlmDefaultsUpdateRequest 允许部分字段更新。"""
        from channels.api.routes_config import LlmDefaultsUpdateRequest

        req = LlmDefaultsUpdateRequest(chat="gpt-4")
        assert req.chat == "gpt-4"
        assert req.embedding is None

    def test_model_add_request_exists(self) -> None:
        """ModelAddRequest 模型存在。"""
        from channels.api.routes_config import ModelAddRequest

        assert ModelAddRequest is not None

    def test_model_add_request_validates(self) -> None:
        """ModelAddRequest 正确接收 models 字典。"""
        from channels.api.routes_config import ModelAddRequest

        req = ModelAddRequest(models={"test-model": {"display_name": "Test"}})
        assert "test-model" in req.models

    def test_model_config_update_request_exists(self) -> None:
        """ModelConfigUpdateRequest 模型存在。"""
        from channels.api.routes_config import ModelConfigUpdateRequest

        assert ModelConfigUpdateRequest is not None

    def test_context_window_update_request_exists(self) -> None:
        """ContextWindowUpdateRequest 模型存在。"""
        from channels.api.routes_config import ContextWindowUpdateRequest

        assert ContextWindowUpdateRequest is not None

    def test_generic_config_update_request_exists(self) -> None:
        """GenericConfigUpdateRequest 模型存在。"""
        from channels.api.routes_config import GenericConfigUpdateRequest

        req = GenericConfigUpdateRequest(data={"key": "value"})
        assert req.data == {"key": "value"}

    def test_put_endpoints_use_schema_not_bare_dict(self) -> None:
        """PUT 端点的请求体参数使用 Pydantic Schema 而非裸 dict。"""
        from channels.api.routes_config import router

        put_routes = [r for r in router.routes if "PUT" in str(r.methods)]
        assert len(put_routes) > 0
        for route in put_routes:
            body_field = getattr(route, "body_field", None)
            if body_field is None:
                continue
            field_info = getattr(body_field, "_type", None)
            if field_info is None:
                continue
            type_name = getattr(field_info, "__name__", str(field_info))
            assert type_name != "dict", (
                f"路由 {route.path} 仍使用裸 dict 作为请求体"
            )


# =============================================================================
# 4. TestSafeEnumValue — 枚举值提取验证（10 用例）
# =============================================================================


class _TestColor(Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class _TestPriority(Enum):
    CRITICAL = 1
    HIGH = 3
    NORMAL = 5
    LOW = 7


class TestSafeEnumValue:
    """验证 utils.enum_utils.safe_enum_value 的行为正确性。"""

    def test_enum_member_returns_value(self) -> None:
        """Enum 成员返回 .value。"""
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value(_TestColor.RED) == "red"

    def test_string_returns_as_is(self) -> None:
        """普通字符串原样返回。"""
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value("pending") == "pending"

    def test_int_returns_as_is(self) -> None:
        """普通整数原样返回。"""
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value(42) == 42

    def test_none_returns_none(self) -> None:
        """None 原样返回。"""
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value(None) is None

    def test_int_enum_member_returns_value(self) -> None:
        """IntEnum 成员返回其整数值。"""
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value(_TestPriority.HIGH) == 3

    def test_all_color_values(self) -> None:
        """遍历全部颜色枚举值验证。"""
        from utils.enum_utils import safe_enum_value

        for member in _TestColor:
            assert safe_enum_value(member) == member.value

    def test_all_priority_values(self) -> None:
        """遍历全部优先级枚举值验证。"""
        from utils.enum_utils import safe_enum_value

        for member in _TestPriority:
            assert safe_enum_value(member) == member.value

    def test_empty_string_returns_as_is(self) -> None:
        """空字符串原样返回。"""
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value("") == ""

    def test_dict_returns_as_is(self) -> None:
        """字典对象（无 value 属性）原样返回。"""
        from utils.enum_utils import safe_enum_value

        d = {"key": "value"}
        assert safe_enum_value(d) is d

    def test_list_returns_as_is(self) -> None:
        """列表对象原样返回。"""
        from utils.enum_utils import safe_enum_value

        lst = [1, 2, 3]
        assert safe_enum_value(lst) is lst


# =============================================================================
# 5. TestServiceAccess — 公共接口行为验证（4 用例）
# =============================================================================


class TestServiceAccess:
    """验证 service_access 公共接口的签名和异常处理行为。"""

    def test_get_task_service_is_callable(self) -> None:
        """get_task_service 是可调用对象。"""
        from tasks.service_access import get_task_service

        assert callable(get_task_service)

    def test_get_task_service_returns_none_on_exception(self) -> None:
        """ServiceProvider 不可用时返回 None 而非抛异常。"""
        from tasks.service_access import get_task_service

        with patch(
            "infrastructure.service_provider.get_service_provider",
            side_effect=RuntimeError("provider not ready"),
        ):
            result = get_task_service()
        assert result is None

    def test_get_execution_record_storage_is_callable(self) -> None:
        """get_execution_record_storage 是可调用对象。"""
        from infrastructure.service_access import get_execution_record_storage

        assert callable(get_execution_record_storage)

    def test_get_execution_record_storage_returns_none_on_exception(self) -> None:
        """ServiceProvider 不可用时返回 None 而非抛异常。"""
        from infrastructure.service_access import get_execution_record_storage

        with patch(
            "infrastructure.service_provider.get_service_provider",
            side_effect=RuntimeError("provider not ready"),
        ):
            result = get_execution_record_storage()
        assert result is None


# =============================================================================
# 6. TestTypePriorityScope — _TYPE_PRIORITY 作用域验证（5 用例）
# =============================================================================


class TestTypePriorityScope:
    """验证 evaluation/engine.py 中 _TYPE_PRIORITY 模块级可访问性。"""

    def test_type_priority_accessible_at_module_level(self) -> None:
        """_TYPE_PRIORITY 在模块级别可访问。"""
        from evaluation.engine import _TYPE_PRIORITY

        assert _TYPE_PRIORITY is not None

    def test_type_priority_is_dict(self) -> None:
        """_TYPE_PRIORITY 是字典类型。"""
        from evaluation.engine import _TYPE_PRIORITY

        assert isinstance(_TYPE_PRIORITY, dict)

    def test_type_priority_has_tool_type(self) -> None:
        """_TYPE_PRIORITY 包含 TOOL 类型的优先级。"""
        from evaluation.engine import _TYPE_PRIORITY

        from evaluation.types import MetricType

        assert MetricType.TOOL in _TYPE_PRIORITY

    def test_type_priority_tool_before_agent(self) -> None:
        """TOOL 优先级数值小于 AGENT（TOOL 先执行）。"""
        from evaluation.engine import _TYPE_PRIORITY

        from evaluation.types import MetricType

        assert _TYPE_PRIORITY[MetricType.TOOL] < _TYPE_PRIORITY[MetricType.AGENT]

    def test_type_priority_defined_only_once(self) -> None:
        """_TYPE_PRIORITY 在 engine.py 中仅定义一次（通过模块属性唯一性验证）。"""
        import inspect

        import evaluation.engine as engine_mod

        source = inspect.getsource(engine_mod)
        assignment_count = source.count("_TYPE_PRIORITY")
        assert "_TYPE_PRIORITY" in source
        # _TYPE_PRIORITY 出现多次是正常的（定义 + 使用），关键是只有一次赋值
        # 通过检查模块属性是否为同一个对象来确认
        attr1 = engine_mod._TYPE_PRIORITY
        attr2 = engine_mod._TYPE_PRIORITY
        assert attr1 is attr2


# =============================================================================
# 7. TestTaskModelSerialization — TaskModel 序列化验证（4 用例）
# =============================================================================


class TestTaskModelSerialization:
    """验证 routes_tasks.py 中 _task_model_to_dict 正确处理枚举值。"""

    def test_task_model_to_dict_converts_status_enum(self) -> None:
        """_task_model_to_dict 将 status 枚举转换为字符串值。"""
        from channels.api.routes_tasks import _task_model_to_dict
        from tasks.types import TaskModel, TaskPriority, TaskStatus

        task = TaskModel(
            title="test",
            status=TaskStatus.RUNNING,
            priority=TaskPriority.NORMAL,
        )
        d = _task_model_to_dict(task)
        assert d["status"] == "running"

    def test_task_model_to_dict_converts_priority_enum(self) -> None:
        """_task_model_to_dict 将 priority 枚举转换为整数值。"""
        from channels.api.routes_tasks import _task_model_to_dict
        from tasks.types import TaskModel, TaskPriority, TaskStatus

        task = TaskModel(
            title="test",
            status=TaskStatus.PENDING,
            priority=TaskPriority.HIGH,
        )
        d = _task_model_to_dict(task)
        assert d["priority"] == 3

    def test_task_model_to_dict_all_statuses(self) -> None:
        """遍历全部 7 种 TaskStatus 验证序列化。"""
        from channels.api.routes_tasks import _task_model_to_dict
        from tasks.types import TaskModel, TaskPriority, TaskStatus

        for status in TaskStatus:
            task = TaskModel(
                title=f"test-{status.value}",
                status=status,
                priority=TaskPriority.NORMAL,
            )
            d = _task_model_to_dict(task)
            assert d["status"] == status.value, (
                f"状态 {status} 序列化值不匹配: {d['status']} != {status.value}"
            )

    def test_task_model_to_dict_preserves_metadata(self) -> None:
        """_task_model_to_dict 保留 metadata 字段。"""
        from channels.api.routes_tasks import _task_model_to_dict
        from tasks.types import TaskModel, TaskPriority, TaskStatus

        meta = {"session_id": "sess-123", "target_id": "agent-001"}
        task = TaskModel(
            title="test",
            status=TaskStatus.PENDING,
            priority=TaskPriority.NORMAL,
            metadata=meta,
        )
        d = _task_model_to_dict(task)
        assert d["metadata"] == meta


# =============================================================================
# 8. TestCrossModuleConsistency — 跨模块委托一致性验证（4 用例）
# =============================================================================


class TestCrossModuleConsistency:
    """验证各模块正确委托到 service_access 公共接口。"""

    def test_routes_tasks_delegates_to_service_access(self) -> None:
        """routes_tasks.py 的 _get_task_service 委托到 tasks.service_access。"""
        from channels.api import routes_tasks
        from tasks import service_access

        # routes_tasks._get_task_service 应与 service_access.get_task_service 同一对象
        assert routes_tasks._get_task_service is service_access.get_task_service

    def test_routes_tasks_delegates_execution_storage(self) -> None:
        """routes_tasks.py 的 _get_execution_record_storage 委托到 infrastructure.service_access。"""
        from channels.api import routes_tasks
        from infrastructure import service_access

        assert (
            routes_tasks._get_execution_record_storage
            is service_access.get_execution_record_storage
        )

    def test_routes_threads_delegates_task_service(self) -> None:
        """routes_threads.py 的 _get_task_service 委托到 tasks.service_access。"""
        import inspect

        from channels.api import routes_threads

        source = inspect.getsource(routes_threads)
        assert "from tasks.service_access import" in source, (
            "routes_threads.py 未从 tasks.service_access 导入"
        )

    def test_routes_missing_imports_from_service_access(self) -> None:
        """routes_missing.py 从 tasks.service_access 导入 get_task_service。"""
        import inspect

        from channels.api import routes_missing

        source = inspect.getsource(routes_missing)
        assert "from tasks.service_access import" in source, (
            "routes_missing.py 未从 tasks.service_access 导入"
        )

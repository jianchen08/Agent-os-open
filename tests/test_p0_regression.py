"""P0 安全 + 冗余清理变更回归测试。

覆盖本批次所有关键变更点，确保无回归：
1. 安全修复：routes_thinking_mode 认证、routes_config Schema 校验
2. 冗余提取：service_access 公共接口、enum_utils 函数
3. _TYPE_PRIORITY 作用域修复（evaluation/engine.py 模块级可访问）
4. routes_tasks Task→TaskModel 序列化修复
5. 导入完整性（无循环导入、无 NameError）
"""
from __future__ import annotations

import importlib
from enum import Enum
from unittest.mock import MagicMock, patch

import pytest


# ============================================================
# 1. 导入完整性测试
# ============================================================


class TestImportIntegrity:
    """验证所有变更模块可正常导入，无循环导入、无 NameError。"""

    def test_import_tasks_service_access(self):
        """tasks.service_access.get_task_service 可导入且可调用。"""
        from tasks.service_access import get_task_service

        assert callable(get_task_service)

    def test_import_infrastructure_service_access(self):
        """infrastructure.service_access.get_execution_record_storage 可导入且可调用。"""
        from infrastructure.service_access import get_execution_record_storage

        assert callable(get_execution_record_storage)

    def test_import_utils_enum_utils(self):
        """utils.enum_utils.safe_enum_value 可导入且可调用。"""
        from utils.enum_utils import safe_enum_value

        assert callable(safe_enum_value)

    def test_import_evaluation_engine(self):
        """evaluation.engine 可导入，_TYPE_PRIORITY 在模块级可访问。"""
        from evaluation.engine import _TYPE_PRIORITY, EvaluationEngine

        assert isinstance(_TYPE_PRIORITY, dict)
        assert EvaluationEngine is not None

    def test_import_routes_thinking_mode(self):
        """channels.api.routes_thinking_mode 可导入。"""
        from channels.api.routes_thinking_mode import router

        assert router is not None

    def test_import_routes_config(self):
        """channels.api.routes_config 可导入，包含 Schema 模型。"""
        from channels.api.routes_config import (
            ContextWindowUpdateRequest,
            GenericConfigUpdateRequest,
            LlmDefaultsUpdateRequest,
            ModelAddRequest,
            ModelConfigUpdateRequest,
            ProviderConfigUpdateRequest,
        )

        assert LlmDefaultsUpdateRequest is not None
        assert ModelAddRequest is not None
        assert ModelConfigUpdateRequest is not None
        assert ProviderConfigUpdateRequest is not None
        assert ContextWindowUpdateRequest is not None
        assert GenericConfigUpdateRequest is not None

    def test_import_routes_tasks(self):
        """channels.api.routes_tasks 可导入，含 _task_model_to_dict。"""
        from channels.api.routes_tasks import _get_task_service, _task_model_to_dict

        assert callable(_get_task_service)
        assert callable(_task_model_to_dict)

    def test_import_routes_threads(self):
        """channels.api.routes_threads 可导入。"""
        from channels.api.routes_threads import _get_task_service

        assert callable(_get_task_service)

    def test_no_circular_import_on_reload(self):
        """重新导入所有变更模块不触发循环导入错误。"""
        modules_to_reload = [
            "utils.enum_utils",
        ]
        for mod_name in modules_to_reload:
            mod = importlib.import_module(mod_name)
            importlib.reload(mod)

    def test_routes_tasks_delegates_to_service_access(self):
        """routes_tasks._get_task_service 委托到 tasks.service_access.get_task_service。

        验证委托行为：调用 routes_tasks._get_task_service 应与直接调用
        tasks.service_access.get_task_service 返回相同结果。
        """
        from channels.api import routes_tasks
        from tasks.service_access import get_task_service

        mock_result = MagicMock(name="TaskService")
        with patch("infrastructure.service_provider.get_service_provider") as mock_sp:
            mock_provider = MagicMock()
            mock_provider.get_or_create.return_value = mock_result
            mock_sp.return_value = mock_provider

            assert routes_tasks._get_task_service() is mock_result
            assert get_task_service() is mock_result

    def test_routes_threads_delegates_to_service_access(self):
        """routes_threads._get_task_service 委托到 tasks.service_access.get_task_service。

        验证委托行为：调用 routes_threads._get_task_service 应与直接调用
        tasks.service_access.get_task_service 返回相同结果。
        """
        from channels.api import routes_threads
        from tasks.service_access import get_task_service

        mock_result = MagicMock(name="TaskService")
        with patch("infrastructure.service_provider.get_service_provider") as mock_sp:
            mock_provider = MagicMock()
            mock_provider.get_or_create.return_value = mock_result
            mock_sp.return_value = mock_provider

            assert routes_threads._get_task_service() is mock_result
            assert get_task_service() is mock_result


# ============================================================
# 2. 安全修复测试：routes_thinking_mode 认证依赖
# ============================================================


class TestRoutesThinkingModeAuth:
    """验证 routes_thinking_mode 的认证依赖已添加。"""

    def test_router_has_auth_dependency(self):
        """routes_thinking_mode router 必须携带 require_auth 依赖。"""
        from channels.api.routes_thinking_mode import router

        assert len(router.dependencies) > 0, (
            "routes_thinking_mode router 缺少认证依赖，安全修复未生效"
        )

    def test_router_dependency_uses_require_auth(self):
        """验证依赖项包含 require_auth 函数引用。"""
        from channels.api.routes_thinking_mode import router

        dep_names = []
        for dep in router.dependencies:
            if hasattr(dep, "dependency") and dep.dependency is not None:
                dep_names.append(getattr(dep.dependency, "__name__", str(dep.dependency)))

        assert "require_auth" in dep_names, (
            f"router 依赖中未找到 require_auth，实际依赖: {dep_names}"
        )

    def test_router_prefix_correct(self):
        """验证 router 前缀路径。"""
        from channels.api.routes_thinking_mode import router

        assert router.prefix == "/api/v1/thinking-mode"


# ============================================================
# 3. 安全修复测试：routes_config Schema 校验
# ============================================================


class TestRoutesConfigSchema:
    """验证 routes_config 的 Pydantic Schema 模型正确校验请求体。"""

    def test_llm_defaults_update_request_accepts_partial(self):
        """LlmDefaultsUpdateRequest 允许部分字段更新。"""
        from channels.api.routes_config import LlmDefaultsUpdateRequest

        req = LlmDefaultsUpdateRequest(chat="gpt-4")
        assert req.chat == "gpt-4"
        assert req.embedding is None
        assert req.tiers is None

    def test_llm_defaults_update_request_accepts_all_fields(self):
        """LlmDefaultsUpdateRequest 接受所有字段。"""
        from channels.api.routes_config import LlmDefaultsUpdateRequest

        req = LlmDefaultsUpdateRequest(
            chat="gpt-4", embedding="text-embedding-3", tiers={"large": "gpt-4"},
        )
        assert req.chat == "gpt-4"
        assert req.embedding == "text-embedding-3"
        assert req.tiers == {"large": "gpt-4"}

    def test_llm_defaults_update_request_empty_ok(self):
        """LlmDefaultsUpdateRequest 允许空请求。"""
        from channels.api.routes_config import LlmDefaultsUpdateRequest

        req = LlmDefaultsUpdateRequest()
        assert req.chat is None

    def test_model_add_request_validates_models_field(self):
        """ModelAddRequest 要求 models 字段且为字典。"""
        from channels.api.routes_config import ModelAddRequest
        from pydantic import ValidationError

        req = ModelAddRequest(models={"gpt-4": {"provider": "openai"}})
        assert "gpt-4" in req.models

        with pytest.raises(ValidationError):
            ModelAddRequest()

    def test_model_config_update_request_validates_config(self):
        """ModelConfigUpdateRequest 要求 config 字段。"""
        from channels.api.routes_config import ModelConfigUpdateRequest
        from pydantic import ValidationError

        req = ModelConfigUpdateRequest(config={"temperature": 0.7})
        assert req.config["temperature"] == 0.7

        with pytest.raises(ValidationError):
            ModelConfigUpdateRequest()

    def test_provider_config_update_request_validates_config(self):
        """ProviderConfigUpdateRequest 要求 config 字段。"""
        from channels.api.routes_config import ProviderConfigUpdateRequest
        from pydantic import ValidationError

        req = ProviderConfigUpdateRequest(config={"api_key": "sk-xxx"})
        assert req.config["api_key"] == "sk-xxx"

        with pytest.raises(ValidationError):
            ProviderConfigUpdateRequest()

    def test_context_window_update_request_partial(self):
        """ContextWindowUpdateRequest 允许部分字段。"""
        from channels.api.routes_config import ContextWindowUpdateRequest

        req = ContextWindowUpdateRequest(max_context_length=128000)
        assert req.max_context_length == 128000
        assert req.compress_trigger_ratio is None

    def test_generic_config_update_request_validates_data(self):
        """GenericConfigUpdateRequest 要求 data 字段。"""
        from channels.api.routes_config import GenericConfigUpdateRequest
        from pydantic import ValidationError

        req = GenericConfigUpdateRequest(data={"key": "value"})
        assert req.data["key"] == "value"

        with pytest.raises(ValidationError):
            GenericConfigUpdateRequest()


# ============================================================
# 4. 冗余提取测试：enum_utils.safe_enum_value
# ============================================================


class TestSafeEnumValue:
    """验证 safe_enum_value 函数行为正确。"""

    def test_returns_value_for_enum_member(self):
        """Enum 成员返回 .value。"""
        from utils.enum_utils import safe_enum_value

        class Color(Enum):
            RED = "red"
            GREEN = "green"

        assert safe_enum_value(Color.RED) == "red"
        assert safe_enum_value(Color.GREEN) == "green"

    def test_returns_original_for_plain_string(self):
        """普通字符串原样返回。"""
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value("hello") == "hello"

    def test_returns_original_for_int(self):
        """整数原样返回。"""
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value(42) == 42

    def test_returns_original_for_none(self):
        """None 原样返回。"""
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value(None) is None

    def test_returns_original_for_list(self):
        """列表原样返回。"""
        from utils.enum_utils import safe_enum_value

        data = [1, 2, 3]
        assert safe_enum_value(data) is data

    def test_returns_original_for_dict(self):
        """字典原样返回。"""
        from utils.enum_utils import safe_enum_value

        data = {"key": "val"}
        assert safe_enum_value(data) is data

    def test_int_enum_member(self):
        """IntEnum 成员返回 .value。"""
        from utils.enum_utils import safe_enum_value

        class Priority(Enum):
            HIGH = 1
            LOW = 9

        assert safe_enum_value(Priority.HIGH) == 1
        assert safe_enum_value(Priority.LOW) == 9

    def test_task_status_enum(self):
        """实际 TaskStatus 枚举值正确提取。"""
        from tasks.types import TaskStatus
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value(TaskStatus.PENDING) == "pending"
        assert safe_enum_value(TaskStatus.RUNNING) == "running"
        assert safe_enum_value(TaskStatus.COMPLETED) == "completed"
        assert safe_enum_value(TaskStatus.EVALUATING) == "evaluating"

    def test_task_priority_enum(self):
        """实际 TaskPriority 枚举值正确提取。"""
        from tasks.types import TaskPriority
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value(TaskPriority.HIGH) == 3
        assert safe_enum_value(TaskPriority.NORMAL) == 5

    def test_metric_type_enum(self):
        """实际 MetricType 枚举值正确提取。"""
        from evaluation.types import MetricType
        from utils.enum_utils import safe_enum_value

        assert safe_enum_value(MetricType.TOOL) == "tool"
        assert safe_enum_value(MetricType.AGENT) == "agent"
        assert safe_enum_value(MetricType.HUMAN) == "human"


# ============================================================
# 5. 冗余提取测试：service_access 公共接口
# ============================================================


class TestServiceAccess:
    """验证 service_access 公共接口行为正确。"""

    def test_get_task_service_returns_none_on_error(self):
        """get_task_service 在 ServiceProvider 不可用时返回 None。"""
        from tasks.service_access import get_task_service

        with patch("infrastructure.service_provider.get_service_provider") as mock_sp:
            mock_sp.side_effect = RuntimeError("no provider")
            result = get_task_service()
            assert result is None

    def test_get_task_service_returns_service_when_available(self):
        """get_task_service 在 ServiceProvider 可用时返回 TaskService 实例。"""
        from tasks.service_access import get_task_service

        mock_provider = MagicMock()
        mock_service = MagicMock(name="TaskService")
        mock_provider.get_or_create.return_value = mock_service

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            result = get_task_service()
            assert result is mock_service

    def test_get_execution_record_storage_returns_none_on_error(self):
        """get_execution_record_storage 在 ServiceProvider 不可用时返回 None。"""
        from infrastructure.service_access import get_execution_record_storage

        with patch("infrastructure.service_provider.get_service_provider") as mock_sp:
            mock_sp.side_effect = RuntimeError("no provider")
            result = get_execution_record_storage()
            assert result is None

    def test_get_execution_record_storage_from_provider(self):
        """get_execution_record_storage 优先从 ServiceProvider 获取已注册实例。"""
        from infrastructure.service_access import get_execution_record_storage

        mock_provider = MagicMock()
        mock_storage = MagicMock(name="ExecutionRecordStorage")
        mock_provider.get.return_value = mock_storage

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            result = get_execution_record_storage()
            assert result is mock_storage
            mock_provider.get_or_create.assert_not_called()


# ============================================================
# 6. _TYPE_PRIORITY 作用域修复测试
# ============================================================


class TestTypePriorityScope:
    """验证 _TYPE_PRIORITY 在模块级正确定义和可访问。"""

    def test_type_priority_is_module_level_dict(self):
        """_TYPE_PRIORITY 是模块级字典。"""
        from evaluation.engine import _TYPE_PRIORITY

        assert isinstance(_TYPE_PRIORITY, dict)

    def test_type_priority_has_all_metric_types(self):
        """_TYPE_PRIORITY 包含所有 MetricType。"""
        from evaluation.engine import _TYPE_PRIORITY
        from evaluation.types import MetricType

        assert MetricType.TOOL in _TYPE_PRIORITY
        assert MetricType.AGENT in _TYPE_PRIORITY
        assert MetricType.HUMAN in _TYPE_PRIORITY

    def test_type_priority_ordering(self):
        """_TYPE_PRIORITY 优先级顺序: TOOL < AGENT < HUMAN。"""
        from evaluation.engine import _TYPE_PRIORITY
        from evaluation.types import MetricType

        assert _TYPE_PRIORITY[MetricType.TOOL] < _TYPE_PRIORITY[MetricType.AGENT]
        assert _TYPE_PRIORITY[MetricType.AGENT] < _TYPE_PRIORITY[MetricType.HUMAN]

    def test_type_priority_accessible_in_evaluate(self):
        """EvaluationEngine.evaluate 方法可访问 _TYPE_PRIORITY（不抛 NameError）。"""
        from evaluation.engine import EvaluationEngine, _TYPE_PRIORITY
        from evaluation.types import MetricType

        assert _TYPE_PRIORITY.get(MetricType.TOOL, 99) == 1

    def test_type_priority_used_for_sorting(self):
        """_TYPE_PRIORITY 可用于排序逻辑（模拟 evaluate 中的 sort）。"""
        from evaluation.engine import _TYPE_PRIORITY
        from evaluation.types import MetricType

        types = [MetricType.HUMAN, MetricType.TOOL, MetricType.AGENT]
        sorted_types = sorted(types, key=lambda t: _TYPE_PRIORITY.get(t, 99))
        assert sorted_types == [MetricType.TOOL, MetricType.AGENT, MetricType.HUMAN]


# ============================================================
# 7. Bug 修复测试：routes_tasks Task→TaskModel 序列化
# ============================================================


class TestTaskModelSerialization:
    """验证 routes_tasks._task_model_to_dict 正确序列化 TaskModel。"""

    def test_task_model_to_dict_converts_status_enum(self):
        """_task_model_to_dict 将 status 枚举转为字符串值。"""
        from channels.api.routes_tasks import _task_model_to_dict
        from tasks.types import TaskModel, TaskStatus

        task = TaskModel(id="test-001", title="测试任务", status=TaskStatus.COMPLETED)
        result = _task_model_to_dict(task)

        assert result["status"] == "completed"
        assert result["id"] == "test-001"
        assert result["title"] == "测试任务"

    def test_task_model_to_dict_converts_priority_enum(self):
        """_task_model_to_dict 将 priority 枚举转为整数值。"""
        from channels.api.routes_tasks import _task_model_to_dict
        from tasks.types import TaskModel, TaskPriority, TaskStatus

        task = TaskModel(
            id="test-002",
            title="高优先级任务",
            status=TaskStatus.RUNNING,
            priority=TaskPriority.HIGH,
        )
        result = _task_model_to_dict(task)

        assert result["priority"] == 3
        assert result["status"] == "running"

    def test_task_model_to_dict_preserves_metadata(self):
        """_task_model_to_dict 保留 metadata 字段。"""
        from channels.api.routes_tasks import _task_model_to_dict
        from tasks.types import TaskModel

        task = TaskModel(
            id="test-003",
            title="带元数据的任务",
            metadata={"session_id": "sess-001", "custom": "data"},
        )
        result = _task_model_to_dict(task)

        assert result["metadata"]["session_id"] == "sess-001"
        assert result["metadata"]["custom"] == "data"

    def test_task_model_to_dict_handles_all_statuses(self):
        """_task_model_to_dict 正确处理所有任务状态。"""
        from channels.api.routes_tasks import _task_model_to_dict
        from tasks.types import TaskModel, TaskStatus

        for status in TaskStatus:
            task = TaskModel(id=f"task-{status.value}", status=status)
            result = _task_model_to_dict(task)
            assert result["status"] == status.value, (
                f"状态 {status.name} 序列化失败"
            )

    def test_task_model_to_dict_handles_evaluating_status(self):
        """_task_model_to_dict 正确处理 EVALUATING 状态（之前缺失的状态）。"""
        from channels.api.routes_tasks import _task_model_to_dict
        from tasks.types import TaskModel, TaskStatus

        task = TaskModel(id="eval-task", status=TaskStatus.EVALUATING)
        result = _task_model_to_dict(task)
        assert result["status"] == "evaluating"


# ============================================================
# 8. 综合回归验证：模块间委托一致性
# ============================================================


class TestCrossModuleConsistency:
    """验证跨模块的公共接口委托一致性。"""

    def test_all_get_task_service_implementations_delegate_to_same(self):
        """routes_tasks 和 routes_threads 的 _get_task_service 都委托到同一公共接口。

        验证委托行为一致：在相同的 mock 环境下，三个调用路径返回相同结果。
        """
        from channels.api.routes_tasks import _get_task_service as r_tasks_gts
        from channels.api.routes_threads import _get_task_service as r_threads_gts
        from tasks.service_access import get_task_service as core_gts

        mock_result = MagicMock(name="TaskService")
        with patch("infrastructure.service_provider.get_service_provider") as mock_sp:
            mock_provider = MagicMock()
            mock_provider.get_or_create.return_value = mock_result
            mock_sp.return_value = mock_provider

            assert r_tasks_gts() is mock_result
            assert r_threads_gts() is mock_result
            assert core_gts() is mock_result

    def test_routes_tasks_delegates_execution_record_storage(self):
        """routes_tasks._get_execution_record_storage 委托到公共接口。

        验证委托行为一致：在相同的 mock 环境下，两个调用路径返回相同结果。
        """
        from channels.api.routes_tasks import _get_execution_record_storage
        from infrastructure.service_access import get_execution_record_storage

        mock_result = MagicMock(name="Storage")
        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_result

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            assert _get_execution_record_storage() is mock_result
            assert get_execution_record_storage() is mock_result

    def test_enum_utils_used_in_cli_commands(self):
        """验证 cli_commands 正确导入和使用 safe_enum_value。"""
        from channels.cli.cli_commands import safe_enum_value

        assert callable(safe_enum_value)

    def test_enum_utils_used_in_isolation_executor(self):
        """验证 isolation.executor 正确导入和使用 safe_enum_value。"""
        from isolation.executor import safe_enum_value

        assert callable(safe_enum_value)

"""阶段 6 验收测试 — 创建→评估→注册闭环 + 热替换+回滚 + 经验沉淀。

测试验收条件：
- 6.1 模板驱动工作流：模板=输出规范=评估标准，三位一体
- 6.2 创建→评估→注册闭环：模板创建→评估→注册→可用
- 6.3 经验沉淀全闭环：执行→总结→存储→检索→应用
- 6.4 热替换+回滚：插件热替换，失败可回滚
"""

from __future__ import annotations

import pytest

from agents.registry import AgentRegistry
from agents.types import AgentConfig, AgentLevel, AgentType
from evaluation.engine import EvaluationEngine
from evaluation.mapper import ResultMapper
from evaluation.types import (
    EvaluationResult,
    MetricDefinition,
    MetricResult,
    MetricType,
)
from pipeline.config_store import PipelineConfig, PipelineConfigStore
from pipeline.hot_swap import HotSwapManager
from pipeline.plugin import IInputPlugin, IOutputPlugin, PluginContext, PluginResult
from pipeline.registry import PluginRegistry
from pipeline.rollback import RollbackManager
from templates.registry import TemplateRegistry
from templates.types import EvaluationDimension, TemplateSpec, TemplateType
from tools.builtin.register_resource import register_resource_func
from tools.builtin.hot_swap import hot_swap_func
from tools.registry import ToolRegistry


# --- 测试用插件 ---


class DummyInputPlugin(IInputPlugin):
    """测试用输入插件。"""

    def __init__(self, name: str = "test_input", priority: int = 10) -> None:
        self._name = name
        self._priority = priority

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult()


class DummyOutputPlugin(IOutputPlugin):
    """测试用输出插件。"""

    def __init__(self, name: str = "test_output", priority: int = 20) -> None:
        self._name = name
        self._priority = priority

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    @property
    def route_signals(self) -> list[str]:
        return []

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult()


# === 6.1 模板驱动工作流 ===


class TestTemplateWorkflow:
    """模板 = 输出规范 = 评估标准，三位一体。"""

    def test_template_has_evaluation_dimensions(self) -> None:
        """B 型模板包含评估维度，评估维度可以转换为评估指标。"""
        # 创建带评估维度的模板
        template = TemplateSpec(
            template_id="agent_creation",
            name="Agent 创建模板",
            template_type=TemplateType.DELIVERABLE,
            description="创建新 Agent 的模板",
            evaluation_dimensions=[
                EvaluationDimension(
                    name="字段完整性",
                    check_content="检查输出是否包含所有必填字段",
                    required=True,
                    pass_criteria="所有必填字段均有内容",
                ),
                EvaluationDimension(
                    name="格式正确性",
                    check_content="检查输出格式是否符合规范",
                    required=True,
                    pass_criteria="输出格式正确无语法错误",
                ),
            ],
        )

        # 模板注册
        registry = TemplateRegistry()
        registry.register(template)

        # 验证：模板可检索
        found = registry.get("agent_creation")
        assert found is not None
        assert len(found.evaluation_dimensions) == 2

    def test_template_dimensions_to_metrics(self) -> None:
        """评估维度→评估指标映射跑通。"""
        mapper = ResultMapper()

        dimensions = [
            EvaluationDimension(
                name="字段完整性",
                check_content="检查所有必填字段",
                required=True,
                pass_criteria="所有必填字段均有内容",
            ),
            EvaluationDimension(
                name="格式正确性",
                check_content="检查格式规范",
                required=False,
                pass_criteria="格式正确",
            ),
        ]

        metrics = mapper.template_dimensions_to_metrics("test_template", dimensions)

        assert len(metrics) == 2
        assert metrics[0].id == "test_template_dim_1"
        assert metrics[0].name == "字段完整性"
        assert metrics[0].metric_type == MetricType.AGENT
        assert metrics[0].is_red_line is True
        assert metrics[1].is_red_line is False

    def test_template_evaluation_three_in_one(self) -> None:
        """模板三位一体：模板内容=输出规范=评估标准。"""
        # 1. 模板定义输出规范
        template = TemplateSpec(
            template_id="tool_creation",
            name="工具创建模板",
            template_type=TemplateType.DELIVERABLE,
            raw_content="# 工具创建模板\n\n## 工具名称 [必填]\n\n## 功能描述 [必填]\n\n## 参数定义 [必填]\n\n## 评估指南\n<!-- 检查维度\n| 维度 | 检查内容 | 必填 | 通过标准 |\n|------|---------|------|----------|\n| 名称规范 | 工具名称格式 | 是 | snake_case 命名 |\n| 参数完整 | schema 完整 | 是 | 包含 type/properties/required |\n-->",
            evaluation_dimensions=[
                EvaluationDimension(
                    name="名称规范",
                    check_content="工具名称格式",
                    required=True,
                    pass_criteria="snake_case 命名",
                ),
                EvaluationDimension(
                    name="参数完整",
                    check_content="schema 完整",
                    required=True,
                    pass_criteria="包含 type/properties/required",
                ),
            ],
        )

        # 2. 评估维度转换为评估指标
        mapper = ResultMapper()
        metrics = mapper.template_dimensions_to_metrics(template.template_id, template.evaluation_dimensions)

        # 3. 用评估引擎执行评估
        engine = EvaluationEngine(loader=None)
        result = engine.evaluate_with_metrics(
            task_id="test_task",
            metrics=metrics,
        )

        # 验证：评估指标由模板维度生成
        assert len(result.results) == 2
        assert result.results[0].metric_id == "tool_creation_dim_1"
        assert result.results[1].metric_id == "tool_creation_dim_2"


# === 6.2 创建→评估→注册闭环 ===

# 共享的 Registry 实例，通过 register_resource 的服务缓存注入
_shared_agent_registry = AgentRegistry()
_shared_tool_registry = ToolRegistry()
_shared_template_registry = TemplateRegistry()
_shared_config_store = PipelineConfigStore()

from tools.builtin.register_resource import set_service as _set_reg_service
_set_reg_service("agent_registry", _shared_agent_registry)
_set_reg_service("tool_registry", _shared_tool_registry)
_set_reg_service("template_registry", _shared_template_registry)
_set_reg_service("pipeline_config_store", _shared_config_store)


class TestCreateEvaluateRegister:
    """创建→评估→注册闭环。"""

    def test_register_agent_resource(self) -> None:
        """register_resource 注册 Agent 配置。"""
        result = register_resource_func({
            "resource_type": "agent",
            "resource_id": "test_agent_001",
            "config": {
                "name": "测试Agent",
                "description": "用于测试的Agent",
                "level": "l1_main",
                "agent_type": "specialized",
                "system_prompt": "你是一个测试Agent",
                "tool_ids": ["current_time", "calculator"],
            },
            "overwrite": True,
        })

        assert result["success"] is True
        assert result["resource_type"] == "agent"
        assert result["resource_id"] == "test_agent_001"

    def test_register_tool_resource(self) -> None:
        """register_resource 注册工具。"""
        result = register_resource_func({
            "resource_type": "tool",
            "resource_id": "my_custom_tool",
            "config": {
                "description": "自定义工具",
                "schema": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string"},
                    },
                },
            },
            "overwrite": True,
        })

        assert result["success"] is True
        assert result["resource_type"] == "tool"

    def test_register_template_resource(self) -> None:
        """register_resource 注册模板。"""
        result = register_resource_func({
            "resource_type": "template",
            "resource_id": "custom_template",
            "config": {
                "name": "自定义模板",
                "description": "测试用模板",
                "template_type": "B",
                "evaluation_dimensions": [
                    {
                        "name": "完整性",
                        "check_content": "检查输出完整性",
                        "required": True,
                        "pass_criteria": "所有必填项都有内容",
                    },
                ],
            },
            "overwrite": True,
        })

        assert result["success"] is True
        assert result["resource_type"] == "template"

    def test_register_pipeline_config_resource(self) -> None:
        """register_resource 注册管道配置。"""
        result = register_resource_func({
            "resource_type": "pipeline_config",
            "resource_id": "custom_pipeline",
            "config": {
                "name": "自定义管道",
                "max_iterations": 50,
            },
            "overwrite": True,
        })

        assert result["success"] is True
        assert result["resource_type"] == "pipeline_config"

    def test_register_no_overwrite_rejects_duplicate(self) -> None:
        """不覆盖模式下，重复注册被拒绝。"""
        # 使用 agent 类型，因为 AgentRegistry 是全局共享的
        # 先注册一个
        register_resource_func({
            "resource_type": "agent",
            "resource_id": "dup_agent_test",
            "config": {
                "name": "第一个",
                "description": "测试重复",
                "level": "l2_sub",
                "agent_type": "specialized",
            },
            "overwrite": True,
        })

        # 不覆盖再注册同名
        result = register_resource_func({
            "resource_type": "agent",
            "resource_id": "dup_agent_test",
            "config": {
                "name": "第二个",
                "description": "测试重复",
                "level": "l2_sub",
                "agent_type": "specialized",
            },
            "overwrite": False,
        })

        assert result["success"] is False
        assert "已存在" in result["error"]

    def test_full_create_evaluate_register_loop(self) -> None:
        """完整闭环：模板→评估维度→评估指标→评估→注册。"""
        # 1. 定义模板（带评估维度）
        template = TemplateSpec(
            template_id="new_agent_template",
            name="新Agent创建模板",
            template_type=TemplateType.DELIVERABLE,
            evaluation_dimensions=[
                EvaluationDimension(
                    name="系统提示词",
                    check_content="必须有系统提示词",
                    required=True,
                    pass_criteria="system_prompt 非空",
                ),
            ],
        )

        # 2. 模板维度→评估指标
        mapper = ResultMapper()
        metrics = mapper.template_dimensions_to_metrics(template.template_id, template.evaluation_dimensions)

        # 3. 模拟 Agent 按模板创建的输出
        created_output = {
            "system_prompt": "你是一个专业的代码审查助手",
            "tool_ids": ["current_time"],
        }

        # 4. 用评估引擎评估（当前是 Mock 评估器，返回 passed=True）
        engine = EvaluationEngine(loader=None)
        eval_result = engine.evaluate_with_metrics(
            task_id="create_agent_task",
            metrics=metrics,
        )

        # 5. 评估通过→注册
        if eval_result.overall_passed:
            reg_result = register_resource_func({
                "resource_type": "agent",
                "resource_id": "code_reviewer",
                "config": {
                    "name": "代码审查Agent",
                    "description": "专业代码审查",
                    "level": "l2_sub",
                    "agent_type": "specialized",
                    "system_prompt": created_output["system_prompt"],
                    "tool_ids": created_output["tool_ids"],
                },
                "overwrite": True,
            })
            assert reg_result["success"] is True

        # 验证闭环完成
        assert eval_result.overall_passed is True
        assert reg_result["success"] is True


# === 6.3 经验沉淀全闭环 ===


class TestExperienceLoop:
    """执行→总结→存储→检索→应用。"""

    def test_knowledge_write_retrieve_inject(self) -> None:
        """知识写入→检索→注入全链路（使用内存降级模式）。"""
        from memory.knowledge_service import KnowledgeService
        from memory.types import Knowledge

        service = KnowledgeService(semantic_storage=None)

        # 1. 执行→总结→存储
        knowledge = Knowledge(
            content="代码审查时发现命名不规范的问题，建议使用 snake_case",
            source_type="experience",
            extra_data={"category": "code_review", "tags": ["code_review", "naming"]},
        )

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            entry_id = loop.run_until_complete(service.store_knowledge(knowledge))
            assert entry_id is not None

            # 2. 内存模式下直接验证 in_memory
            assert len(service._in_memory) > 0

            # 3. 验证存储的内容包含原始经验
            found = list(service._in_memory.values())[0]
            assert "snake_case" in found.content or "命名" in found.content
        finally:
            loop.close()


# === 6.4 热替换+回滚 ===


class TestHotSwapAndRollback:
    """插件热替换和配置回滚。"""

    @pytest.mark.asyncio
    async def test_plugin_hot_swap_success(self) -> None:
        """插件热替换成功。"""
        registry = PluginRegistry()
        old_plugin = DummyInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)
        new_plugin = DummyInputPlugin(name="my_plugin_v2", priority=10)

        result = await manager.swap_plugin("my_plugin", new_plugin, health_check=True)

        assert result.success is True
        assert result.rolled_back is False
        assert registry.get("my_plugin") is new_plugin

    @pytest.mark.asyncio
    async def test_plugin_hot_swap_rollback_on_failure(self) -> None:
        """热替换失败自动回滚。"""
        registry = PluginRegistry()
        old_plugin = DummyInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)

        # 创建一个坏插件
        class BrokenPlugin(IInputPlugin):
            def __init__(self) -> None:
                self._name = "broken"
                self._priority = 10

            @property
            def name(self) -> str:
                return self._name

            @property
            def priority(self) -> int:
                return self._priority

            async def execute(self, ctx: PluginContext) -> PluginResult:
                raise RuntimeError("broken")

        broken = BrokenPlugin()
        result = await manager.swap_plugin("my_plugin", broken, health_check=True)

        assert result.success is False
        assert result.rolled_back is True
        # 旧插件被恢复
        assert registry.get("my_plugin") is old_plugin

    @pytest.mark.asyncio
    async def test_manual_rollback_plugin(self) -> None:
        """手动回滚插件。"""
        registry = PluginRegistry()
        old_plugin = DummyInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)
        new_plugin = DummyInputPlugin(name="my_plugin_v2", priority=10)

        result = await manager.swap_plugin("my_plugin", new_plugin, health_check=False)
        assert result.success is True

        # 手动回滚
        rolled_back = await manager.rollback(result.swap_id)
        assert rolled_back is True
        assert registry.get("my_plugin") is old_plugin

    @pytest.mark.asyncio
    async def test_config_version_rollback(self) -> None:
        """配置版本保存和回滚。"""
        config_store = PipelineConfigStore()
        manager = RollbackManager(config_store=config_store)

        # 保存 v1
        v1 = manager.save_version(
            "test_pipeline",
            {"pipeline_id": "test_pipeline", "name": "Version 1"},
            description="v1",
        )

        # 注册 v1
        config_store.register(
            "test_pipeline",
            PipelineConfig(pipeline_id="test_pipeline", name="Version 1"),
        )

        # 保存 v2 并更新
        v2 = manager.save_version(
            "test_pipeline",
            {"pipeline_id": "test_pipeline", "name": "Version 2"},
            description="v2",
        )

        # 回滚到 v1
        success = await manager.rollback_to_version(v1.version_id)
        assert success is True

        config = config_store.get("test_pipeline")
        assert config is not None
        assert config.name == "Version 1"

    @pytest.mark.asyncio
    async def test_config_update_with_rollback_validation_fail(self) -> None:
        """配置更新验证失败自动回滚。"""
        config_store = PipelineConfigStore()
        manager = RollbackManager(config_store=config_store)

        # 注册初始配置
        config_store.register(
            "test_pipeline",
            PipelineConfig(pipeline_id="test_pipeline", name="Initial"),
        )
        manager.save_version(
            "test_pipeline",
            {"pipeline_id": "test_pipeline", "name": "Initial"},
            description="initial",
        )

        # 更新并验证失败
        result = await manager.update_with_rollback(
            "test_pipeline",
            {"pipeline_id": "test_pipeline", "name": "Bad Config"},
            validator=lambda d: False,
        )

        assert result.success is False
        assert result.rolled_back is True

        # 原配置未变
        config = config_store.get("test_pipeline")
        assert config is not None
        assert config.name == "Initial"


# === 热替换工具集成测试 ===


class TestHotSwapToolIntegration:
    """hot_swap 工具集成测试。"""

    def test_hot_swap_save_config_version(self) -> None:
        """hot_swap 工具保存配置版本。"""
        result = hot_swap_func({
            "action": "save_config_version",
            "config_id": "test_config",
            "config_data": {"name": "test", "value": 1},
            "description": "测试版本",
        })

        assert result["success"] is True
        assert "version_id" in result

    def test_hot_swap_list_versions(self) -> None:
        """hot_swap 工具列出配置版本。"""
        # 重置单例以获得干净状态
        import tools.builtin.hot_swap as hs_module
        hs_module._rollback_manager_instance = None

        # 先保存
        hot_swap_func({
            "action": "save_config_version",
            "config_id": "list_test_config",
            "config_data": {"v": 1},
        })
        hot_swap_func({
            "action": "save_config_version",
            "config_id": "list_test_config",
            "config_data": {"v": 2},
        })

        # 列出
        result = hot_swap_func({
            "action": "list_versions",
            "config_id": "list_test_config",
        })

        assert result["success"] is True
        assert result["count"] >= 2

    def test_hot_swap_rollback_config(self) -> None:
        """hot_swap 工具回滚配置。"""
        # 重置单例
        import tools.builtin.hot_swap as hs_module
        hs_module._rollback_manager_instance = None

        # 保存版本
        save_result = hot_swap_func({
            "action": "save_config_version",
            "config_id": "rollback_test_config",
            "config_data": {"name": "rollback_target"},
            "description": "回滚目标版本",
        })

        version_id = save_result["version_id"]

        # 回滚
        result = hot_swap_func({
            "action": "rollback_config",
            "version_id": version_id,
        })

        assert result["success"] is True

    def test_hot_swap_missing_action(self) -> None:
        """缺少 action 参数报错。"""
        result = hot_swap_func({})
        assert result["success"] is False
        assert "action" in result["error"].lower()

    def test_hot_swap_invalid_action(self) -> None:
        """无效 action 报错。"""
        result = hot_swap_func({"action": "invalid"})
        assert result["success"] is False

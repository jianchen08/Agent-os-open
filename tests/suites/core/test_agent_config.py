"""Agent 配置系统测试。

覆盖 types.py、loader.py、registry.py、context_builder.py、schema_validator.py
的所有核心功能。

运行方式：
    python -m pytest src/agent_os/tests/test_agent_config.py -v
"""

import os
import tempfile
from pathlib import Path

import pytest

from agents import (
    AgentConfig,
    AgentConfigLoader,
    AgentLevel,
    AgentRegistry,
    AgentType,
    ContextBuilder,
    ContextConfig,
    ContextVarItem,
    DeliverableSpec,
    KnowledgeConfig,
    MetricRef,
    RuleReinforcement,
    SchemaValidator,
)


# ============================================================================
# 测试数据路径
# ============================================================================

CONFIG_DIR = Path(__file__).parent.parent / "config" / "agents"


# ============================================================================
# 1. types.py 测试
# ============================================================================


class TestAgentLevel:
    """AgentLevel 枚举测试。"""

    def test_level_values(self) -> None:
        """测试枚举值正确。"""
        assert AgentLevel.L1_MAIN.value == "L1"
        assert AgentLevel.L2_SUBTASK.value == "L2"
        assert AgentLevel.L3_ATOMIC.value == "L3"

    def test_level_from_value(self) -> None:
        """测试通过值获取枚举。"""
        assert AgentLevel("L1") == AgentLevel.L1_MAIN
        assert AgentLevel("L2") == AgentLevel.L2_SUBTASK
        assert AgentLevel("L3") == AgentLevel.L3_ATOMIC

    def test_level_members_count(self) -> None:
        """测试枚举成员数量。"""
        assert len(AgentLevel) == 3


class TestAgentType:
    """AgentType 枚举测试。"""

    def test_type_values(self) -> None:
        """测试枚举值正确。"""
        assert AgentType.MAIN.value == "main"
        assert AgentType.SPECIALIZED.value == "specialized"
        assert AgentType.SYSTEM.value == "system"

    def test_type_from_value(self) -> None:
        """测试通过值获取枚举。"""
        assert AgentType("main") == AgentType.MAIN
        assert AgentType("specialized") == AgentType.SPECIALIZED
        assert AgentType("system") == AgentType.SYSTEM


class TestContextVarItem:
    """ContextVarItem 数据类测试。"""

    def test_default_values(self) -> None:
        """测试默认值。"""
        item = ContextVarItem()
        assert item.name == ""
        assert item.type == ""
        assert item.path == ""
        assert item.tags == []
        assert item.inject_type == ""
        assert item.top_k == 5
        assert item.content == ""
        assert item.memory_type == ""
        assert item.memory_layer == ""

    def test_custom_values(self) -> None:
        """测试自定义值。"""
        item = ContextVarItem(
            name="行为约束",
            type="rules",
            top_k=10,
            tags=["tag1", "tag2"],
        )
        assert item.name == "行为约束"
        assert item.type == "rules"
        assert item.top_k == 10
        assert item.tags == ["tag1", "tag2"]


class TestContextConfig:
    """ContextConfig 数据类测试。"""

    def test_default_values(self) -> None:
        """测试默认值。"""
        config = ContextConfig()
        assert config.enabled is True
        assert config.items == []

    def test_with_items(self) -> None:
        """测试带 items 的配置。"""
        items = [ContextVarItem(name="test", type="rules")]
        config = ContextConfig(enabled=False, items=items)
        assert config.enabled is False
        assert len(config.items) == 1
        assert config.items[0].name == "test"


class TestKnowledgeConfig:
    """KnowledgeConfig 数据类测试。"""

    def test_default_values(self) -> None:
        """测试默认值。"""
        config = KnowledgeConfig()
        assert config.mode == "compressed"
        assert config.max_tokens == 1000
        assert config.top_k == 3
        assert config.score_threshold == 0.7

    def test_custom_values(self) -> None:
        """测试自定义值。"""
        config = KnowledgeConfig(mode="full", max_tokens=2000, top_k=5)
        assert config.mode == "full"
        assert config.max_tokens == 2000
        assert config.top_k == 5


class TestRuleReinforcement:
    """RuleReinforcement 数据类测试。"""

    def test_default_values(self) -> None:
        """测试默认值。"""
        rr = RuleReinforcement()
        assert rr.enabled is True
        assert rr.include_hard_constraints is True
        assert rr.include_soft_constraints is False
        assert rr.max_rules == 10
        assert "【重要】" in rr.extraction_markers

    def test_custom_rules(self) -> None:
        """测试自定义规则。"""
        rr = RuleReinforcement(custom_rules=["规则1", "规则2"])
        assert rr.custom_rules == ["规则1", "规则2"]


class TestDeliverableSpec:
    """DeliverableSpec 数据类测试。"""

    def test_default_values(self) -> None:
        """测试默认值。"""
        spec = DeliverableSpec()
        assert spec.name == ""
        assert spec.type == "markdown"
        assert spec.required is True

    def test_custom_values(self) -> None:
        """测试自定义值。"""
        spec = DeliverableSpec(
            name="report",
            type="json",
            required=False,
            template_source="knowledge",
        )
        assert spec.name == "report"
        assert spec.type == "json"
        assert spec.required is False
        assert spec.template_source == "knowledge"


class TestMetricRef:
    """MetricRef 数据类测试。"""

    def test_default_values(self) -> None:
        """测试默认值。"""
        ref = MetricRef()
        assert ref.metric_id == ""
        assert ref.default_params == {}

    def test_custom_values(self) -> None:
        """测试自定义值。"""
        ref = MetricRef(
            metric_id="file_check",
            default_params={"action": "read"},
        )
        assert ref.metric_id == "file_check"
        assert ref.default_params == {"action": "read"}


class TestAgentConfig:
    """AgentConfig 数据类测试。"""

    def test_default_values(self) -> None:
        """测试默认值。"""
        config = AgentConfig()
        assert config.config_id == ""
        assert config.agent_type == AgentType.SPECIALIZED
        assert config.level == AgentLevel.L3_ATOMIC
        assert config.is_active is True
        assert config.timeout_seconds == -1
        assert config.tool_ids == []
        assert config.hard_constraints == []
        assert config.soft_constraints == []

    def test_custom_values(self) -> None:
        """测试自定义值。"""
        config = AgentConfig(
            config_id="test_agent",
            name="测试",
            agent_type=AgentType.MAIN,
            level=AgentLevel.L1_MAIN,
            tool_ids=["tool1", "tool2"],
        )
        assert config.config_id == "test_agent"
        assert config.name == "测试"
        assert config.agent_type == AgentType.MAIN
        assert config.level == AgentLevel.L1_MAIN
        assert config.tool_ids == ["tool1", "tool2"]

    def test_nested_structures(self) -> None:
        """测试嵌套数据结构。"""
        config = AgentConfig(
            config_id="nested_test",
            static_vars=ContextConfig(
                enabled=True,
                items=[ContextVarItem(name="规则", type="rules")],
            ),
            hard_constraints=["约束1", "约束2"],
            deliverables=[DeliverableSpec(name="报告", type="markdown")],
        )
        assert len(config.static_vars.items) == 1
        assert config.static_vars.items[0].name == "规则"
        assert len(config.hard_constraints) == 2
        assert config.deliverables[0].name == "报告"


# ============================================================================
# 2. loader.py 测试
# ============================================================================


class TestAgentConfigLoader:
    """AgentConfigLoader 加载器测试。"""

    def test_load_main_agent_yaml(self) -> None:
        """测试加载主控 Agent YAML。"""
        path = CONFIG_DIR / "test_main.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        assert config.config_id == "test_main_agent"
        assert config.name == "测试主控"
        assert config.agent_type == AgentType.MAIN
        assert config.level == AgentLevel.L1_MAIN
        assert config.category == "system"
        assert "task_submit" in config.tool_ids
        assert "resource_search" in config.tool_ids

    def test_load_orchestrator_yaml(self) -> None:
        """测试加载编排 Agent YAML。"""
        path = CONFIG_DIR / "test_orchestrator.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        assert config.config_id == "test_orchestrator_agent"
        assert config.agent_type == AgentType.SPECIALIZED  # orchestrator 映射为 specialized
        assert config.level == AgentLevel.L2_SUBTASK
        assert config.category == "task"

    def test_load_evaluator_yaml(self) -> None:
        """测试加载系统 Agent YAML。"""
        path = CONFIG_DIR / "test_evaluator.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        assert config.config_id == "test_evaluator_agent"
        assert config.agent_type == AgentType.SYSTEM
        assert config.level == AgentLevel.L3_ATOMIC
        assert "file_read" in config.tool_ids

    def test_load_static_vars(self) -> None:
        """测试加载静态变量配置。"""
        path = CONFIG_DIR / "test_main.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        assert config.static_vars.enabled is True
        assert len(config.static_vars.items) >= 1
        # 第一个项应该是 rules 类型
        rules_item = config.static_vars.items[0]
        assert rules_item.name == "行为约束"
        assert rules_item.type == "rules"

    def test_load_dynamic_vars(self) -> None:
        """测试加载动态变量配置。"""
        path = CONFIG_DIR / "test_main.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        assert config.dynamic_vars.enabled is True
        assert len(config.dynamic_vars.items) >= 1
        ts_item = config.dynamic_vars.items[0]
        assert ts_item.type == "timestamp"

    def test_load_inline_content(self) -> None:
        """测试加载内联内容。"""
        path = CONFIG_DIR / "test_main.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        # 第二个 static_vars 项有 content 字段
        content_item = config.static_vars.items[1]
        assert content_item.name == "可扩展工具索引"
        assert "file_read" in content_item.content

    def test_load_path_type(self) -> None:
        """测试加载 path 类型变量。"""
        path = CONFIG_DIR / "test_orchestrator.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        path_item = config.static_vars.items[1]
        assert path_item.name == "执行报告模板"
        assert path_item.type == "path"
        assert "execution_report_template" in path_item.path

    def test_load_schemas(self) -> None:
        """测试加载 input/output schema。"""
        path = CONFIG_DIR / "test_orchestrator.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        assert "properties" in config.input_schema
        assert "phase" in config.input_schema["properties"]
        assert config.input_schema["required"] == ["phase", "task_description"]
        assert "properties" in config.output_schema
        assert "result_summary" in config.output_schema["properties"]

    def test_load_deliverables(self) -> None:
        """测试加载产出物定义。"""
        path = CONFIG_DIR / "test_orchestrator.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        assert len(config.deliverables) == 2
        assert config.deliverables[0].name == "trigger_config"
        assert config.deliverables[0].type == "json"
        assert config.deliverables[0].required is True
        assert config.deliverables[1].name == "execution_log"
        assert config.deliverables[1].required is False

    def test_load_metrics(self) -> None:
        """测试加载评估指标引用。"""
        path = CONFIG_DIR / "test_orchestrator.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        assert len(config.recommended_metrics) == 1
        assert config.recommended_metrics[0].metric_id == "file_check"
        assert config.recommended_metrics[0].default_params["action"] == "read"

    def test_load_constraints(self) -> None:
        """测试加载约束规则。"""
        path = CONFIG_DIR / "test_main.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        assert len(config.hard_constraints) >= 2
        assert any("派发" in c for c in config.hard_constraints)
        assert len(config.soft_constraints) >= 1

    def test_load_tags_and_metadata(self) -> None:
        """测试加载标签和元数据。"""
        path = CONFIG_DIR / "test_main.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        assert "main" in config.tags
        assert "L1" in config.tags
        assert config.metadata.get("author") == "Test"

    def test_load_file_not_found(self) -> None:
        """测试文件不存在时抛出异常。"""
        with pytest.raises(FileNotFoundError, match="不存在"):
            AgentConfigLoader.load_from_yaml("/nonexistent/agent.yaml")

    def test_load_missing_config_id(self) -> None:
        """测试缺少 config_id 时抛出异常。"""
        path = CONFIG_DIR / "invalid_no_id.yaml"
        with pytest.raises(ValueError, match="config_id"):
            AgentConfigLoader.load_from_yaml(path)

    def test_load_invalid_level(self) -> None:
        """测试无效 level 值时抛出异常。"""
        path = CONFIG_DIR / "invalid_bad_level.yaml"
        with pytest.raises(ValueError, match="层级"):
            AgentConfigLoader.load_from_yaml(path)

    def test_load_from_directory(self) -> None:
        """测试从目录递归加载。"""
        configs = AgentConfigLoader.load_from_directory(CONFIG_DIR)
        # 至少有 3 个有效测试 YAML
        valid_ids = {c.config_id for c in configs}
        assert "test_main_agent" in valid_ids
        assert "test_orchestrator_agent" in valid_ids
        assert "test_evaluator_agent" in valid_ids

    def test_load_directory_not_found(self) -> None:
        """测试目录不存在时抛出异常。"""
        with pytest.raises(FileNotFoundError, match="不存在"):
            AgentConfigLoader.load_from_directory("/nonexistent/dir")

    def test_load_from_directory_strict_true_raises_on_syntax_error(
        self, tmp_path: Path
    ) -> None:
        """strict=True（默认）时 YAML 语法错误必须上抛（fail-fast 契约守护）。"""
        (tmp_path / "good.yaml").write_text(
            "config_id: good_agent\nname: Good\nlevel: L3\n", encoding="utf-8"
        )
        # 与 test_yaml_error_chain 一致的坏缩进语法错误
        (tmp_path / "bad.yaml").write_text(
            "config_id: bad\nname: Bad\n  bad_indent: oops\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="YAML 解析失败"):
            AgentConfigLoader.load_from_directory(tmp_path)

    def test_load_from_directory_strict_false_isolates_syntax_error(
        self, tmp_path: Path
    ) -> None:
        """strict=False 时语法错误按文件隔离：好配置照常加载，坏文件跳过。"""
        (tmp_path / "good.yaml").write_text(
            "config_id: good_agent\nname: Good\nlevel: L3\n", encoding="utf-8"
        )
        (tmp_path / "bad.yaml").write_text(
            "config_id: bad\nname: Bad\n  bad_indent: oops\n", encoding="utf-8"
        )
        configs = AgentConfigLoader.load_from_directory(tmp_path, strict=False)
        ids = {c.config_id for c in configs}
        assert ids == {"good_agent"}

    def test_load_empty_yaml(self) -> None:
        """测试加载空 YAML 文件。"""
        temp_path = Path(tempfile.gettempdir()) / "test_empty_agent.yaml"
        try:
            temp_path.write_text("", encoding="utf-8")
            with pytest.raises(ValueError, match="字典类型"):
                AgentConfigLoader.load_from_yaml(str(temp_path))
        finally:
            temp_path.unlink(missing_ok=True)

    def test_load_retrieval_type_vars(self) -> None:
        """测试加载 retrieval 类型变量（带 tags/memory_type/memory_layer）。"""
        path = CONFIG_DIR / "test_evaluator.yaml"
        config = AgentConfigLoader.load_from_yaml(path)
        # 动态变量中有 retrieval 类型项
        retrieval_items = [
            i for i in config.dynamic_vars.items if i.tags or i.inject_type == "retrieval"
        ]
        assert len(retrieval_items) >= 1
        item = retrieval_items[0]
        assert "recent_context" in item.tags
        assert item.memory_type == "chunk"
        assert item.memory_layer == "l1"


# ============================================================================
# 3. registry.py 测试
# ============================================================================


class TestAgentRegistry:
    """AgentRegistry 注册表测试。"""

    def _make_config(self, config_id: str = "test", **kwargs: object) -> AgentConfig:
        """创建测试用 AgentConfig。"""
        defaults: dict[str, object] = {
            "config_id": config_id,
            "name": f"测试_{config_id}",
            "agent_type": AgentType.SPECIALIZED,
            "level": AgentLevel.L3_ATOMIC,
            "category": "test",
            "tags": ["test"],
            "tool_ids": ["tool_a"],
        }
        defaults.update(kwargs)
        return AgentConfig(**defaults)  # type: ignore[arg-type]

    def test_register_and_get(self) -> None:
        """测试注册和查找。"""
        registry = AgentRegistry()
        config = self._make_config("agent_1")
        registry.register(config)
        assert registry.get("agent_1") is config

    def test_get_not_found(self) -> None:
        """测试查找不存在的配置。"""
        registry = AgentRegistry()
        assert registry.get("nonexistent") is None

    def test_register_empty_id_raises(self) -> None:
        """测试注册空 config_id 抛出异常。"""
        registry = AgentRegistry()
        config = AgentConfig(config_id="")
        with pytest.raises(ValueError, match="不能为空"):
            registry.register(config)

    def test_register_overwrite(self) -> None:
        """测试覆盖已有配置。"""
        registry = AgentRegistry()
        config1 = self._make_config("agent_1", name="v1")
        config2 = self._make_config("agent_1", name="v2")
        registry.register(config1)
        registry.register(config2)
        assert registry.get("agent_1").name == "v2"

    def test_find_by_level(self) -> None:
        """测试按层级筛选。"""
        registry = AgentRegistry()
        registry.register(self._make_config("l1", level=AgentLevel.L1_MAIN))
        registry.register(self._make_config("l2", level=AgentLevel.L2_SUBTASK))
        registry.register(self._make_config("l3", level=AgentLevel.L3_ATOMIC))

        l1 = registry.find_by_level(AgentLevel.L1_MAIN)
        assert len(l1) == 1
        assert l1[0].config_id == "l1"

        l3 = registry.find_by_level(AgentLevel.L3_ATOMIC)
        assert len(l3) == 1

    def test_find_by_type(self) -> None:
        """测试按类型筛选。"""
        registry = AgentRegistry()
        registry.register(self._make_config("main", agent_type=AgentType.MAIN))
        registry.register(self._make_config("spec", agent_type=AgentType.SPECIALIZED))
        registry.register(self._make_config("sys", agent_type=AgentType.SYSTEM))

        mains = registry.find_by_type(AgentType.MAIN)
        assert len(mains) == 1
        assert mains[0].config_id == "main"

    def test_find_by_category(self) -> None:
        """测试按分类筛选。"""
        registry = AgentRegistry()
        registry.register(self._make_config("a1", category="code"))
        registry.register(self._make_config("a2", category="task"))
        registry.register(self._make_config("a3", category="code"))

        code_agents = registry.find_by_category("code")
        assert len(code_agents) == 2

    def test_find_by_tag(self) -> None:
        """测试按标签筛选。"""
        registry = AgentRegistry()
        registry.register(self._make_config("a1", tags=["code", "test"]))
        registry.register(self._make_config("a2", tags=["system"]))

        tagged = registry.find_by_tag("code")
        assert len(tagged) == 1
        assert tagged[0].config_id == "a1"

    def test_find_by_tool(self) -> None:
        """测试按工具筛选。"""
        registry = AgentRegistry()
        registry.register(self._make_config("a1", tool_ids=["file_read", "bash"]))
        registry.register(self._make_config("a2", tool_ids=["web_search"]))

        tool_agents = registry.find_by_tool("file_read")
        assert len(tool_agents) == 1
        assert tool_agents[0].config_id == "a1"

    def test_list_all(self) -> None:
        """测试列出所有配置。"""
        registry = AgentRegistry()
        registry.register(self._make_config("a1"))
        registry.register(self._make_config("a2"))
        assert len(registry.list_all()) == 2

    def test_count(self) -> None:
        """测试计数。"""
        registry = AgentRegistry()
        assert registry.count() == 0
        registry.register(self._make_config("a1"))
        assert registry.count() == 1

    def test_unregister(self) -> None:
        """测试注销配置。"""
        registry = AgentRegistry()
        registry.register(self._make_config("a1"))
        assert registry.unregister("a1") is True
        assert registry.get("a1") is None
        assert registry.unregister("a1") is False

    def test_load_directory(self) -> None:
        """测试从目录批量加载。"""
        registry = AgentRegistry()
        count = registry.load_directory(CONFIG_DIR)
        assert count >= 3
        assert registry.get("test_main_agent") is not None
        assert registry.get("test_orchestrator_agent") is not None

    def test_load_directory_isolates_bad_yaml(self, tmp_path: Path) -> None:
        """单个坏 YAML 不应拖垮整体加载：好 agent 照常注册，坏文件跳过。"""
        (tmp_path / "good.yaml").write_text(
            "config_id: good_agent\nname: Good\nlevel: L3\n", encoding="utf-8"
        )
        (tmp_path / "bad.yaml").write_text(
            "config_id: bad\nname: Bad\n  bad_indent: oops\n", encoding="utf-8"
        )
        registry = AgentRegistry()
        count = registry.load_directory(tmp_path)
        assert count == 1
        assert registry.get("good_agent") is not None
        assert registry.get("bad") is None


# ============================================================================
# 4. context_builder.py 测试
# ============================================================================


class TestContextBuilder:
    """ContextBuilder 上下文构建器测试。"""

    def _make_config_with_context(
        self,
        static_items: list[ContextVarItem] | None = None,
        dynamic_items: list[ContextVarItem] | None = None,
        hard_constraints: list[str] | None = None,
        soft_constraints: list[str] | None = None,
    ) -> AgentConfig:
        """创建带上下文配置的测试 AgentConfig。"""
        return AgentConfig(
            config_id="ctx_test",
            hard_constraints=hard_constraints or ["约束1"],
            soft_constraints=soft_constraints or ["建议1"],
            static_vars=ContextConfig(
                enabled=True, items=static_items or []
            ),
            dynamic_vars=ContextConfig(
                enabled=True, items=dynamic_items or []
            ),
        )

    def test_build_static_context_rules(self) -> None:
        """测试构建 rules 类型的静态上下文。"""
        config = self._make_config_with_context(
            static_items=[ContextVarItem(name="行为约束", type="rules")],
            hard_constraints=["必须遵守规则A", "禁止做B"],
        )
        builder = ContextBuilder()
        ctx = builder.build_static_context(config)
        assert ctx["enabled"] is True
        rules_item = ctx["items"][0]
        assert rules_item["name"] == "行为约束"
        assert "必须遵守规则A" in rules_item["content"]
        assert "禁止做B" in rules_item["content"]

    def test_build_static_context_inline_content(self) -> None:
        """测试构建 inline 内容的静态上下文。"""
        config = self._make_config_with_context(
            static_items=[
                ContextVarItem(name="工具索引", content="file_read, file_write")
            ],
        )
        builder = ContextBuilder()
        ctx = builder.build_static_context(config)
        inline_item = ctx["items"][0]
        assert inline_item["type"] == "inline"
        assert "file_read" in inline_item["content"]

    def test_build_static_context_path(self) -> None:
        """测试构建 path 类型的静态上下文。"""
        # 创建临时文件
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write("# 测试模板\n这是内容")
            temp_path = f.name

        try:
            config = self._make_config_with_context(
                static_items=[
                    ContextVarItem(name="模板", type="path", path=temp_path)
                ],
            )
            builder = ContextBuilder(base_path=Path(temp_path).parent)
            ctx = builder.build_static_context(config)
            path_item = ctx["items"][0]
            assert path_item["type"] == "path"
            assert "测试模板" in path_item["content"]
        finally:
            os.unlink(temp_path)

    def test_build_static_context_path_not_found(self) -> None:
        """测试 path 类型文件不存在时返回空内容。"""
        config = self._make_config_with_context(
            static_items=[
                ContextVarItem(name="不存在", type="path", path="/nonexistent/file.md")
            ],
        )
        builder = ContextBuilder()
        ctx = builder.build_static_context(config)
        path_item = ctx["items"][0]
        assert path_item["content"] == ""

    def test_build_dynamic_context_timestamp(self) -> None:
        """测试构建 timestamp 类型的动态上下文。"""
        config = self._make_config_with_context(
            dynamic_items=[ContextVarItem(name="当前时间", type="timestamp")],
        )
        builder = ContextBuilder()
        ctx = builder.build_dynamic_context(config)
        ts_item = ctx["items"][0]
        assert ts_item["type"] == "timestamp"
        assert "T" in ts_item["content"]  # ISO 格式包含 T

    def test_build_dynamic_context_session(self) -> None:
        """测试构建 session 类型的动态上下文。"""
        config = self._make_config_with_context(
            dynamic_items=[ContextVarItem(name="会话信息", type="session")],
        )
        builder = ContextBuilder()
        ctx = builder.build_dynamic_context(config)
        session_item = ctx["items"][0]
        assert session_item["type"] == "session"
        assert "session_id" in session_item["content"]

    def test_build_dynamic_context_agent(self) -> None:
        """测试构建 agent 类型的动态上下文。"""
        config = self._make_config_with_context(
            dynamic_items=[ContextVarItem(name="当前Agent", type="agent")],
        )
        builder = ContextBuilder()
        ctx = builder.build_dynamic_context(config)
        agent_item = ctx["items"][0]
        assert agent_item["type"] == "agent"
        assert "agent_id" in agent_item["content"]

    def test_build_dynamic_context_model(self) -> None:
        """测试构建 model 类型的动态上下文。"""
        config = self._make_config_with_context(
            dynamic_items=[ContextVarItem(name="当前模型", type="model")],
        )
        builder = ContextBuilder()
        ctx = builder.build_dynamic_context(config)
        model_item = ctx["items"][0]
        assert model_item["type"] == "model"
        assert "model_id" in model_item["content"]

    def test_build_context_disabled(self) -> None:
        """测试禁用上下文时返回空结果。"""
        config = AgentConfig(
            config_id="disabled_test",
            static_vars=ContextConfig(enabled=False, items=[]),
            dynamic_vars=ContextConfig(enabled=False, items=[]),
        )
        builder = ContextBuilder()
        static = builder.build_static_context(config)
        dynamic = builder.build_dynamic_context(config)
        assert static["enabled"] is False
        assert dynamic["enabled"] is False

    def test_build_full_context(self) -> None:
        """测试构建完整上下文（静态 + 动态合并）。"""
        config = self._make_config_with_context(
            static_items=[ContextVarItem(name="规则", type="rules")],
            dynamic_items=[ContextVarItem(name="时间", type="timestamp")],
        )
        builder = ContextBuilder()
        full = builder.build_full_context(config)
        assert "static" in full
        assert "dynamic" in full
        assert full["static"]["enabled"] is True
        assert full["dynamic"]["enabled"] is True

    def test_build_retrieval_type(self) -> None:
        """测试构建 retrieval 类型的上下文。"""
        config = self._make_config_with_context(
            dynamic_items=[
                ContextVarItem(
                    tags=["recent_context"],
                    inject_type="retrieval",
                    top_k=3,
                    memory_type="chunk",
                    memory_layer="l1",
                )
            ],
        )
        builder = ContextBuilder()
        ctx = builder.build_dynamic_context(config)
        retrieval_item = ctx["items"][0]
        assert retrieval_item["type"] == "retrieval"
        assert "recent_context" in retrieval_item["tags"]
        assert retrieval_item["top_k"] == 3

    def test_build_routed_static_var(self) -> None:
        """测试构建 routed 类型的静态上下文变量。"""
        config = self._make_config_with_context(
            static_items=[
                ContextVarItem(
                    name="场景人格",
                    type="routed",
                    route_key="scene",
                    routes={
                        "coding": "你是专业的编程助手",
                        "chatting": "你是温暖的聊天伙伴",
                        "_default": "你是全能助手",
                    },
                )
            ],
        )
        builder = ContextBuilder()
        ctx = builder.build_static_context(config)
        routed_item = ctx["items"][0]
        assert routed_item["type"] == "routed"
        assert routed_item["name"] == "场景人格"
        assert routed_item["route_key"] == "scene"
        assert "coding" in routed_item["routes"]
        assert "_default" in routed_item["routes"]

    def test_build_routed_dynamic_var(self) -> None:
        """测试构建 routed 类型的动态上下文变量。"""
        config = self._make_config_with_context(
            dynamic_items=[
                ContextVarItem(
                    name="时间段问候",
                    type="routed",
                    route_key="time_period",
                    routes={
                        "morning": "早上好",
                        "evening": "晚上好",
                    },
                )
            ],
        )
        builder = ContextBuilder()
        ctx = builder.build_dynamic_context(config)
        routed_item = ctx["items"][0]
        assert routed_item["type"] == "routed"
        assert routed_item["route_key"] == "time_period"

    def test_to_state_includes_routed_fields(self) -> None:
        """测试 to_state() 包含 route_key 和 routes 字段。"""
        config = AgentConfig(
            config_id="routed_state_test",
            static_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(
                        name="人格",
                        type="routed",
                        route_key="scene",
                        routes={"coding": "编程助手"},
                    )
                ],
            ),
            dynamic_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(
                        name="模式",
                        type="routed",
                        route_key="mode",
                        routes={"fast": "快速模式"},
                    )
                ],
            ),
        )
        state = config.to_state()
        static_var = state["context.static_vars"][0]
        assert static_var["route_key"] == "scene"
        assert static_var["routes"] == {"coding": "编程助手"}
        dynamic_var = state["context.dynamic_vars"][0]
        assert dynamic_var["route_key"] == "mode"
        assert dynamic_var["routes"] == {"fast": "快速模式"}

    def test_loader_parses_routed_fields(self) -> None:
        """测试 AgentConfigLoader 解析 routed 相关字段。"""
        from agents.loader import AgentConfigLoader

        item_data = {
            "name": "场景人格",
            "type": "routed",
            "route_key": "scene",
            "routes": {
                "coding": "你是专业的编程助手",
                "_default": "你是全能助手",
            },
        }
        item = AgentConfigLoader._parse_context_var_item(item_data)
        assert item.type == "routed"
        assert item.route_key == "scene"
        assert item.routes["coding"] == "你是专业的编程助手"
        assert item.routes["_default"] == "你是全能助手"

    def test_build_static_context_folder(self) -> None:
        """测试构建 folder 类型的静态上下文 — 自动加载文件夹内容。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建子目录并放入多个文件
            docs_dir = Path(tmpdir) / "my_docs"
            docs_dir.mkdir()
            (docs_dir / "guide.md").write_text("# 指南\n这是使用指南", encoding="utf-8")
            (docs_dir / "rules.txt").write_text("规则1\n规则2", encoding="utf-8")
            (docs_dir / "ignored.json").write_text('{"skip": true}', encoding="utf-8")

            config = self._make_config_with_context(
                static_items=[
                    ContextVarItem(name="文档目录", type="folder", path="my_docs"),
                ],
            )
            builder = ContextBuilder(base_path=tmpdir)
            ctx = builder.build_static_context(config)
            folder_item = ctx["items"][0]
            assert folder_item["type"] == "folder"
            assert folder_item["name"] == "文档目录"
            # 应包含所有文件内容
            assert "指南" in folder_item["content"]
            assert "规则1" in folder_item["content"]
            assert "skip" in folder_item["content"]

    def test_build_static_context_folder_with_extension_filter(self) -> None:
        """测试 folder 类型使用 extensions 过滤文件扩展名。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            docs_dir = Path(tmpdir) / "my_docs"
            docs_dir.mkdir()
            (docs_dir / "guide.md").write_text("# 指南", encoding="utf-8")
            (docs_dir / "rules.txt").write_text("规则", encoding="utf-8")
            (docs_dir / "data.json").write_text('{"key": "val"}', encoding="utf-8")

            config = self._make_config_with_context(
                static_items=[
                    ContextVarItem(
                        name="文档目录",
                        type="folder",
                        path="my_docs",
                        extensions=[".md"],
                    ),
                ],
            )
            builder = ContextBuilder(base_path=tmpdir)
            ctx = builder.build_static_context(config)
            folder_item = ctx["items"][0]
            assert folder_item["type"] == "folder"
            assert "指南" in folder_item["content"]
            # .txt 和 .json 文件应被过滤掉
            assert "规则" not in folder_item["content"]
            assert '"key"' not in folder_item["content"]
            assert folder_item["extensions"] == [".md"]

    def test_build_static_context_folder_not_found(self) -> None:
        """测试 folder 类型文件夹不存在时返回空内容。"""
        config = self._make_config_with_context(
            static_items=[
                ContextVarItem(name="不存在", type="folder", path="/nonexistent/folder"),
            ],
        )
        builder = ContextBuilder()
        ctx = builder.build_static_context(config)
        folder_item = ctx["items"][0]
        assert folder_item["content"] == ""

    def test_build_static_context_folder_empty_dir(self) -> None:
        """测试 folder 类型文件夹为空时返回空内容。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config_with_context(
                static_items=[
                    ContextVarItem(name="空目录", type="folder", path="empty"),
                ],
            )
            builder = ContextBuilder(base_path=tmpdir)
            ctx = builder.build_static_context(config)
            folder_item = ctx["items"][0]
            assert folder_item["content"] == ""

    def test_build_static_context_folder_empty_path(self) -> None:
        """folder 类型未指定 path 时不应回退到 base_path，必须显式给定路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "stray.md").write_text("不应被注入", encoding="utf-8")

            config = self._make_config_with_context(
                static_items=[
                    ContextVarItem(name="空路径", type="folder", path=""),
                ],
            )
            builder = ContextBuilder(base_path=tmpdir)
            ctx = builder.build_static_context(config)
            folder_item = ctx["items"][0]
            assert folder_item["type"] == "folder"
            # 不允许默认注入整个工作空间/base_path
            assert folder_item["content"] == ""
            assert "不应被注入" not in folder_item["content"]

    def test_build_dynamic_context_folder(self) -> None:
        """测试构建 folder 类型的动态上下文。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            (logs_dir / "status.txt").write_text("运行中", encoding="utf-8")

            config = self._make_config_with_context(
                dynamic_items=[
                    ContextVarItem(name="状态文件", type="folder", path="logs"),
                ],
            )
            builder = ContextBuilder(base_path=tmpdir)
            ctx = builder.build_dynamic_context(config)
            folder_item = ctx["items"][0]
            assert folder_item["type"] == "folder"
            assert "运行中" in folder_item["content"]

    def test_to_state_includes_folder_extensions(self) -> None:
        """测试 to_state() 包含 folder 类型的 extensions 字段。"""
        config = AgentConfig(
            config_id="folder_state_test",
            static_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(
                        name="代码目录",
                        type="folder",
                        path="src/",
                        extensions=[".py"],
                    ),
                ],
            ),
            dynamic_vars=ContextConfig(
                enabled=True,
                items=[
                    ContextVarItem(
                        name="日志目录",
                        type="folder",
                        path="logs/",
                        extensions=[".log", ".txt"],
                    ),
                ],
            ),
        )
        state = config.to_state()
        static_var = state["context.static_vars"][0]
        assert static_var["extensions"] == [".py"]
        dynamic_var = state["context.dynamic_vars"][0]
        assert dynamic_var["extensions"] == [".log", ".txt"]

    def test_loader_parses_folder_with_extensions(self) -> None:
        """测试 AgentConfigLoader 解析 folder 类型的 extensions 字段。"""
        from agents.loader import AgentConfigLoader

        item_data = {
            "name": "代码目录",
            "type": "folder",
            "path": "src/",
            "extensions": [".py", ".md"],
        }
        item = AgentConfigLoader._parse_context_var_item(item_data)
        assert item.type == "folder"
        assert item.path == "src/"
        assert item.extensions == [".py", ".md"]

    def test_loader_parses_folder_without_extensions(self) -> None:
        """测试 AgentConfigLoader 解析无 extensions 的 folder 类型。"""
        from agents.loader import AgentConfigLoader

        item_data = {
            "name": "全部文件",
            "type": "folder",
            "path": "data/",
        }
        item = AgentConfigLoader._parse_context_var_item(item_data)
        assert item.type == "folder"
        assert item.extensions == []


# ============================================================================
# 5. schema_validator.py 测试
# ============================================================================


class TestSchemaValidator:
    """SchemaValidator Schema 验证器测试。"""

    def test_validate_input_success(self) -> None:
        """测试输入验证通过。"""
        config = AgentConfig(
            config_id="schema_test",
            input_schema={
                "type": "object",
                "properties": {
                    "phase": {"type": "string"},
                    "task_description": {"type": "string"},
                },
                "required": ["phase", "task_description"],
            },
        )
        validator = SchemaValidator()
        errors = validator.validate_input(
            config, {"phase": "setup", "task_description": "测试"}
        )
        assert errors == []

    def test_validate_input_missing_required(self) -> None:
        """测试输入缺少必填字段。"""
        config = AgentConfig(
            config_id="schema_test",
            input_schema={
                "type": "object",
                "properties": {
                    "phase": {"type": "string"},
                },
                "required": ["phase"],
            },
        )
        validator = SchemaValidator()
        errors = validator.validate_input(config, {})
        assert len(errors) >= 1
        assert any("phase" in e for e in errors)

    def test_validate_input_wrong_type(self) -> None:
        """测试输入类型错误。"""
        config = AgentConfig(
            config_id="schema_test",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                },
                "required": ["count"],
            },
        )
        validator = SchemaValidator()
        errors = validator.validate_input(config, {"count": "not_a_number"})
        assert len(errors) >= 1
        assert any("类型错误" in e for e in errors)

    def test_validate_input_enum_check(self) -> None:
        """测试输入枚举值检查。"""
        config = AgentConfig(
            config_id="schema_test",
            input_schema={
                "type": "object",
                "properties": {
                    "phase": {
                        "type": "string",
                        "enum": ["setup", "triggered", "completed"],
                    },
                },
                "required": ["phase"],
            },
        )
        validator = SchemaValidator()
        errors = validator.validate_input(config, {"phase": "invalid"})
        assert len(errors) >= 1
        assert any("枚举" in e for e in errors)

    def test_validate_input_no_schema(self) -> None:
        """测试无 input_schema 时直接通过。"""
        config = AgentConfig(config_id="no_schema")
        validator = SchemaValidator()
        errors = validator.validate_input(config, {"anything": "ok"})
        assert errors == []

    def test_validate_output_success(self) -> None:
        """测试输出验证通过。"""
        config = AgentConfig(
            config_id="schema_test",
            output_schema={
                "type": "object",
                "properties": {
                    "phase": {"type": "string"},
                    "result_summary": {"type": "string"},
                },
                "required": ["phase", "result_summary"],
            },
        )
        validator = SchemaValidator()
        errors = validator.validate_output(
            config, {"phase": "completed", "result_summary": "成功"}
        )
        assert errors == []

    def test_validate_output_missing_required(self) -> None:
        """测试输出缺少必填字段。"""
        config = AgentConfig(
            config_id="schema_test",
            output_schema={
                "type": "object",
                "properties": {
                    "passed": {"type": "boolean"},
                    "score": {"type": "number"},
                },
                "required": ["passed", "score"],
            },
        )
        validator = SchemaValidator()
        errors = validator.validate_output(config, {"passed": True})
        assert len(errors) >= 1
        assert any("score" in e for e in errors)

    def test_validate_output_wrong_type_boolean(self) -> None:
        """测试输出 boolean 类型错误。"""
        config = AgentConfig(
            config_id="schema_test",
            output_schema={
                "type": "object",
                "properties": {
                    "passed": {"type": "boolean"},
                },
                "required": ["passed"],
            },
        )
        validator = SchemaValidator()
        errors = validator.validate_output(config, {"passed": "yes"})
        assert len(errors) >= 1
        assert any("类型错误" in e for e in errors)

    def test_validate_nested_object(self) -> None:
        """测试嵌套 object 的验证。"""
        config = AgentConfig(
            config_id="schema_test",
            input_schema={
                "type": "object",
                "properties": {
                    "trigger_config": {
                        "type": "object",
                        "properties": {
                            "trigger_type": {"type": "string"},
                        },
                        "required": ["trigger_type"],
                    },
                },
                "required": ["trigger_config"],
            },
        )
        validator = SchemaValidator()
        # 缺少嵌套的必填字段
        errors = validator.validate_input(
            config, {"trigger_config": {}}
        )
        assert len(errors) >= 1
        assert any("trigger_type" in e for e in errors)

    def test_validate_no_schema_passes(self) -> None:
        """测试无 output_schema 时直接通过。"""
        config = AgentConfig(config_id="no_schema")
        validator = SchemaValidator()
        errors = validator.validate_output(config, {"anything": "ok"})
        assert errors == []

    def test_validate_non_required_missing_ok(self) -> None:
        """测试非必填字段缺失不报错。"""
        config = AgentConfig(
            config_id="schema_test",
            input_schema={
                "type": "object",
                "properties": {
                    "required_field": {"type": "string"},
                    "optional_field": {"type": "string"},
                },
                "required": ["required_field"],
            },
        )
        validator = SchemaValidator()
        errors = validator.validate_input(config, {"required_field": "ok"})
        assert errors == []

    def test_validate_number_type(self) -> None:
        """测试 number 类型验证（int 和 float 都通过）。"""
        config = AgentConfig(
            config_id="schema_test",
            input_schema={
                "type": "object",
                "properties": {
                    "score": {"type": "number"},
                },
                "required": ["score"],
            },
        )
        validator = SchemaValidator()
        # int 应该通过
        errors = validator.validate_input(config, {"score": 85})
        assert errors == []
        # float 也应该通过
        errors = validator.validate_input(config, {"score": 85.5})
        assert errors == []

    def test_validate_array_type(self) -> None:
        """测试 array 类型验证。"""
        config = AgentConfig(
            config_id="schema_test",
            input_schema={
                "type": "object",
                "properties": {
                    "items": {"type": "array"},
                },
                "required": ["items"],
            },
        )
        validator = SchemaValidator()
        errors = validator.validate_input(config, {"items": [1, 2, 3]})
        assert errors == []
        errors = validator.validate_input(config, {"items": "not array"})
        assert len(errors) >= 1

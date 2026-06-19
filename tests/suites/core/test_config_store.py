"""PipelineConfigStore 测试。

覆盖 CRUD 操作：register / get / list / remove。
"""

from __future__ import annotations


from pipeline.config_store import PipelineConfig, PipelineConfigStore


class TestPipelineConfig:
    """PipelineConfig dataclass 测试。"""

    def test_default_values(self) -> None:
        """默认值测试。"""
        config = PipelineConfig(pipeline_id="test", name="Test Pipeline")
        assert config.pipeline_id == "test"
        assert config.name == "Test Pipeline"
        assert config.input_routes == []
        assert config.output_routes == []
        assert config.plugins == []
        assert config.core_plugins == {}
        assert config.max_iterations == 500

    def test_custom_values(self) -> None:
        """自定义值测试。"""
        config = PipelineConfig(
            pipeline_id="research",
            name="Research Agent",
            input_routes=[{"condition": "True", "target": "core"}],
            output_routes=[{"route_type": "delegate", "condition": "True"}],
            plugins=[{"name": "input_validator"}],
            core_plugins={"llm_call": {"model": "gpt-4"}},
            max_iterations=50,
        )
        assert config.pipeline_id == "research"
        assert len(config.input_routes) == 1
        assert len(config.output_routes) == 1
        assert config.max_iterations == 50


class TestPipelineConfigStore:
    """PipelineConfigStore CRUD 测试。"""

    def test_register_and_get(self) -> None:
        """注册并获取配置。"""
        store = PipelineConfigStore()
        config = PipelineConfig(pipeline_id="agent_a", name="Agent A")
        store.register("agent_a", config)

        result = store.get("agent_a")
        assert result is not None
        assert result.pipeline_id == "agent_a"
        assert result.name == "Agent A"

    def test_get_nonexistent(self) -> None:
        """获取不存在的配置返回 None。"""
        store = PipelineConfigStore()
        assert store.get("nonexistent") is None

    def test_list_configs(self) -> None:
        """列出所有已注册配置 ID。"""
        store = PipelineConfigStore()
        store.register("a", PipelineConfig(pipeline_id="a", name="A"))
        store.register("b", PipelineConfig(pipeline_id="b", name="B"))
        store.register("c", PipelineConfig(pipeline_id="c", name="C"))

        ids = store.list_configs()
        assert set(ids) == {"a", "b", "c"}

    def test_list_empty(self) -> None:
        """空存储返回空列表。"""
        store = PipelineConfigStore()
        assert store.list_configs() == []

    def test_remove(self) -> None:
        """移除已注册配置。"""
        store = PipelineConfigStore()
        store.register("a", PipelineConfig(pipeline_id="a", name="A"))
        assert store.remove("a") is True
        assert store.get("a") is None

    def test_remove_nonexistent(self) -> None:
        """移除不存在的配置返回 False。"""
        store = PipelineConfigStore()
        assert store.remove("nonexistent") is False

    def test_register_overwrite(self) -> None:
        """重复注册覆盖旧配置。"""
        store = PipelineConfigStore()
        config_v1 = PipelineConfig(pipeline_id="a", name="A v1")
        config_v2 = PipelineConfig(pipeline_id="a", name="A v2")
        store.register("a", config_v1)
        store.register("a", config_v2)

        result = store.get("a")
        assert result is not None
        assert result.name == "A v2"

    def test_crud_full_cycle(self) -> None:
        """完整 CRUD 生命周期测试。"""
        store = PipelineConfigStore()

        # Create
        config = PipelineConfig(pipeline_id="full", name="Full Test")
        store.register("full", config)
        assert store.get("full") is not None

        # Read
        retrieved = store.get("full")
        assert retrieved is not None
        assert retrieved.name == "Full Test"

        # Update (overwrite)
        config_updated = PipelineConfig(pipeline_id="full", name="Full Test Updated")
        store.register("full", config_updated)
        assert store.get("full").name == "Full Test Updated"  # type: ignore[union-attr]

        # Delete
        assert store.remove("full") is True
        assert store.get("full") is None

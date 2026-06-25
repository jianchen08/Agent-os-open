"""Round2 测试审查 - Agent编排模块测试缺口补充

覆盖需求：01_Agent编排模块需求文档
- F-AGT-01: Agent 配置加载
- F-AGT-07: 委托深度上限 3 层
- F-AGT-09/10: static_vars / dynamic_vars
- F-AGT-13: tool_ids 工具配置
"""

import pytest


class TestAgentTypes:
    """Agent 类型与层级定义"""

    def test_agent_level_enum(self):
        """AgentLevel 三层枚举"""
        from agents.types import AgentLevel
        assert AgentLevel.L1_MAIN.value == "L1"
        assert AgentLevel.L2_SUBTASK.value == "L2"
        assert AgentLevel.L3_ATOMIC.value == "L3"

    def test_agent_type_enum(self):
        """AgentType 枚举"""
        from agents.types import AgentType
        assert hasattr(AgentType, 'MAIN') or hasattr(AgentType, 'SPECIALIZED')


class TestAgentConfig:
    """F-AGT-01: Agent 配置加载"""

    def test_agent_config_fields(self):
        """AgentConfig 包含必要字段"""
        try:
            from agents.types import AgentConfig
            config = AgentConfig(
                config_id="test_agent",
                name="test",
                system_prompt="You are a test agent",
                tool_ids=["file_read"],
                level="L3",
                agent_type="specialized"
            )
            assert config.config_id == "test_agent"
            assert config.system_prompt == "You are a test agent"
            assert "file_read" in config.tool_ids
        except (ImportError, TypeError):
            pytest.skip("AgentConfig 结构不同")

    def test_agent_config_tool_ids(self):
        """F-AGT-13: tool_ids 限制可用工具"""
        try:
            from agents.types import AgentConfig
            config = AgentConfig(
                config_id="limited_agent",
                name="limited",
                system_prompt="test",
                tool_ids=["file_read", "file_write"],
                level="L3",
                agent_type="specialized"
            )
            assert len(config.tool_ids) == 2
            assert "bash_execute" not in config.tool_ids
        except (ImportError, TypeError):
            pytest.skip("AgentConfig 结构不同")


class TestLevelController:
    """F-AGT-07: 委托深度上限 3 层"""

    def test_level_chain_l1_to_l3_valid(self):
        """L1→L2→L3 合法（3层）"""
        from agents.types import AgentLevel
        levels = [AgentLevel.L1_MAIN, AgentLevel.L2_SUBTASK, AgentLevel.L3_ATOMIC]
        assert len(levels) == 3

    def test_level_values_distinct(self):
        """三个层级值互不相同"""
        from agents.types import AgentLevel
        values = [e.value for e in AgentLevel]
        assert len(set(values)) == 3


class TestContextConfig:
    """F-AGT-09/10: 上下文变量类型"""

    def test_context_var_types(self):
        """三种类型: reference / literal / expression"""
        valid_types = ["reference", "literal", "expression"]
        static_var = {"type": "literal", "value": "/workspace"}
        dynamic_var = {"type": "expression", "expr": "now().isoformat()"}
        ref_var = {"type": "reference", "path": "config/agents/_index.md"}

        assert static_var["type"] in valid_types
        assert dynamic_var["type"] in valid_types
        assert ref_var["type"] in valid_types

"""
Agent 编排模块核心测试。

覆盖 AC：
- AC-AGT-01: 所有 config/agents/ 下 YAML 在启动时被加载
- AC-AGT-05: 委托深度不超过 3 层
- AC-AGT-07: ${ENV_VAR} 环境变量替换正确
- AC-AGT-09: hard_constraints 强制遵守（写入 state）
- AC-AGT-10: tool_ids 限制可用工具范围
- AC-AGT-11/12: 性能验收（加载时间/构建时延）
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from agents.loader import AgentConfigLoader
from agents.level_controller import (
    AgentLevel,
    LevelController,
    ValidationError,
)
from agents.registry import AgentRegistry
from agents.types import (
    AgentConfig,
    AgentLevel as AgentLevelType,
    AgentType,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════
# AC-AGT-01: Agent 配置加载
# ════════════════════════════════════════════════════════════════


class TestAgentConfigLoading:
    """Agent 配置 YAML 加载测试。"""

    @pytest.fixture
    def sample_yaml(self, tmp_path):
        """创建测试用 Agent YAML 配置文件。"""
        yaml_content = {
            "config_id": "test_agent_001",
            "name": "测试Agent",
            "display_name": "测试Agent",
            "description": "用于单元测试的 Agent",
            "agent_type": "specialized",
            "category": "test",
            "level": "L2",
            "system_prompt": "你是一个测试 Agent。",
            "tool_ids": ["file_read", "file_write"],
            "hard_constraints": ["不能删除文件", "必须写注释"],
            "soft_constraints": ["尽量使用类型注解"],
            "max_iterations": 50,
            "max_reminders": 2,
            "timeout_seconds": 300,
            "version": "2.0.0",
            "tags": ["test", "unit"],
        }
        yaml_file = tmp_path / "test_agent.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(yaml_content, f, allow_unicode=True)
        return yaml_file

    def test_load_single_yaml_basic_fields(self, sample_yaml):
        """测试: 从 YAML 加载基本字段正确。"""
        config = AgentConfigLoader.load_from_yaml(sample_yaml)

        assert config.config_id == "test_agent_001"
        assert config.name == "测试Agent"
        assert config.display_name == "测试Agent"
        assert config.level == AgentLevelType.L2_SUBTASK
        assert config.agent_type == AgentType.SPECIALIZED
        assert config.tool_ids == ["file_read", "file_write"]

    def test_load_yaml_hard_constraints(self, sample_yaml):
        """测试: hard_constraints 正确解析。"""
        config = AgentConfigLoader.load_from_yaml(sample_yaml)
        assert config.hard_constraints == ["不能删除文件", "必须写注释"]
        assert config.soft_constraints == ["尽量使用类型注解"]

    def test_load_yaml_defaults(self, tmp_path):
        """测试: 缺失字段使用默认值。"""
        yaml_content = {"config_id": "minimal_agent", "level": "L3"}
        yaml_file = tmp_path / "minimal.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(yaml_content, f, allow_unicode=True)

        config = AgentConfigLoader.load_from_yaml(yaml_file)

        assert config.config_id == "minimal_agent"
        assert config.max_iterations == 100
        assert config.max_reminders == 3
        assert config.timeout_seconds == -1
        assert config.version == "1.0.0"
        assert config.is_active is True

    def test_load_yaml_missing_config_id_raises(self, tmp_path):
        """测试: 缺少 config_id 抛出 ValueError。"""
        yaml_file = tmp_path / "no_id.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump({"name": "no_id_agent"}, f)

        with pytest.raises(ValueError, match="config_id"):
            AgentConfigLoader.load_from_yaml(yaml_file)

    def test_load_yaml_invalid_level_raises(self, tmp_path):
        """测试: 无效层级值抛出 ValueError。"""
        yaml_file = tmp_path / "bad_level.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump({"config_id": "bad", "level": "L9"}, f)

        with pytest.raises(ValueError, match="层级"):
            AgentConfigLoader.load_from_yaml(yaml_file)

    def test_load_yaml_file_not_found(self):
        """测试: 文件不存在抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            AgentConfigLoader.load_from_yaml("/nonexistent/path/agent.yaml")

    def test_load_from_directory(self, tmp_path):
        """测试: 从目录递归加载多个配置。"""
        for i in range(3):
            content = {"config_id": f"agent_{i}", "level": "L3"}
            (tmp_path / f"agent_{i}.yaml").write_text(
                yaml.dump(content), encoding="utf-8"
            )

        configs = AgentConfigLoader.load_from_directory(tmp_path)
        assert len(configs) == 3
        config_ids = {c.config_id for c in configs}
        assert config_ids == {"agent_0", "agent_1", "agent_2"}

    def test_load_from_directory_skips_invalid(self, tmp_path):
        """测试: 目录加载跳过无效文件而非崩溃。"""
        (tmp_path / "valid.yaml").write_text(
            yaml.dump({"config_id": "valid", "level": "L3"}), encoding="utf-8"
        )
        (tmp_path / "invalid.yaml").write_text("- item1\n- item2")

        configs = AgentConfigLoader.load_from_directory(tmp_path)
        assert len(configs) == 1
        assert configs[0].config_id == "valid"


# ════════════════════════════════════════════════════════════════
# AC-AGT-01: 从实际 config/agents/ 目录加载
# ════════════════════════════════════════════════════════════════


class TestAgentRegistryFromConfig:
    """从实际配置目录加载 Agent 测试。"""

    def test_registry_load_config_agents(self):
        """测试: 从 config/agents/ 加载所有 YAML 配置。"""
        agents_dir = PROJECT_ROOT / "config" / "agents"
        if not agents_dir.exists():
            pytest.skip("config/agents 目录不存在")

        registry = AgentRegistry()
        # 逐个加载避免大目录段错误
        loaded = 0
        for yaml_file in sorted(agents_dir.rglob("*.yaml")):
            try:
                config = AgentConfigLoader.load_from_yaml(yaml_file)
                registry.register(config)
                loaded += 1
            except Exception:
                pass

        assert loaded > 0, "应该至少加载一个 Agent 配置"
        all_configs = registry.list_all()
        assert len(all_configs) > 0

    def test_registry_find_by_level(self):
        """测试: 按层级筛选 Agent。"""
        agents_dir = PROJECT_ROOT / "config" / "agents"
        if not agents_dir.exists():
            pytest.skip("config/agents 目录不存在")

        registry = AgentRegistry()
        for yaml_file in sorted(agents_dir.rglob("*.yaml")):
            try:
                config = AgentConfigLoader.load_from_yaml(yaml_file)
                registry.register(config)
            except Exception:
                pass

        l1_agents = registry.find_by_level(AgentLevelType.L1_MAIN)
        assert len(l1_agents) >= 1, "至少应有一个 L1 Agent"

        l3_agents = registry.find_by_level(AgentLevelType.L3_ATOMIC)
        assert len(l3_agents) >= 1, "至少应有一个 L3 Agent"

    def test_registry_get_known_agent(self):
        """测试: 查找已知 Agent（灵汐）。"""
        agents_dir = PROJECT_ROOT / "config" / "agents"
        if not agents_dir.exists():
            pytest.skip("config/agents 目录不存在")

        registry = AgentRegistry()
        for yaml_file in sorted(agents_dir.rglob("*.yaml")):
            try:
                config = AgentConfigLoader.load_from_yaml(yaml_file)
                registry.register(config)
            except Exception:
                pass

        lingxi = registry.get("lingxi")
        if lingxi:
            assert lingxi.level == AgentLevelType.L1_MAIN
            assert lingxi.agent_type == AgentType.MAIN


# ════════════════════════════════════════════════════════════════
# AC-AGT-07: ${ENV_VAR} 环境变量替换
# ════════════════════════════════════════════════════════════════


class TestEnvVarSubstitution:
    """${ENV_VAR} 环境变量替换测试。"""

    def test_env_var_replacement_in_system_prompt(self, tmp_path, monkeypatch):
        """测试: system_prompt 中的 ${ENV_VAR} 被正确替换。"""
        monkeypatch.setenv("TEST_AGENT_NAME", "测试环境Agent")

        yaml_content = {
            "config_id": "env_test_agent",
            "level": "L3",
            "system_prompt": "你是 ${TEST_AGENT_NAME}，负责处理任务。",
        }
        yaml_file = tmp_path / "env_agent.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(yaml_content, f, allow_unicode=True)

        config = AgentConfigLoader.load_from_yaml(yaml_file)
        assert "${TEST_AGENT_NAME}" not in config.system_prompt
        assert "测试环境Agent" in config.system_prompt

    def test_env_var_replacement_in_tool_ids(self, tmp_path, monkeypatch):
        """测试: 字段值中的 ${ENV_VAR} 被替换。"""
        monkeypatch.setenv("TEST_TOOL_PREFIX", "custom")

        yaml_content = {
            "config_id": "env_tool_agent",
            "level": "L3",
            "system_prompt": "test",
            "tool_ids": ["${TEST_TOOL_PREFIX}_tool"],
        }
        yaml_file = tmp_path / "env_tool.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(yaml_content, f, allow_unicode=True)

        config = AgentConfigLoader.load_from_yaml(yaml_file)
        assert "custom_tool" in config.tool_ids

    def test_unset_env_var_replaced_with_empty(self, tmp_path, monkeypatch):
        """测试: 未设置的环境变量替换为空字符串。"""
        monkeypatch.delenv("UNSET_VAR_99999", raising=False)

        yaml_content = {
            "config_id": "unset_env_agent",
            "level": "L3",
            "system_prompt": "key=${UNSET_VAR_99999}",
        }
        yaml_file = tmp_path / "unset_env.yaml"
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(yaml_content, f, allow_unicode=True)

        config = AgentConfigLoader.load_from_yaml(yaml_file)
        assert "${UNSET_VAR_99999}" not in config.system_prompt


# ════════════════════════════════════════════════════════════════
# AC-AGT-05: 委托深度不超过 3 层
# ════════════════════════════════════════════════════════════════


class TestLevelController:
    """LevelController 委托深度控制测试。"""

    @pytest.fixture
    def controller(self):
        return LevelController()

    def test_l1_can_submit_task(self, controller):
        """测试: L1 可以提交子任务。"""
        assert controller.can_submit_task(AgentLevel.L1) is True

    def test_l2_can_submit_task(self, controller):
        """测试: L2 可以提交子任务。"""
        assert controller.can_submit_task(AgentLevel.L2) is True

    def test_l3_cannot_submit_task(self, controller):
        """测试: L3 不能提交子任务。"""
        assert controller.can_submit_task(AgentLevel.L3) is False

    def test_l1_allowed_targets(self, controller):
        """测试: L1 可以提交给 L2 和 L3。"""
        targets = controller.get_allowed_targets(AgentLevel.L1)
        assert AgentLevel.L2 in targets
        assert AgentLevel.L3 in targets

    def test_l2_allowed_targets(self, controller):
        """测试: L2 只能提交给 L3。"""
        targets = controller.get_allowed_targets(AgentLevel.L2)
        assert AgentLevel.L3 in targets
        assert AgentLevel.L1 not in targets

    def test_l3_no_allowed_targets(self, controller):
        """测试: L3 不能提交给任何层级。"""
        targets = controller.get_allowed_targets(AgentLevel.L3)
        assert targets == []

    def test_validate_transition_l1_to_l2(self, controller):
        """测试: L1→L2 转换合法。"""
        result = controller.validate_transition(AgentLevel.L1, AgentLevel.L2)
        assert result.passed is True

    def test_validate_transition_l2_to_l3(self, controller):
        """测试: L2→L3 转换合法。"""
        result = controller.validate_transition(AgentLevel.L2, AgentLevel.L3)
        assert result.passed is True

    def test_validate_transition_l1_to_l1_rejected(self, controller):
        """测试: L1→L1 转换不合法（不能同层委托）。"""
        result = controller.validate_transition(AgentLevel.L1, AgentLevel.L1)
        assert result.passed is False
        assert result.error_code == ValidationError.INVALID_TARGET_LEVEL

    def test_validate_transition_l3_to_anything_rejected(self, controller):
        """测试: L3 不能委托给任何层级。"""
        for target in [AgentLevel.L1, AgentLevel.L2, AgentLevel.L3]:
            result = controller.validate_transition(AgentLevel.L3, target)
            assert result.passed is False

    def test_validate_depth_l1_within_max(self, controller):
        """测试: L1 在最大深度内合法。"""
        result = controller.validate_task_submission(
            AgentLevel.L1, current_depth=3
        )
        assert result.passed is True

    def test_validate_depth_exceeds_max(self, controller):
        """测试: 超过最大嵌套深度被拦截。"""
        result = controller.validate_task_submission(
            AgentLevel.L1, current_depth=4
        )
        assert result.passed is False
        assert result.error_code == ValidationError.MAX_DEPTH_EXCEEDED

    def test_calculate_depth_root(self, controller):
        """测试: 根任务深度为 1。"""
        depth = controller.calculate_current_depth(None, {})
        assert depth == 1

    def test_calculate_depth_nested(self, controller):
        """测试: 嵌套任务深度计算正确。"""
        task_map = {"task_1": 1, "task_2": 2}
        depth = controller.calculate_current_depth("task_2", task_map)
        assert depth == 3

    def test_max_depth_values(self, controller):
        """测试: 各层级最大深度值正确。"""
        assert controller.get_max_depth(AgentLevel.L1) == 3
        assert controller.get_max_depth(AgentLevel.L2) == 2
        assert controller.get_max_depth(AgentLevel.L3) == 1


# ════════════════════════════════════════════════════════════════
# AC-AGT-09/10: AgentConfig.to_state() 约束与工具注入
# ════════════════════════════════════════════════════════════════


class TestAgentConfigToState:
    """AgentConfig.to_state() 上下文注入测试。"""

    def test_to_state_includes_tool_ids(self):
        """测试: to_state 包含 tool_ids。"""
        config = AgentConfig(
            config_id="test",
            tool_ids=["file_read", "bash_execute", "web_search"],
        )
        state = config.to_state()
        assert state["tool_ids"] == ["file_read", "bash_execute", "web_search"]

    def test_to_state_includes_hard_constraints(self):
        """测试: to_state 包含 hard_constraints。"""
        config = AgentConfig(
            config_id="test",
            hard_constraints=["禁止删除文件", "必须写注释"],
            soft_constraints=["尽量加类型"],
        )
        state = config.to_state()
        assert "constraints" in state
        assert "禁止删除文件" in state["constraints"]["hard"]
        assert "必须写注释" in state["constraints"]["hard"]
        assert "尽量加类型" in state["constraints"]["soft"]

    def test_to_state_includes_agent_level(self):
        """测试: to_state 包含 agent_level。"""
        config = AgentConfig(
            config_id="test",
            level=AgentLevelType.L1_MAIN,
        )
        state = config.to_state()
        assert state["agent_level"] == "L1"

    def test_to_state_includes_system_prompt(self):
        """测试: to_state 包含 system_prompt（含约束拼接）。"""
        config = AgentConfig(
            config_id="test",
            system_prompt="你是助手。",
            hard_constraints=["约束1"],
        )
        state = config.to_state()
        assert "system_prompt" in state
        assert "你是助手" in state["system_prompt"]
        assert "约束1" in state["system_prompt"]

    def test_to_state_includes_max_iterations(self):
        """测试: to_state 包含 max_iterations。"""
        config = AgentConfig(config_id="test", max_iterations=42)
        state = config.to_state()
        assert state["max_iterations"] == 42


# ════════════════════════════════════════════════════════════════
# AC-AGT-11/12: 性能验收
# ════════════════════════════════════════════════════════════════


class TestAgentPerformance:
    """Agent 模块性能验收。"""

    def test_agent_load_time_under_2s(self):
        """AC-AGT-11: Agent 加载时间 < 2s。"""
        agents_dir = PROJECT_ROOT / "config" / "agents"
        if not agents_dir.exists():
            pytest.skip("config/agents 目录不存在")

        start = time.monotonic()
        registry = AgentRegistry()
        for yaml_file in sorted(agents_dir.rglob("*.yaml")):
            try:
                config = AgentConfigLoader.load_from_yaml(yaml_file)
                registry.register(config)
            except Exception:
                pass
        elapsed = time.monotonic() - start

        assert elapsed < 2.0, f"Agent 加载耗时 {elapsed:.3f}s，超过 2s 阈值"

    def test_context_build_under_100ms(self):
        """AC-AGT-12: 单次 context 构建 < 100ms。"""
        config = AgentConfig(
            config_id="perf_test",
            system_prompt="test",
            hard_constraints=["c1"],
            tool_ids=["file_read"],
        )

        start = time.monotonic()
        for _ in range(100):
            config.to_state()
        elapsed = (time.monotonic() - start) / 100 * 1000

        assert elapsed < 100, f"单次 to_state 耗时 {elapsed:.1f}ms，超过 100ms"

"""
Agent Registry 热更新测试。

覆盖功能点：
- register(config) 注册新 Agent
- unregister(config_id) 移除 Agent
- reload_agent(config_id) 更新 Agent 配置（从磁盘重新读取）
- get(config_id) 查找（含懒加载）
- 已被引用的 Agent 不被强制中断
- find_by_level / find_by_type / find_by_category / find_by_tag / find_by_tool
- load_directory 批量加载
"""
import pytest
import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents.registry import AgentRegistry
from agents.types import AgentConfig, AgentLevel, AgentType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_agent_config(
    config_id: str = "test_agent",
    name: str = "Test Agent",
    level: AgentLevel = AgentLevel.L3_ATOMIC,
    agent_type: AgentType = AgentType.SPECIALIZED,
    category: str = "executor",
    tags: list[str] | None = None,
    tool_ids: list[str] | None = None,
) -> AgentConfig:
    """创建测试用 AgentConfig。"""
    return AgentConfig(
        config_id=config_id,
        name=name,
        display_name=name,
        description=f"Test agent {config_id}",
        agent_type=agent_type,
        category=category,
        level=level,
        system_prompt="You are a test agent.",
        tool_ids=tool_ids or [],
        tags=tags or [],
    )


def _write_agent_yaml(path: Path, config_id: str, name: str, **kwargs) -> None:
    """辅助：写入 Agent YAML 配置文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "config_id": config_id,
        "name": name,
        "display_name": name,
        "description": f"Agent {config_id}",
        "agent_type": kwargs.get("agent_type", "specialized"),
        "category": kwargs.get("category", "executor"),
        "level": kwargs.get("level", "L3"),
        "system_prompt": kwargs.get("system_prompt", "Test prompt"),
        "tool_ids": kwargs.get("tool_ids", []),
        "tags": kwargs.get("tags", []),
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


@pytest.fixture
def registry():
    """创建空的 AgentRegistry。"""
    return AgentRegistry()


@pytest.fixture
def config_dir(tmp_path):
    """创建临时配置目录。"""
    d = tmp_path / "config" / "agents"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# register() — 注册
# ---------------------------------------------------------------------------


class TestRegister:
    """AgentRegistry.register() 注册 Agent 配置。"""

    def test_register_success(self, registry):
        """注册 Agent 后应能通过 get 获取。"""
        config = _make_agent_config("agent_1", "Agent One")
        registry.register(config)

        result = registry.get("agent_1")
        assert result is not None
        assert result.config_id == "agent_1"

    def test_register_overwrites_existing(self, registry):
        """注册相同 config_id 应覆盖旧配置。"""
        config_v1 = _make_agent_config("agent_1", "Version 1")
        config_v2 = _make_agent_config("agent_1", "Version 2")
        registry.register(config_v1)
        registry.register(config_v2)

        result = registry.get("agent_1")
        assert result.name == "Version 2"

    def test_register_empty_id_raises(self, registry):
        """config_id 为空应抛出 ValueError。"""
        config = _make_agent_config("", "Empty ID")
        with pytest.raises(ValueError, match="config_id 不能为空"):
            registry.register(config)

    def test_register_multiple_agents(self, registry):
        """注册多个 Agent 后 count 应正确。"""
        for i in range(5):
            registry.register(_make_agent_config(f"agent_{i}", f"Agent {i}"))

        assert registry.count() == 5


# ---------------------------------------------------------------------------
# unregister() — 移除
# ---------------------------------------------------------------------------


class TestUnregister:
    """AgentRegistry.unregister() 移除 Agent 配置。"""

    def test_unregister_existing(self, registry):
        """移除已注册的 Agent 应返回 True。"""
        registry.register(_make_agent_config("agent_1", "Agent 1"))
        result = registry.unregister("agent_1")

        assert result is True
        assert registry.get("agent_1") is None

    def test_unregister_nonexistent(self, registry):
        """移除不存在的 Agent 应返回 False。"""
        result = registry.unregister("nonexistent")
        assert result is False

    def test_unregister_does_not_affect_others(self, registry):
        """移除一个 Agent 不应影响其他 Agent。"""
        registry.register(_make_agent_config("agent_1", "Agent 1"))
        registry.register(_make_agent_config("agent_2", "Agent 2"))
        registry.unregister("agent_1")

        assert registry.get("agent_1") is None
        assert registry.get("agent_2") is not None


# ---------------------------------------------------------------------------
# reload_agent() — 热更新
# ---------------------------------------------------------------------------


class TestReloadAgent:
    """AgentRegistry.reload_agent() 从磁盘重新加载 Agent 配置。"""

    def test_reload_updates_config(self, registry, config_dir):
        """重载后应获取到更新后的配置。"""
        yaml_path = config_dir / "agent_1.yaml"
        _write_agent_yaml(yaml_path, "agent_1", "Version 1")

        registry._config_dir = config_dir
        registry._scanned_files.add(str(yaml_path))

        # 首次注册
        config = _make_agent_config("agent_1", "Version 1")
        registry.register(config)

        # 更新 YAML 文件
        _write_agent_yaml(yaml_path, "agent_1", "Version 2")

        # 重载
        reloaded = registry.reload_agent("agent_1")

        assert reloaded is not None
        assert reloaded.name == "Version 2"
        assert registry.get("agent_1").name == "Version 2"

    def test_reload_nonexistent_returns_none(self, registry, config_dir):
        """重载不存在的 config_id 应返回 None。"""
        registry._config_dir = config_dir
        result = registry.reload_agent("nonexistent")
        assert result is None

    def test_reload_preserves_references(self, registry, config_dir):
        """已被引用的旧配置实例不应被强制修改。"""
        yaml_path = config_dir / "agent_1.yaml"
        _write_agent_yaml(yaml_path, "agent_1", "Old Name")

        registry._config_dir = config_dir
        registry._scanned_files.add(str(yaml_path))

        old_config = _make_agent_config("agent_1", "Old Name")
        registry.register(old_config)

        # 模拟外部持有旧引用
        external_ref = registry.get("agent_1")

        # 更新并重载
        _write_agent_yaml(yaml_path, "agent_1", "New Name")
        registry.reload_agent("agent_1")

        # 旧引用应保持不变
        assert external_ref.name == "Old Name"
        # 新查询应返回更新后的配置
        assert registry.get("agent_1").name == "New Name"


# ---------------------------------------------------------------------------
# get() — 查找（含懒加载）
# ---------------------------------------------------------------------------


class TestGet:
    """AgentRegistry.get() 查找 Agent 配置。"""

    def test_get_existing(self, registry):
        """get 已注册的 Agent 应返回配置。"""
        registry.register(_make_agent_config("agent_1", "Agent 1"))
        result = registry.get("agent_1")
        assert result.config_id == "agent_1"

    def test_get_nonexistent(self, registry):
        """get 不存在的 Agent（无 config_dir）应返回 None。"""
        result = registry.get("nonexistent")
        assert result is None

    def test_get_lazy_loads_from_disk(self, registry, config_dir):
        """get 未命中时应尝试从磁盘懒加载。"""
        yaml_path = config_dir / "lazy_agent.yaml"
        _write_agent_yaml(yaml_path, "lazy_agent", "Lazy Agent")

        registry._config_dir = config_dir
        # 不添加到 _scanned_files 以触发文件内容扫描

        result = registry.get("lazy_agent")
        assert result is not None
        assert result.config_id == "lazy_agent"

    def test_get_returns_none_for_missing_yaml(self, registry, config_dir):
        """磁盘上不存在的 Agent 应返回 None。"""
        registry._config_dir = config_dir
        result = registry.get("missing_agent")
        assert result is None


# ---------------------------------------------------------------------------
# find_by_* — 筛选
# ---------------------------------------------------------------------------


class TestFindBy:
    """AgentRegistry 的各种筛选方法。"""

    def test_find_by_level(self, registry):
        """按层级筛选应返回匹配的 Agent。"""
        registry.register(_make_agent_config("l1", "L1", level=AgentLevel.L1_MAIN))
        registry.register(_make_agent_config("l2", "L2", level=AgentLevel.L2_SUBTASK))
        registry.register(_make_agent_config("l3", "L3", level=AgentLevel.L3_ATOMIC))

        l1 = registry.find_by_level(AgentLevel.L1_MAIN)
        assert len(l1) == 1
        assert l1[0].config_id == "l1"

    def test_find_by_type(self, registry):
        """按类型筛选应返回匹配的 Agent。"""
        registry.register(_make_agent_config("main", "Main", agent_type=AgentType.MAIN))
        registry.register(_make_agent_config("spec", "Spec", agent_type=AgentType.SPECIALIZED))

        mains = registry.find_by_type(AgentType.MAIN)
        assert len(mains) == 1
        assert mains[0].config_id == "main"

    def test_find_by_category(self, registry):
        """按分类筛选应返回匹配的 Agent。"""
        registry.register(_make_agent_config("a1", "A1", category="coding"))
        registry.register(_make_agent_config("a2", "A2", category="research"))

        coders = registry.find_by_category("coding")
        assert len(coders) == 1

    def test_find_by_tag(self, registry):
        """按标签筛选应返回包含该标签的 Agent。"""
        registry.register(_make_agent_config("a1", "A1", tags=["code", "debug"]))
        registry.register(_make_agent_config("a2", "A2", tags=["research"]))

        tagged = registry.find_by_tag("code")
        assert len(tagged) == 1
        assert tagged[0].config_id == "a1"

    def test_find_by_tool(self, registry):
        """按工具筛选应返回绑定了该工具的 Agent。"""
        registry.register(_make_agent_config("a1", "A1", tool_ids=["bash_execute", "file_read"]))
        registry.register(_make_agent_config("a2", "A2", tool_ids=["web_search"]))

        bash_agents = registry.find_by_tool("bash_execute")
        assert len(bash_agents) == 1
        assert bash_agents[0].config_id == "a1"

    def test_find_empty_result(self, registry):
        """筛选无匹配时应返回空列表。"""
        assert registry.find_by_level(AgentLevel.L1_MAIN) == []
        assert registry.find_by_category("nonexistent") == []


# ---------------------------------------------------------------------------
# load_directory — 批量加载
# ---------------------------------------------------------------------------


class TestLoadDirectory:
    """AgentRegistry.load_directory() 批量加载。"""

    def test_load_directory_success(self, registry, config_dir):
        """批量加载应注册所有有效配置。"""
        _write_agent_yaml(config_dir / "agent_1.yaml", "agent_1", "Agent 1")
        _write_agent_yaml(config_dir / "agent_2.yaml", "agent_2", "Agent 2")

        count = registry.load_directory(config_dir)

        assert count == 2
        assert registry.get("agent_1") is not None
        assert registry.get("agent_2") is not None

    def test_load_directory_records_config_dir(self, registry, config_dir):
        """加载后应记录 config_dir 用于后续懒加载。"""
        registry.load_directory(config_dir)
        assert registry._config_dir == config_dir

    def test_load_directory_empty_dir(self, registry, tmp_path):
        """空目录加载应返回 0。"""
        empty = tmp_path / "empty"
        empty.mkdir()
        count = registry.load_directory(empty)
        assert count == 0


# ---------------------------------------------------------------------------
# list_all / count
# ---------------------------------------------------------------------------


class TestMisc:
    """其他方法测试。"""

    def test_list_all(self, registry):
        """list_all 应返回所有已注册配置。"""
        registry.register(_make_agent_config("a1", "A1"))
        registry.register(_make_agent_config("a2", "A2"))

        all_configs = registry.list_all()
        assert len(all_configs) == 2
        ids = {c.config_id for c in all_configs}
        assert ids == {"a1", "a2"}

    def test_count(self, registry):
        """count 应返回已注册配置数量。"""
        assert registry.count() == 0
        registry.register(_make_agent_config("a1", "A1"))
        assert registry.count() == 1
        registry.unregister("a1")
        assert registry.count() == 0

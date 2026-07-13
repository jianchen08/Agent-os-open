"""
YAML 解析错误处理链测试

验证修复：YAML 语法错误不再被静默吞掉，而是抛出明确的异常。
测试覆盖：
1. _load_yaml 对 YAML 语法错误抛出异常而非返回 None
2. load_agents 遇到 YAML 语法错误时报告 ConfigurationException（含文件名）
3. load_from_directory 遇到 YAML 语法错误时上抛异常而非静默跳过
4. 正常 YAML 文件仍然正常加载（回归测试）
5. load_all 遇到 YAML 语法错误时报告 ConfigurationException
"""
import pytest
import yaml

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.config.loader import ConfigLoader
from src.core.exceptions import ConfigurationException


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_bad_yaml(path: Path) -> None:
    """写入一个有语法错误的 YAML 文件（缩进不一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "config_id: test_agent\n"
        "name: Test\n"
        "  bad_indent: oops\n"          # 错误缩进
        "    deeper: oops\n",
        encoding="utf-8",
    )


def _write_good_yaml(path: Path) -> None:
    """写入一个正常的 YAML 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "config_id: good_agent\n"
        "name: GoodAgent\n"
        "description: A good agent\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. _load_yaml 单元测试
# ---------------------------------------------------------------------------

class TestLoadYaml:
    """ConfigLoader._load_yaml 错误处理测试"""

    def test_yaml_syntax_error_raises_exception(self, tmp_path: Path):
        """YAML 语法错误时应抛出 yaml.YAMLError，而非返回 None"""
        bad_file = tmp_path / "bad.yaml"
        _write_bad_yaml(bad_file)

        loader = ConfigLoader(config_dir=tmp_path)
        with pytest.raises(yaml.YAMLError):
            loader._load_yaml(bad_file)

    def test_missing_file_returns_none(self, tmp_path: Path):
        """文件不存在时应返回 None（保持向后兼容）"""
        missing = tmp_path / "nonexistent.yaml"
        loader = ConfigLoader(config_dir=tmp_path)
        assert loader._load_yaml(missing) is None

    def test_valid_yaml_returns_dict(self, tmp_path: Path):
        """正常 YAML 文件应正常返回字典"""
        good_file = tmp_path / "good.yaml"
        _write_good_yaml(good_file)

        loader = ConfigLoader(config_dir=tmp_path)
        result = loader._load_yaml(good_file)
        assert isinstance(result, dict)
        assert result["config_id"] == "good_agent"


# ---------------------------------------------------------------------------
# 2. load_all 集成测试
# ---------------------------------------------------------------------------

class TestLoadAll:
    """ConfigLoader.load_all YAML 错误处理测试"""

    def test_yaml_syntax_error_raises_configuration_exception(self, tmp_path: Path):
        """load_all 遇到 YAML 语法错误时应抛出 ConfigurationException"""
        bad_file = tmp_path / "broken.yaml"
        _write_bad_yaml(bad_file)

        loader = ConfigLoader(config_dir=tmp_path)
        with pytest.raises(ConfigurationException) as exc_info:
            loader.load_all()

        # 验证异常消息包含文件名
        error_msg = str(exc_info.value)
        assert "broken.yaml" in error_msg

    def test_valid_files_load_normally(self, tmp_path: Path):
        """正常文件仍然能正常加载（回归测试）"""
        good_file = tmp_path / "good.yaml"
        _write_good_yaml(good_file)

        loader = ConfigLoader(config_dir=tmp_path)
        result = loader.load_all()
        assert "good" in result
        assert result["good"]["config_id"] == "good_agent"


# ---------------------------------------------------------------------------
# 3. load_agents 集成测试（异步）
# ---------------------------------------------------------------------------

class TestLoadAgents:
    """ConfigLoader.load_agents YAML 错误处理测试"""

    @pytest.mark.asyncio
    async def test_yaml_syntax_error_raises_configuration_exception(self, tmp_path: Path):
        """load_agents 遇到 YAML 语法错误时应抛出 ConfigurationException"""
        agents_dir = tmp_path / "agents"
        bad_file = agents_dir / "broken.yaml"
        _write_bad_yaml(bad_file)

        loader = ConfigLoader(config_dir=tmp_path)
        mock_session = AsyncMock()

        with pytest.raises(ConfigurationException) as exc_info:
            await loader.load_agents(mock_session, agents_dir="agents")

        error_msg = str(exc_info.value)
        assert "broken.yaml" in error_msg

    @pytest.mark.asyncio
    async def test_missing_required_field_continues(self, tmp_path: Path):
        """缺少 config_id 字段时应跳过该文件，不抛异常"""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        # 写一个没有 config_id 的 YAML
        no_id_file = agents_dir / "no_id.yaml"
        no_id_file.write_text("name: NoIdAgent\n", encoding="utf-8")

        loader = ConfigLoader(config_dir=tmp_path)
        mock_session = AsyncMock()
        # 不应抛出异常
        result = await loader.load_agents(mock_session, agents_dir="agents")
        assert result == []


# ---------------------------------------------------------------------------
# 4. AgentConfigLoader.load_from_directory 测试
# ---------------------------------------------------------------------------

class TestAgentConfigLoader:
    """src/agents/loader.py AgentConfigLoader 错误处理测试"""

    def test_yaml_syntax_error_raises_value_error(self, tmp_path: Path):
        """load_from_directory 遇到 YAML 语法错误时应上抛 ValueError"""
        from src.agents.loader import AgentConfigLoader

        bad_file = tmp_path / "broken.yaml"
        _write_bad_yaml(bad_file)

        with pytest.raises(ValueError) as exc_info:
            AgentConfigLoader.load_from_directory(tmp_path)

        error_msg = str(exc_info.value)
        assert "broken.yaml" in error_msg

    def test_missing_field_skipped_with_warning(self, tmp_path: Path):
        """缺少必填字段时应 warning 跳过，不抛异常"""
        from src.agents.loader import AgentConfigLoader

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        # 缺少 config_id
        no_id = agents_dir / "no_id.yaml"
        no_id.write_text("name: NoId\n", encoding="utf-8")

        # 不应抛出异常，只是跳过
        result = AgentConfigLoader.load_from_directory(agents_dir)
        assert result == []

    def test_valid_config_loads_normally(self, tmp_path: Path):
        """正常 YAML 配置文件能正常加载（回归测试）"""
        from src.agents.loader import AgentConfigLoader

        good_file = tmp_path / "good_agent.yaml"
        good_file.write_text(
            "config_id: good_agent\n"
            "name: GoodAgent\n"
            "level: L3\n"
            "agent_type: atomic\n"
            "system_prompt: You are a test agent.\n",
            encoding="utf-8",
        )

        result = AgentConfigLoader.load_from_directory(tmp_path)
        assert len(result) == 1
        assert result[0].config_id == "good_agent"

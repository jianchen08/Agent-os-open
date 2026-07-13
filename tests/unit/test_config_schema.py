"""
配置 Schema 校验器测试。

覆盖功能点：
- validate_agent_config 必填字段（config_id、name）检查
- validate_agent_config 合法 level（L1/L2/L3）通过
- validate_agent_config 合法 agent_type 全部通过
- validate_agent_config 非法 agent_type 被拒绝

回归保障：agent_type 的合法集合必须与 src/agents/loader.py::_resolve_agent_type
映射键一致（main/orchestrator/specialized/atomic/system），防止热重载误拒编排/原子 Agent。
"""
import pytest

from config.schema import ConfigSchemaValidator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def validator():
    return ConfigSchemaValidator()


def _valid_agent(**overrides):
    """构造一份字段齐全的合法 Agent 配置。"""
    base = {"config_id": "agent1", "name": "测试Agent", "level": "L2", "agent_type": "specialized"}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 必填字段
# ---------------------------------------------------------------------------


def test_validate_agent_config_missing_config_id(validator):
    errors = validator.validate_agent_config({"name": "x"})
    assert any("config_id" in e for e in errors)


def test_validate_agent_config_missing_name(validator):
    errors = validator.validate_agent_config({"config_id": "x"})
    assert any("name" in e for e in errors)


def test_validate_agent_config_valid_baseline(validator):
    """字段齐全且值合法时应无错误。"""
    assert validator.validate_agent_config(_valid_agent()) == []


# ---------------------------------------------------------------------------
# agent_type 合法集合（回归核心：必须与 loader._resolve_agent_type 一致）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_type", ["main", "orchestrator", "specialized", "atomic", "system"])
def test_validate_agent_config_accepts_all_loader_types(validator, agent_type):
    """orchestrator/atomic 等被 loader 认可的类型必须通过校验。"""
    assert validator.validate_agent_config(_valid_agent(agent_type=agent_type)) == []


def test_validate_agent_config_rejects_unknown_type(validator):
    """未知 agent_type 仍应被拒绝。"""
    errors = validator.validate_agent_config(_valid_agent(agent_type="unknown"))
    assert any("agent_type" in e for e in errors)

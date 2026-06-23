"""review_agent 配置契约测试。

锁定 review_agent.yaml 的关键字段，防止后续修改悄悄破坏触发链路。

存在意义：tool.py 里硬编码了 REVIEW_AGENT_ID = "review_agent"，配置文件必须存在
且 config_id 匹配，否则触发复盘工具时会找不到 agent 配置。历史上这个配置缺失
导致整个工具触发链路静默失败（被 except 吞掉）。本测试显式守护这个契约。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.loader import AgentConfigLoader
from agents.registry import AgentRegistry
from agents.types import AgentLevel, AgentType

CONFIG_PATH = Path("config/agents/system/review_agent.yaml")
EXPECTED_CONFIG_ID = "review_agent"
# 必须与 src/tools/builtin/trigger_review/tool.py 的 REVIEW_AGENT_ID 一致


class TestReviewAgentConfigContract:
    """锁定 review_agent 配置契约。"""

    def test_config_file_exists(self):
        """配置文件必须存在于 system 目录。"""
        assert CONFIG_PATH.exists(), f"review_agent 配置文件缺失: {CONFIG_PATH}"

    def test_config_loads_without_error(self):
        """配置必须能被 AgentConfigLoader 成功加载（YAML 语法+字段校验通过）。"""
        cfg = AgentConfigLoader.load_from_yaml(CONFIG_PATH)
        assert cfg is not None

    def test_config_id_matches_trigger_tool_constant(self):
        """config_id 必须是 review_agent，与 tool.py 的 REVIEW_AGENT_ID 对齐。

        这是最关键的契约：如果不匹配，trigger_review 工具触发时找不到 agent。
        """
        cfg = AgentConfigLoader.load_from_yaml(CONFIG_PATH)
        assert cfg.config_id == EXPECTED_CONFIG_ID

    def test_has_required_review_tools(self):
        """必须包含复盘必需的工具：read_execution_detail（查记录）+ file_read（读规则/文件）。"""
        cfg = AgentConfigLoader.load_from_yaml(CONFIG_PATH)
        assert "read_execution_detail" in cfg.tool_ids, (
            "review_agent 必须能用 read_execution_detail 读取管道执行记录"
        )
        assert "file_read" in cfg.tool_ids, (
            "review_agent 必须能用 file_read 读取规则文件和产出物"
        )

    def test_is_system_level_active_agent(self):
        """应为 system 类型、L3 层级、激活状态（可被自动触发）。"""
        cfg = AgentConfigLoader.load_from_yaml(CONFIG_PATH)
        assert cfg.agent_type == AgentType.SYSTEM
        assert cfg.level == AgentLevel.L3_ATOMIC
        assert cfg.is_active is True
        assert cfg.status == "active"

    def test_has_meaningful_system_prompt(self):
        """system_prompt 必须非空且足够具体（包含复盘方法论关键词）。"""
        cfg = AgentConfigLoader.load_from_yaml(CONFIG_PATH)
        assert len(cfg.system_prompt) > 200, "system_prompt 过短，应包含完整复盘方法论"
        # 必须包含核心方法论关键词，确保 prompt 不是空壳
        assert "根因" in cfg.system_prompt or "root" in cfg.system_prompt.lower()
        assert "read_execution_detail" in cfg.system_prompt, (
            "system_prompt 应指导 agent 使用 read_execution_detail 工具"
        )

    def test_has_hard_constraints_on_evidence(self):
        """必须包含"基于事实"的硬约束（防止空洞复盘）。"""
        cfg = AgentConfigLoader.load_from_yaml(CONFIG_PATH)
        constraints_text = " ".join(cfg.hard_constraints)
        assert "事实" in constraints_text or "执行记录" in constraints_text, (
            "硬约束应要求基于执行记录的事实，禁止凭空推断"
        )

    def test_produces_json_deliverable(self):
        """必须定义 JSON 格式的复盘报告产出物。"""
        cfg = AgentConfigLoader.load_from_yaml(CONFIG_PATH)
        assert len(cfg.deliverables) > 0, "应至少定义一个产出物"
        review_deliverable = next(
            (d for d in cfg.deliverables if "review" in d.name.lower()), None
        )
        assert review_deliverable is not None, "应有 review_report 产出物"
        assert review_deliverable.type == "json"


class TestReviewAgentRegistryDiscovery:
    """验证 review_agent 能被 AgentRegistry 发现（触发链路的关键）。"""

    def test_registry_can_find_review_agent(self):
        """AgentRegistry.load_directory 后必须能 get('review_agent')。

        这是 trigger_review 工具触发复盘时的真实查找路径。
        """
        registry = AgentRegistry()
        registry.load_directory("config/agents/")
        cfg = registry.get(EXPECTED_CONFIG_ID)
        assert cfg is not None, (
            f"AgentRegistry 找不到 {EXPECTED_CONFIG_ID}，trigger_review 工具将无法触发 LLM 复盘"
        )
        assert cfg.config_id == EXPECTED_CONFIG_ID

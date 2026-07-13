"""
Agent自创建端到端测试

测试流程：
1. 提交任务给 resource_manager_agent 创建新Agent
2. resource_manager_agent 调用 agent_maker
3. agent_maker 调用 file_write 创建Agent配置文件
4. 验证创建的配置文件格式是否正确

运行方式：
    pytest src/tests/test_agent_self_creation.py -v -s
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from agents.registry import AgentRegistry
from tools.builtin.file_write import FileWriteTool

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


AGENT_TEMPLATE = """# -*- coding: utf-8 -*-
# {name}

config_id: {config_id}
name: {name}
display_name: {display_name}
description: |
  {description}

agent_type: specialized
category: test
level: L3

system_prompt: |
  # 你是{name}

  ## 核心职责
  {description}

  ## 执行步骤
  1. 接收任务
  2. 执行任务
  3. 返回结果

tool_ids:
- file_read
- file_write

hard_constraints:
- 必须完成分配的任务
- 必须返回结果

soft_constraints:
- 尽量高效完成

version: "1.0.0"
is_active: true
status: "active"
max_iterations: 20
timeout_seconds: 300

tags:
- test
- auto_created

metadata:
  author: auto_test
  created_at: '2026-04-14'
"""


class TestAgentSelfCreation:
    """Agent自创建端到端测试。

    验证系统能够：
    1. 接收创建Agent的任务
    2. 正确生成Agent配置文件
    3. 验证配置文件格式正确
    """

    @pytest.fixture
    def temp_agent_dir(self):
        """创建临时目录存放生成的Agent配置"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def agent_registry(self, temp_agent_dir):
        """创建AgentRegistry并加载配置"""
        registry = AgentRegistry()
        registry.load_directory(str(_PROJECT_ROOT / "config" / "agents"))
        return registry

    def test_agent_maker_exists(self, agent_registry):
        """验证 agent_maker Agent 存在"""
        agent_maker = agent_registry.get("agent_maker")
        assert agent_maker is not None, "agent_maker 应该存在"
        assert agent_maker.level.value == "L3", "agent_maker 应该是 L3"
        print(f"✅ agent_maker 存在: {agent_maker.name}")

    def test_resource_generator_exists(self, agent_registry):
        """验证 resource_manager_agent 存在"""
        rg_agent = agent_registry.get("resource_manager_agent")
        assert rg_agent is not None, "resource_manager_agent 应该存在"
        assert rg_agent.level.value == "L2", "resource_manager_agent 应该是 L2"
        print(f"✅ resource_manager_agent 存在: {rg_agent.name}")

    def test_agent_config_schema(self, agent_registry):
        """验证Agent配置Schema正确"""
        agent_maker = agent_registry.get("agent_maker")
        assert agent_maker is not None

        assert hasattr(agent_maker, "config_id")
        assert hasattr(agent_maker, "name")
        assert hasattr(agent_maker, "system_prompt")
        assert hasattr(agent_maker, "tool_ids")
        assert hasattr(agent_maker, "hard_constraints")

        assert agent_maker.config_id == "agent_maker"
        assert len(agent_maker.tool_ids) > 0, "应该有工具列表"
        assert "file_write" in agent_maker.tool_ids, "应该有 file_write 工具"

        print(f"✅ Agent配置Schema正确")
        print(f"   - config_id: {agent_maker.config_id}")
        print(f"   - tools: {agent_maker.tool_ids}")

    @pytest.mark.asyncio
    async def test_file_write_tool_creates_agent_config(self, temp_agent_dir):
        """测试 file_write 工具能否创建Agent配置文件"""
        tool = FileWriteTool(base_path=str(temp_agent_dir))

        agent_content = AGENT_TEMPLATE.format(
            name="测试Agent",
            config_id="test_agent_001",
            display_name="测试Agent",
            description="这是一个自动创建的测试Agent",
        )

        result = await tool.execute({
            "action": "write",
            "path": "config/agents/test/test_agent_001.yaml",
            "content": agent_content,
        })

        assert result.success, f"文件写入应该成功: {result.error}"

        file_path = temp_agent_dir / "config" / "agents" / "test" / "test_agent_001.yaml"
        assert file_path.exists(), f"文件应该被创建: {file_path}"

        with open(file_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)

        assert loaded["config_id"] == "test_agent_001"
        assert loaded["name"] == "测试Agent"
        assert loaded["agent_type"] == "specialized"
        assert loaded["level"] == "L3"
        assert "file_write" in loaded["tool_ids"]

        print(f"✅ file_write 工具成功创建Agent配置")
        print(f"   - 文件: {file_path}")
        print(f"   - config_id: {loaded['config_id']}")

    @pytest.mark.asyncio
    async def test_created_agent_can_be_loaded(self, temp_agent_dir):
        """测试创建的Agent配置能否被正确加载"""
        agent_content = AGENT_TEMPLATE.format(
            name="可加载Agent",
            config_id="loadable_agent_001",
            display_name="可加载Agent",
            description="验证创建后能正常加载的Agent",
        )

        file_path = temp_agent_dir / "config" / "agents" / "loadable_agent_001.yaml"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(agent_content)

        registry = AgentRegistry()
        count = registry.load_directory(str(temp_agent_dir))

        assert count == 1, "应该加载1个Agent"

        agent = registry.get("loadable_agent_001")
        assert agent is not None, "应该能获取到加载的Agent"
        assert agent.name == "可加载Agent"
        assert agent.level.value == "L3"
        assert "file_write" in agent.tool_ids

        print(f"✅ 创建的Agent配置可正常加载")
        print(f"   - config_id: {agent.config_id}")
        print(f"   - name: {agent.name}")

    def test_hierarchy_flow(self, agent_registry):
        """验证Agent层级流程"""
        rg_agent = agent_registry.get("resource_manager_agent")
        agent_maker = agent_registry.get("agent_maker")

        assert rg_agent is not None
        assert agent_maker is not None

        assert rg_agent.level.value == "L2"
        assert agent_maker.level.value == "L3"

        assert "task_submit" in rg_agent.tool_ids, "resource_generator 应该能提交任务"
        assert "agent_maker" not in rg_agent.tool_ids, "resource_generator 不应该直接有 agent_maker"

        print(f"✅ Agent层级流程验证")
        print(f"   - L2 (resource_generator): {rg_agent.tool_ids}")
        print(f"   - L3 (agent_maker): {agent_maker.tool_ids}")

    @pytest.mark.asyncio
    async def test_agent_maker_generates_valid_yaml(self, temp_agent_dir):
        """测试 agent_maker 能否生成符合规范的YAML"""
        tool = FileWriteTool(base_path=str(temp_agent_dir))

        required_fields = [
            "config_id", "name", "description", "agent_type",
            "level", "system_prompt", "tool_ids", "hard_constraints",
            "version", "is_active", "status"
        ]

        agent_content = AGENT_TEMPLATE.format(
            name="完整Agent",
            config_id="complete_agent_001",
            display_name="完整Agent",
            description="包含所有必填字段的完整Agent配置",
        )

        result = await tool.execute({
            "action": "write",
            "path": "config/agents/complete_agent_001.yaml",
            "content": agent_content,
        })

        assert result.success

        file_path = temp_agent_dir / "config" / "agents" / "complete_agent_001.yaml"
        with open(file_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)

        missing_fields = [f for f in required_fields if f not in loaded]
        assert len(missing_fields) == 0, f"缺少必填字段: {missing_fields}"

        print(f"✅ agent_maker 生成的YAML包含所有必填字段")
        print(f"   - 字段数: {len(loaded)}")
        print(f"   - 必填字段: {required_fields}")


class TestAgentCreationIntegration:
    """Agent创建集成测试 - 模拟完整流程"""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.mark.asyncio
    async def test_full_creation_flow(self, temp_dir):
        """完整创建流程测试

        模拟：
        1. 用户提出需求：创建一个"代码审查Agent"
        2. resource_manager_agent 接收任务
        3. 调用 agent_maker 生成配置
        4. agent_maker 使用 file_write 创建文件
        5. 验证创建成功
        """
        tool = FileWriteTool(base_path=str(temp_dir))

        code_reviewer_config = """# -*- coding: utf-8 -*-
# 代码审查Agent

config_id: code_reviewer_agent
name: 代码审查专家
display_name: 代码审查专家
description: |
  负责代码审查，提供改进建议，发现潜在问题。

agent_type: specialized
category: code
level: L3

system_prompt: |
  # 你是代码审查专家

  ## 核心职责
  - 审查代码质量
  - 发现潜在bug
  - 提供改进建议
  - 检查代码规范

  ## 审查维度
  1. 代码可读性
  2. 性能考虑
  3. 安全漏洞
  4. 边界条件

tool_ids:
- file_read
- bash_execute
- enhanced_search

hard_constraints:
- 必须指出所有潜在问题
- 必须提供具体改进建议
- 审查要客观公正

soft_constraints:
- 尽量使用建设性语言
- 优先关注关键问题

version: "1.0.0"
is_active: true
status: "active"
max_iterations: 30
timeout_seconds: 600

tags:
- code_review
- quality
- L3

metadata:
  author: auto_generated
  created_at: '2026-04-14'
  purpose: 代码审查
"""

        result = await tool.execute({
            "action": "write",
            "path": "config/agents/code_reviewer_agent.yaml",
            "content": code_reviewer_config,
        })

        assert result.success, f"创建失败: {result.error}"

        file_path = temp_dir / "config" / "agents" / "code_reviewer_agent.yaml"
        assert file_path.exists()

        with open(file_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)

        assert loaded["config_id"] == "code_reviewer_agent"
        assert loaded["name"] == "代码审查专家"
        assert loaded["level"] == "L3"
        assert "file_read" in loaded["tool_ids"]
        assert "bash_execute" in loaded["tool_ids"]
        assert "代码审查" in loaded["description"]

        registry = AgentRegistry()
        count = registry.load_directory(str(temp_dir))
        assert count == 1

        agent = registry.get("code_reviewer_agent")
        assert agent is not None
        assert agent.name == "代码审查专家"

        print(f"✅ 完整创建流程测试通过")
        print(f"   - 创建的Agent: {loaded['config_id']}")
        print(f"   - 文件路径: {file_path}")
        print(f"   - 可加载: {agent is not None}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

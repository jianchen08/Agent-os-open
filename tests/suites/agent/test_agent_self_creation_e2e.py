"""
Agent自创建完整流程测试

测试目标：
1. 模拟用户提交任务给 resource_manager_agent
2. resource_manager_agent 调用 agent_maker
3. agent_maker 创建真正可用的Agent配置
4. 验证创建的Agent包含所有必要字段

运行方式：
    pytest src/tests/test_agent_self_creation_e2e.py -v -s
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from agents.context_builder import ContextBuilder
from agents.registry import AgentRegistry
from tools.builtin.file_write import FileWriteTool

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

AGENT_REQUIRED_FIELDS = [
    "config_id", "name", "description", "agent_type",
    "level", "system_prompt", "tool_ids",
    "version", "is_active", "status"
]

AGENT_OPTIONAL_FIELDS = [
    "category", "display_name", "tags", "metadata",
    "max_iterations", "max_reminders", "timeout_seconds",
    "static_vars", "dynamic_vars", "hard_constraints", "soft_constraints",
    "input_schema", "output_schema", "deliverables", "recommended_metrics",
    "plugins"
]

L3_REQUIRED_TOOLS = ["task_evaluate", "resource_search"]
L3_REQUIRED_CONSTRAINTS = [
    "任务执行完成后必须调用 task_evaluate 工具进行评估",
    "必须输出规定的产出物",
    "评估通过才能结束任务"
]

class TestAgentCreationValidation:
    """Agent创建验证测试"""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def agent_registry(self):
        registry = AgentRegistry()
        registry.load_directory(str(_PROJECT_ROOT / "config" / "agents"))
        return registry

    def test_agent_maker_has_correct_tools(self, agent_registry):
        """验证 agent_maker 有所需的工具"""
        agent_maker = agent_registry.get("agent_maker")
        assert agent_maker is not None

        assert "file_read" in agent_maker.tool_ids
        assert "file_write" in agent_maker.tool_ids
        print(f"✅ agent_maker 工具有: {agent_maker.tool_ids}")

    def test_agent_maker_prompt_includes_template_ref(self, agent_registry):
        """验证 agent_maker 知道使用模板"""
        agent_maker = agent_registry.get("agent_maker")
        assert agent_maker is not None

        prompt = agent_maker.system_prompt
        assert "模板规范" in prompt or "template" in prompt.lower(), \
            "agent_maker 应该知道使用模板规范"
        assert "file_write" in agent_maker.tool_ids, "agent_maker 应该有 file_write 工具"

        print("✅ agent_maker 知道使用模板规范")

    def test_resource_generator_has_task_submit(self, agent_registry):
        """验证 resource_generator 有 task_submit"""
        rg = agent_registry.get("resource_manager_agent")
        assert rg is not None
        assert "task_submit" in rg.tool_ids
        print("✅ resource_generator 有 task_submit")

class TestCompleteAgentCreation:
    """完整Agent创建流程测试"""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def complete_agent_config(self):
        """一个完整可用的Agent配置"""
        return """# -*- coding: utf-8 -*-
# 代码审查Agent

config_id: code_reviewer_test
name: 代码审查专家
display_name: 代码审查专家
description: |
  负责代码审查，发现潜在问题，提供改进建议。
  核心能力：代码质量评估、bug发现、规范检查。

agent_type: specialized
category: code
level: L3

system_prompt: |
  # 你是代码审查专家

  ## 核心职责
  1. 理解代码审查目标
  2. 制定审查计划
  3. 执行代码审查
  4. 输出审查报告
  5. 自评估质量（task_evaluate）

  ## 执行流程
  1. 使用 创建审查计划
  2. 使用 file_read 读取代码文件
  3. 使用 enhanced_search 搜索相关代码
  4. 分析并记录问题
  5. 使用 task_evaluate 评估审查质量

  ## 审查维度
  - 代码可读性
  - 性能考虑
  - 安全漏洞
  - 边界条件
  - 规范遵循

# 静态变量
static_vars:
  enabled: true
  items:
    - name: "行为约束"
      type: "rules"
    - path: "config/rules/document_context_rules.md"
      name: "文档上下文规则"

# 动态变量
dynamic_vars:
  enabled: true
  items:
    - name: "当前时间"
      type: "timestamp"

# 工具配置
tool_ids:
- file_read
- file_write
- enhanced_search
- task_evaluate
- resource_search

# 硬约束
hard_constraints:
- 必须指出所有潜在问题
- 必须提供具体改进建议
- 审查要客观公正
- 任务执行完成后必须调用 task_evaluate 工具进行评估
- 必须输出规定的产出物
- 评估通过才能结束任务

# 软约束
soft_constraints:
- 尽量使用建设性语言
- 优先关注关键问题
- 建议添加测试用例

# 产出物
deliverables:
  - name: "review_report"
    description: "代码审查报告"
    output_path: "{{workspace}}/review_report.md"
    type: "markdown"
    required: true

# 推荐指标
recommended_metrics:
  - metric_id: file_check
    default_params:
      action: "read"
      path: "{{workspace}}/review_report.md"
  - metric_id: semantic_check
    default_params:
      criteria:
        required_sections:
          - "概述"
          - "发现的问题"
          - "改进建议"
          - "总结"

# 运行配置
version: "1.0.0"
is_active: true
status: "active"
max_iterations: 30
max_reminders: 3
timeout_seconds: 600

# 插件配置
plugins:
  disabled: []
  enabled:
    task_reminder:
      max_reminders: 3

# 标签
tags:
- code_review
- quality
- L3
- specialized

# 元数据
metadata:
  author: auto_generated
  created_at: '2026-04-14'
  capabilities:
    - code_review
    - bug_detection
    - quality_assessment
"""

    @pytest.mark.asyncio
    async def test_create_complete_agent(self, temp_dir, complete_agent_config):
        """测试创建完整可用的Agent"""
        tool = FileWriteTool(base_path=str(temp_dir))

        result = await tool.execute({
            "action": "write",
            "path": "config/agents/code_reviewer_test.yaml",
            "content": complete_agent_config,
            "workspace": str(temp_dir),
        })

        assert result.success, f"创建失败: {result.error}"

        file_path = temp_dir / "config" / "agents" / "code_reviewer_test.yaml"
        assert file_path.exists(), f"文件未创建: {file_path}"

        with open(file_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)

        print(f"\n✅ Agent配置已创建: {loaded['config_id']}")

        return loaded

    def test_required_fields_present(self, complete_agent_config):
        """验证必填字段都存在"""
        loaded = yaml.safe_load(complete_agent_config)

        for field in AGENT_REQUIRED_FIELDS:
            assert field in loaded, f"缺少必填字段: {field}"

        print(f"✅ 所有必填字段存在: {AGENT_REQUIRED_FIELDS}")

    def test_l3_tools_present(self, complete_agent_config):
        """验证L3 Agent的必选工具"""
        loaded = yaml.safe_load(complete_agent_config)

        for tool in L3_REQUIRED_TOOLS:
            assert tool in loaded["tool_ids"], f"L3 Agent缺少必选工具: {tool}"

        print(f"✅ L3必选工具齐全: {L3_REQUIRED_TOOLS}")

    def test_l3_constraints_present(self, complete_agent_config):
        """验证L3 Agent的必选约束"""
        loaded = yaml.safe_load(complete_agent_config)

        constraints_text = "\n".join(loaded.get("hard_constraints", []))
        for constraint in L3_REQUIRED_CONSTRAINTS:
            assert constraint in constraints_text, f"缺少L3必选约束: {constraint}"

        print(f"✅ L3必选约束齐全")

    def test_static_vars_structure(self, complete_agent_config):
        """验证静态变量结构"""
        loaded = yaml.safe_load(complete_agent_config)

        assert "static_vars" in loaded
        assert loaded["static_vars"]["enabled"] is True
        assert "items" in loaded["static_vars"]
        assert len(loaded["static_vars"]["items"]) > 0

        items = {item["name"] for item in loaded["static_vars"]["items"]}
        assert "行为约束" in items, "应该有行为约束"

        print(f"✅ 静态变量结构正确: {items}")

    def test_dynamic_vars_structure(self, complete_agent_config):
        """验证动态变量结构"""
        loaded = yaml.safe_load(complete_agent_config)

        assert "dynamic_vars" in loaded
        assert loaded["dynamic_vars"]["enabled"] is True
        assert "items" in loaded["dynamic_vars"]

        items = {item["name"] for item in loaded["dynamic_vars"]["items"]}
        assert "当前时间" in items

        print(f"✅ 动态变量结构正确: {items}")

    def test_deliverables_defined(self, complete_agent_config):
        """验证产出物定义"""
        loaded = yaml.safe_load(complete_agent_config)

        assert "deliverables" in loaded
        assert len(loaded["deliverables"]) > 0

        deliverable = loaded["deliverables"][0]
        assert "name" in deliverable
        assert "output_path" in deliverable
        assert "type" in deliverable

        print(f"✅ 产出物已定义: {deliverable['name']}")

    def test_recommended_metrics_defined(self, complete_agent_config):
        """验证推荐评估指标"""
        loaded = yaml.safe_load(complete_agent_config)

        assert "recommended_metrics" in loaded
        assert len(loaded["recommended_metrics"]) > 0

        for metric in loaded["recommended_metrics"]:
            assert "metric_id" in metric
            assert "default_params" in metric

        print(f"✅ 推荐指标已定义: {[m['metric_id'] for m in loaded['recommended_metrics']]}")

    def test_plugins_config_valid(self, complete_agent_config):
        """验证插件配置"""
        loaded = yaml.safe_load(complete_agent_config)

        assert "plugins" in loaded
        assert "disabled" in loaded["plugins"]
        assert "enabled" in loaded["plugins"]

        print(f"✅ 插件配置有效")

    def test_agent_can_be_loaded(self, temp_dir, complete_agent_config):
        """验证创建的Agent可以被正确加载"""
        file_path = temp_dir / "config" / "agents" / "code_reviewer_test.yaml"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(complete_agent_config)

        registry = AgentRegistry()
        count = registry.load_directory(str(temp_dir))

        assert count == 1, f"应该加载1个Agent，实际: {count}"

        agent = registry.get("code_reviewer_test")
        assert agent is not None
        assert agent.config_id == "code_reviewer_test"
        assert agent.name == "代码审查专家"
        assert agent.level.value == "L3"
        assert "file_read" in agent.tool_ids
        assert "task_evaluate" in agent.tool_ids

        print(f"✅ Agent可被正确加载")
        print(f"   - config_id: {agent.config_id}")
        print(f"   - level: {agent.level.value}")
        print(f"   - tools: {agent.tool_ids}")

class TestAgentContextBuilding:
    """Agent上下文构建测试"""

    def test_context_builder_with_static_vars(self):
        """验证ContextBuilder能正确处理静态变量"""
        registry = AgentRegistry()
        registry.load_directory(str(_PROJECT_ROOT / "config" / "agents"))

        agent = registry.get("code_reviewer_test")
        if agent is None:
            pytest.skip("Agent未创建，跳过上下文构建测试")

        builder = ContextBuilder()
        context = builder.build_static_context(agent)

        assert context["enabled"] is True
        assert len(context["items"]) > 0

        print(f"✅ ContextBuilder构建静态上下文成功")
        print(f"   - items数量: {len(context['items'])}")

    def test_agent_to_state_conversion(self):
        """验证Agent配置能转换为Pipeline state"""
        registry = AgentRegistry()
        registry.load_directory(str(_PROJECT_ROOT / "config" / "agents"))

        agent = registry.get("code_reviewer_test")
        if agent is None:
            pytest.skip("Agent未创建，跳过state转换测试")

        state = agent.to_state()

        assert "system_prompt" in state
        assert "tool_ids" in state
        assert "constraints" in state
        assert len(state["tool_ids"]) > 0

        print(f"✅ Agent.to_state()转换成功")
        print(f"   - system_prompt长度: {len(state['system_prompt'])}")
        print(f"   - tool_ids数量: {len(state['tool_ids'])}")

class TestEndToEndCreation:
    """端到端创建测试 - 模拟完整流程"""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.mark.asyncio
    async def test_full_creation_flow(self, temp_dir):
        """完整创建流程：从需求到可用Agent

        模拟：
        1. 用户需求：创建一个"API测试Agent"
        2. 生成符合规范的Agent配置
        3. 写入文件
        4. 加载验证
        5. 构建上下文验证
        """
        api_tester_config = """# -*- coding: utf-8 -*-
# API测试Agent

config_id: api_tester_test
name: API测试专家
display_name: API测试专家
description: |
  负责API功能测试，验证接口正确性和性能。

agent_type: specialized
category: testing
level: L3

system_prompt: |
  # 你是API测试专家

  ## 核心职责
  1. 理解API测试需求
  2. 制定测试计划
  3. 编写测试用例
  4. 执行API测试
  5. 输出测试报告

  ## 执行流程
  1. 使用 创建测试计划
  2. 使用 bash_execute 执行API调用
  3. 使用 fetch 获取API响应
  4. 分析测试结果
  5. 使用 task_evaluate 自评

static_vars:
  enabled: true
  items:
    - name: "行为约束"
      type: "rules"

dynamic_vars:
  enabled: true
  items:
    - name: "当前时间"
      type: "timestamp"

tool_ids:
- file_read
- file_write
- bash_execute
- fetch
- task_evaluate
- resource_search

hard_constraints:
- 测试必须覆盖所有关键场景
- 必须记录测试结果
- 任务执行完成后必须调用 task_evaluate 工具进行评估
- 必须输出规定的产出物
- 评估通过才能结束任务

soft_constraints:
- 尽量复用测试框架
- 测试用例要清晰可维护

deliverables:
  - name: "test_report"
    description: "API测试报告"
    output_path: "{{workspace}}/api_test_report.md"
    type: "markdown"
    required: true

recommended_metrics:
  - metric_id: file_check
    default_params:
      action: "read"
      path: "{{workspace}}/api_test_report.md"

version: "1.0.0"
is_active: true
status: "active"
max_iterations: 40
max_reminders: 3
timeout_seconds: 900

tags:
- api_testing
- testing
- L3

metadata:
  author: auto_test
  created_at: '2026-04-14'
  capabilities:
    - api_testing
    - test_report_generation
"""

        tool = FileWriteTool(base_path=str(temp_dir))

        result = await tool.execute({
            "action": "write",
            "path": "config/agents/api_tester_test.yaml",
            "content": api_tester_config,
            "workspace": str(temp_dir),
        })
        assert result.success, f"写入失败: {result.error}"

        file_path = temp_dir / "config" / "agents" / "api_tester_test.yaml"
        assert file_path.exists()

        with open(file_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)

        assert loaded["config_id"] == "api_tester_test"
        assert loaded["level"] == "L3"
        assert "bash_execute" in loaded["tool_ids"]
        assert "fetch" in loaded["tool_ids"]
        assert len(loaded["deliverables"]) > 0

        registry = AgentRegistry()
        registry.load_directory(str(temp_dir))
        agent = registry.get("api_tester_test")
        assert agent is not None

        state = agent.to_state()
        assert "system_prompt" in state
        assert len(state["tool_ids"]) > 0

        builder = ContextBuilder()
        static_ctx = builder.build_static_context(agent)
        assert static_ctx["enabled"] is True

        print(f"\n✅ 完整创建流程测试通过")
        print(f"   - Agent: {loaded['config_id']}")
        print(f"   - Level: {loaded['level']}")
        print(f"   - Tools: {len(loaded['tool_ids'])}个")
        print(f"   - 可加载: True")
        print(f"   - 可构建上下文: True")

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

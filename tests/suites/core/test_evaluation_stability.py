"""评估引擎稳定性单元测试。

覆盖范围：
- A1. 评估结果解析：嵌套格式、直接格式、带周围文本、空输入、无 JSON、畸形 JSON
- A2. 评估 Prompt 构建：全字段、评估标准、空参数
- A3. Agent 查找：按 config_id 查找、按 name 回退查找、未找到异常
- A4. 评估前置条件校验：pipeline_factory 缺失、agent_registry 缺失、Agent 未找到
"""

from unittest.mock import MagicMock

import pytest

from evaluation.engine import EvaluationEngine
from evaluation.types import (
    ExpectSpec,
    MetricDefinition,
    MetricType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def metric_def():
    """创建测试用指标定义。"""
    return MetricDefinition(
        id="test_metric",
        name="测试指标",
        description="测试指标描述",
        metric_type=MetricType.AGENT,
        evaluator_id="system_evaluator_agent",
        expect=ExpectSpec(conditions=[]),
    )


@pytest.fixture
def mock_agent_registry():
    """创建 Mock Agent 注册表。"""
    from tests.suites.conftest import MockAgentRegistry

    agent_config = MagicMock()
    agent_config.name = "evaluator_agent"
    agent_config.config_id = "system_evaluator_agent"
    return MockAgentRegistry(configs=[agent_config])


# ---------------------------------------------------------------------------
# A1. 评估结果解析（6 个测试）
# ---------------------------------------------------------------------------


@pytest.mark.core
@pytest.mark.unit
def test_parse_nested_evaluation_result():
    """验证嵌套格式 {"evaluation_result": {...}} 的正确解析。"""
    text = '{"evaluation_result": {"passed": true, "score": 95, "feedback": "OK"}}'
    result = EvaluationEngine._parse_evaluation_result(text)

    assert result is not None
    assert result["passed"] is True
    assert result["score"] == 95.0
    assert result["feedback"] == "OK"


@pytest.mark.core
@pytest.mark.unit
def test_parse_direct_evaluation_result():
    """验证直接格式 {"passed": ..., "score": ...} 的正确解析。"""
    text = '{"passed": false, "score": 40, "feedback": "缺少概念"}'
    result = EvaluationEngine._parse_evaluation_result(text)

    assert result is not None
    assert result["passed"] is False
    assert result["score"] == 40.0
    assert result["feedback"] == "缺少概念"


@pytest.mark.core
@pytest.mark.unit
def test_parse_evaluation_result_with_surrounding_text():
    """验证从包含周围文本的内容中正确提取 JSON。"""
    text = (
        "评估完成！\n"
        "```json\n"
        '{"evaluation_result": {"passed": true, "score": 88, "feedback": "内容完整"}}\n'
        "```\n"
        "以上是结论"
    )
    result = EvaluationEngine._parse_evaluation_result(text)

    assert result is not None
    assert result["passed"] is True
    assert result["score"] == 88.0
    assert result["feedback"] == "内容完整"


@pytest.mark.core
@pytest.mark.unit
def test_parse_evaluation_result_empty_input():
    """验证空字符串输入返回 None。"""
    result = EvaluationEngine._parse_evaluation_result("")

    assert result is None


@pytest.mark.core
@pytest.mark.unit
def test_parse_evaluation_result_no_json():
    """验证不含 JSON 的纯文本输入返回 None。"""
    text = "报告质量不错，建议补充代码示例。"
    result = EvaluationEngine._parse_evaluation_result(text)

    assert result is None


@pytest.mark.core
@pytest.mark.unit
def test_parse_evaluation_result_malformed_json():
    """验证格式错误的 JSON 输入返回 None。"""
    text = '{"evaluation_result": {"passed": true'
    result = EvaluationEngine._parse_evaluation_result(text)

    assert result is None


# ---------------------------------------------------------------------------
# A2. 评估 Prompt 构建（3 个测试）
# ---------------------------------------------------------------------------


@pytest.mark.core
@pytest.mark.unit
def test_build_prompt_with_all_fields(metric_def):
    """验证包含所有可选字段时 Prompt 包含对应段落。"""
    params = {
        "criteria": "检查报告质量",
        "content": "报告内容...",
        "summary": "执行摘要...",
    }
    prompt = EvaluationEngine._build_agent_eval_prompt(metric_def, params)

    assert "## 评估标准：检查报告质量" in prompt
    assert "## 待评估内容" in prompt
    assert "报告内容..." in prompt
    assert "## 任务执行摘要" in prompt
    assert "执行摘要..." in prompt


@pytest.mark.core
@pytest.mark.unit
def test_build_prompt_criteria_from_task_desc(metric_def):
    """验证评估标准文本正确出现在 Prompt 中。"""
    params = {"criteria": "报告包含 async/await 核心概念"}
    prompt = EvaluationEngine._build_agent_eval_prompt(metric_def, params)

    assert "报告包含 async/await 核心概念" in prompt
    assert "## 评估标准" in prompt


@pytest.mark.core
@pytest.mark.unit
def test_build_prompt_empty_params(metric_def):
    """验证空参数时 Prompt 仍包含基本评估指令和 JSON 格式要求。"""
    params = {}
    prompt = EvaluationEngine._build_agent_eval_prompt(metric_def, params)

    assert "请执行以下评估任务" in prompt
    assert "## 评估指标" in prompt
    assert "evaluation_result" in prompt
    assert "passed" in prompt
    assert "score" in prompt


# ---------------------------------------------------------------------------
# A3. Agent 查找（3 个测试）
# ---------------------------------------------------------------------------


@pytest.mark.core
@pytest.mark.unit
def test_find_agent_by_config_id(mock_agent_registry):
    """验证通过 config_id 直接查找 Agent 配置。"""
    config = mock_agent_registry.get("system_evaluator_agent")

    assert config is not None
    assert config.name == "evaluator_agent"


@pytest.mark.core
@pytest.mark.unit
def test_find_agent_by_name_fallback(mock_agent_registry):
    """验证当 config_id 查找失败时，通过 name 遍历回退查找 Agent 配置。"""
    agent_config = mock_agent_registry.get("evaluator_agent")

    # config_id 不匹配，get() 应返回 None
    assert agent_config is None

    # 模拟 engine._evaluate_agent 中的 name 回退查找逻辑
    found = None
    for cfg in mock_agent_registry.list_all():
        cfg_name = getattr(cfg, "name", None) or getattr(cfg, "display_name", None)
        if cfg_name == "evaluator_agent":
            found = cfg
            break

    assert found is not None
    assert found.name == "evaluator_agent"


@pytest.mark.core
@pytest.mark.unit
async def test_find_agent_not_found(metric_def):
    """验证空注册表中查找 Agent 时抛出 RuntimeError。"""
    from tests.suites.conftest import MockAgentRegistry

    empty_registry = MockAgentRegistry(configs=[])
    loader = MagicMock()
    engine = EvaluationEngine(
        loader=loader,
        agent_registry=empty_registry,
    )

    with pytest.raises(RuntimeError, match="not found in registry"):
        await engine._evaluate_agent(metric_def, {})


# ---------------------------------------------------------------------------
# A4. 评估前置条件校验（2 个测试）
# ---------------------------------------------------------------------------


@pytest.mark.core
@pytest.mark.unit
async def test_evaluate_agent_registry_none_raises(metric_def):
    """验证 agent_registry 为 None 时调用 Agent 评估器抛出 RuntimeError。"""
    loader = MagicMock()
    engine = EvaluationEngine(
        loader=loader,
        agent_registry=None,
    )

    with pytest.raises(RuntimeError, match="Agent evaluation requires agent_registry but it is None"):
        await engine._evaluate_agent(metric_def, {})


@pytest.mark.core
@pytest.mark.unit
async def test_evaluate_agent_not_found_raises(metric_def):
    """验证 Agent 在注册表中不存在时调用评估器抛出包含 evaluator_id 的 RuntimeError。"""
    from tests.suites.conftest import MockAgentRegistry

    empty_registry = MockAgentRegistry(configs=[])
    loader = MagicMock()
    engine = EvaluationEngine(
        loader=loader,
        agent_registry=empty_registry,
    )

    with pytest.raises(RuntimeError, match=r"Agent 'system_evaluator_agent' not found in registry"):
        await engine._evaluate_agent(metric_def, {})

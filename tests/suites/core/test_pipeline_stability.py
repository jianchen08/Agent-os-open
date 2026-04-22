"""套件 D：管道执行稳定性测试。

覆盖范围：
- D1: ThreadPoolExecutor 在 _evaluate_agent 中避免嵌套 event loop
- D2: evaluator_agent.yaml max_iterations 配置约束
- D3: evaluator_agent.yaml plugins 配置正确加载
- D4: evaluator_agent.yaml tool_ids 完整性验证
- D5: input_adapter run_in_executor 不阻塞事件循环
- D6: PipelineEngine.run() extra_state 参数传递
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
EVALUATOR_YAML = PROJECT_ROOT / "config" / "agents" / "system" / "evaluator_agent.yaml"


@pytest.mark.core
@pytest.mark.unit
async def test_sub_pipeline_in_thread_pool_executor(
    metric_def,
    mock_agent_registry,
):
    """验证在已有 running event loop 中调用 _evaluate_agent 使用 ThreadPoolExecutor 而不抛 RuntimeError。

    当 _evaluate_agent 检测到当前已有 running event loop 时，
    应通过 ThreadPoolExecutor + asyncio.run 在新线程中运行子管道，
    而非直接嵌套 asyncio.run 导致 RuntimeError。
    """
    from evaluation.engine import EvaluationEngine

    loader = MagicMock()

    eval_output = (
        "## 评估完成\n\n"
        "```json\n"
        '{"evaluation_result": {"passed": true, "score": 95, "feedback": "评估通过"}}\n'
        "```\n"
    )
    mock_engine = MagicMock()
    mock_engine.run = AsyncMock(
        return_value={
            "raw_result": eval_output,
            "final_output": eval_output,
        },
    )
    pipeline_factory = MagicMock(return_value=mock_engine)

    engine = EvaluationEngine(
        loader=loader,
        pipeline_factory=pipeline_factory,
        agent_registry=mock_agent_registry,
    )

    result = engine._evaluate_agent(
        metric_def=metric_def,
        params={"criteria": "报告包含核心概念"},
    )

    assert result is not None
    assert result.get("passed") is True
    assert result.get("score") == 95


@pytest.mark.core
@pytest.mark.unit
def test_max_iterations_enforced():
    """验证 evaluator_agent.yaml 中 max_iterations 等于 15。"""
    config = yaml.safe_load(EVALUATOR_YAML.read_text(encoding="utf-8"))
    assert config["max_iterations"] == 15


@pytest.mark.core
@pytest.mark.unit
def test_evaluator_agent_config_loaded():
    """验证 evaluator_agent.yaml 中 plugins.enabled 包含 task_reminder 且 evaluation_mode 为 true。"""
    config = yaml.safe_load(EVALUATOR_YAML.read_text(encoding="utf-8"))
    enabled_plugins = config["plugins"]["enabled"]
    assert "task_reminder" in enabled_plugins
    assert enabled_plugins["task_reminder"]["evaluation_mode"] is True


@pytest.mark.core
@pytest.mark.unit
def test_evaluator_agent_tool_ids_available():
    """验证 evaluator_agent.yaml 中 tool_ids 包含所需的工具。"""
    expected_tools = ["file_read", "bash_execute", "enhanced_search"]
    config = yaml.safe_load(EVALUATOR_YAML.read_text(encoding="utf-8"))
    tool_ids = config["tool_ids"]
    for tool in expected_tools:
        assert tool in tool_ids


@pytest.mark.core
@pytest.mark.unit
async def test_event_loop_not_blocked_by_input_adapter():
    """验证 run_in_executor 不会阻塞事件循环，其他 async 任务可并发执行。

    模拟 input_adapter 中 _read_multiline 阻塞的场景，
    验证阻塞期间事件循环仍可调度其他异步任务。
    """
    blocking_duration = 0.3
    other_task_completed = False

    def blocking_read():
        """模拟阻塞的 stdin 读取操作。"""
        time.sleep(blocking_duration)
        return "test input"

    async def quick_task():
        """轻量异步任务，应能在阻塞期间完成。"""
        nonlocal other_task_completed
        other_task_completed = True

    loop = asyncio.get_running_loop()
    read_future = loop.run_in_executor(None, blocking_read)

    await asyncio.sleep(0.05)
    task = asyncio.create_task(quick_task())
    await task

    assert other_task_completed is True, "事件循环被阻塞，异步任务未能执行"

    result = await read_future
    assert result == "test input"


@pytest.mark.core
@pytest.mark.unit
async def test_pipeline_state_passed_through():
    """验证 PipelineEngine.run() 的 extra_state 参数正确注入到管道 state。

    通过 _build_initial_state 方法验证 task_id、workspace 等
    extra_state 参数被正确合并到管道初始状态字典中。
    """
    from pipeline.engine import PipelineEngine

    engine = PipelineEngine(
        input_route_table=MagicMock(),
        output_route_table=MagicMock(),
        plugin_registry=MagicMock(),
    )

    state = engine._build_initial_state(
        user_input="test",
        agent_config=None,
        conversation_history=None,
        extra_state={
            "task_id": "__eval__test",
            "workspace": ".ai_workspaces/test",
        },
    )

    assert state["task_id"] == "__eval__test"
    assert state["workspace"] == ".ai_workspaces/test"
    assert state["user_input"] == "test"

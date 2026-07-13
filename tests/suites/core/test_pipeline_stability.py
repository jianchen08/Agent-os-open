"""套件 D：管道执行稳定性测试。

覆盖范围：
- D2: evaluator_agent.yaml max_iterations 配置约束
- D3: evaluator_agent.yaml plugins 配置正确加载
- D4: evaluator_agent.yaml tool_ids 完整性验证
- D5: input_adapter run_in_executor 不阻塞事件循环
- D6: build_initial_state extra_state 参数传递

注：原 D1（_evaluate_agent 经 ThreadPoolExecutor 避免嵌套 event loop）已删除——
该架构随 I1~I5 不变量重构移除（评估改走 send + CollectingSink，不再 ThreadPoolExecutor
+ asyncio.run 嵌套，也不再有 pipeline_factory）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
EVALUATOR_YAML = PROJECT_ROOT / "config" / "agents" / "system" / "evaluator_agent.yaml"


@pytest.mark.core
@pytest.mark.unit
def test_max_iterations_enforced():
    """验证 evaluator_agent.yaml 中 max_iterations 等于 500。

    评估是推理型任务，允许足够迭代轮次完成多维度评分 + 证据收集。
    """
    config = yaml.safe_load(EVALUATOR_YAML.read_text(encoding="utf-8"))
    assert config["max_iterations"] == 500


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
    """验证 evaluator_agent.yaml 中 tool_ids 包含评估所需的核心工具。

    evaluator 是推理型评估者，只读产出物进行分析，核心工具为 file_read。
    """
    expected_tools = ["file_read"]
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
    """验证 build_initial_state 的 extra_state 参数正确注入到管道 state。

    通过模块级公开函数 build_initial_state 验证 task_id、workspace 等
    extra_state 参数被正确合并到管道初始状态字典中。
    """
    from pipeline.state_builder import build_initial_state

    state = build_initial_state(
        user_input="test",
        agent_config=None,
        conversation_history=None,
        pipeline_id="__eval__test",
        services={},
        extra_state={
            "task_id": "__eval__test",
            "workspace": ".ai_workspaces/test",
        },
    )

    assert state["task_id"] == "__eval__test"
    assert state["workspace"] == ".ai_workspaces/test"
    assert state["user_input"] == "test"

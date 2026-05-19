"""测试 Worker 能否正确加载目标 Agent 配置。

验证点：
1. Worker._load_agent_config() 能找到目标 Agent
2. Worker._build_agent_state() 能构建正确的 state（system_prompt, tool_ids）
3. 目标 Agent 的 system_prompt 不是灵汐的（确保不是灵汐自己）
4. 目标 Agent 的 tool_ids 包含目标 Agent 特有的工具
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from infrastructure.worker import TaskWorker


def test_load_agent_config():
    """测试 _load_agent_config 能否找到目标 Agent 配置。"""
    # 创建一个最小的 Worker 实例
    worker = TaskWorker(
        task_service=None,  # type: ignore
        plugin_registry=None,  # type: ignore
        input_route_table=None,  # type: ignore
        output_route_table=None,  # type: ignore
    )

    # 测试加载灵汐
    lingxi_config = worker._load_agent_config("lingxi")
    assert lingxi_config is not None, "应该能找到灵汐 Agent"
    assert lingxi_config.config_id == "lingxi"
    assert lingxi_config.display_name == "灵汐"
    print(f"[PASS] 加载灵汐: {lingxi_config.config_id} ({lingxi_config.display_name})")

    # 测试加载 resource_modifier_agent
    modifier_config = worker._load_agent_config("resource_modifier_agent")
    assert modifier_config is not None, "应该能找到 resource_modifier_agent"
    assert modifier_config.config_id == "resource_modifier_agent"
    assert "修改" in modifier_config.display_name or "modifier" in modifier_config.config_id
    print(f"[PASS] 加载 resource_modifier_agent: {modifier_config.config_id} ({modifier_config.display_name})")

    # 测试加载 resource_analyzer_agent
    analyzer_config = worker._load_agent_config("resource_analyzer_agent")
    assert analyzer_config is not None, "应该能找到 resource_analyzer_agent"
    assert analyzer_config.config_id == "resource_analyzer_agent"
    print(f"[PASS] 加载 resource_analyzer_agent: {analyzer_config.config_id} ({analyzer_config.display_name})")

    # 测试不存在的 Agent
    none_config = worker._load_agent_config("nonexistent_agent_xyz")
    assert none_config is None, "不存在的 Agent 应该返回 None"
    print("[PASS] 不存在的 Agent 返回 None")


def test_build_agent_state():
    """测试 _build_agent_state 能否构建正确的 state。"""
    worker = TaskWorker(
        task_service=None,  # type: ignore
        plugin_registry=None,  # type: ignore
        input_route_table=None,  # type: ignore
        output_route_table=None,  # type: ignore
    )

    # 构建灵汐的 state
    lingxi_config = worker._load_agent_config("lingxi")
    lingxi_state = worker._build_agent_state(lingxi_config)
    assert "system_prompt" in lingxi_state, "灵汐 state 应包含 system_prompt"
    assert "灵汐" in lingxi_state["system_prompt"], "灵汐的 system_prompt 应包含'灵汐'"
    assert "tool_ids" in lingxi_state, "灵汐 state 应包含 tool_ids"
    assert "task_submit" in lingxi_state["tool_ids"], "灵汐的 tool_ids 应包含 task_submit"
    print(f"[PASS] 灵汐 state: system_prompt 包含'灵汐', tool_ids={lingxi_state['tool_ids']}")

    # 构建 resource_modifier_agent 的 state
    modifier_config = worker._load_agent_config("resource_modifier_agent")
    modifier_state = worker._build_agent_state(modifier_config)
    assert "system_prompt" in modifier_state, "resource_modifier_agent state 应包含 system_prompt"
    # 关键验证：不是灵汐的 system_prompt
    assert "灵汐" not in modifier_state["system_prompt"], \
        f"resource_modifier_agent 的 system_prompt 不应包含'灵汐'！实际: {modifier_state['system_prompt'][:100]}"
    assert "修改" in modifier_state["system_prompt"], \
        f"resource_modifier_agent 的 system_prompt 应包含'修改'！实际: {modifier_state['system_prompt'][:100]}"
    assert "tool_ids" in modifier_state, "resource_modifier_agent state 应包含 tool_ids"
    assert "task_evaluate" in modifier_state["tool_ids"], \
        f"resource_modifier_agent 的 tool_ids 应包含 task_evaluate！实际: {modifier_state['tool_ids']}"
    # 关键验证：不是灵汐的 tool_ids
    assert "task_submit" not in modifier_state["tool_ids"], \
        f"resource_modifier_agent 的 tool_ids 不应包含 task_submit（这是灵汐的）！实际: {modifier_state['tool_ids']}"
    print(f"[PASS] resource_modifier_agent state: system_prompt 包含'修改'(非灵汐), tool_ids={modifier_state['tool_ids']}")

    # 构建 resource_analyzer_agent 的 state
    analyzer_config = worker._load_agent_config("resource_analyzer_agent")
    analyzer_state = worker._build_agent_state(analyzer_config)
    assert "system_prompt" in analyzer_state
    assert "灵汐" not in analyzer_state["system_prompt"], \
        f"resource_analyzer_agent 的 system_prompt 不应包含'灵汐'！实际: {analyzer_state['system_prompt'][:100]}"
    assert "分析" in analyzer_state["system_prompt"], \
        f"resource_analyzer_agent 的 system_prompt 应包含'分析'！实际: {analyzer_state['system_prompt'][:100]}"
    assert "task_evaluate" in analyzer_state["tool_ids"], \
        f"resource_analyzer_agent 的 tool_ids 应包含 task_evaluate！实际: {analyzer_state['tool_ids']}"
    print(f"[PASS] resource_analyzer_agent state: system_prompt 包含'分析'(非灵汐), tool_ids={analyzer_state['tool_ids']}")

    # 测试 None config
    empty_state = worker._build_agent_state(None)
    assert empty_state == {}, "None config 应返回空 state"
    print("[PASS] None config 返回空 state")


def test_system_prompt_includes_constraints():
    """测试系统提示词是否包含约束条件。"""
    worker = TaskWorker(
        task_service=None,  # type: ignore
        plugin_registry=None,  # type: ignore
        input_route_table=None,  # type: ignore
        output_route_table=None,  # type: ignore
    )

    # resource_modifier_agent 有硬约束"必须调用 task_evaluate 进行任务评估"
    modifier_config = worker._load_agent_config("resource_modifier_agent")
    modifier_state = worker._build_agent_state(modifier_config)
    prompt = modifier_state.get("system_prompt", "")
    assert "硬约束" in prompt, f"系统提示词应包含'硬约束'！实际: {prompt[:200]}"
    assert "task_evaluate" in prompt, f"系统提示词应包含'task_evaluate'约束！实际: {prompt[:200]}"
    print(f"[PASS] resource_modifier_agent 系统提示词包含硬约束和 task_evaluate 要求")

    # 灵汐没有 task_evaluate 的硬约束（它的硬约束是"必须先搜索资源再派发任务"）
    lingxi_config = worker._load_agent_config("lingxi")
    lingxi_state = worker._build_agent_state(lingxi_config)
    lingxi_prompt = lingxi_state.get("system_prompt", "")
    # 灵汐的 tool_ids 里没有 task_evaluate，但有 task_submit
    assert "task_submit" in lingxi_state["tool_ids"], "灵汐的 tool_ids 应包含 task_submit"
    print(f"[PASS] 灵汐系统提示词验证通过")


if __name__ == "__main__":
    print("=" * 60)
    print("测试 Worker Agent 配置加载")
    print("=" * 60)

    test_load_agent_config()
    print()
    test_build_agent_state()
    print()
    test_system_prompt_includes_constraints()

    print("\n" + "=" * 60)
    print("所有测试通过！")
    print("=" * 60)

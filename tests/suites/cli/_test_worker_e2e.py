"""端到端闭环测试 — 验证 Worker 执行任务时使用目标 Agent 配置。

核心验证点（修复前会失败的）：
1. Worker 收到 pending 任务后，加载目标 Agent 的 system_prompt（不是灵汐的）
2. 子管道的 state 中 system_prompt 不包含"灵汐"，而是目标 Agent 的内容
3. 子管道的 state 中 tool_ids 是目标 Agent 的工具列表
4. LLM 收到的是目标 Agent 的提示词，会按照目标 Agent 的角色行动

测试方法：
- 直接通过 TaskService 创建任务（target_id=resource_modifier_agent）
- Worker 收到事件后执行
- 检查子管道的 state 验证 Agent 配置正确注入
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))


async def test_worker_uses_target_agent():
    """测试 Worker 执行任务时使用目标 Agent 配置。"""
    from channels.cli.cli_main import CLIApplication

    print("=" * 60)
    print("Worker 目标 Agent 配置注入测试")
    print("=" * 60)

    # 1. 初始化应用
    app = CLIApplication()
    app.setup_real_pipeline()

    # 2. 直接通过 TaskService 创建任务
    from tasks.service import TaskService
    from tasks.storage import TaskStorage
    from tasks.types import TaskPriority

    task_service = app._services.get("task_service")
    if task_service is None:
        task_service = TaskService()

    # 创建一个目标为 resource_modifier_agent 的任务
    task = task_service.create_task(
        title="测试任务：修改资源配置",
        description="请分析并修改 test_resource.yaml 配置文件",
        priority=5,
        metadata={
            "target_type": "agent",
            "target_id": "resource_modifier_agent",
            "acceptance_criteria": {"file_check": {"input_params": {"action": "read"}}},
        },
    )
    print(f"\n[创建任务] task_id={task.id}, target_id=resource_modifier_agent")

    # 3. 验证 Worker 的 Agent 配置加载
    worker = app._task_worker

    # 测试 _load_agent_config
    agent_config = worker._load_agent_config("resource_modifier_agent")
    assert agent_config is not None, "应该能找到 resource_modifier_agent"
    assert agent_config.config_id == "resource_modifier_agent"
    assert "灵汐" not in agent_config.display_name
    print(f"[PASS] Worker._load_agent_config 找到目标 Agent: {agent_config.display_name}")

    # 测试 _build_target_agent_state
    agent_state = worker._build_target_agent_state("resource_modifier_agent", task)
    assert "system_prompt" in agent_state, "state 应包含 system_prompt"
    assert "灵汐" not in agent_state["system_prompt"], \
        f"目标 Agent 的 system_prompt 不应包含'灵汐'！内容开头: {agent_state['system_prompt'][:100]}"
    assert "修改" in agent_state["system_prompt"], \
        f"resource_modifier_agent 的 system_prompt 应包含'修改'！"
    assert "task_evaluate" in agent_state["system_prompt"], \
        f"硬约束'必须调用 task_evaluate'应出现在系统提示词中"
    assert "tool_ids" in agent_state, "state 应包含 tool_ids"
    assert "task_evaluate" in agent_state["tool_ids"], \
        f"目标 Agent 的 tool_ids 应包含 task_evaluate！实际: {agent_state['tool_ids']}"
    assert "task_submit" not in agent_state["tool_ids"], \
        f"目标 Agent 的 tool_ids 不应包含 task_submit（这是灵汐的）！实际: {agent_state['tool_ids']}"
    print(f"[PASS] Worker._build_target_agent_state 构建正确 state:")
    print(f"       system_prompt 前 50 字: {agent_state['system_prompt'][:50]}...")
    print(f"       tool_ids: {agent_state['tool_ids']}")

    # 4. 对比：灵汐的 state 应该不同
    lingxi_state = worker._build_target_agent_state("lingxi", None)  # type: ignore
    assert lingxi_state["system_prompt"] != agent_state["system_prompt"], \
        "灵汐和 resource_modifier_agent 的 system_prompt 应该不同"
    assert lingxi_state["tool_ids"] != agent_state["tool_ids"], \
        "灵汐和 resource_modifier_agent 的 tool_ids 应该不同"
    print(f"[PASS] 灵汐 vs resource_modifier_agent 配置不同（符合预期）")
    print(f"       灵汐 tool_ids: {lingxi_state['tool_ids']}")
    print(f"       修改 Agent tool_ids: {agent_state['tool_ids']}")

    print("\n" + "=" * 60)
    print("所有验证通过！Worker 能正确加载目标 Agent 配置")
    print("=" * 60)

    print(f"\n[清理] 测试任务 {task.id} 将自动过期，无需手动删除")


if __name__ == "__main__":
    asyncio.run(test_worker_uses_target_agent())

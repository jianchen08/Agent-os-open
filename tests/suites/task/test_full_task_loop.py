"""完整任务闭环测试 - 验证任务提交→执行→评估→通知全流程

测试场景：
1. 灵汐 Agent 提交任务给 resource_manager_agent
2. resource_manager_agent 执行并创建 Agent 配置
3. 任务评估（验收标准检查）
4. 任务状态更新（completed/failed）
5. 结果返回给调用方
"""

import asyncio
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from channels.cli.cli_main import CLIApplication
from tasks.service import TaskService
from tasks.storage import TaskStorage


@pytest.mark.integration
@pytest.mark.skip(reason="依赖已移除的 _build_agent_state API，需要 LLM API key")
async def test_full_loop():
    print("=" * 60)
    print("完整任务闭环测试")
    print("=" * 60)

    # 初始化 CLI
    app = CLIApplication()
    app.setup_pipeline()

    # 启动 Worker
    if hasattr(app, '_task_worker') and app._task_worker:
        await app._task_worker.start()
        print("[Worker] 已启动")

    # 构建用户输入：让灵汐创建一个测试 Agent
    prompt = "请帮我创建一个名为'test_data_analyst'的数据分析专家Agent，用于处理CSV数据分析任务"

    # 构建 Agent 状态
    agent_state = app._build_agent_state()
    merged_state = {**agent_state, "user_input": prompt}

    print(f"\n[用户输入] {prompt}")
    print("\n[执行中...]")

    # 运行管道
    result = await app._engine.run(merged_state)

    # 检查结果
    print("\n" + "=" * 60)
    print("执行结果分析")
    print("=" * 60)

    # 1. 检查工具调用
    last_tool_calls = result.get("_last_raw_tool_calls", [])
    tool_results = result.get("tool_results", [])

    print("\n1. 工具调用情况:")
    print(f"   - 调用次数: {len(last_tool_calls)}")
    for tc in last_tool_calls:
        print(f"   - 调用: {tc.get('name')}")

    print("\n2. 工具执行结果:")
    print(f"   - 结果数: {len(tool_results)}")

    task_submit_result = None
    for tr in tool_results:
        name = tr.get('tool_name')
        success = tr.get('success')
        print(f"   - {name}: success={success}")
        if name == 'task_submit':
            task_submit_result = tr

    # 3. 检查任务提交详情
    print("\n3. 任务提交详情:")
    task_id = None
    if task_submit_result:
        data = task_submit_result.get('data', {})
        task_id = data.get('task_id')
        print(f"   - task_id: {task_id}")
        print(f"   - status: {data.get('status')}")
        print(f"   - title: {data.get('title')}")
        print(f"   - target_id: {data.get('target_id')}")
        print(f"   - message: {data.get('message')}")
    else:
        print("   - 未找到 task_submit 调用!")

    # 4. 等待 Worker 执行任务
    print("\n4. 等待 Worker 执行任务...")
    if task_id:
        # 创建 TaskService 检查任务状态
        storage = TaskStorage()
        task_service = TaskService(storage)

        # 等待最多 60 秒
        for i in range(60):
            task = task_service.get_task(task_id)
            if task and task.status.value in ("completed", "failed"):
                print(f"   - 任务已完成，状态: {task.status.value}")
                print(f"   - 结果: {task.result}")
                break
            await asyncio.sleep(1)
            if i % 10 == 0:
                print(f"   - 等待中... {i}s")
        else:
            print("   - 等待超时，任务可能仍在执行")

    # 5. 检查 Agent 配置是否创建
    print("\n5. Agent 配置创建检查:")
    config_path = Path("config/agents/test_data_analyst.yaml")
    if config_path.exists():
        print(f"   - [PASS] Agent 配置文件已创建: {config_path}")
    else:
        print(f"   - [INFO] Agent 配置文件未找到（可能任务未完成）")
        agents_dir = Path("config/agents")
        if agents_dir.exists():
            agents = list(agents_dir.glob("*.yaml"))
            print(f"   - 现有 Agent 配置 ({len(agents)}个):")
            for a in agents[:5]:
                print(f"     - {a.name}")

    # 6. 停止 Worker
    if hasattr(app, '_task_worker') and app._task_worker:
        await app._task_worker.stop()
        print("\n[Worker] 已停止")

    print("\n" + "=" * 60)
    print("测试结论")
    print("=" * 60)

    if task_submit_result and task_submit_result.get('success'):
        data = task_submit_result.get('data', {})
        if data.get('status') == 'completed':
            print("[PASS] 任务闭环完整: 提交->执行->评估->完成")
            return True
        else:
            print(f"[INFO] 任务提交成功，状态: {data.get('status')}")
            print("[INFO] Worker 已启动，任务应在后台执行")
            return True
    else:
        print("[FAIL] 任务提交失败")
        return False


if __name__ == "__main__":
    success = asyncio.run(test_full_loop())
    sys.exit(0 if success else 1)

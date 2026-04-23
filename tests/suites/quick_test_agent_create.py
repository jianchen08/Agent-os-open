"""快速测试脚本：发送创建 Agent 请求，等 60 秒后退出，然后手动查日志和数据。

用法: python -m pytest tests/quick_test_agent_create.py -s --run-integration
"""

import asyncio
import os
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(PROJECT_ROOT)

TASK_SUBMIT_MSG = (
    "请帮我创建一个新的 Agent，名字叫 e2e_time_agent，"
    "职责是回答用户关于时间的问题，例如当前时间、日期计算、时区转换等。"
    "配置文件保存到 config/agents/executor/test/ 目录下。"
)

WAIT_SECONDS = 90


@pytest.mark.asyncio
async def test_quick_agent_create():
    """发送消息后等待指定秒数退出，手动查看日志和数据。"""
    from channels.cli.cli_main import CLIApplication, setup_logging
    from infrastructure.service_provider import ServiceProvider

    setup_logging(debug=False)

    print("=" * 60, flush=True)
    print("  Phase 1: 初始化系统", flush=True)
    print("=" * 60, flush=True)

    ServiceProvider.reset()
    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    tw = getattr(app, "_task_worker", None)
    print(f"  TaskWorker: {'OK' if tw else 'NOT AVAILABLE'}", flush=True)
    print(f"  Agent: {app._agent_config.config_id}", flush=True)

    if tw:
        await tw.start()
        print("  TaskWorker 已启动", flush=True)

    print("\n" + "=" * 60, flush=True)
    print("  Phase 2: 发送请求", flush=True)
    print("=" * 60, flush=True)
    print(f"  请求: {TASK_SUBMIT_MSG}", flush=True)

    start_time = time.time()

    async def _run():
        try:
            await app._engine.run(
                user_input=TASK_SUBMIT_MSG,
                agent_config=app._agent_config,
                streaming=False,
                auto_approve=True,
                interaction_mode="auto",
            )
            elapsed = time.time() - start_time
            print(f"\n  L1 engine.run() 完成 ({elapsed:.1f}s)", flush=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"\n  L1 engine.run() 异常: {e}", flush=True)

    run_task = asyncio.create_task(_run())

    # 等待指定时间
    print(f"\n  等待 {WAIT_SECONDS} 秒...", flush=True)
    for i in range(WAIT_SECONDS):
        await asyncio.sleep(1)
        if i % 15 == 14:
            elapsed = time.time() - start_time
            print(f"  [{elapsed:.0f}s] 等待中...", flush=True)

    # 打印当前任务状态
    import yaml
    tasks_dir = PROJECT_ROOT / "data" / "tasks"
    if tasks_dir.exists():
        print("\n  当前任务:", flush=True)
        for tree_dir in sorted(tasks_dir.iterdir()):
            if tree_dir.is_dir():
                for task_file in tree_dir.glob("*.yaml"):
                    try:
                        td = yaml.safe_load(task_file.read_text(encoding="utf-8"))
                        if isinstance(td, dict):
                            tid = td.get("id", "?")[:12]
                            status = td.get("status", "?")
                            target = (td.get("metadata") or {}).get("target_id", "")
                            title = str(td.get("title", "?"))[:40]
                            print(f"    [{tid}] {status} | {target} | {title}", flush=True)
                    except Exception:
                        pass

    # 检查产出文件
    agent_yaml = PROJECT_ROOT / "config" / "agents" / "executor" / "test" / "e2e_time_agent.yaml"
    if agent_yaml.exists():
        print(f"\n  产出文件已存在: {agent_yaml}", flush=True)
    else:
        ws_root = PROJECT_ROOT / ".ai_workspaces"
        found = list(ws_root.rglob("e2e_time_agent.yaml")) if ws_root.exists() else []
        if found:
            print(f"\n  产出文件在工作空间: {found[0]}", flush=True)
        else:
            print("\n  产出文件尚未创建", flush=True)

    elapsed = time.time() - start_time
    print(f"\n  总耗时: {elapsed:.1f}s", flush=True)
    print("=" * 60, flush=True)
    print("  退出。请手动检查:", flush=True)
    print(f"    日志: logs/agent_os.log", flush=True)
    print(f"    任务: data/tasks/", flush=True)
    print(f"    管道: data/pipelines/", flush=True)
    print(f"    工作空间: .ai_workspaces/", flush=True)
    print("=" * 60, flush=True)

    run_task.cancel()
    try:
        await run_task
    except (asyncio.CancelledError, Exception):
        pass

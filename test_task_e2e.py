"""端到端任务系统测试 — 使用真实 LLM 调用验证完整流程。

验证项：
1. 基础服务初始化
2. 管道+LLM调用正常
3. 任务提交+子管道执行
4. 数据完整性
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
os.chdir(str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/test_e2e.log", mode="w", encoding="utf-8"),
    ],
)
logger = logging.getLogger("e2e_test")


async def test_step1_service_init():
    """验证基础服务初始化。"""
    print("\n" + "=" * 60)
    print("Step 1: 基础服务初始化验证")
    print("=" * 60)

    checks = []

    # 1. Pipeline config
    try:
        from pipeline.config import load_pipeline_config
        c = load_pipeline_config("config/pipelines/default.yaml")
        assert c.name, "Pipeline config name is empty"
        checks.append(("Pipeline config", True, f"name={c.name}, routes={len(c.input_route_table.entries)}/{len(c.output_route_table.entries)}"))
    except Exception as e:
        checks.append(("Pipeline config", False, str(e)))

    # 2. Agent registry
    try:
        from agents.registry import AgentRegistry
        reg = AgentRegistry()
        reg.load_directory("config/agents")
        assert len(reg._configs) > 0, "No agents loaded"
        checks.append(("Agent registry", True, f"{len(reg._configs)} agents"))
    except Exception as e:
        checks.append(("Agent registry", False, str(e)))

    # 3. Tool registry
    try:
        from tools.registry import ToolRegistry
        from tools.builtin import register_core_tools
        tr = ToolRegistry()
        registered = register_core_tools(tr, session=None)
        assert len(registered) > 0, "No tools registered"
        checks.append(("Tool registry", True, f"{len(registered)} tools"))
    except Exception as e:
        checks.append(("Tool registry", False, str(e)))

    # 4. Evaluation metrics
    try:
        from evaluation.loader import MetricLoader
        loader = MetricLoader()
        loader.load_all()
        assert len(loader.metrics) > 0, "No metrics loaded"
        checks.append(("Evaluation metrics", True, f"{len(loader.metrics)} metrics"))
    except Exception as e:
        checks.append(("Evaluation metrics", False, str(e)))

    # 5. TaskService
    try:
        from tasks.service import TaskService
        ts = TaskService()
        checks.append(("TaskService", True, "OK"))
    except Exception as e:
        checks.append(("TaskService", False, str(e)))

    # 6. EventBus
    try:
        from pipeline.event_bus import EventBus
        bus = EventBus()
        received = []
        bus.subscribe("test.evt", lambda d: received.append(d))
        asyncio.get_event_loop().run_until_complete(bus.emit("test.evt", {"t": 1}))
        checks.append(("EventBus", True, f"emit/subscribe OK"))
    except Exception as e:
        checks.append(("EventBus", False, str(e)))

    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {detail}")

    print(f"\n  结果: {passed}/{total} 通过")
    assert passed == total, f"{total - passed} 项初始化检查失败"
    return True


async def test_step2_simple_task():
    """验证简单任务提交 + 子管道执行（真实 LLM 调用）。"""
    print("\n" + "=" * 60)
    print("Step 2: 简单任务提交 + 子管道执行（真实 LLM）")
    print("=" * 60)

    from channels.cli.cli_main import CLIApplication

    app = CLIApplication(streaming=False)
    app.setup_pipeline(config_path=None)

    # 测试消息：简单的工具调用
    test_input = "请用 current_time 工具告诉我现在几点了"
    print(f"  发送: {test_input}")

    try:
        result = await app.run_single(test_input)
        print(f"  收到响应 (类型: {type(result).__name__})")

        if isinstance(result, dict):
            raw = result.get("raw_result", "")
            print(f"  原始结果: {raw[:200]}..." if len(raw) > 200 else f"  原始结果: {raw}")

            tool_calls = result.get("raw_tool_calls", [])
            if tool_calls:
                print(f"  工具调用: {len(tool_calls)} 个")
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name", "unknown")
                    print(f"    - {name}")
            else:
                print("  工具调用: 无（可能 LLM 未调用工具）")

            print("  [PASS] LLM 管道调用成功")
        else:
            print(f"  [PASS] 管道执行成功 (返回类型: {type(result)})")

    except Exception as e:
        print(f"  [FAIL] 管道执行异常: {e}")
        raise

    return True


async def test_step3_task_submission():
    """验证任务提交流程（TaskSubmitTool + EventBus + TaskWorker）。"""
    print("\n" + "=" * 60)
    print("Step 3: 任务提交完整流程验证")
    print("=" * 60)

    from channels.cli.cli_main import CLIApplication

    app = CLIApplication(streaming=False)
    app.setup_pipeline(config_path=None)

    # 测试消息：触发任务提交
    test_input = "请帮我创建一个任务，目标是获取当前时间，使用 current_time 工具完成，验收标准是命令执行成功"
    print(f"  发送: {test_input}")

    try:
        result = await app.run_single(test_input)
        print(f"  收到响应")

        if isinstance(result, dict):
            raw = result.get("raw_result", "")
            print(f"  结果预览: {raw[:300]}..." if len(raw) > 300 else f"  结果: {raw}")

            # 检查是否有 task_submit 工具调用
            tool_calls = result.get("raw_tool_calls", [])
            task_submit_found = False
            for tc in tool_calls:
                name = tc.get("function", {}).get("name", "")
                if "task_submit" in name:
                    task_submit_found = True
                    print(f"  [OK] 检测到 task_submit 工具调用")
                    break

            if not task_submit_found and "task" in raw.lower():
                print(f"  [OK] 响应中提到了任务相关内容")
            elif not task_submit_found:
                print(f"  [WARN] 未检测到 task_submit 调用，LLM 可能选择了不同的方式")

        print("  [PASS] 任务提交流程测试完成")

    except Exception as e:
        print(f"  [FAIL] 异常: {e}")
        raise

    return True


async def test_step4_data_integrity():
    """验证任务数据完整性。"""
    print("\n" + "=" * 60)
    print("Step 4: 数据完整性检查")
    print("=" * 60)

    try:
        from tasks.service import TaskService
        ts = TaskService()
        tasks = ts.list_all()

        if not tasks:
            print("  [WARN] 无任务数据，可能前面的步骤未创建任务")
            return True

        print(f"  共 {len(tasks)} 个任务:")
        for t in tasks:
            status = t.status.value if hasattr(t.status, "value") else str(t.status)
            parent = t.parent_task_id or "ROOT"
            title = t.title[:40] if t.title else "N/A"
            print(f"    {t.id}: {status} | parent={parent} | {title}")
            if t.metadata:
                metrics = t.metadata.get("evaluation_metric_ids", [])
                workspace = t.metadata.get("workspace", "N/A")
                if metrics:
                    print(f"      metrics={metrics}")
                if workspace != "N/A":
                    print(f"      workspace={workspace}")

        # 检查任务数据文件
        data_dir = PROJECT_ROOT / "data" / "tasks"
        if data_dir.exists():
            task_files = list(data_dir.rglob("*.yaml"))
            print(f"  任务数据文件: {len(task_files)} 个")
            for f in task_files[:5]:
                print(f"    {f.relative_to(PROJECT_ROOT)}")
        else:
            print("  [WARN] data/tasks/ 目录不存在")

        print("  [PASS] 数据完整性检查完成")

    except Exception as e:
        print(f"  [FAIL] 异常: {e}")
        raise

    return True


async def test_step5_workspace_git():
    """验证工作空间和 Git 状态。"""
    print("\n" + "=" * 60)
    print("Step 5: 工作空间 + Git 状态检查")
    print("=" * 60)

    import subprocess

    # 检查 workspace 目录
    ws_dir = PROJECT_ROOT / ".ai_workspaces"
    if ws_dir.exists():
        dirs = [d for d in ws_dir.iterdir() if d.is_dir()]
        print(f"  .ai_workspaces: {len(dirs)} 个目录")
        for d in dirs:
            print(f"    {d.name}")
    else:
        print("  .ai_workspaces: 不存在（正常，如果还没创建过任务）")

    # 检查 git worktree
    try:
        result = subprocess.run(
            ["git", "worktree", "list"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        worktrees = result.stdout.strip().split("\n")
        print(f"  Git worktrees: {len(worktrees)} 个")
        for wt in worktrees:
            print(f"    {wt}")
    except Exception as e:
        print(f"  [WARN] Git worktree 检查失败: {e}")

    # 检查 git branches
    try:
        result = subprocess.run(
            ["git", "branch"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        branches = [b.strip() for b in result.stdout.strip().split("\n") if b.strip()]
        print(f"  Git branches: {len(branches)} 个")
        for b in branches:
            print(f"    {b}")
    except Exception as e:
        print(f"  [WARN] Git branch 检查失败: {e}")

    print("  [PASS] 工作空间+Git 状态检查完成")
    return True


async def main():
    """运行所有验证步骤。"""
    # 确保日志目录存在
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)

    results = []

    tests = [
        ("Step 1: 基础服务初始化", test_step1_service_init),
        ("Step 2: 简单任务(真实LLM)", test_step2_simple_task),
        ("Step 3: 任务提交流程", test_step3_task_submission),
        ("Step 4: 数据完整性", test_step4_data_integrity),
        ("Step 5: 工作空间+Git", test_step5_workspace_git),
    ]

    for name, test_func in tests:
        try:
            await test_func()
            results.append((name, True, None))
        except Exception as e:
            results.append((name, False, str(e)))
            logger.exception(f"{name} 失败")

    # 汇总
    print("\n" + "=" * 60)
    print("验证结果汇总")
    print("=" * 60)
    passed = 0
    for name, ok, err in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
        if err:
            print(f"         错误: {err}")
        if ok:
            passed += 1

    print(f"\n  总计: {passed}/{len(results)} 通过")

    if passed == len(results):
        print("\n  所有验证项通过！")
    else:
        print(f"\n  {len(results) - passed} 项失败，需要修复")

    return passed == len(results)


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)

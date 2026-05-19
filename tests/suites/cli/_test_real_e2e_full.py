"""真实端到端测试：LLM+工具执行→验证持久化→恢复管道。

测试项：
1. LLM 调用工具执行任务
2. 验证任务持久化（data/tasks.json 有数据）
3. 验证LLM/tool执行记录保存到数据（output/execution_logs/ 有文件）
4. 验证管道恢复后重新读取历史消息执行
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("e2e_test")


async def main():
    """主测试流程。"""
    project_root = Path(__file__).parent

    # ============================================================
    # Step 0: 初始化管道引擎
    # ============================================================
    logger.info("=" * 60)
    logger.info("Step 0: 初始化管道引擎")

    from channels.cli.cli_main import CLIApplication

    app = CLIApplication(streaming=False)
    app.setup_real_pipeline()
    engine = app._engine
    services = app._services
    agent_config = app._agent_config

    if engine is None:
        logger.error("Engine 初始化失败！")
        return False

    # 覆盖 tool_ids 为当前可用的工具
    if agent_config and hasattr(agent_config, 'tool_ids'):
        agent_config.tool_ids = ['current_time', 'calculator']
        logger.info("Override tool_ids to: %s", agent_config.tool_ids)

    logger.info("Engine OK, Agent: %s, Services: %s",
                agent_config.display_name if agent_config else "None",
                list(services.keys()))

    # ============================================================
    # Step 1: LLM 调用工具执行任务
    # ============================================================
    logger.info("=" * 60)
    logger.info("Step 1: LLM 调用工具")

    user_input = "请用 current_time 工具查询当前时间，然后再用 calculator 计算 2025 + 1 等于多少。必须使用这两个工具。"

    final_state = await engine.run(
        user_input=user_input,
        agent_config=agent_config,
        extra_state={
            "streaming": False,
            "auto_approve": True,
            "interaction_mode": "auto",
        },
    )

    raw_result = final_state.get("raw_result", "")
    tool_results = final_state.get("tool_results", [])
    messages = final_state.get("messages", [])
    iteration = final_state.get("iteration", 0)

    logger.info("  迭代: %d, 工具调用: %d 次, 消息: %d", iteration, len(tool_results), len(messages))
    logger.info("  LLM 回复: %s", raw_result[:200] if raw_result else "(空)")
    for tr in tool_results:
        logger.info("    - %s: success=%s", tr.get("tool_name", "?"), tr.get("success"))

    test1_pass = len(tool_results) > 0
    logger.info("✅ Step 1 通过" if test1_pass else "❌ Step 1 失败")

    # ============================================================
    # Step 2: 验证任务持久化
    # ============================================================
    logger.info("=" * 60)
    logger.info("Step 2: 验证任务持久化")

    tasks_file = project_root / "data" / "tasks.json"
    if tasks_file.exists():
        tasks_data = json.loads(tasks_file.read_text(encoding="utf-8"))
        task_count = len(tasks_data)
        logger.info("  tasks.json: %d 个任务", task_count)
        for tid, task in list(tasks_data.items())[:3]:
            logger.info("    - %s: status=%s", tid[:8], task.get("status"))
        test2_pass = task_count > 0
    else:
        test2_pass = False
        logger.error("  tasks.json 不存在！")

    logger.info("✅ Step 2 通过" if test2_pass else "❌ Step 2 失败")

    # ============================================================
    # Step 3: 验证执行记录持久化
    # ============================================================
    logger.info("=" * 60)
    logger.info("Step 3: 验证执行记录持久化")

    exec_log_dir = project_root / "output" / "execution_logs"
    if exec_log_dir.exists():
        log_files = list(exec_log_dir.glob("*.json"))
        logger.info("  execution_logs/: %d 个文件", len(log_files))
        sorted_files = sorted(log_files, key=lambda f: f.stat().st_mtime, reverse=True)
        for lf in sorted_files[:3]:
            try:
                rec = json.loads(lf.read_text(encoding="utf-8"))
                logger.info("    - iter=%s, core=%s, tools=%s",
                            rec.get("iteration"), rec.get("core_type", ""),
                            rec.get("tool_results", ""))
            except Exception:
                pass
        test3_pass = len(log_files) > 0
    else:
        test3_pass = False
        logger.error("  execution_logs/ 不存在！")

    logger.info("✅ Step 3 通过" if test3_pass else "❌ Step 3 失败")

    # ============================================================
    # Step 4: 验证管道恢复（新 Engine 传入历史消息）
    # ============================================================
    logger.info("=" * 60)
    logger.info("Step 4: 验证管道恢复")

    engine2 = type(engine)(
        input_route_table=engine.input_route_table,
        output_route_table=engine.output_route_table,
        plugin_registry=engine.plugin_registry,
        services=engine._services,
    )

    continuation_input = "根据之前的对话，你还记得我刚才问了什么吗？请简要说明。"

    final_state3 = await engine2.run(
        user_input=continuation_input,
        agent_config=agent_config,
        conversation_history=messages,
        extra_state={
            "streaming": False,
            "auto_approve": True,
            "interaction_mode": "auto",
        },
    )

    raw_result3 = final_state3.get("raw_result", "")
    messages3 = final_state3.get("messages", [])

    logger.info("  LLM 回复: %s", raw_result3[:300] if raw_result3 else "(空)")
    logger.info("  消息: %d → %d", len(messages), len(messages3))

    test4_pass = len(messages3) > len(messages) and bool(raw_result3)
    logger.info("✅ Step 4 通过" if test4_pass else "❌ Step 4 失败")

    # ============================================================
    # 最终结果
    # ============================================================
    logger.info("=" * 60)
    results = {
        "1_LLM调用工具": test1_pass,
        "2_任务持久化": test2_pass,
        "3_执行记录持久化": test3_pass,
        "4_管道恢复历史": test4_pass,
    }
    for name, passed in results.items():
        logger.info("  %s: %s", name, "✅ PASS" if passed else "❌ FAIL")

    all_pass = all(results.values())
    logger.info("=" * 60)
    logger.info("总体: %s", "🎉 全部通过！" if all_pass else "⚠️ 部分失败")
    return all_pass


if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result else 1)

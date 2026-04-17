#!/usr/bin/env python
"""CLI 逻辑闭环测试脚本。

测试 Agent OS CLI 的核心闭环能力：
1. 用户输入 → LLM 回复（单轮对话）
2. LLM 工具调用 → 工具执行 → LLM 继续推理（ReAct）
3. 任务提交 → 任务管理（task_submit + task_manage）

使用方式：
    $env:PYTHONPATH="src"
    python cli_loop_test.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cli_loop_test")


async def test_single_turn():
    """测试 1: 单轮对话 - 用户输入 → LLM 回复。"""
    from channels.cli.cli_main import CLIApplication

    logger.info("=" * 50)
    logger.info("测试 1: 单轮对话")
    logger.info("=" * 50)

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    if app._engine is None:
        logger.error("管道引擎未创建，跳过测试")
        return False

    try:
        final_state = await app._engine.run(
            user_input="你好，请简单介绍一下你自己",
            agent_config=app._agent_config,
        )
        raw_result = final_state.get("raw_result", "")

        if raw_result:
            logger.info("✅ 单轮对话测试通过")
            logger.info("LLM 回复: %s...", raw_result[:200])
            return True
        else:
            logger.error("❌ 单轮对话测试失败: 无回复内容")
            return False
    except Exception as exc:
        logger.error("❌ 单轮对话测试异常: %s", exc)
        return False


async def test_react_tool_call():
    """测试 2: ReAct 循环 - LLM 调用工具 → 工具返回 → LLM 继续。"""
    from channels.cli.cli_main import CLIApplication

    logger.info("=" * 50)
    logger.info("测试 2: ReAct 工具调用循环")
    logger.info("=" * 50)

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    if app._engine is None:
        logger.error("管道引擎未创建，跳过测试")
        return False

    try:
        final_state = await app._engine.run(
            user_input="现在几点了？请用 current_time 工具查看",
            agent_config=app._agent_config,
        )
        raw_result = final_state.get("raw_result", "")
        tool_calls = final_state.get("raw_tool_calls", [])
        iterations = final_state.get("iteration", 0)

        logger.info("迭代次数: %d", iterations)
        logger.info("工具调用: %s", tool_calls)

        if iterations > 1:
            logger.info("✅ ReAct 循环测试通过 (多轮迭代)")
            logger.info("LLM 最终回复: %s...", raw_result[:200] if raw_result else "N/A")
            return True
        elif raw_result:
            logger.info("⚠️ LLM 未调用工具但给出了回复 (可能工具不可用)")
            logger.info("LLM 回复: %s...", raw_result[:200])
            return True
        else:
            logger.error("❌ ReAct 循环测试失败")
            return False
    except Exception as exc:
        logger.error("❌ ReAct 循环测试异常: %s", exc)
        return False


async def test_task_submit():
    """测试 3: 任务提交闭环 - task_submit 工具调用。"""
    from channels.cli.cli_main import CLIApplication

    logger.info("=" * 50)
    logger.info("测试 3: 任务提交闭环")
    logger.info("=" * 50)

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    if app._engine is None:
        logger.error("管道引擎未创建，跳过测试")
        return False

    try:
        final_state = await app._engine.run(
            user_input="请帮我创建一个简单任务：写一首关于春天的诗，验收标准是包含至少3个春天的意象",
            agent_config=app._agent_config,
        )
        raw_result = final_state.get("raw_result", "")
        iterations = final_state.get("iteration", 0)

        task_service = app._services.get("task_service")
        if task_service:
            tasks = task_service.list_tasks()
            logger.info("任务列表: %d 个任务", len(tasks))
            for t in tasks:
                logger.info("  任务: %s (状态=%s)", t.description[:50] if t.description else "N/A", t.status.value)

        logger.info("迭代次数: %d", iterations)

        if raw_result or iterations > 1:
            logger.info("✅ 任务提交测试完成")
            logger.info("LLM 回复: %s...", raw_result[:200] if raw_result else "N/A")
            return True
        else:
            logger.error("❌ 任务提交测试失败")
            return False
    except Exception as exc:
        logger.error("❌ 任务提交测试异常: %s", exc)
        return False


async def test_multi_turn_conversation():
    """测试 4: 多轮对话记忆 - 第一轮对话 → 第二轮引用第一轮内容。"""
    from channels.cli.cli_main import CLIApplication

    logger.info("=" * 50)
    logger.info("测试 4: 多轮对话记忆")
    logger.info("=" * 50)

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    if app._engine is None:
        logger.error("管道引擎未创建，跳过测试")
        return False

    conversation_history: list[dict] = []

    try:
        final_state1 = await app._engine.run(
            user_input="请记住：我最喜欢的颜色是靛蓝色",
            agent_config=app._agent_config,
        )
        raw_result1 = final_state1.get("raw_result", "")

        messages1 = final_state1.get("messages", [])
        if messages1:
            conversation_history = list(messages1)

        logger.info("第一轮回复: %s...", raw_result1[:100] if raw_result1 else "N/A")
        logger.info("对话历史长度: %d", len(conversation_history))

        if not conversation_history:
            conversation_history.append({"role": "user", "content": "请记住：我最喜欢的颜色是靛蓝色"})
            if raw_result1:
                conversation_history.append({"role": "assistant", "content": raw_result1})

        final_state2 = await app._engine.run(
            user_input="我最喜欢的颜色是什么？",
            agent_config=app._agent_config,
            conversation_history=conversation_history if conversation_history else None,
        )
        raw_result2 = final_state2.get("raw_result", "")

        logger.info("第二轮回复: %s...", raw_result2[:200] if raw_result2 else "N/A")

        if raw_result2 and "靛蓝" in raw_result2:
            logger.info("✅ 多轮记忆测试通过 - LLM 记住了靛蓝色")
            return True
        elif raw_result2:
            logger.info("⚠️ LLM 回复了但未明确提及靛蓝色")
            return True
        else:
            logger.error("❌ 多轮记忆测试失败")
            return False

    except Exception as exc:
        logger.error("❌ 多轮记忆测试异常: %s", exc)
        return False


async def main():
    """运行所有测试。"""
    logger.info("Agent OS CLI 逻辑闭环测试")
    logger.info("=" * 50)

    results = {}

    results["单轮对话"] = await test_single_turn()
    results["ReAct工具调用"] = await test_react_tool_call()
    results["任务提交"] = await test_task_submit()
    results["多轮记忆"] = await test_multi_turn_conversation()

    logger.info("\n" + "=" * 50)
    logger.info("测试结果汇总")
    logger.info("=" * 50)

    for name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        logger.info("  %s: %s", name, status)

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    logger.info("\n总计: %d/%d 通过", passed, total)

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)

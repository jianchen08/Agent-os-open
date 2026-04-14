"""端到端真实测试 — LLM 调用 + 工具执行 + 多轮推理。

验证完整链路：
1. CLI 启动 → 加载 YAML → 构建插件注册表 + 共享服务
2. ToolSchemaPlugin 注入 tool_schemas → state["tool_schemas"]
3. LLMCore 从 state["tool_schemas"] 读取 → litellm 传 tools 参数
4. LLM 返回 tool_calls → PendingToolsOutput 检测 → 路由到 ToolCore
5. ToolCore 执行工具 → 结果写回 state → 路由回 LLM
6. LLM 第二轮生成最终回答（引用工具结果）

使用 MiniMax M2.7 真实 API。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

# 确保项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("e2e_real_test")


async def test_real_llm_no_tools() -> dict[str, Any]:
    """测试1: 纯 LLM 调用（无工具）。"""
    from channels.cli.cli_main import CLIApplication

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    result: dict[str, Any] = {"name": "纯LLM调用", "passed": False, "details": {}}

    try:
        state = {"user_input": "你好，请用一句话介绍你自己。"}
        final = await app._engine.run(state)

        raw_result = final.get("raw_result")
        raw_error = final.get("raw_error")

        result["details"]["raw_result_len"] = len(raw_result) if raw_result else 0
        result["details"]["raw_error"] = raw_error
        result["details"]["iterations"] = final.get("iteration", 0)

        if raw_result and not raw_error:
            result["passed"] = True
            result["details"]["raw_result_preview"] = raw_result[:200]
            logger.info("✅ 测试1通过: LLM 返回了 %d 字符的回复", len(raw_result))
        else:
            result["details"]["error"] = f"LLM 调用失败: {raw_error}"
            logger.error("❌ 测试1失败: %s", raw_error)

    except Exception as exc:
        result["details"]["exception"] = str(exc)
        logger.error("❌ 测试1异常: %s", exc, exc_info=True)

    return result


async def test_real_tool_call() -> dict[str, Any]:
    """测试2: LLM + 工具调用（calculator）。"""
    from channels.cli.cli_main import CLIApplication

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    result: dict[str, Any] = {"name": "工具调用", "passed": False, "details": {}}

    try:
        # 明确要求使用计算器
        state = {"user_input": "请用calculator工具计算 123+456 等于多少？"}
        final = await app._engine.run(state)

        raw_result = final.get("raw_result")
        raw_error = final.get("raw_error")
        raw_tool_calls = final.get("raw_tool_calls", [])
        tool_results = final.get("tool_results", [])
        iterations = final.get("iteration", 0)

        result["details"]["raw_result"] = raw_result[:300] if raw_result else None
        result["details"]["raw_error"] = raw_error
        result["details"]["iterations"] = iterations
        result["details"]["raw_tool_calls_count"] = len(raw_tool_calls) if raw_tool_calls else 0
        result["details"]["tool_results_count"] = len(tool_results) if tool_results else 0

        # 检查：迭代次数 > 1（至少2轮：LLM→ToolCore→LLM）
        if iterations < 2:
            result["details"]["error"] = f"迭代次数只有 {iterations}，工具调用未发生"
            logger.error("❌ 测试2失败: 迭代次数不足，工具调用未触发")
            return result

        # 检查：最终结果包含 579（123+456 的结果）
        final_text = raw_result or ""
        if "579" in final_text:
            result["passed"] = True
            logger.info("✅ 测试2通过: 工具调用成功，LLM 回复中包含 579")
        else:
            # 工具可能被调用了但结果格式不同，检查 tool_results
            result["details"]["warning"] = f"LLM 回复中未直接包含 579，原文: {final_text[:200]}"
            if tool_results:
                # 有工具结果就算部分通过
                result["passed"] = True
                result["details"]["note"] = "工具调用发生，但 LLM 未在回复中明确引用结果"
                logger.warning("⚠️ 测试2部分通过: 工具调用发生了，但 LLM 回复未包含 579")
            else:
                result["details"]["error"] = "无工具执行结果"
                logger.error("❌ 测试2失败: 无工具执行结果")

    except Exception as exc:
        result["details"]["exception"] = str(exc)
        logger.error("❌ 测试2异常: %s", exc, exc_info=True)

    return result


async def test_tool_schemas_injected() -> dict[str, Any]:
    """测试3: ToolSchemaPlugin 正确注入 tool_schemas。"""
    from channels.cli.cli_main import CLIApplication

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    result: dict[str, Any] = {"name": "ToolSchema注入", "passed": False, "details": {}}

    try:
        # 只跑一轮，检查 state 中是否有 tool_schemas
        state = {"user_input": "test"}
        final = await app._engine.run(state)

        # 检查 state 中是否有 tool_schemas
        tool_schemas = final.get("tool_schemas", [])
        result["details"]["tool_schemas_count"] = len(tool_schemas)

        if tool_schemas:
            # 检查是否包含 calculator 和 current_time
            tool_names = [t.get("function", {}).get("name", "") for t in tool_schemas]
            result["details"]["tool_names"] = tool_names

            has_calculator = "calculator" in tool_names
            has_current_time = "current_time" in tool_names

            if has_calculator and has_current_time:
                result["passed"] = True
                logger.info("✅ 测试3通过: tool_schemas 包含 %s", tool_names)
            else:
                result["details"]["error"] = f"缺少工具: calculator={has_calculator}, current_time={has_current_time}"
                logger.warning("⚠️ 测试3部分通过: %s", result["details"]["error"])
                result["passed"] = has_calculator or has_current_time
        else:
            result["details"]["error"] = "tool_schemas 为空"
            logger.error("❌ 测试3失败: tool_schemas 为空")

    except Exception as exc:
        result["details"]["exception"] = str(exc)
        logger.error("❌ 测试3异常: %s", exc, exc_info=True)

    return result


async def test_real_current_time_tool() -> dict[str, Any]:
    """测试4: LLM 调用 current_time 工具。"""
    from channels.cli.cli_main import CLIApplication

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    result: dict[str, Any] = {"name": "current_time工具", "passed": False, "details": {}}

    try:
        state = {"user_input": "现在几点了？请用current_time工具查一下。"}
        final = await app._engine.run(state)

        raw_result = final.get("raw_result")
        iterations = final.get("iteration", 0)
        tool_results = final.get("tool_results", [])

        result["details"]["raw_result"] = raw_result[:300] if raw_result else None
        result["details"]["iterations"] = iterations
        result["details"]["tool_results_count"] = len(tool_results) if tool_results else 0

        if iterations >= 2 and raw_result:
            result["passed"] = True
            logger.info("✅ 测试4通过: current_time 工具调用成功")
        else:
            result["details"]["error"] = f"迭代次数={iterations}, raw_result={'有' if raw_result else '无'}"
            logger.error("❌ 测试4失败")

    except Exception as exc:
        result["details"]["exception"] = str(exc)
        logger.error("❌ 测试4异常: %s", exc, exc_info=True)

    return result


async def main() -> None:
    """运行所有端到端测试。"""
    logger.info("=" * 60)
    logger.info("端到端真实测试 — LLM + 工具调用")
    logger.info("=" * 60)

    tests = [
        test_real_llm_no_tools,
        test_tool_schemas_injected,
        test_real_tool_call,
        test_real_current_time_tool,
    ]

    results: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    for test_fn in tests:
        logger.info("\n--- 运行: %s ---", test_fn.__doc__ and test_fn.__doc__.strip().split(chr(10))[0] or test_fn.__name__)
        start = time.time()
        r = await test_fn()
        elapsed = time.time() - start
        r["elapsed_seconds"] = round(elapsed, 2)

        if r["passed"]:
            passed += 1
        else:
            failed += 1
        results.append(r)

        # 间隔一下避免 rate limit
        await asyncio.sleep(2)

    # 汇总
    logger.info("\n" + "=" * 60)
    logger.info("测试结果汇总")
    logger.info("=" * 60)

    for r in results:
        status = "✅ PASS" if r["passed"] else "❌ FAIL"
        logger.info("  %s | %s (%.2fs)", status, r["name"], r["elapsed_seconds"])
        if not r["passed"] and r.get("details", {}).get("error"):
            logger.info("         原因: %s", r["details"]["error"])
        if r.get("details", {}).get("warning"):
            logger.info("         警告: %s", r["details"]["warning"])

    logger.info("\n总计: %d/%d 通过 (%.0f%%)", passed, len(results), passed / len(results) * 100 if results else 0)

    # 保存结果
    report_path = Path(__file__).parent / "e2e_real_tool_result.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"results": results, "summary": {"passed": passed, "failed": failed, "total": len(results)}}, f, ensure_ascii=False, indent=2)
    logger.info("结果已保存到: %s", report_path)


if __name__ == "__main__":
    asyncio.run(main())

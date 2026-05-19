"""快速端到端真实测试 — 只测一次工具调用。"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("quick_test")


async def main() -> None:
    from channels.cli.cli_main import CLIApplication

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    # 测试：让 LLM 用 calculator 计算
    state = {"user_input": "请用calculator工具计算 123+456 等于多少？只计算，不要废话。"}
    logger.info("=== 开始测试: 工具调用 ===")
    final = await app._engine.run(
        user_input=state["user_input"],
        agent_config=app._agent_config,
    )

    # 输出关键信息
    logger.info("=== 结果 ===")
    logger.info("iterations: %s", final.get("iteration"))
    logger.info("core_type: %s", final.get("core_type"))
    logger.info("raw_result: %s", (final.get("raw_result") or "")[:300])
    logger.info("raw_error: %s", final.get("raw_error"))
    logger.info("raw_tool_calls: %s", final.get("raw_tool_calls"))
    logger.info("tool_results: %s", json.dumps(final.get("tool_results", []), ensure_ascii=False, default=str)[:500])
    logger.info("ended: %s", final.get("ended"))

    # 检查
    iterations = final.get("iteration", 0)
    tool_results = final.get("tool_results", [])
    raw_result = final.get("raw_result", "")

    if iterations >= 2 and tool_results:
        # 检查工具是否成功执行
        success_count = sum(1 for r in tool_results if r.get("success"))
        logger.info("✅ 工具调用成功: %d/%d 工具执行成功", success_count, len(tool_results))
        if "579" in raw_result or any("579" in str(r.get("data", "")) for r in tool_results):
            logger.info("✅ 计算结果正确: 579")
        else:
            logger.warning("⚠️ 计算结果未包含 579，但工具调用已发生")
    else:
        logger.error("❌ 工具调用失败: iterations=%d, tool_results=%d", iterations, len(tool_results))


if __name__ == "__main__":
    asyncio.run(main())

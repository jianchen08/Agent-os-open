#!/usr/bin/env python
"""快速验证真实 LLM 模式是否正常工作。"""
import asyncio
from channels.cli.cli_main import CLIApplication


async def test():
    app = CLIApplication()
    app.setup_pipeline()
    print("Pipeline ready")

    result = await app._engine.run({"user_input": "你好，请用一句话回复", "iteration": 1})
    raw = result.get("raw_result", "")
    ended = result.get("ended", False)
    iters = result.get("iteration", 0)
    print(f"Ended: {ended}, Iterations: {iters}")
    print(f"LLM response: {raw[:300] if raw else 'EMPTY'}")


if __name__ == "__main__":
    asyncio.run(test())

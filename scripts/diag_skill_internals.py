"""探针2：精确定位 simple 模式为何也返回空。

execute() 有向量检索前置（168-196行）：如果搜索引擎存在且 query 非空，
会先走 _search_with_engine，命中则提前 return，否则继续遍历。
搜索引擎是否被注入是关键。逐层打点。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tools.builtin.resource_search.tool import ResourceSearchTool


async def main():
    tool = ResourceSearchTool(tool_registry=None)

    print("[A] execute() 前搜索引擎状态:")
    se = tool._get_search_engine()
    print(f"    _search_engine = {tool._search_engine}")
    print(f"    _get_search_engine() = {se}")
    print(f"    → {'有引擎，会走向量检索前置' if se else '无引擎，应走遍历'}")

    print("\n[B] 直接调 _search_skills 各模式：")
    for mode in ("simple", "detailed"):
        detailed = mode == "detailed"
        exact = detailed
        for q in ("代码实现", "code-implement"):
            n, d_, det = await tool._search_skills(q, None, 20, detailed, exact)
            print(f"    mode={mode:8} exact={exact!s:5} query={q!r:18} -> {n}")

    print("\n[C] 完整 execute() simple 模式原始返回：")
    res = await tool.execute({
        "resource_type": "skill", "query": "代码实现", "mode": "simple",
        "session_id": "x", "parent_record_id": "",
    })
    print(f"    {res.to_dict()}")


if __name__ == "__main__":
    asyncio.run(main())

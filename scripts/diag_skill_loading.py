"""修复后回归测试：真实 ResourceSearchTool.execute() 验证技能加载。

覆盖矩阵：4 个技能 × {中文名, 英文目录名} × {simple, detailed}。
全部应命中；detailed 应额外返回 skill_content。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tools.builtin.resource_search.tool import ResourceSearchTool

# 中文名 ↔ 英文目录名 对照
SKILLS = [
    ("代码实现", "code-implement"),
    ("前端编码", "code-frontend"),
    ("后端编码", "code-backend"),
    ("代码调试", "code-debug"),
]


async def call(tool, resource_type, query, mode):
    res = await tool.execute({
        "resource_type": resource_type, "query": query, "mode": mode,
        "session_id": "regress", "parent_record_id": "",
    })
    out = res.to_dict().get("output", {}) or {}
    return out


async def main():
    tool = ResourceSearchTool(tool_registry=None)
    print("=" * 70)
    print("修复后回归：真实 resource_search 加载技能（中英文名 × simple/detailed）")
    print("=" * 70)

    all_pass = True
    print("\n[A] detailed 模式（agent 加载技能用的模式）")
    for cn, en in SKILLS:
        for q, lang in ((cn, "中文"), (en, "英文")):
            out = await call(tool, "skill", q, "detailed")
            rows = out.get("skill_d", [])
            hit = bool(rows)
            content_len = len(str(rows[0][2])) if hit and len(rows[0]) >= 3 else 0
            flag = "✅" if hit and content_len > 0 else "❌"
            if not (hit and content_len > 0):
                all_pass = False
            print(f"  {flag} detailed query={q!r:14}[{lang}] → {'skill_content '+str(content_len)+'字符' if hit else '空'}")

    print("\n[B] simple 模式（对照）")
    for cn, en in SKILLS:
        for q, lang in ((cn, "中文"), (en, "英文")):
            out = await call(tool, "skill", q, "simple")
            rows = out.get("skill_d", [])
            flag = "✅" if rows else "❌"
            if not rows:
                all_pass = False
            print(f"  {flag} simple   query={q!r:14}[{lang}] → {rows[0][0] if rows else '空'}")

    print("\n[C] 负向：不存在的技能应返回空（防误命中）")
    for q in ("不存在的技能", "nonexistent-skill"):
        out = await call(tool, "skill", q, "detailed")
        flag = "✅" if not out.get("skill_d") else "❌误命中"
        if out.get("skill_d"):
            all_pass = False
        print(f"  {flag} detailed query={q!r:22} → {'空' if not out.get('skill_d') else out.get('skill_d')}")

    print("\n[D] 工具 detailed 不受影响（回归保护）")
    out = await call(tool, "tool", "yaml_validate", "detailed")
    flag = "✅" if out.get("tool_d") else "❌"
    if not out.get("tool_d"):
        all_pass = False
    print(f"  {flag} tool detailed yaml_validate → {'OK' if out.get('tool_d') else '空'}")

    print("\n" + "=" * 70)
    print("总体：" + ("✅ 全部通过" if all_pass else "❌ 有失败项"))
    print("=" * 70)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

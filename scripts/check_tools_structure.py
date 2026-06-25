"""检查每轮的工具列表和 MSG 结构是否一致。"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_TOOL_SCHEMAS = re.compile(r"\[llm_core\] tool_schemas count=(\d+)\s*\|\s*(.*)")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")
RE_USAGE = re.compile(r"'input_tokens':\s*(\d+).*?'cached_tokens':\s*(\d+)")
RE_MSG = re.compile(r"\[llm_core\] MSG-(\d+)\s+role=(\w+)(?:\s+name=(\w+))?")
RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_SYSTEM_MSG = re.compile(r"\[prompt_build\] SystemMessage built \| content_len=(\d+) \| dynamic_vars=(\w+)")


def parse_all():
    iters = {}
    cur_tools = None
    cur_msg_roles = []
    cur_msg_count = 0
    cur_system_len = 0
    cur_has_dynamic = False
    sending = False

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")

            m = RE_SYSTEM_MSG.search(line)
            if m:
                cur_system_len = int(m.group(1))
                cur_has_dynamic = m.group(2) == "True"
                continue

            m = RE_TOOL_SCHEMAS.search(line)
            if m:
                cur_tools = {"count": int(m.group(1)), "names": m.group(2).strip()}
                continue

            m = RE_SENDING.search(line)
            if m:
                cur_msg_count = int(m.group(1))
                cur_msg_roles = []
                sending = True
                continue

            if sending:
                m2 = RE_MSG.search(line)
                if m2:
                    cur_msg_roles.append({
                        "idx": int(m2.group(1)),
                        "role": m2.group(2),
                        "name": m2.group(3) or "",
                    })
                    continue

            m3 = RE_ITER.search(line)
            if m3:
                it = int(m3.group(1))
                iters[it] = {
                    "tools": cur_tools,
                    "msg_count": cur_msg_count,
                    "msg_roles": list(cur_msg_roles),
                    "system_len": cur_system_len,
                    "has_dynamic": cur_has_dynamic,
                }
                sending = False
                continue

            m4 = RE_USAGE.search(line)
            if m4:
                last = max(iters.keys()) if iters else None
                if last is not None:
                    iters[last]["input"] = int(m4.group(1))
                    iters[last]["cached"] = int(m4.group(2))

    return iters


def main():
    data = parse_all()
    print(f"共 {len(data)} 个迭代\n")

    sorted_iters = sorted(data.keys())

    # 检查工具列表
    print("=" * 120)
    print("工具列表对比")
    print("=" * 120)
    prev_tools = None
    for it in sorted_iters:
        d = data[it]
        tools = d.get("tools")
        if tools:
            if prev_tools and tools["names"] != prev_tools["names"]:
                print(f"  Iter {it}: 工具列表变化! count={tools['count']}")
                print(f"    之前: {prev_tools['names']}")
                print(f"    现在: {tools['names']}")
            prev_tools = tools

    print(f"\n  所有轮次工具数量: {', '.join(str(data[it].get('tools',{}).get('count','?')) for it in sorted_iters[:10])}")

    # 检查 MSG 结构
    print("\n" + "=" * 120)
    print("每轮 MSG 结构摘要（重点关注 dynamic_context 位置）")
    print("=" * 120)

    for it in sorted_iters:
        d = data[it]
        roles = d.get("msg_roles", [])
        inp = d.get("input", 0)
        cached = d.get("cached", 0)
        hit = cached / inp * 100 if inp else 0
        mc = d.get("msg_count", 0)
        sys_len = d.get("system_len", 0)
        has_dyn = d.get("has_dynamic", False)

        # 找 dynamic_context 的位置
        dyn_positions = [r["idx"] for r in roles if r["name"] == "dynamic_context"]
        last_role = roles[-1] if roles else {}

        marker = "⚠️" if hit < 50 else ("✅" if hit > 90 else "")

        print(f"  Iter {it:>2}: {mc}msgs, 命中={hit:>5.1f}%, sys_len={sys_len:,}, dyn={has_dyn}, "
              f"dynamic_context@{dyn_positions}, 最后一条=MSG-{last_role.get('idx','?')} role={last_role.get('role','?')}{' name='+last_role.get('name','') if last_role.get('name') else ''} {marker}")

    # 重点：检查 MSG-0 是否包含 dynamic_vars
    print("\n" + "=" * 120)
    print("关键问题：MSG-0 是否合并了 dynamic_vars？")
    print("=" * 120)

    for it in sorted_iters[:5]:
        d = data[it]
        roles = d.get("msg_roles", [])
        sys_len = d.get("system_len", 0)
        has_dyn = d.get("has_dynamic", False)

        msg0 = roles[0] if roles else {}
        dyn_msgs = [r for r in roles if r["name"] == "dynamic_context"]

        print(f"  Iter {it}: MSG-0 role={msg0.get('role','?')} name={msg0.get('name','')}, "
              f"system_len={sys_len:,}, has_dynamic_vars={has_dyn}, "
              f"独立dynamic_context消息数={len(dyn_msgs)}")

        # 如果 MSG-0 是 system 且 system_len 很大，说明 dynamic_vars 可能被合并了
        # 如果 MSG-0 是 system 但 system_len 很小，说明 dynamic_vars 没被合并
        if msg0.get("role") == "system" and msg0.get("name") == "":
            if has_dyn and len(dyn_msgs) > 0:
                print(f"    ⚠️ MSG-0 是 system 但 dynamic_vars 也作为独立消息存在！合并可能没生效")


if __name__ == "__main__":
    main()

"""分析 pipeline_1064abdb1e0b.log 的缓存命中率和 MSG 差异。"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_1064abdb1e0b.log"

RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_MSG = re.compile(r"\[llm_core\] MSG-(\d+)\s+(.*)")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")
RE_USAGE = re.compile(r"'input_tokens':\s*(\d+).*?'cached_tokens':\s*(\d+)")
RE_TIMESTAMP = re.compile(r"^(\d{2}:\d{2}:\d{2})")


def parse_all():
    iters = {}
    cur_msg_count = 0
    cur_time = ""
    cur_msgs = []
    cur_msg_idx = None
    cur_buf = []
    sending = False

    def flush():
        nonlocal cur_msg_idx, cur_buf
        if cur_msg_idx is not None:
            cur_msgs.append((cur_msg_idx, "\n".join(cur_buf)))
        cur_msg_idx = None
        cur_buf = []

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            tm = RE_TIMESTAMP.match(line)
            if tm:
                cur_time = tm.group(1)
            m = RE_SENDING.search(line)
            if m:
                flush()
                cur_msgs = []
                cur_msg_count = int(m.group(1))
                sending = True
                continue
            if sending:
                m2 = RE_MSG.search(line)
                if m2:
                    flush()
                    cur_msg_idx = int(m2.group(1))
                    cur_buf = [m2.group(2)]
                    continue
                if cur_msg_idx is not None:
                    if line.strip() and not re.match(r"\d{2}:\d{2}:\d{2}.*\[llm_core\]", line):
                        cur_buf.append(line)
                    else:
                        flush()
            m3 = RE_ITER.search(line)
            if m3:
                flush()
                it = int(m3.group(1))
                iters[it] = {
                    "msg_count": cur_msg_count,
                    "time": cur_time,
                    "msgs": list(cur_msgs),
                }
                cur_msgs = []
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

    print(f"{'Iter':>4} | {'时间':>8} | {'消息数':>4} | {'输入tokens':>12} | {'缓存tokens':>12} | {'命中率':>6} | {'未命中':>10} | 标记")
    print("-" * 110)

    low_iters = []
    for it in sorted_iters:
        d = data[it]
        inp = d.get("input", 0)
        cached = d.get("cached", 0)
        hit = cached / inp * 100 if inp else 0
        missed = inp - cached
        mc = d.get("msg_count", 0)
        t = d.get("time", "?")

        marker = ""
        if hit < 50:
            marker = "⚠️ 低命中"
            low_iters.append(it)
        elif hit > 90:
            marker = "✅ 高命中"

        print(f"{it:>4} | {t:>8} | {mc:>4} | {inp:>12,} | {cached:>12,} | {hit:>5.1f}% | {missed:>10,} | {marker}")

    if not low_iters:
        print("\n没有低命中率轮次！")
        return

    print(f"\n{'='*120}")
    print(f"低命中率轮次 ({len(low_iters)} 个) 与前一轮 MSG 对比：")
    print(f"{'='*120}")

    for it in low_iters:
        idx = sorted_iters.index(it)
        if idx == 0:
            print(f"\n  Iter {it}: 无前一轮")
            continue

        prev_it = sorted_iters[idx - 1]
        a = data[prev_it]
        b = data[it]
        a_msgs = a.get("msgs", [])
        b_msgs = b.get("msgs", [])
        a_hit = a.get("cached", 0) / a.get("input", 1) * 100
        b_hit = b.get("cached", 0) / b.get("input", 1) * 100

        print(f"\n  Iter {prev_it} (命中={a_hit:.1f}%, {len(a_msgs)}msgs) → Iter {it} (命中={b_hit:.1f}%, {len(b_msgs)}msgs)")

        min_len = min(len(a_msgs), len(b_msgs))
        first_diff = None
        for i in range(min_len):
            a_idx, a_content = a_msgs[i]
            b_idx, b_content = b_msgs[i]
            if a_idx != b_idx or a_content != b_content:
                if first_diff is None:
                    first_diff = i
                a_role_m = re.match(r'role=(\w+)', a_content)
                a_role = a_role_m.group(1) if a_role_m else "?"
                a_name_m = re.search(r'name=(\w+)', a_content)
                a_name = a_name_m.group(1) if a_name_m else ""
                b_role_m = re.match(r'role=(\w+)', b_content)
                b_role = b_role_m.group(1) if b_role_m else "?"
                b_name_m = re.search(r'name=(\w+)', b_content)
                b_name = b_name_m.group(1) if b_name_m else ""
                print(f"    [{i}] ✗ A: MSG-{a_idx} role={a_role}{' name='+a_name if a_name else ''} len={len(a_content)}")
                print(f"           {a_content[:120].replace(chr(10),'\\n')}")
                print(f"         ✗ B: MSG-{b_idx} role={b_role}{' name='+b_name if b_name else ''} len={len(b_content)}")
                print(f"           {b_content[:120].replace(chr(10),'\\n')}")

        if len(b_msgs) > min_len:
            for i in range(min_len, len(b_msgs)):
                idx2, content = b_msgs[i]
                role_m = re.match(r'role=(\w+)', content)
                role = role_m.group(1) if role_m else "?"
                name_m = re.search(r'name=(\w+)', content)
                name = name_m.group(1) if name_m else ""
                print(f"    [{i}] B多出: MSG-{idx2} role={role}{' name='+name if name else ''} len={len(content)}")
                print(f"           {content[:120].replace(chr(10),'\\n')}")

        if first_diff is not None:
            print(f"    *** 首个差异在 index {first_diff}，前 {first_diff} 条完全相同 ***")
        else:
            print(f"    前 {min_len} 条完全相同")


if __name__ == "__main__":
    main()

"""对比低命中轮次与其前一轮（高命中）的 MSG 差异。"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_MSG = re.compile(r"\[llm_core\] MSG-(\d+)\s+(.*)")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")
RE_USAGE = re.compile(r"'input_tokens':\s*(\d+).*?'cached_tokens':\s*(\d+)")


def parse_iters():
    iters = {}
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
            m = RE_SENDING.search(line)
            if m:
                flush()
                cur_msgs = []
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
                iters[it] = {"msgs": list(cur_msgs)}
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


def get_role_summary(content):
    """提取 role 和简短摘要。"""
    role_m = re.match(r'role=(\w+)', content)
    role = role_m.group(1) if role_m else "?"
    name_m = re.search(r'name=(\w+)', content)
    name = name_m.group(1) if name_m else ""
    preview = content[:120].replace("\n", "\\n")
    return role, name, preview


def compare(a_it, b_it, data):
    """对比两轮，找出首个差异。"""
    a = data.get(a_it, {})
    b = data.get(b_it, {})
    a_msgs = a.get("msgs", [])
    b_msgs = b.get("msgs", [])
    a_hit = a.get("cached", 0) / a.get("input", 1) * 100
    b_hit = b.get("cached", 0) / b.get("input", 1) * 100

    print(f"\n{'='*120}")
    print(f"Iter {a_it} (命中={a_hit:.1f}%, {a.get('input',0):,}in/{a.get('cached',0):,}cache, {len(a_msgs)}msgs)")
    print(f"Iter {b_it} (命中={b_hit:.1f}%, {b.get('input',0):,}in/{b.get('cached',0):,}cache, {len(b_msgs)}msgs)")
    print(f"{'='*120}")

    min_len = min(len(a_msgs), len(b_msgs))
    first_diff = None

    for i in range(min_len):
        a_idx, a_content = a_msgs[i]
        b_idx, b_content = b_msgs[i]
        if a_idx != b_idx or a_content != b_content:
            if first_diff is None:
                first_diff = i
            a_role, a_name, a_prev = get_role_summary(a_content)
            b_role, b_name, b_prev = get_role_summary(b_content)
            print(f"  [{i}] ✗ A: MSG-{a_idx} role={a_role}{' name='+a_name if a_name else ''} len={len(a_content)}")
            print(f"         {a_prev}")
            print(f"       ✗ B: MSG-{b_idx} role={b_role}{' name='+b_name if b_name else ''} len={len(b_content)}")
            print(f"         {b_prev}")

    if len(a_msgs) > min_len:
        for i in range(min_len, len(a_msgs)):
            idx, content = a_msgs[i]
            role, name, prev = get_role_summary(content)
            print(f"  [{i}] A多出: MSG-{idx} role={role}{' name='+name if name else ''} len={len(content)}")
            print(f"         {prev}")
    if len(b_msgs) > min_len:
        for i in range(min_len, len(b_msgs)):
            idx, content = b_msgs[i]
            role, name, prev = get_role_summary(content)
            print(f"  [{i}] B多出: MSG-{idx} role={role}{' name='+name if name else ''} len={len(content)}")
            print(f"         {prev}")

    if first_diff is not None:
        print(f"\n  *** 首个差异在 index {first_diff}，前 {first_diff} 条完全相同 ***")
    else:
        print(f"\n  前 {min_len} 条完全相同")


def main():
    data = parse_iters()
    print(f"解析到 {len(data)} 个迭代")

    # 低命中 vs 前一轮（高命中）
    pairs = [
        (4, 6),    # 高→低
        (28, 30),  # 高→低
        (30, 31),  # 低→低
        (31, 32),  # 低→低
        (33, 35),  # 高→低
        (68, 70),  # 高→低
        (71, 73),  # 高→低
    ]

    for a, b in pairs:
        if a in data and b in data:
            compare(a, b, data)


if __name__ == "__main__":
    main()

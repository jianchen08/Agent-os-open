"""逐条对比两轮迭代的 MSG 消息内容，找出首个差异位置。"""

import re
import sys

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_MSG = re.compile(r"\[llm_core\] MSG-(\d+)\s+(.*)")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")
RE_USAGE = re.compile(r"'input_tokens':\s*(\d+).*?'cached_tokens':\s*(\d+)")


def parse_iters():
    """解析日志，提取每轮的完整 MSG 列表。"""
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


def compare(a_it, b_it, data):
    """逐条对比两轮 MSG，输出首个差异。"""
    a = data.get(a_it, {})
    b = data.get(b_it, {})
    a_msgs = a.get("msgs", [])
    b_msgs = b.get("msgs", [])

    a_hit = a.get("cached", 0) / a.get("input", 1) * 100
    b_hit = b.get("cached", 0) / b.get("input", 1) * 100

    print(f"\n{'='*120}")
    print(f"Iter {a_it}: 命中率={a_hit:.1f}% 输入={a.get('input',0):,} 缓存={a.get('cached',0):,} 消息数={len(a_msgs)}")
    print(f"Iter {b_it}: 命中率={b_hit:.1f}% 输入={b.get('input',0):,} 缓存={b.get('cached',0):,} 消息数={len(b_msgs)}")
    print(f"{'='*120}")

    min_len = min(len(a_msgs), len(b_msgs))
    first_diff = None

    for i in range(min_len):
        a_idx, a_content = a_msgs[i]
        b_idx, b_content = b_msgs[i]

        if a_idx != b_idx or a_content != b_content:
            if first_diff is None:
                first_diff = i
            print(f"\n  [{i}] ✗ 差异!")
            print(f"    A: MSG-{a_idx} | B: MSG-{b_idx}")
            if a_content != b_content:
                for ci in range(min(len(a_content), len(b_content))):
                    if a_content[ci] != b_content[ci]:
                        ctx = 80
                        s = max(0, ci - ctx)
                        e = min(max(len(a_content), len(b_content)), ci + ctx)
                        print(f"    首个字符差异 @ pos {ci}:")
                        print(f"    A[{s}:{e}]: {a_content[s:e]!r}")
                        print(f"    B[{s}:{e}]: {b_content[s:e]!r}")
                        break
                else:
                    if len(a_content) != len(b_content):
                        print(f"    前缀相同，长度不同: A={len(a_content)} B={len(b_content)}")
            continue

    if len(a_msgs) > min_len:
        for i in range(min_len, len(a_msgs)):
            idx, content = a_msgs[i]
            print(f"\n  [{i}] A 多出: MSG-{idx} content={content[:150]!r}")
    if len(b_msgs) > min_len:
        for i in range(min_len, len(b_msgs)):
            idx, content = b_msgs[i]
            print(f"\n  [{i}] B 多出: MSG-{idx} content={content[:150]!r}")

    if first_diff is not None:
        print(f"\n  *** 首个差异在 index {first_diff}，前 {first_diff} 条完全相同 ***")
    else:
        print(f"\n  前 {min_len} 条完全相同")


def main():
    data = parse_iters()
    print(f"解析到 {len(data)} 个迭代")

    pairs = [(6, 7), (30, 31), (35, 36), (69, 70)]
    for a, b in pairs:
        if a in data and b in data:
            compare(a, b, data)


if __name__ == "__main__":
    main()

"""逐条对比高命中和低命中迭代轮次的 MSG 消息序列。

正确解析逻辑：MSG 行出现在 "LLM returned" 之前，
所以需要缓冲 MSG 行，等看到 "LLM returned" 时再分配给对应迭代。
"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_MSG_START = re.compile(r"\[llm_core\] MSG-(\d+)\s+role=(\w+)(.*)")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")
RE_USAGE = re.compile(r"'input_tokens':\s*(\d+).*?'cached_tokens':\s*(\d+)")


def parse_all_iters() -> dict:
    """解析日志，正确提取每轮的消息序列。"""
    iter_data = {}
    pending_msgs = []
    pending_msg_count = 0
    current_msg_idx = None
    current_msg_role = None
    current_msg_lines = None

    def flush_msg():
        nonlocal current_msg_idx, current_msg_role, current_msg_lines
        if current_msg_idx is not None:
            content = "\n".join(current_msg_lines) if current_msg_lines else ""
            pending_msgs.append({
                "idx": current_msg_idx,
                "role": current_msg_role,
                "content": content,
            })
        current_msg_idx = None
        current_msg_role = None
        current_msg_lines = None

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")

            m = RE_SENDING.search(line)
            if m:
                flush_msg()
                pending_msgs = []
                pending_msg_count = int(m.group(1))
                continue

            m = RE_MSG_START.search(line)
            if m:
                flush_msg()
                current_msg_idx = int(m.group(1))
                current_msg_role = m.group(2)
                first_line = m.group(3).strip()
                if first_line.startswith("content="):
                    first_line = first_line[len("content="):]
                elif first_line.startswith("tool_calls="):
                    first_line = first_line[len("tool_calls="):]
                elif first_line.startswith("name="):
                    name_part = first_line
                    rest = ""
                    content_match = re.search(r"\s+content=(.*)", first_line)
                    if content_match:
                        name_part = first_line[:content_match.start()]
                        rest = content_match.group(1)
                    first_line = rest
                current_msg_lines = [first_line] if first_line else []
                continue

            if current_msg_idx is not None:
                if line.strip() and not re.match(r"\d{2}:\d{2}:\d{2}", line.strip()):
                    current_msg_lines.append(line.rstrip())
                else:
                    flush_msg()

            m = RE_ITER.search(line)
            if m:
                flush_msg()
                it = int(m.group(1))
                iter_data[it] = {
                    "msgs": list(pending_msgs),
                    "msg_count": pending_msg_count,
                }
                pending_msgs = []
                continue

            m = RE_USAGE.search(line)
            if m:
                last_iter = max(iter_data.keys()) if iter_data else None
                if last_iter is not None:
                    iter_data[last_iter]["input_tokens"] = int(m.group(1))
                    iter_data[last_iter]["cached_tokens"] = int(m.group(2))

    return iter_data


def compare_iters(iter_data: dict, it_a: int, it_b: int) -> None:
    """逐条对比两个迭代的消息序列，找出第一个差异。"""
    a = iter_data.get(it_a, {})
    b = iter_data.get(it_b, {})

    a_msgs = a.get("msgs", [])
    b_msgs = b.get("msgs", [])

    a_input = a.get("input_tokens", 0)
    a_cached = a.get("cached_tokens", 0)
    b_input = b.get("input_tokens", 0)
    b_cached = b.get("cached_tokens", 0)
    a_hit = a_cached / a_input * 100 if a_input else 0
    b_hit = b_cached / b_input * 100 if b_input else 0

    print(f"\n{'='*130}")
    print(f"Iter {it_a}: 命中率={a_hit:.1f}%, 输入={a_input:,}, 缓存={a_cached:,}, 消息数={len(a_msgs)}")
    print(f"Iter {it_b}: 命中率={b_hit:.1f}%, 输入={b_input:,}, 缓存={b_cached:,}, 消息数={len(b_msgs)}")
    print(f"缓存差: {b_cached - a_cached:+,} tokens")
    print(f"{'='*130}")

    min_len = min(len(a_msgs), len(b_msgs))
    first_diff = None

    for i in range(min_len):
        am = a_msgs[i]
        bm = b_msgs[i]

        if am["idx"] != bm["idx"] or am["role"] != bm["role"] or am["content"] != bm["content"]:
            if first_diff is None:
                first_diff = i
            print(f"  [{i:>3}] ✗ 差异! A: MSG-{am['idx']} role={am['role']} len={len(am['content'])} | B: MSG-{bm['idx']} role={bm['role']} len={len(bm['content'])}")
            a_preview = am["content"][:200].replace("\n", "\\n")
            b_preview = bm["content"][:200].replace("\n", "\\n")
            print(f"         A: {a_preview}")
            print(f"         B: {b_preview}")
        else:
            pass

    if len(a_msgs) != len(b_msgs):
        print(f"\n  消息数差异: A={len(a_msgs)}, B={len(b_msgs)}")
        if len(a_msgs) > min_len:
            for i in range(min_len, len(a_msgs)):
                print(f"  [{i:>3}] A 多出: MSG-{a_msgs[i]['idx']} role={a_msgs[i]['role']} len={len(a_msgs[i]['content'])}")
        if len(b_msgs) > min_len:
            for i in range(min_len, len(b_msgs)):
                print(f"  [{i:>3}] B 多出: MSG-{b_msgs[i]['idx']} role={b_msgs[i]['role']} len={len(b_msgs[i]['content'])}")

    if first_diff is not None:
        print(f"\n  *** 首个差异在 MSG index {first_diff} ***")
        print(f"  前 {first_diff} 条消息完全相同")
        print(f"  这意味着前 {first_diff} 条消息的前缀应该被缓存")
        am = a_msgs[first_diff]
        bm = b_msgs[first_diff]

        if am["role"] == bm["role"] and am["idx"] == bm["idx"]:
            min_c = min(len(am["content"]), len(bm["content"]))
            for ci in range(min_c):
                if am["content"][ci] != bm["content"][ci]:
                    ctx_s = max(0, ci - 50)
                    ctx_e = min(len(am["content"]), ci + 50)
                    print(f"  内容首个字符差异 @ pos {ci}:")
                    print(f"    A: ...{am['content'][ctx_s:ctx_e]}...")
                    print(f"    B: ...{bm['content'][ctx_s:ctx_e]}...")
                    break
            else:
                if len(am["content"]) != len(bm["content"]):
                    print(f"  前缀相同但长度不同: A={len(am['content'])}, B={len(bm['content'])}")
    else:
        print(f"\n  前 {min_len} 条消息完全相同 ✓")


def main() -> None:
    """主函数。"""
    iter_data = parse_all_iters()

    print(f"共解析 {len(iter_data)} 个迭代轮次")
    for k in sorted(iter_data.keys())[:5]:
        v = iter_data[k]
        hit = v.get("cached_tokens", 0) / v.get("input_tokens", 1) * 100
        print(f"  iter {k}: {len(v.get('msgs',[]))} 条MSG, 命中率={hit:.1f}%")

    pairs = [
        (6, 7),
        (30, 31),
        (35, 36),
        (70, 71),
    ]

    for it_a, it_b in pairs:
        compare_iters(iter_data, it_a, it_b)


if __name__ == "__main__":
    main()

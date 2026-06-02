"""验证 MSG-0 (system_message) 的内容是否每轮不同。

如果 _build_messages 把 dynamic_vars 合并到 system_message 中，
而 dynamic_vars 包含时间戳，那 MSG-0 每轮都不同，
导致整个前缀匹配失败。
"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_MSG_START = re.compile(r"\[llm_core\] MSG-(\d+)\s+role=(\w+)(.*)")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")
RE_USAGE = re.compile(r"'input_tokens':\s*(\d+).*?'cached_tokens':\s*(\d+)")


def parse_msg0_per_iter() -> dict:
    """提取每轮的 MSG-0 内容。"""
    iter_data = {}
    pending_msg0 = None
    current_msg_idx = None
    current_msg_role = None
    current_msg_lines = None
    pending_msg_count = 0

    def flush_msg():
        nonlocal current_msg_idx, current_msg_role, current_msg_lines, pending_msg0
        if current_msg_idx is not None:
            content = "\n".join(current_msg_lines) if current_msg_lines else ""
            if current_msg_idx == 0:
                pending_msg0 = {
                    "idx": 0,
                    "role": current_msg_role,
                    "content": content,
                    "len": len(content),
                }

        current_msg_idx = None
        current_msg_role = None
        current_msg_lines = None

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")

            m = RE_SENDING.search(line)
            if m:
                flush_msg()
                pending_msg0 = None
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
                    content_match = re.search(r"\s+content=(.*)", first_line)
                    if content_match:
                        first_line = content_match.group(1)
                    else:
                        first_line = ""
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
                    "msg0": pending_msg0,
                    "msg_count": pending_msg_count,
                }
                pending_msg0 = None
                continue

            m = RE_USAGE.search(line)
            if m:
                last_iter = max(iter_data.keys()) if iter_data else None
                if last_iter is not None:
                    iter_data[last_iter]["input_tokens"] = int(m.group(1))
                    iter_data[last_iter]["cached_tokens"] = int(m.group(2))

    return iter_data


def main() -> None:
    """主函数。"""
    iter_data = parse_msg0_per_iter()

    print("每轮 MSG-0 (system_message) 的内容长度和缓存命中率：")
    print(f"{'Iter':>4} | {'MSG-0 len':>10} | {'Input':>10} | {'Cached':>10} | {'Hit%':>6} | {'MSG-0 末尾100字符'}")
    print("-" * 130)

    for it in sorted(iter_data.keys()):
        d = iter_data[it]
        msg0 = d.get("msg0")
        if not msg0:
            continue
        input_t = d.get("input_tokens", 0)
        cached_t = d.get("cached_tokens", 0)
        hit = cached_t / input_t * 100 if input_t else 0
        content = msg0["content"]
        tail = content[-100:].replace("\n", "\\n") if len(content) > 100 else content.replace("\n", "\\n")
        marker = " ⚠️" if hit < 50 else ""
        print(f"{it:>4} | {msg0['len']:>10,} | {input_t:>10,} | {cached_t:>10,} | {hit:>5.1f}% | ...{tail}{marker}")

    print("\n" + "=" * 130)
    print("对比相邻轮次的 MSG-0 长度差异")
    print("=" * 130)

    sorted_iters = sorted(iter_data.keys())
    for i in range(1, len(sorted_iters)):
        prev_it = sorted_iters[i - 1]
        curr_it = sorted_iters[i]
        prev_m0 = iter_data[prev_it].get("msg0")
        curr_m0 = iter_data[curr_it].get("msg0")
        if prev_m0 and curr_m0:
            len_diff = curr_m0["len"] - prev_m0["len"]
            if len_diff != 0:
                prev_hit = iter_data[prev_it].get("cached_tokens", 0) / iter_data[prev_it].get("input_tokens", 1) * 100
                curr_hit = iter_data[curr_it].get("cached_tokens", 0) / iter_data[curr_it].get("input_tokens", 1) * 100
                print(f"  Iter {prev_it} → {curr_it}: MSG-0 长度变化 {prev_m0['len']:,} → {curr_m0['len']:,} (Δ={len_diff:+,}), 命中率 {prev_hit:.1f}% → {curr_hit:.1f}%")

    print("\n" + "=" * 130)
    print("检查 MSG-0 中是否包含 dynamic_vars 内容")
    print("=" * 130)

    for it in sorted_iters[:10]:
        d = iter_data[it]
        msg0 = d.get("msg0")
        if not msg0:
            continue
        content = msg0["content"]
        has_dynamic = "<dynamic_vars>" in content
        has_timestamp = "当前时间" in content
        print(f"  Iter {it}: MSG-0 len={msg0['len']:,}, 包含<dynamic_vars>={has_dynamic}, 包含当前时间={has_timestamp}")


if __name__ == "__main__":
    main()

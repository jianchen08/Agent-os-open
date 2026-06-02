"""计算 23,680 cached_tokens 对应到第几条 MSG。

用字符数/4 粗估 token 数，找到累加到 23,680 附近的 MSG 边界。
"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_MSG = re.compile(r"\[llm_core\] MSG-(\d+)\s+(.*)")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")
RE_USAGE = re.compile(r"'input_tokens':\s*(\d+).*?'cached_tokens':\s*(\d+)")


def parse_one_iter(target_iter):
    """提取指定迭代的 MSG 列表。"""
    cur_msgs = []
    cur_msg_idx = None
    cur_buf = []
    sending = False
    found = None

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
                it = int(m3.group(1))
                if it == target_iter:
                    flush()
                    found = {"msgs": list(cur_msgs)}
                cur_msgs = []
                sending = False
                continue

            m4 = RE_USAGE.search(line)
            if m4 and found is not None:
                found["input"] = int(m4.group(1))
                found["cached"] = int(m4.group(2))
                return found

    return found


def main():
    target = 6
    data = parse_one_iter(target)
    if not data:
        print(f"未找到 iter {target}")
        return

    msgs = data["msgs"]
    cached = data.get("cached", 23680)
    input_t = data.get("input", 0)

    print(f"Iter {target}: input={input_t:,}, cached={cached:,}")
    print(f"{'IDX':>4} | {'MSG#':>4} | {'字符数':>8} | {'估算token':>8} | {'累加token':>8} | {'累加%':>5} | 角色摘要")
    print("-" * 120)

    cumulative = 0
    for i, (idx, content) in enumerate(msgs):
        char_len = len(content)
        est_tokens = char_len // 4
        cumulative += est_tokens
        pct = cumulative / input_t * 100 if input_t else 0

        role_match = re.match(r'role=(\w+)', content)
        role = role_match.group(1) if role_match else "?"
        name_match = re.search(r'name=(\w+)', content)
        name = name_match.group(1) if name_match else ""
        preview = content[:80].replace("\n", "\\n")

        marker = ""
        if abs(cumulative - cached) < est_tokens:
            marker = " <<<< CACHED 边界"
        elif cumulative > cached and cumulative - est_tokens < cached:
            marker = " <<<< CACHED 边界在这条MSG内"

        print(f"{i:>4} | {idx:>4} | {char_len:>8,} | {est_tokens:>8,} | {cumulative:>8,} | {pct:>5.1f}% | {role}{' name='+name if name else ''} {marker}")

    print(f"\n缓存命中 {cached:,} tokens，占总输入 {input_t:,} 的 {cached/input_t*100:.1f}%")


if __name__ == "__main__":
    main()

"""列出所有迭代的缓存命中率，找出低命中轮次的真正原因。"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_MSG = re.compile(r"\[llm_core\] MSG-(\d+)\s+(.*)")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")
RE_USAGE = re.compile(r"'input_tokens':\s*(\d+).*?'cached_tokens':\s*(\d+)")
RE_TIMESTAMP = re.compile(r"^(\d{2}:\d{2}:\d{2})")


def parse_all():
    """解析所有迭代。"""
    iters = {}
    cur_msg_count = 0
    cur_time = ""
    sending = False

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")

            tm = RE_TIMESTAMP.match(line)
            if tm:
                cur_time = tm.group(1)

            m = RE_SENDING.search(line)
            if m:
                cur_msg_count = int(m.group(1))
                sending = True
                continue

            m = RE_ITER.search(line)
            if m:
                it = int(m.group(1))
                iters[it] = {
                    "msg_count": cur_msg_count,
                    "time": cur_time,
                }
                sending = False
                continue

            m = RE_USAGE.search(line)
            if m:
                last = max(iters.keys()) if iters else None
                if last is not None:
                    iters[last]["input"] = int(m.group(1))
                    iters[last]["cached"] = int(m.group(2))

    return iters


def main():
    data = parse_all()
    print(f"共 {len(data)} 个迭代\n")

    print(f"{'Iter':>4} | {'时间':>8} | {'消息数':>4} | {'输入tokens':>12} | {'缓存tokens':>12} | {'命中率':>6} | {'未命中':>10} | 标记")
    print("-" * 110)

    sorted_iters = sorted(data.keys())
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
        elif hit > 90:
            marker = "✅ 高命中"

        print(f"{it:>4} | {t:>8} | {mc:>4} | {inp:>12,} | {cached:>12,} | {hit:>5.1f}% | {missed:>10,} | {marker}")

    print("\n" + "=" * 110)
    print("低命中率轮次 (<50%) 分析：")
    print("=" * 110)

    low_iters = [it for it in sorted_iters if data[it].get("cached", 0) / max(data[it].get("input", 1), 1) < 50]

    for i, it in enumerate(low_iters):
        d = data[it]
        inp = d.get("input", 0)
        cached = d.get("cached", 0)
        hit = cached / inp * 100 if inp else 0

        prev_it = None
        for j in range(len(sorted_iters)):
            if sorted_iters[j] == it and j > 0:
                prev_it = sorted_iters[j - 1]
                break

        prev_info = ""
        if prev_it and prev_it in data:
            pd = data[prev_it]
            p_inp = pd.get("input", 0)
            p_cached = pd.get("cached", 0)
            p_hit = p_cached / p_inp * 100 if p_inp else 0
            time_diff = ""
            try:
                t1 = pd.get("time", "")
                t2 = d.get("time", "")
                if t1 and t2:
                    h1, m1, s1 = map(int, t1.split(":"))
                    h2, m2, s2 = map(int, t2.split(":"))
                    diff = (h2 * 3600 + m2 * 60 + s2) - (h1 * 3600 + m1 * 60 + s1)
                    time_diff = f"间隔={diff}s"
            except:
                pass
            prev_info = f"前一轮 iter={prev_it} 命中={p_hit:.1f}% 消息数={pd.get('msg_count',0)} {time_diff}"
        else:
            prev_info = "无前一轮"

        print(f"  Iter {it}: 命中={hit:.1f}% cached={cached:,} | {prev_info}")


if __name__ == "__main__":
    main()

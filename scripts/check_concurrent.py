"""检查低命中轮次之前是否有其他管道的 API 调用导致缓存淘汰。"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"
TARGET_PIPELINE = "81f98f451dc4"

RE_ITER = re.compile(r"pipeline=(\w+)\s+iter=(\d+)\s+LLM returned")
RE_CALLING = re.compile(r"\[llm_core\] Calling LLM.*pipeline=(\w+)")
RE_SENDING = re.compile(r"\[llm_core\] Sending \d+ messages")
RE_USAGE = re.compile(r"'input_tokens':\s*(\d+).*?'cached_tokens':\s*(\d+)")
RE_TIMESTAMP = re.compile(r"^(\d{2}:\d{2}:\d{2})")


def parse_events():
    """解析所有 API 调用事件，包括不同管道的。"""
    events = []
    cur_time = ""
    cur_pipeline = None
    cur_iter = None

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            tm = RE_TIMESTAMP.match(line)
            if tm:
                cur_time = tm.group(1)

            m = RE_ITER.search(line)
            if m:
                pipe = m.group(1)
                it = int(m.group(2))
                events.append({
                    "time": cur_time,
                    "type": "iter_done",
                    "pipeline": pipe,
                    "iter": it,
                })
                continue

            m = RE_CALLING.search(line)
            if m:
                pipe = m.group(1)
                events.append({
                    "time": cur_time,
                    "type": "calling_llm",
                    "pipeline": pipe,
                })
                continue

            m = RE_USAGE.search(line)
            if m:
                inp = int(m.group(1))
                cached = int(m.group(2))
                events.append({
                    "time": cur_time,
                    "type": "usage",
                    "pipeline": TARGET_PIPELINE,
                    "input": inp,
                    "cached": cached,
                })

    return events


def main():
    events = parse_events()

    # 找出目标管道的所有迭代
    target_iters = {}
    last_usage = None
    for ev in events:
        if ev["type"] == "iter_done" and ev["pipeline"] == TARGET_PIPELINE:
            target_iters[ev["iter"]] = {"time": ev["time"]}
        if ev["type"] == "usage" and ev["pipeline"] == TARGET_PIPELINE:
            last_iter = max(target_iters.keys()) if target_iters else None
            if last_iter is not None:
                target_iters[last_iter]["input"] = ev["input"]
                target_iters[last_iter]["cached"] = ev["cached"]

    # 找低命中轮次
    low_iters = []
    for it in sorted(target_iters.keys()):
        d = target_iters[it]
        inp = d.get("input", 0)
        cached = d.get("cached", 0)
        hit = cached / inp * 100 if inp else 0
        if hit < 50 and inp > 0:
            low_iters.append(it)

    print(f"目标管道 {TARGET_PIPELINE} 的迭代数: {len(target_iters)}")
    print(f"低命中轮次: {low_iters}")

    # 检查低命中轮次之前的所有事件
    all_iters_sorted = sorted(target_iters.keys())

    for it in low_iters:
        idx = all_iters_sorted.index(it)
        if idx == 0:
            print(f"\nIter {it}: 首轮，跳过")
            continue

        prev_it = all_iters_sorted[idx - 1]
        prev_time = target_iters[prev_it]["time"]
        cur_time = target_iters[it]["time"]

        print(f"\n{'='*100}")
        d = target_iters[it]
        inp = d.get("input", 0)
        cached = d.get("cached", 0)
        hit = cached / inp * 100 if inp else 0
        print(f"Iter {it}: 命中={hit:.1f}% cached={cached:,}")
        print(f"前一轮 Iter {prev_it} 时间={prev_time} → 当前 Iter {it} 时间={cur_time}")

        # 找这段时间内的所有事件
        between_events = []
        capturing = False
        for ev in events:
            if ev["type"] == "iter_done" and ev["pipeline"] == TARGET_PIPELINE and ev["iter"] == prev_it:
                capturing = True
                continue
            if ev["type"] == "iter_done" and ev["pipeline"] == TARGET_PIPELINE and ev["iter"] == it:
                capturing = False
                break
            if capturing:
                between_events.append(ev)

        # 统计其他管道的 API 调用
        other_pipe_calls = [e for e in between_events if e.get("pipeline") != TARGET_PIPELINE]
        same_pipe_events = [e for e in between_events if e.get("pipeline") == TARGET_PIPELINE]

        print(f"  间隔内事件: 总计={len(between_events)}, 本管道={len(same_pipe_events)}, 其他管道={len(other_pipe_calls)}")

        if other_pipe_calls:
            print(f"  其他管道的 API 调用:")
            other_pipes = set(e.get("pipeline", "?") for e in other_pipe_calls)
            for op in other_pipes:
                op_events = [e for e in other_pipe_calls if e.get("pipeline") == op]
                op_types = {}
                for e in op_events:
                    t = e["type"]
                    op_types[t] = op_types.get(t, 0) + 1
                print(f"    管道 {op}: {len(op_events)} 个事件 {op_types}")

        # 也看高命中轮次的间隔
        # 找最近的高命中轮次
        for hi_idx in range(idx - 1, -1, -1):
            hi_it = all_iters_sorted[hi_idx]
            hi_d = target_iters[hi_it]
            hi_inp = hi_d.get("input", 0)
            hi_cached = hi_d.get("cached", 0)
            hi_hit = hi_cached / hi_inp * 100 if hi_inp else 0
            if hi_hit > 90:
                hi_prev_it = all_iters_sorted[hi_idx - 1] if hi_idx > 0 else None
                if hi_prev_it:
                    hi_prev_time = target_iters[hi_prev_it]["time"]
                    hi_time = hi_d["time"]
                    hi_between = []
                    cap = False
                    for ev in events:
                        if ev["type"] == "iter_done" and ev["pipeline"] == TARGET_PIPELINE and ev["iter"] == hi_prev_it:
                            cap = True
                            continue
                        if ev["type"] == "iter_done" and ev["pipeline"] == TARGET_PIPELINE and ev["iter"] == hi_it:
                            cap = False
                            break
                        if cap:
                            hi_between.append(ev)
                    hi_other = [e for e in hi_between if e.get("pipeline") != TARGET_PIPELINE]
                    print(f"  对比: 高命中 Iter {hi_it} ({hi_hit:.1f}%) 间隔内其他管道事件={len(hi_other)}")
                break


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""深入分析 trace：提取 FunctionCall 的 functionName、按业务模块归类。"""
import sys, json, collections, os

def load_events(path):
    with open(path, encoding='utf-8') as f:
        # 文件是 JSON 数组
        data = f.read()
    return json.loads(data)

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'probe-out/jank-trace.json'
    events = load_events(path)
    print(f"总事件: {len(events)}")

    # 1) FunctionCall 的 functionName 分布
    func_calls = collections.defaultdict(lambda: {'count':0, 'dur':0.0})
    # 2) 所有事件的 args.url / args.functionName 归类
    url_durs = collections.defaultdict(float)
    # 3) 按时间窗口统计（看流式期间 vs 非流式期间）
    # 先找 stream_start / stream_end 时间戳
    stream_start_ts = None
    stream_end_ts = None
    for e in events:
        if e.get('name') == 'stream_start' or e.get('cat','').startswith('blink.user_timing'):
            pass
    # user timing 可能没有，用 ws 发送 user_input 的近似
    # 直接按 ts 范围切分：前 8 秒是准备，8-45 秒是流式

    # 提取所有 X 事件的 name + dur + args
    name_dur = collections.defaultdict(float)
    name_cnt = collections.defaultdict(int)
    long_x = []  # (dur, name, args, ts)
    for e in events:
        if e.get('ph') != 'X':
            continue
        dur = e.get('dur', 0)
        name = e.get('name', '?')
        args = e.get('args', {}) or {}
        if dur >= 5:
            name_dur[name] += dur
            name_cnt[name] += 1
        if dur >= 50:
            long_x.append((dur, name, args, e.get('ts',0), e.get('cat','')))

    # FunctionCall 里有没有 functionName
    print("\n===== FunctionCall 的 functionName 分布 =====")
    fc_names = collections.defaultdict(lambda: {'count':0, 'dur':0.0})
    for e in events:
        if e.get('name') == 'FunctionCall' and e.get('ph') == 'X':
            args = e.get('args', {}) or {}
            data = args.get('data', {}) or {}
            fn = data.get('functionName') or data.get('url') or '(unknown)'
            fc_names[fn]['count'] += 1
            fc_names[fn]['dur'] += e.get('dur', 0)
    for fn, v in sorted(fc_names.items(), key=lambda x:-x[1]['dur'])[:15]:
        print(f"  {fn[:60]:<61} {v['dur']/1000:>8.1f}ms ×{v['count']}")

    # 长任务里带 args 的
    print("\n===== >=50ms 的 X 事件（含 args）=====")
    for dur, name, args, ts, cat in sorted(long_x, key=lambda x:-x[0])[:20]:
        data = (args.get('data', {}) or {}) if args else {}
        extra = ''
        for k in ('functionName','url','stackTrace','type','beginData'):
            if k in data:
                v = data[k]
                if isinstance(v, list):
                    v = v[0] if v else ''
                extra += f" {k}={str(v)[:40]}"
        print(f"  {dur/1000:>7.1f}ms {name[:30]:<31} {cat.split(',')[0][:20]:<21}{extra}")

    # 按 cat 归类总耗时
    print("\n===== 按 category 归类总耗时 =====")
    cat_dur = collections.defaultdict(float)
    for e in events:
        if e.get('ph') == 'X':
            cat_dur[e.get('cat','?')] += e.get('dur',0)
    for c, d in sorted(cat_dur.items(), key=lambda x:-x[1])[:12]:
        print(f"  {c[:50]:<51} {d/1000:>10.1f}ms")

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""分析 Playwright 导出的 Chrome trace.trace，找出主线程上最耗时的函数。
用法: python scripts/analyze_trace.py probe-out/jank-trace.zip
"""
import sys, json, zipfile, collections, os

def load_trace(path):
    if path.endswith('.zip'):
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if n.endswith('.trace'))
        data = z.read(name).decode('utf-8', errors='ignore')
    else:
        with open(path, encoding='utf-8') as f:
            data = f.read()
    stripped = data.lstrip()
    if stripped.startswith('['):
        return json.loads(data)
    # JSON Lines 或混合：逐行解析，失败则整块尝试
    events = []
    for line in data.split('\n'):
        line = line.strip().rstrip(',')
        if line.startswith('{'):
            try:
                events.append(json.loads(line))
            except Exception:
                pass
    if events:
        return events
    # 兜底：尝试整块
    try:
        obj = json.loads(data)
        return obj.get('traceEvents', obj) if isinstance(obj, dict) else obj
    except Exception:
        return []

def analyze(events):
    # 找所有 X（完成）类型的 CPU profile / 函数事件
    # Chrome trace 里函数执行是 ph=X (complete events)，cat 可能是 disabled-by-default-v8.cpu_profiler
    # 或 devtools.timeline
    func_durations = collections.defaultdict(float)  # name -> total self/total time
    func_counts = collections.defaultdict(int)

    # 我们关心 main 线程（tid 通常标记）上的事件
    # 先收集所有 X 事件，按 name 聚合 total duration
    long_events = []

    for e in events:
        if e.get('ph') != 'X':
            continue
        dur = e.get('dur', 0) or e.get('tdur', 0)
        if dur < 5:  # 忽略 <5ms
            continue
        name = e.get('name', '?')
        cat = e.get('cat', '')
        func_durations[name] += dur
        func_counts[name] += 1
        if dur >= 100:
            long_events.append((dur, name, cat, e.get('ts', 0), e.get('pid'), e.get('tid')))

    print(f"总事件数: {len(events)}")
    print(f"\n===== Top 20 累计耗时函数（dur>=5ms）=====")
    print(f"{'函数':<45} {'累计ms':>10} {'次数':>6} {'平均ms':>8}")
    for name, total in sorted(func_durations.items(), key=lambda x: -x[1])[:20]:
        cnt = func_counts[name]
        print(f"{name[:44]:<45} {total/1000:>10.1f} {cnt:>6} {total/cnt/1000:>8.1f}")

    print(f"\n===== 单次 >=100ms 的长任务（按耗时排序）=====")
    print(f"{'耗时ms':>8} {'函数':<40} {'类别':<35} {'起始ms':>10}")
    for dur, name, cat, ts, pid, tid in sorted(long_events, key=lambda x: -x[0])[:25]:
        cat_short = cat.split(',')[0][:34] if cat else ''
        print(f"{dur/1000:>8.1f} {name[:39]:<40} {cat_short:<35} {ts/1000:>10.1f}")

    # 额外：找 markdown / react / diff 相关
    print(f"\n===== 渲染相关函数（markdown/react/diff/highlight）=====")
    keywords = ['markdown', 'Markdown', 'parse', 'diff', 'Diff', 'highlight', 'Highlight',
                'FunctionCall', 'commit', 'reconcile', 'parseHtml', 'layout', 'Paint',
                'Streamdown', 'mermaid', 'Mermaid']
    for kw in keywords:
        for name, total in func_durations.items():
            if kw.lower() in name.lower():
                print(f"  {name[:50]:<51} {total/1000:>8.1f}ms ×{func_counts[name]}")

if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else 'probe-out/jank-trace.zip'
    if not os.path.exists(path):
        # 找最近的
        for root, dirs, files in os.walk('probe-out'):
            for f in files:
                if f.endswith('.zip'):
                    path = os.path.join(root, f)
                    break
    print(f"分析: {path}")
    events = load_trace(path)
    analyze(events)

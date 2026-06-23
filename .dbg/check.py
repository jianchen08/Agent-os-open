import json, collections
lines = open('.dbg/trae-debug-log-sys-notify-stream-stuck.ndjson', encoding='utf-8').readlines()
evs = [json.loads(l) for l in lines]
cnt = collections.Counter(e.get('location', '?') for e in evs)
print('=== All locations count ===')
for loc, c in cnt.most_common():
    print(f'  {c:5d} {loc}')

print()
print('=== drain_loop iter events ===')
iter_evs = [e for e in evs if 'drain_loop' in e.get('location', '')]
for e in iter_evs[:30]:
    data_str = json.dumps(e.get('data', {}), ensure_ascii=False)[:200]
    print(f'  ts={e["ts"]} loc={e["location"]} data={data_str}')
print(f'... total drain_loop events: {len(iter_evs)}')

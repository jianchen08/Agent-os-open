"""检查管道 YAML 文件的 records 分布"""
import yaml
import os

files = [
    "data/pipelines/1064abdb1e0b.yaml",
    "data/pipelines/1064abdb1e0b_002.yaml",
]

for path in files:
    if not os.path.exists(path):
        print(f"=== {path} === NOT FOUND")
        continue
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    summary = data.get("summary", {})
    records = data.get("records", [])
    print(f"=== {path} ===")
    print(f"  run_id: {summary.get('run_id')}")
    print(f"  record_count: {summary.get('record_count')}")
    print(f"  actual_records: {len(records)}")
    if records:
        seqs = [r.get("sequence", 0) for r in records]
        print(f"  sequence range: {min(seqs)} - {max(seqs)}")
        first = records[0]
        last = records[-1]
        c1 = (first.get("content") or "")[:60]
        c2 = (last.get("content") or "")[:60]
        print(f"  first: seq={first.get('sequence')} role={first.get('role')} content={c1}")
        print(f"  last:  seq={last.get('sequence')} role={last.get('role')} content={c2}")
        # 找到包含'老公'的消息
        for r in records:
            c = r.get("content") or ""
            if "老公" in c:
                print(f"  [老公] seq={r.get('sequence')} role={r.get('role')} content={c[:80]}")
                break
    print()

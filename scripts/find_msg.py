"""查找指定消息的 sequence"""
import yaml

target = "老公，两个任务都完成了"

for path in ["data/pipelines/1064abdb1e0b.yaml", "data/pipelines/1064abdb1e0b_002.yaml"]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    records = data.get("records", [])
    for r in records:
        c = r.get("content") or ""
        if isinstance(c, str) and target in c:
            print(f"[FOUND] file={path} seq={r.get('sequence')} role={r.get('role')} content={c[:80]}")
        # 也检查 parts 里的内容
        parts = r.get("parts") or []
        for p in parts:
            pc = p.get("content") or ""
            if isinstance(pc, str) and target in pc:
                print(f"[FOUND-PART] file={path} seq={r.get('sequence')} role={r.get('role')} part_content={pc[:80]}")

# 也看看 seq=1 的是什么
for path in ["data/pipelines/1064abdb1e0b.yaml"]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    records = data.get("records", [])
    for r in records:
        if r.get("sequence") == 1:
            c = r.get("content") or ""
            print(f"[SEQ-1] role={r.get('role')} type={r.get('type')} content={str(c)[:80]}")

"""检查 YAML 执行记录中是否有 dynamic_context 消息。"""

import yaml

YAML_PATH = r"d:\myproject\container_08f57bc14532\data\pipelines\b28e14d77b5b\81f98f451dc4.yaml"

with open(YAML_PATH, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

records = []
if isinstance(data, dict):
    for key in data:
        if isinstance(data[key], list) and len(data[key]) > 10:
            records = data[key]
            break
elif isinstance(data, list):
    records = data

dyn_count = 0
for r in records:
    if isinstance(r, dict):
        name = r.get("name") or ""
        role = r.get("role") or ""
        content = str(r.get("content") or "")[:100]
        if "dynamic" in str(name).lower() or "dynamic" in content.lower():
            dyn_count += 1
            print(f"  name={name} role={role} content={content[:80]}")

print(f"\n共 {dyn_count} 条 dynamic_context 记录")
print(f"总记录数: {len(records)}")

# 也看前 5 条记录的结构
print("\n前 5 条记录:")
for i, r in enumerate(records[:5]):
    if isinstance(r, dict):
        print(f"  [{i}] keys={list(r.keys())}")

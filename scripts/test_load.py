"""模拟 list_by_pipeline 的翻页行为"""
import sys
sys.path.insert(0, "src")
from infrastructure.execution_record_storage import ExecutionRecordStorage

storage = ExecutionRecordStorage(data_dir="data/pipelines")

pid = "1064abdb1e0b"

# 模拟首次加载（无 before_sequence，有 limit）
records, has_more = storage.list_by_pipeline(pid, limit=50)
print(f"首次加载: {len(records)} 条, has_more={has_more}")
if records:
    print(f"  seq range: {records[0].sequence} - {records[-1].sequence}")
    print(f"  first content: {(records[0].content or '')[:60]}")
    print(f"  last content: {(records[-1].content or '')[:60]}")

# 模拟翻页
top_cursor = records[0].sequence if records else 0
page = 1
while has_more and page < 30:
    page += 1
    records, has_more = storage.list_by_pipeline(pid, limit=50, before_sequence=top_cursor)
    print(f"\n翻页 #{page}: before_seq={top_cursor}, got {len(records)} 条, has_more={has_more}")
    if records:
        print(f"  seq range: {records[0].sequence} - {records[-1].sequence}")
        print(f"  first content: {(records[0].content or '')[:60]}")
        top_cursor = records[0].sequence
    else:
        print("  空结果!")
        break

print(f"\n最终: top_cursor={top_cursor}, has_more={has_more}")

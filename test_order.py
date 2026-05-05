"""测试：用实际管道文件验证 API 返回的消息顺序"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from infrastructure.execution_record_storage import ExecutionRecordStorage
from infrastructure.session.models import SessionModel
from channels.api.routes_threads import _record_to_message_response


def test_real_pipeline():
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pipelines")
    storage = ExecutionRecordStorage(data_dir=data_dir)

    summaries = storage.list_summaries(limit=10)
    print(f"找到 {len(summaries)} 个管道文件:")
    for s in summaries:
        print(f"  {s.run_id}: {s.total_records} 条记录, status={s.status}")

    for s in summaries:
        records = storage.list_by_pipeline(s.run_id)
        print(f"\n=== 管道 {s.run_id}: {len(records)} 条记录 ===")
        print(f"  pipeline_ids=[\"{s.run_id}\"] 的排序结果:")

        pipeline_order = {s.run_id: 0}
        records.sort(key=lambda r: (
            pipeline_order.get(r.pipeline_run_id, 999),
            r.sequence,
            r.created_at or "",
        ))

        for r in records:
            role_map = {"user": "user", "ai": "assistant", "tool": "tool"}
            role = role_map.get(r.type, r.role or "?")
            content_preview = (r.content or "")[:50].replace("\n", " ")
            print(f"  [{role}] seq={r.sequence} iter={r.iteration} content={content_preview}")

        msgs = [_record_to_message_response(r, "test_thread") for r in records]
        print(f"  API 返回顺序: {[f'{m.role}(seq={m.sequence})' for m in msgs]}")


if __name__ == "__main__":
    test_real_pipeline()

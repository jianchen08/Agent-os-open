"""ExecutionRecordStorage 组合 key 回归测试。

背景 BUG（fix_20260625_ai_record_id_duplicate 修订）：
一个 run() 多轮 LLM 迭代共享同一个 bridge message_id（裸 hex），每轮各落一条 ai
记录。原方案给 iteration>1 的 record_id 追加 #iteration 后缀来避免 storage._records
dict 互相覆盖，但这破坏了 record_id 与前端 WS message_id 的 id 契约，导致前端
initFromAPI 精确 id 对账失败、乐观占位符残留。

修订方案：record_id 始终保持裸 hex（id 契约），_records dict 的 key 改用
record_id::sequence 组合键，由 sequence 区分同一逻辑消息的多条落盘记录。

本测试锁定核心契约：
1. 同 record_id、不同 sequence 的多条记录不互相覆盖（list 返回全部）。
2. 所有 list/count 方法不依赖 dict key 格式（.values() 遍历正确）。
3. 磁盘往返：save → 新实例加载 → list 完整且按 sequence 排序。
4. record_id 字段保持原值（不被 key 格式污染）。
"""
from __future__ import annotations

from pathlib import Path

from infrastructure.execution_record_storage import ExecutionRecordStorage, _record_key
from infrastructure.execution_record_storage import ExecutionRecordData


def _make_ai(record_id: str, sequence: int, iteration: int = 1) -> ExecutionRecordData:
    """构造一条 ai 记录（模拟同一 bridge message_id 的不同迭代轮次）。"""
    return ExecutionRecordData(
        record_id=record_id,
        pipeline_run_id="pipe-test-001",
        type="ai",
        sequence=sequence,
        iteration=iteration,
        role="assistant",
        content=f"reply iter {iteration}",
    )


class TestCompositeKey:
    """组合 key (record_id::sequence) 行为。"""

    def test_record_key_format_is_record_id_double_colon_sequence(self):
        rec = _make_ai("abc123", 5)
        assert _record_key(rec) == "abc123::5"

    def test_record_key_sequence_missing_falls_back_to_zero(self):
        rec = ExecutionRecordData(record_id="abc", pipeline_run_id="p", sequence=0)
        assert _record_key(rec) == "abc::0"

    def test_same_record_id_different_sequence_not_overwritten(self):
        """核心契约：多轮迭代共享 record_id，sequence 不同，全部保留。"""
        storage = ExecutionRecordStorage(data_dir=None)
        rid = "shared_msg_id"
        storage.save(_make_ai(rid, sequence=1, iteration=1))
        storage.save(_make_ai(rid, sequence=2, iteration=2))
        storage.save(_make_ai(rid, sequence=3, iteration=3))

        records, _ = storage.list_by_pipeline("pipe-test-001", limit=None)
        assert len(records) == 3
        # 全部保留，按 sequence 升序
        seqs = [r.sequence for r in records]
        assert seqs == [1, 2, 3]
        # record_id 全部是裸 id（未被 key 格式污染）
        assert all(r.record_id == rid for r in records)

    def test_same_record_id_same_sequence_still_overwrites_idempotent(self):
        """相同 (record_id, sequence) 视为同一条，覆盖（幂等写入）。"""
        storage = ExecutionRecordStorage(data_dir=None)
        rid = "msg_id_x"
        storage.save(_make_ai(rid, sequence=1, iteration=1))
        # 再写一条相同 key 的，content 不同
        dup = _make_ai(rid, sequence=1, iteration=1)
        dup.content = "updated content"
        storage.save(dup)

        records, _ = storage.list_by_pipeline("pipe-test-001", limit=None)
        assert len(records) == 1
        assert records[0].content == "updated content"


class TestListMethodsIndependentOfKeyFormat:
    """list / count 方法不依赖 dict key 格式（.values() 遍历）。"""

    def test_list_by_pipeline_mixed_record_ids_correct(self):
        storage = ExecutionRecordStorage(data_dir=None)
        # 3 个不同 record_id + 1 个多轮共享 record_id
        storage.save(_make_ai("msg_a", sequence=1))
        storage.save(_make_ai("msg_b", sequence=2))
        storage.save(_make_ai("msg_c", sequence=3, iteration=1))
        storage.save(_make_ai("msg_c", sequence=4, iteration=2))  # 同 record_id 多轮

        records, _ = storage.list_by_pipeline("pipe-test-001", limit=None)
        assert len(records) == 4
        seqs = [r.sequence for r in records]
        assert seqs == [1, 2, 3, 4]

    def test_count_by_session_counts_correctly(self):
        storage = ExecutionRecordStorage(data_dir=None)
        for i in range(1, 6):
            storage.save(_make_ai("same_rid", sequence=i, iteration=i))
        assert storage.count_by_session("pipe-test-001") == 5

    def test_list_by_pipeline_limit_truncates_correctly(self):
        storage = ExecutionRecordStorage(data_dir=None)
        for i in range(1, 11):
            storage.save(_make_ai(f"rid_{i}", sequence=i))
        records, _ = storage.list_by_pipeline("pipe-test-001", limit=3)
        # 取最近 3 条（sequence 最大）
        assert [r.sequence for r in records] == [8, 9, 10]


class TestDiskRoundTrip:
    """磁盘往返：save → 新实例加载 → list 完整有序。"""

    def test_save_then_reload_new_instance_keeps_all_iterations(self, tmp_path: Path):
        storage = ExecutionRecordStorage(data_dir=str(tmp_path))
        rid = "roundtrip_msg"
        storage.save(_make_ai(rid, sequence=10, iteration=1))
        storage.save(_make_ai(rid, sequence=11, iteration=2))
        storage.save(_make_ai(rid, sequence=12, iteration=3))

        # 新实例从磁盘加载
        storage2 = ExecutionRecordStorage(data_dir=str(tmp_path))
        records, _ = storage2.list_by_pipeline("pipe-test-001", limit=None)

        # 关键：全量加载不丢中间轮次
        assert len(records) == 3
        assert [r.sequence for r in records] == [10, 11, 12]
        # record_id 保持裸 hex（id 契约）
        assert all(r.record_id == rid for r in records)

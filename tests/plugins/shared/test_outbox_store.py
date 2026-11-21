# @feature: FP-0.2.二 内部模块 outbox | @ci: python-coverage
"""OutboxStore 有界缓冲契约测试（投递队列单点实现）。

锁定行为：
- 追加/恢复/游标推进的基本语义（断网不丢：文件即队列序列化）；
- 有界语义：超限丢最旧并留痕告警，压实后文件等于队内内容；
- **超限路径的成本形态**：追加成本必须与追加量同阶，不得「每条追加都
  全量序列化 + 全量重写」——2026-11-10 装机实证：64MB 触顶后每条事件触发
  1~37 次全量序列化，事件循环被同步阻塞 8~12s/30s，cohost watchdog 判
  心跳停滞自杀，light_stable 组崩溃循环 17 次。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import outbox_store  # noqa: E402, I001  (需先推 sys.path 再导入；平铺裸模块先例)
from outbox_store import OutboxStore  # noqa: E402, I001


def _entry(idx: int, payload_chars: int = 80) -> dict[str, object]:
    """定长事件体：便于按字节推算容量边界（不去猜实现常数）。"""
    return {"seq": idx, "body": "x" * payload_chars}


class TestBasicSemantics:
    def test_append_then_load_roundtrip(self, tmp_path: Path) -> None:
        """追加的事件在重启后原样恢复（断网续传的载体）。"""
        path = tmp_path / "outbox.jsonl"
        store = OutboxStore(path)
        assert store.append({"seq": 1, "body": "甲"}) == 0
        store.append({"seq": 2, "body": "乙"})
        assert len(store) == 2

        restored = OutboxStore(path)
        assert restored.load() == 2
        assert restored.peek(2) == [{"seq": 1, "body": "甲"}, {"seq": 2, "body": "乙"}]

    def test_peek_does_not_consume(self, tmp_path: Path) -> None:
        store = OutboxStore(tmp_path / "outbox.jsonl")
        store.append({"seq": 1})
        store.append({"seq": 2})
        assert store.peek(1) == [{"seq": 1}]
        assert len(store) == 2

    def test_commit_advances_cursor_and_rewrites_file(self, tmp_path: Path) -> None:
        """投递成功后去掉已投递前缀：内存与文件同刀推进。"""
        path = tmp_path / "outbox.jsonl"
        store = OutboxStore(path)
        for idx in range(3):
            store.append({"seq": idx})
        store.commit(2)
        assert store.peek(5) == [{"seq": 2}]
        lines = path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["seq"] for line in lines] == [2]

    def test_load_drops_corrupt_tail_line(self, tmp_path: Path) -> None:
        """崩溃截断的半行丢弃并告警，整条队列不因半行卡死。"""
        path = tmp_path / "outbox.jsonl"
        path.write_text('{"seq": 1}\n{"seq": 2', encoding="utf-8")
        store = OutboxStore(path)
        assert store.load() == 1
        assert store.peek(5) == [{"seq": 1}]


class TestBoundSemantics:
    def test_overflow_drops_oldest_and_file_stays_bounded(self, tmp_path: Path) -> None:
        """超限丢最旧：返回值 = 丢弃条数，文件压实到上限内。"""
        path = tmp_path / "outbox.jsonl"
        max_bytes = 2000
        store = OutboxStore(path, max_bytes=max_bytes)
        dropped_total = 0
        for idx in range(200):
            dropped_total += store.append(_entry(idx))
        assert dropped_total > 0
        assert path.stat().st_size <= max_bytes + 200  # 余量 = 单条事件体量级
        # 文件内容 == 队内内容（压实不变量）
        disk = [json.loads(line)["seq"] for line in path.read_text(encoding="utf-8").splitlines()]
        assert disk == [entry["seq"] for entry in store.peek(len(store))]
        # 保留的是最新的尾巴
        assert disk[-1] == 199
        assert disk[0] > 0

    def test_rewrite_cost_amortized_not_per_append(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归锁：超限后追加不是「每条一次全量重写」。

        判据是成本形态而非绝对值：整文件重写次数必须与「追加量 / 低水位余量」
        同阶（每次重写至少要由一段新追加挣得），不得随追加条数线性膨胀。
        修复前实测该场景约 290 次重写（≈每条一次），修复后 ≈30 次。
        """
        path = tmp_path / "outbox.jsonl"
        max_bytes = 10_000
        store = OutboxStore(path, max_bytes=max_bytes)

        rewritten_bytes: list[int] = []
        original_rewrite = store._rewrite

        def counting_rewrite(content: str) -> None:
            rewritten_bytes.append(len(content.encode("utf-8")))
            original_rewrite(content)

        monkeypatch.setattr(store, "_rewrite", counting_rewrite)

        appended = 0
        for idx in range(300):
            appended += len(json.dumps(_entry(idx), ensure_ascii=False).encode("utf-8")) + 1
            store.append(_entry(idx))

        # 每次重写至少由「低水位余量」那么大的新追加挣得
        slack_bytes = int(max_bytes * outbox_store._DROP_SLACK_RATIO)
        allowed_rewrites = appended // slack_bytes + 2
        assert len(rewritten_bytes) <= allowed_rewrites, (
            f"重写 {len(rewritten_bytes)} 次，超过摊销上界 {allowed_rewrites}"
            "（退回逐条全量重写）"
        )
        # 写放大必须停在「1/余量比例」量级，不是「1/单条事件占比」量级
        assert sum(rewritten_bytes) <= appended * 15

    def test_single_oversized_entry_leaves_empty_queue(self, tmp_path: Path) -> None:
        """单条即超上限：队列清空（有界语义优先，不留超限残条）。"""
        store = OutboxStore(tmp_path / "outbox.jsonl", max_bytes=100)
        assert store.append({"seq": 0, "body": "y" * 500}) == 1
        assert len(store) == 0
        assert outbox_store.OutboxStore  # 模块导入即可用（保持裸模块形态）

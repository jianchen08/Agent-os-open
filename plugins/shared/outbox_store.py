"""JSONL 追加式 outbox 缓冲 —— 断网不丢的投递队列单点实现。

先例出处：trace_exporter（E 事件汇聚，docs/working/分布式执行设计_事件汇聚E
与插件宿主F_20261012.md §3.2）；sync_agent（2b 多端同步）按同款机制复用
（ADR 2026-10-15-multi-device-sync-hlc-lww D3"outbox + 指数退避，借
trace_exporter 先例"）。2026-10-15 由 trace_exporter 原类平移至共享根成为
单点实现（两消费方机制逐字节同型，复制必漂移——两处小段不同提取公共部分
纪律）。

不变量：文件内容恒等于待投递队列的序列化——
- :meth:`OutboxStore.append` 追加一行 + 内存队列入队（O(1) 快路径）；
- :meth:`OutboxStore.commit` 投递成功后原子重写文件去掉已投递前缀（游标推进；
  崩溃窗口 ≤ 一次重复投递，由消费端幂等键吸收——at-least-once + 幂等收 =
  exactly-once 效果）；
- :meth:`OutboxStore.load` 启动时从文件恢复队列（断网/重启续传）。
``max_bytes`` 有界：压实后仍超限丢最旧并告警（有界缓冲语义）。
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from atomic_io import atomic_write_text

logger = logging.getLogger(__name__)


class OutboxStore:
    """JSONL 追加缓冲 + 投递游标（已投递前缀压实）。

    Args:
        path: outbox 文件路径（父目录按需自动创建）。
        max_bytes: 有界上限；压实后仍超限丢最旧（断网不丢以不超过上限为界，
            超限即容量语义，丢弃必须留痕告警）。
    """

    def __init__(self, path: Path, max_bytes: int = 64 * 1024 * 1024) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._pending: deque[dict[str, Any]] = deque()

    @property
    def path(self) -> Path:
        return self._path

    def __len__(self) -> int:
        return len(self._pending)

    def append(self, entry: Mapping[str, Any]) -> int:
        """追加一条待投递事件（文件 + 内存队列同刀）。

        追加模式单行写（O(1)，事件高频路径）；崩溃截断的半行由 :meth:`load`
        丢弃并告警。返回为守住 ``max_bytes`` 丢弃的最旧事件条数（正常为 0）。
        """
        self._pending.append(dict(entry))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(self._pending[-1], ensure_ascii=False) + "\n")
        return self._enforce_bound()

    def peek(self, count: int) -> list[dict[str, Any]]:
        """取队首至多 count 条（不消费——投递成功才 commit）。"""
        return [self._pending[i] for i in range(min(count, len(self._pending)))]

    def commit(self, count: int) -> None:
        """标记队首 count 条已投递：重写文件去掉该前缀（游标推进）。"""
        if count <= 0:
            return
        for _ in range(min(count, len(self._pending))):
            self._pending.popleft()
        self._rewrite(_serialize(self._pending))

    def load(self) -> int:
        """启动恢复：读文件重建待投递队列，返回恢复条数。

        损坏尾行（崩溃截断的半行 JSON）丢弃并告警——半行事件本就不完整，
        丢弃优于卡死整条队列；幂等键保证已投递但未游标的重传零重复。
        """
        self._pending.clear()
        if not self._path.is_file():
            return 0
        kept: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                logger.warning("[outbox_store] 损坏行丢弃（崩溃截断）: %.80s", stripped)
                continue
            if isinstance(entry, dict):
                kept.append(entry)
        self._pending.extend(kept)
        return len(kept)

    def _enforce_bound(self) -> int:
        """超限时丢最旧（返回丢弃条数）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.stat().st_size <= self._max_bytes:
            return 0
        dropped = 0
        while self._pending and _serialized_size(self._pending) > self._max_bytes:
            self._pending.popleft()
            dropped += 1
        self._rewrite(_serialize(self._pending))
        if dropped:
            logger.warning(
                "[outbox_store] outbox 超限（max_bytes=%s），丢最旧 %s 条（容量语义，非静默）",
                self._max_bytes, dropped,
            )
        return dropped

    def _rewrite(self, content: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path, content, encoding="utf-8")


def _serialize(pending: deque[dict[str, Any]]) -> str:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in pending)


def _serialized_size(pending: deque[dict[str, Any]]) -> int:
    return len(_serialize(pending).encode("utf-8"))

"""replay_link 存储：密文记录的落盘与取回（内存 + JSON 文件持久化）。

设计真值：docs/working/dsh-go移动端本地运行调研与接入工作量_20261112.md §四B
Yopass 件——服务端只存密文，密钥随 URL fragment 由持有者传递，服务端不可解密。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("replay_link")

# 记录默认存活 7 天（回放链接时效；到期读取返回 not_found）
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
MAX_CIPHERTEXT_LEN = 2_000_000  # 单条密文上限 2MB（防滥用；回放快照 JSON 足够）
MAX_TOTAL_BYTES = 64 * 1024 * 1024  # 全仓记录总量上限 64MB（DoS 面：超限拒绝并报明确错误）


class StoreFullError(RuntimeError):
    """存储总量超限（配额拒绝，调用方转 HTTP 4xx/5xx 明确错误）。"""


class ReplayStore:
    """密文记录仓：{id: {ciphertext_b64, iv_b64, created_at, expires_at}}。"""

    def __init__(self, path: str | os.PathLike[str] | None = None,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_total_bytes: int = MAX_TOTAL_BYTES) -> None:
        self._ttl = ttl_seconds
        self._max_total = max_total_bytes
        self._path = Path(path) if path else Path("data/replay_link/records.json")
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        """启动时从磁盘恢复。损坏文件保留现场并日志告警（不清空——数据正确性优先，
        记录属用户密文资产；服务以内存空仓继续运行，重启前可人工抢救文件）。"""
        try:
            if self._path.exists():
                loaded = json.loads(self._path.read_text("utf-8"))
                if not isinstance(loaded, dict):
                    # 合法 JSON 但顶层非 dict（MF10）：形态损坏同 corruption 处置——
                    # 保留现场 + 告警 + 空仓运行，不做部分采纳（记录键集不可信）
                    raise ValueError(f"store top-level type is {type(loaded).__name__}, expect dict")
                self._records = loaded
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.error("replay_link store load failed, serving empty: %s (file kept at %s)",
                         exc, self._path)
            self._records = {}

    def _flush(self) -> None:
        """原子写（临时文件 + os.replace）；失败日志告警并抛出——落盘失败必须可感知，
        不静默（调用方 create 失败即返回错误，用户明确知道记录未保存）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._records, ensure_ascii=False))
            os.replace(tmp_name, self._path)
        except OSError:
            logger.exception("replay_link store flush failed: %s", self._path)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _purge_expired(self) -> None:
        now = time.time()
        self._records = {k: v for k, v in self._records.items() if v.get("expires_at", 0) > now}

    def _total_bytes(self) -> int:
        return sum(len(r.get("ciphertext_b64", "")) for r in self._records.values())

    def create(self, ciphertext_b64: str, iv_b64: str) -> str:
        """创建记录，返回新 id。入参非法抛 ValueError；总量超限抛 StoreFullError。"""
        if not isinstance(ciphertext_b64, str) or not ciphertext_b64:
            raise ValueError("ciphertext_b64 required")
        if not isinstance(iv_b64, str) or not iv_b64:
            raise ValueError("iv_b64 required")
        if len(ciphertext_b64) > MAX_CIPHERTEXT_LEN:
            raise ValueError("ciphertext too large")
        self._purge_expired()
        # 配额闸：现有总量 + 本条 > 上限 → 拒绝（DoS 面：配额封顶，不无限增长）
        if self._total_bytes() + len(ciphertext_b64) > self._max_total:
            raise StoreFullError(
                f"replay store quota exceeded ({self._total_bytes()} bytes in use, "
                f"limit {self._max_total})")
        now = time.time()
        record_id = secrets.token_urlsafe(16)
        self._records[record_id] = {
            "ciphertext_b64": ciphertext_b64,
            "iv_b64": iv_b64,
            "created_at": now,
            "expires_at": now + self._ttl,
        }
        self._flush()
        return record_id

    def get(self, record_id: str) -> dict[str, str] | None:
        """取回密文记录；不存在/过期返回 None。"""
        if not isinstance(record_id, str) or not record_id:
            return None
        self._purge_expired()
        rec = self._records.get(record_id)
        if rec is None:
            return None
        return {"ciphertext_b64": rec["ciphertext_b64"], "iv_b64": rec["iv_b64"]}

"""有界 dict：TTL + MAX 双收敛（长驻 sidecar 内存无界增长治理共享件）。

背景：review/evaluation 等 sidecar 的结果 dict 只插不删，进程常驻即随
历史请求线性泄漏。本件移植 approval/server.py ``_record_decision`` 的成熟
收敛范式为共享实现，供多处接入防复制漂移：

- **写时清扫过期**：每次写入做一次 TTL 清扫（条目量小，代价可忽略）；
- **超限逐出最旧**：清扫后仍超 MAX 则按 ts 逐出最旧（硬上限兜底异常灌写）；
- **条目补 ts**：写入时给条目副本盖 ``ts``（``time.time()`` 秒级）时间戳，
  读取面零变化（get/contains/迭代照旧，条目多一个 ts 字段）。

参数：TTL 默认 24h、MAX 默认 1024，可经环境变量
``AGENTOS_BOUNDED_DICT_TTL_SECONDS`` / ``AGENTOS_BOUNDED_DICT_MAX_ENTRIES``
覆盖；非法（<=0 / 非数值）回退默认值。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from typing import Any, MutableMapping

__all__ = ["BoundedDict"]

_DEFAULT_TTL_SECONDS = 86400.0  # 24h
_DEFAULT_MAX_ENTRIES = 1024

_TTL_ENV = "AGENTOS_BOUNDED_DICT_TTL_SECONDS"
_MAX_ENV = "AGENTOS_BOUNDED_DICT_MAX_ENTRIES"


def _read_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        val = float(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _read_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        val = int(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


class BoundedDict(MutableMapping[str, dict[str, Any]]):
    """TTL + MAX 双收敛的 str→dict 映射（写入收敛，读取面与 dict 一致）。

    Args:
        ttl_seconds: 条目存活秒数；None 读环境变量/默认 24h。
        max_entries: 条目数硬上限；None 读环境变量/默认 1024。
        clock: 时间源（默认 ``time.time()``）；测试注缝。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float | None = None,
        max_entries: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else _read_env_float(_TTL_ENV, _DEFAULT_TTL_SECONDS)
        self._max = max_entries if max_entries is not None else _read_env_int(_MAX_ENV, _DEFAULT_MAX_ENTRIES)
        if self._ttl <= 0:
            self._ttl = _DEFAULT_TTL_SECONDS
        if self._max <= 0:
            self._max = _DEFAULT_MAX_ENTRIES
        self._clock = clock
        self._data: dict[str, dict[str, Any]] = {}

    # ── 写面：唯一入口，写入即收敛 ─────────────────────────────

    def _sweep_expired(self, now: float) -> None:
        """TTL 清扫（写入路径顺带执行；与 approval _record_decision 同式）。"""
        expired = [k for k, v in self._data.items() if now - v.get("ts", 0) > self._ttl]
        for k in expired:
            del self._data[k]

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        now = self._clock()
        self._sweep_expired(now)
        while len(self._data) >= self._max:
            oldest = min(self._data.items(), key=lambda kv: kv[1].get("ts", 0), default=None)
            if oldest is None:
                break
            del self._data[oldest[0]]
        entry = dict(value)
        entry["ts"] = now
        self._data[key] = entry

    # ── 读面：与内置 dict 语义一致，零收敛动作 ────────────────

    def __getitem__(self, key: str) -> dict[str, Any]:
        return self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __delitem__(self, key: str) -> None:
        del self._data[key]

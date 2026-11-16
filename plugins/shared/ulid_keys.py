"""ULID 幂等键公共模块 —— exporter 生成 / ingestion 校验的单点实现。

E 事件汇聚协议（docs/working/分布式执行设计_事件汇聚E与插件宿主F_20261012.md
§3.2）的幂等与排序地基：每事件携带 ULID 作幂等键，云端按 (origin_host, ulid)
排序。ULID = 48bit UTC 毫秒 + 80bit 随机，Crockford Base32 26 字符，字典序
即时间序（同毫秒内由本模块的进程内单调递增保证不回退）。

导入形态与 ``http_json`` 先例一致：插件侧把 ``plugins/shared`` 推上 sys.path
后裸名导入（``from ulid_keys import new_ulid as _new_ulid``）。

不依赖第三方库：sidecar venv 不保证有 ulid/ulid-py 等包，标准库自足。
"""

from __future__ import annotations

import os
import threading
import time

# Crockford Base32 字母表（无 I/L/O/U）；ULID 规范编码。
_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {ch: idx for idx, ch in enumerate(_ENCODING)}
# 宽容解码别名（Crockford 规范：O→0、I/L→1）；U 不在表内不宽容。
_DECODE_ALIASES = {"O": 0, "I": 1, "L": 1}

ULID_LENGTH = 26
_TIMESTAMP_BITS = 48
_RANDOM_BITS = 80
_MAX_RANDOM = (1 << _RANDOM_BITS) - 1

_lock = threading.Lock()
_last_ms = 0
_last_random = 0


def new_ulid() -> str:
    """生成一个 ULID（同进程同毫秒单调递增）。

    单调性：同毫秒内取上一枚随机段 +1（超过 80bit 空间按规范等待下一毫秒）；
    时钟回拨时按上一毫秒继续发号（ULID 规范建议的守卫，字典序仍单调）。
    """
    global _last_ms, _last_random
    with _lock:
        ms = int(time.time() * 1000) & ((1 << _TIMESTAMP_BITS) - 1)
        if ms <= _last_ms:
            ms = _last_ms
            if _last_random >= _MAX_RANDOM:
                # 随机段耗尽（单进程单毫秒 >2^80 次发号，现实不可达）：
                # 等待下一毫秒，保持全局单调。
                ms = (ms + 1) & ((1 << _TIMESTAMP_BITS) - 1)
                random_part = os.urandom(10)
            else:
                random_part = (_last_random + 1).to_bytes(10, "big")
        else:
            random_part = os.urandom(10)
        _last_ms = ms
        _last_random = int.from_bytes(random_part, "big")
        value = (ms << _RANDOM_BITS) | _last_random
    return _encode(value)


def is_valid_ulid(value: str) -> bool:
    """严格校验 ULID 形态：26 字符、Crockford Base32（小写宽容、O/I/L 别名收）。"""
    if not isinstance(value, str) or len(value) != ULID_LENGTH:
        return False
    return all(ch.upper() in _DECODE or ch.upper() in _DECODE_ALIASES for ch in value)


def _encode(value: int) -> str:
    chars: list[str] = []
    for shift in range(ULID_LENGTH - 1, -1, -1):
        chars.append(_ENCODING[(value >> (shift * 5)) & 0x1F])
    return "".join(chars)

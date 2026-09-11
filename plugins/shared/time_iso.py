"""UTC 时间戳公共模块 —— 插件面 ``now_iso_utc`` 单点实现。

曾以八份 ``_now_iso`` 逐字拷贝散布于 context_window_guard / artifacts /
hindsight_memory / review（server+models）/ workspace / task_evaluate /
task_submit（拷贝漂移实证：hindsight 为 ``Z`` 后缀秒级，其余 ``+00:00``
微秒），本模块为唯一实现。

导入形态与 http_json 先例一致：插件侧把 plugins/shared 推上 sys.path 后
裸名导入（``from time_iso import now_iso_utc as _now_iso``）。

[来源: docs/working/修复方案_双审查报告_20260911.md 5.3]
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def now_iso_utc() -> str:
    """当前 UTC 时刻 ISO 8601（``+00:00`` 后缀微秒精度，多数派既有格式）。"""
    return datetime.now(timezone.utc).isoformat()


def iso_sort_key(value: str) -> tuple[int, Any]:
    """ISO 时间字符串 → 可排序键：可解析的按真实时刻排序，不可解析的回退字符串。

    兼容并存格式（``Z`` 后缀秒级存量 vs ``+00:00`` 微秒新写入）：直接字符串
    比较会因后缀字符序（'Z' > '.'）产生跨格式乱序，必须先解析归一。不可解析
    值归 (1, 原串) 桶，桶内保持字符串序（测试夹具等非法值不炸、相对序稳定）。
    """
    try:
        return (0, datetime.fromisoformat(value))
    except ValueError:
        return (1, value)

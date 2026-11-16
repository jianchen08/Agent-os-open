"""HLC 混合逻辑时钟 —— 多端同步的全序可比时间戳单点实现。

设计依据：ADR ``docs/decisions/2026-10-15-multi-device-sync-hlc-lww.md`` D2/D3——
共享面冲突裁决 = HLC-LWW（后改赢），"先后"必须跨端可信才允许 LWW 上场：

- **单调**：同一时钟实例发出的时间戳严格递增（本机只进不退，重启经状态恢复
  续接，不回退）；
- **保因果**：observe 过某远端时间戳之后发出的事件，其 HLC 严格大于该远端值
  （causally-after 可判定）；
- **防时钟漂**：NTP 跳变/人为改时只影响物理分量（取 wall clock 与内部状态
  的 max），逻辑计数器兜底单调——裸墙钟 LWW 已被该 ADR 否（Alternatives #3）。

全序：``(physical_ms, logical, device_id)`` 字典序——跨端同物理同逻辑毫秒由
device_id 决胜，使任意两枚时间戳恒可比（LWW 恒有唯一胜者）。

算法：Kulkarni et al. Hybrid Logical Clocks（issue/observe 两式），与
trace_exporter 的 ULID 单调发号（``ulid_keys.py``）同源于就绪约束 #3
"全 ULID 主键 + 单调时间戳"——ULID 管"是不是重复"，HLC 管"谁后"。

导入形态与 ``http_json`` 先例一致：插件侧把 ``plugins/shared`` 推上 sys.path
后裸名导入。不依赖第三方库（sidecar venv 标准库自足）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["HlcTimestamp", "HybridLogicalClock"]


@dataclass(frozen=True, order=True)
class HlcTimestamp:
    """一枚 HLC 时间戳（frozen：发出去的戳不可变）。

    ``order=True`` 按字段声明序 ``(physical_ms, logical, device_id)`` 字典序
    全序比较——LWW 裁决直接用 ``>``/``<``。
    """

    physical_ms: int
    logical: int
    device_id: str

    def to_dict(self) -> dict[str, Any]:
        """线格式（sync_event.hlc 字段载荷）。"""
        return {"physical_ms": self.physical_ms, "logical": self.logical, "device_id": self.device_id}

    @classmethod
    def from_dict(cls, raw: Any) -> HlcTimestamp:
        """从线格式解析；形态违约抛 ValueError（schema 校验错惯例）。"""
        if not isinstance(raw, dict):
            raise ValueError(f"hlc 必须是对象，得到 {type(raw).__name__}")
        physical = raw.get("physical_ms")
        logical = raw.get("logical")
        device = raw.get("device_id")
        if isinstance(physical, bool) or not isinstance(physical, int) or physical < 0:
            raise ValueError("hlc.physical_ms 必须是非负整数")
        if isinstance(logical, bool) or not isinstance(logical, int) or logical < 0:
            raise ValueError("hlc.logical 必须是非负整数")
        if not isinstance(device, str) or not device:
            raise ValueError("hlc.device_id 必须是非空字符串")
        return cls(physical_ms=physical, logical=logical, device_id=device)

    @property
    def sort_key(self) -> tuple[int, int, str]:
        """全序键（存储排序 / SQL 侧同口径排序用）。"""
        return (self.physical_ms, self.logical, self.device_id)


def _default_clock_ms() -> int:
    return int(time.time() * 1000)


class HybridLogicalClock:
    """每端一个的 HLC 状态对象（sync_agent 自持，ADR D6/D3）。

    Args:
        device_id: 本端设备标识（进入全序决胜位）。
        clock_ms: 物理毫秒钟（可注入 fake clock 测试时序不变量；运行态 wall clock）。
        initial: 恢复的 ``(physical_ms, logical)`` 状态（agent 状态库持久化值，
            保证跨重启单调续接）；缺省从零起步（首端首事件物理分量即取当前钟）。
    """

    def __init__(
        self,
        device_id: str,
        *,
        clock_ms: Callable[[], int] | None = None,
        initial: tuple[int, int] | None = None,
    ) -> None:
        if not device_id:
            raise ValueError("HLC 需要 device_id（全序决胜位，不允许空）")
        self._device_id = device_id
        self._clock_ms = clock_ms if clock_ms is not None else _default_clock_ms
        self._physical = initial[0] if initial is not None else 0
        self._logical = initial[1] if initial is not None else 0

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def state(self) -> tuple[int, int]:
        """当前 ``(physical_ms, logical)``（持久化恢复用）。"""
        return (self._physical, self._logical)

    def now(self) -> HlcTimestamp:
        """本地事件打戳（严格大于本实例此前发出的任何一枚）。"""
        pt = self._clock_ms()
        if pt > self._physical:
            self._physical, self._logical = pt, 0
        else:
            # wall 钟停走/回拨：物理分量守住原值，逻辑计数器兜底单调。
            self._logical += 1
        return HlcTimestamp(self._physical, self._logical, self._device_id)

    def observe(self, remote: HlcTimestamp) -> HlcTimestamp:
        """观察到远端时间戳后推进本地状态（保因果），返回推进后的状态戳。

        保证：返回值 > remote，且 > 本实例此前任何状态（issue/observe 后本机
        后续事件 causally-after 该远端事件）。
        """
        pt = self._clock_ms()
        physical = max(self._physical, remote.physical_ms, pt)
        if physical == self._physical == remote.physical_ms:
            logical = max(self._logical, remote.logical) + 1
        elif physical == self._physical:
            logical = self._logical + 1
        elif physical == remote.physical_ms:
            logical = remote.logical + 1
        else:
            logical = 0
        self._physical, self._logical = physical, logical
        return HlcTimestamp(self._physical, self._logical, self._device_id)

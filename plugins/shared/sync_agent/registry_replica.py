"""sync_agent 端侧注册表副本 —— 全网设备注册表的本机持久复制（A 档对等信任）。

定位（ADR 2026-10-22-distributed-registry-peer-trust）：每台设备持有全网设备
注册表副本（含各设备 per-device token 的 sha256 哈希）；目标端借权核验先本地
比对（``hash(presented) == 副本条目 token_hash``），中枢退化为分发通道——已
复制到本机即可核验，非在线依赖。

生命周期：引擎周期拉取 ``GET {relay_url}/registry`` 刷新（节奏 = 心跳间隔，
独立退避），原子落盘（临时文件 rename，断电不损旧副本）；中枢离线/重启后旧
副本继续可用。撤销语义：设备被全网表剔除后经下一轮复制传播（≤1 心跳周期），
窗口内旧副本仍可核验通过——同注册表信任边界（入网码物理递手）下的 eventual
一致，不引入在线撤销依赖（那正是本档要消除的）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any


def token_sha256(raw: str) -> str:
    """per-device token → sha256 摘要（sync_relay/relay_core 同款孪生实现——
    播种件禁共享根跨包 import，改动两处必须同刀）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RegistryReplica:
    """注册表副本持有面：持久 JSON 文件 + 本地对等核验。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any] | None:
        """副本整体读取（``{fetched_at, devices}``）；缺失/损坏/形态不对 = None。"""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def save(self, *, devices: list[dict[str, Any]], fetched_at: str) -> None:
        """原子落盘（tmp + rename）；目录惰性创建。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps({"fetched_at": fetched_at, "devices": devices}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def devices(self) -> list[dict[str, Any]]:
        """副本设备条目（仅 dict 形态；空副本/缺失 = 空表）。"""
        data = self.load()
        if data is None:
            return []
        raw = data.get("devices")
        if not isinstance(raw, list):
            return []
        return [entry for entry in raw if isinstance(entry, dict)]

    def fetched_at(self) -> str:
        """副本拉取时刻（呈现面新鲜度）；无副本 = 空串。"""
        data = self.load()
        return str(data.get("fetched_at") or "") if data is not None else ""

    def verify(self, bearer: str) -> str | None:
        """本地对等核验：sha256(presented) 与某条目 token_hash 恒等比对 → 设备 id。

        未命中（含空副本/无哈希存量条目）返回 None——裁决归调用方（relay
        /verify 回退面），本方法只给本地事实。
        """
        if not bearer:
            return None
        digest = token_sha256(bearer)
        for entry in self.devices():
            stored = str(entry.get("token_hash") or "")
            if stored and hmac.compare_digest(stored, digest):
                return str(entry.get("device_id") or "")
        return None

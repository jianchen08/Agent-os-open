"""统一路径树编址记法 —— 设备前缀编址的对照纯函数（D9' 编址 / D16 规范记法单源）。

编址契约（用户拍板③，2026-10-17）：全局统一按**设备前缀**编址——
``/<device_id>/<相对路径>``（别名见 tree_alias），所有设备看到的树**完全一致**；
本端设备无编址特殊性（同样有自己的固定前缀），只是读写自身前缀**零网络开销**
（本地执行）；不做 /local/ 类特殊前缀（"本地"逐端歧义已否）。

现状（ADR 2026-10-18-unified-namespace-fs-mount-and-mcp-transfer D14/D16）：
路径统一承载 = OS 文件系统挂载，运行时零路径翻译服务——本模块**无运行时
消费面**，保留作 D16 统一编址规范记法与导出声明（allowed_roots 平移）的
对照单源；前缀替换记法（``/<id>/rel`` ↔ ``<子树根>/rel``）即挂载锚点语义的
记法对照。

掉盘语义（诚实不可用）：属主离线 = 该子树不可达，归属仍可答、可达性如实呈现
（无本地副本，不抛错）；本端前缀可达性不依赖注册表心跳（本地执行零网络）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

_SEPARATOR = "/"


@dataclass(frozen=True)
class PathResolution:
    """路径解析结果（诚实语义：归属/翻译/可达性/说明齐备）。"""

    path: str
    owner_device: str | None = None  # 命中的设备前缀属主（None = 不在统一树上）
    prefix: str = ""  # 命中的设备前缀（device_id 或别名）
    relative_path: str = ""  # 前缀后的相对路径（统一分隔符）
    real_path: str = ""  # 属主端真实路径（一次翻译，执行面契约）
    online: bool = False
    reachable: bool = False
    local: bool = False  # 本端前缀 = 零网络开销本地执行
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "owner_device": self.owner_device,
            "prefix": self.prefix,
            "relative_path": self.relative_path,
            "real_path": self.real_path,
            "online": self.online,
            "reachable": self.reachable,
            "local": self.local,
            "note": self.note,
        }


def resolve_path(
    path: str,
    view: dict[str, Any],
    *,
    self_device_id: str = "",
) -> PathResolution:
    """统一编址路径 → 属主设备 + 属主端真实路径（前缀替换一次翻译）。

    前缀匹配序：device_id 精确 → tree_alias 精确（别名须唯一，歧义诚实拒）。
    本端前缀 = 本地执行（可达性不依赖注册表心跳）；远端前缀 reachable = online
    （掉盘诚实呈现）。
    """
    if not path or path == _SEPARATOR:
        return PathResolution(path=path, note="空路径（无前缀归属）")
    parts = [p for p in path.split(_SEPARATOR) if p]
    if not parts:
        return PathResolution(path=path, note="空路径（无前缀归属）")
    prefix, relative = parts[0], _SEPARATOR.join(parts[1:])

    hits: list[tuple[dict[str, Any], bool]] = []  # (device, is_alias)
    for device in view.get("devices", []):
        device_id = str(device.get("device_id", ""))
        if not device_id:
            continue
        if device_id == prefix:
            hits.append((device, False))
            continue
        alias = ((device.get("capabilities") or {}).get("exec_channel") or {}).get("tree_alias")
        if alias and str(alias) == prefix:
            hits.append((device, True))
    if not hits:
        return PathResolution(path=path, note=f"未知前缀 {prefix}（不在统一树上）")
    if len(hits) > 1:
        return PathResolution(path=path, note=f"前缀 {prefix} 歧义（多设备声明同名别名，配置错误，拒绝路由）")
    device, _is_alias = hits[0]
    device_id = str(device["device_id"])
    channel = (device.get("capabilities") or {}).get("exec_channel") or {}
    root = _subtree_root(channel)
    if not root:
        return PathResolution(
            path=path,
            note=f"设备 {device_id} 未声明子树根（exec_channel 缺 allowed_roots/tree_root，不构成可执行地盘）",
        )
    real_path = os.path.join(root, *relative.split(_SEPARATOR)) if relative else root
    is_self = device_id == self_device_id
    online = bool(device.get("online", False))
    reachable = True if is_self else online
    if is_self:
        note = "本端前缀：零网络开销，本地执行"
    elif online:
        note = "子树可达（远端执行，路径已按属主端真实路径翻译）"
    else:
        note = "掉盘：属主离线，该子树暂不可用（诚实不可用，无本地副本）"
    return PathResolution(
        path=path,
        owner_device=device_id,
        prefix=prefix,
        relative_path=relative,
        real_path=real_path,
        online=online,
        reachable=reachable,
        local=is_self,
        note=note,
    )


def _subtree_root(channel: dict[str, Any]) -> str:
    """子树根：tree_root 显式声明优先，缺省取 allowed_roots 首根（单根契约）。"""
    tree_root = str(channel.get("tree_root", "") or "")
    if tree_root:
        return tree_root
    roots = channel.get("allowed_roots") or []
    return str(roots[0]) if roots else ""

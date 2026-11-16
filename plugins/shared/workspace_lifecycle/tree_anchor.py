"""统一路径树锚定 —— 工作空间真实路径 → D16 设备前缀编址记法。

契约（ADR 2026-10-17-unified-path-tree-and-capability-plane D9' 编址语义；
实现层现状见 ADR 2026-10-18-unified-namespace-fs-mount-and-mcp-transfer）：
- 编址 = 全局统一设备前缀 ``/<device_id>/<相对路径>``，所有设备看到的树完全
  一致；本端设备无编址特殊性，只是"离你最近的前缀"。
- 本函数与 tree_core.resolve_path（D16 统一编址规范记法对照单源）互为逆运算
  的记法对照：工作空间创建期把真实路径锚定成树上的地址，落 ws_meta 坐标
  单键——tree_path 作跨端引用/寻址的对照元数据；运行时路径统一承载 =
  OS 文件系统挂载（D14），锚定不参与任何运行时解析。
- 子树根约定 = 工作空间基目录（会话与任务工作空间的共同父目录）；设备对该
  根的领地声明（exec_channel.allowed_roots 覆盖，平移为导出声明）是部署面，
  锚定不虚构——路径不在基根下 = tree_path 省略（None），诚实性不受影响。

纯函数零副作用；编址失败（空输入/越根/跨盘）一律返回 None 由调用方省略，
绝不抛错阻断工作空间创建。
"""

from __future__ import annotations

import os
from pathlib import Path

_SEPARATOR = "/"


def workspace_tree_path(workspace_path: str, *, device_id: str, subtree_root: str) -> str | None:
    """真实工作区路径 → 统一路径树地址 ``/<device_id>/<相对路径>``。

    不在子树根下 / 输入退化（空 device_id、空根、相对路径、device_id 含
    路径分隔符）/ 跨盘无法相对化 = None（调用方省略 tree_path）。
    工作区 == 子树根本身 → 裸前缀 ``/<device_id>``。
    """
    device = device_id.strip()
    if not device or _SEPARATOR in device or device in {".", ".."}:
        return None
    if not subtree_root or not workspace_path:
        return None
    ws = Path(workspace_path)
    root = Path(subtree_root)
    if not (ws.is_absolute() and root.is_absolute()):
        return None
    try:
        rel = os.path.relpath(ws, root)
    except ValueError:  # pragma: no cover —— Windows 跨盘符才触发（POSIX 无此
        # 异常形态），防御面按仓规排除出覆盖度量（file_channel 同律）
        return None
    rel_posix = Path(rel).as_posix()
    if rel_posix == ".":
        return _SEPARATOR + device
    if rel_posix.startswith("..") or Path(rel_posix).is_absolute():
        return None
    return _SEPARATOR + device + _SEPARATOR + rel_posix

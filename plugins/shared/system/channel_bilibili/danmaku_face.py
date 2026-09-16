"""blivedm 弹幕面（可选依赖，缺失优雅降级——插件装载不炸）。

真连接属 T3.1 外部依赖（需用户 B 站 cookie）；本模块只保证：
- blivedm 可导入且 cookie 已配 → available=True，真连接留真机联调；
- 任一条件缺失 → available=False + reason（director 侧按"无人弹幕"降级）。
"""

from __future__ import annotations

from typing import Any

try:  # 可选依赖：venv 未装 blivedm 时通道其余能力不受影响
    import blivedm  # type: ignore[import-not-found] # noqa: F401

    _BLIVEDM_AVAILABLE = True
    _IMPORT_REASON = ""
except ImportError as _exc:  # pragma: no cover - 视环境而定
    _BLIVEDM_AVAILABLE = False
    _IMPORT_REASON = f"blivedm 未安装: {_exc}"


class DanmakuFace:
    """弹幕面可用性判定（真连接的 start/stop 留 T3.1 真机联调）。"""

    def __init__(self, cookie: str | None, room_id: str) -> None:
        self._cookie = cookie or ""
        self._room_id = room_id

    def status(self) -> dict[str, Any]:
        """可用性状态：available + reason（不可用时）。"""
        if not _BLIVEDM_AVAILABLE:
            return {"available": False, "reason": _IMPORT_REASON}
        if not self._cookie:
            return {"available": False, "reason": "COOKIE_UNSET"}
        return {"available": True, "room_id": self._room_id}

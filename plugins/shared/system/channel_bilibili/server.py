#!/usr/bin/env python3
"""channel_bilibili 服务面——B 站直播通道。

配置来源（环境变量，不入仓）：
- AGENTOS_BILI_COOKIE：B 站登录 cookie（含 SESSDATA 与 bili_jct）
- AGENTOS_BILI_ROOM_ID：直播间房间号

未配置时各写面服务显式报错（COOKIE_UNSET / ROOM_UNSET）——不降级假开播。
弹幕真连接（blivedm）属真机联调项；本服务面只暴露可用性状态。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("channel_bilibili")


def _load(filename: str, mod_name: str) -> Any:
    here = str(Path(__file__).parent)
    sys.path.insert(0, here)
    try:
        spec = importlib.util.spec_from_file_location(mod_name, Path(here) / filename)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    finally:
        while here in sys.path:
            sys.path.remove(here)
    return mod


_bc = _load("bili_client.py", "bili_client")
_df = _load("danmaku_face.py", "danmaku_face")


def _client() -> Any:
    cookie = os.environ.get("AGENTOS_BILI_COOKIE")
    room_id = os.environ.get("AGENTOS_BILI_ROOM_ID", "")
    if not room_id:
        raise _bc.BiliApiError("ROOM_UNSET: 未配置 AGENTOS_BILI_ROOM_ID")
    return _bc.BiliLiveClient(cookie=cookie, room_id=room_id)


@plugin.tool(
    name="bili.start_live",
    schema={"type": "object", "properties": {}},
    description="开播：返回 RTMP 推流地址与推流码",
)
async def start_live() -> dict[str, Any]:
    """开播。"""
    result = await _client().start_live()
    return {"addr": result["addr"], "code": result["code"]}


@plugin.tool(
    name="bili.stop_live",
    schema={"type": "object", "properties": {}},
    description="下播",
)
async def stop_live() -> dict[str, Any]:
    """下播。"""
    await _client().stop_live()
    return {"ok": True}


@plugin.tool(
    name="bili.update_title",
    schema={
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    },
    description="改标题（[AI] 前缀纪律：缺失自动补）",
)
async def update_title(title: str = "") -> dict[str, Any]:
    """改标题。"""
    clean = title.strip()
    if not clean.startswith("[AI]"):
        clean = "[AI] " + clean
    await _client().update_title(clean)
    return {"ok": True, "title": clean}


@plugin.tool(
    name="bili.get_popularity",
    schema={"type": "object", "properties": {}},
    description="房间人气值轮询",
)
async def get_popularity() -> dict[str, Any]:
    """人气轮询。"""
    value = await _client().get_popularity()
    return {"popularity": value}


@plugin.tool(
    name="bili.danmaku_status",
    schema={"type": "object", "properties": {}},
    description="弹幕面可用性（blivedm/cookie 缺失优雅降级）",
)
async def danmaku_status() -> dict[str, Any]:
    """弹幕面状态。"""
    face = _df.DanmakuFace(
        cookie=os.environ.get("AGENTOS_BILI_COOKIE"),
        room_id=os.environ.get("AGENTOS_BILI_ROOM_ID", ""),
    )
    return face.status()


if __name__ == "__main__":
    plugin.run()

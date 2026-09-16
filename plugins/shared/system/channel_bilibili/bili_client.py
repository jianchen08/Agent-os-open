"""B 站直播 API 客户端（channel_bilibili 开播面）。

契约：
- B 站信封 {code, message, data}：code=0 成功，非 0 抛 BiliApiError（带平台 message）。
- Cookie/CSRF 走构造参数（server.py 从 AGENTOS_BILI_COOKIE 注入，不入仓）；
  未配置显式 COOKIE_UNSET——不降级假开播。
- 接口路径按 B 站公开惯例（api.live.bilibili.com）记录在端点常量，
  真机联调（T3.1/T3.4）时校准；传输可注入（request_fn）保证全测。
- 标题纪律（D2）：update_title 强制 [AI] 前缀——缺失自动补、已有不重复。
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

_API_BASE = "https://api.live.bilibili.com"
_ENDPOINT_START = "/room/v1/Room/startLive"
_ENDPOINT_STOP = "/room/v1/Room/stopLive"
_ENDPOINT_TITLE = "/room/v1/Room/updateRoomTitle"
_ENDPOINT_INFO = "/xlive/web-room/v1/index/getInfoByRoom"
_AI_PREFIX = "[AI] "


class BiliApiError(Exception):
    """B 站 API 调用失败（信封非 0 / 配置缺失 / 传输异常）。"""


class TransportFn(Protocol):
    """传输协议：form 表单 POST（B 站直播 API 惯例）。"""

    async def __call__(
        self, method: str, path: str, *, form: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


def _httpx_transport(cookie: str) -> TransportFn:
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (AgentOS channel_bilibili)",
        "Referer": "https://live.bilibili.com",
    }

    async def call(
        method: str, path: str, *, form: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=_API_BASE, headers=headers, timeout=15.0
        ) as client:
            resp = await client.request(method, path, data=form)
            try:
                body: dict[str, Any] = resp.json()
            except ValueError as exc:
                raise BiliApiError(f"NON_JSON:{resp.status_code}") from exc
            return body

    return call


class BiliLiveClient:
    """B 站直播客户端：开播/下播/改标题/人气。"""

    def __init__(
        self,
        cookie: str | None,
        room_id: str,
        request_fn: TransportFn | None = None,
    ) -> None:
        self._cookie = cookie or ""
        self._room_id = room_id
        self._csrf = self._extract_csrf(self._cookie)
        self._request: TransportFn = request_fn or _httpx_transport(self._cookie)

    @staticmethod
    def _extract_csrf(cookie: str) -> str:
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("bili_jct="):
                return part[len("bili_jct="):]
        return ""

    def _require_cookie(self) -> None:
        if not self._cookie or not self._csrf:
            raise BiliApiError(
                "COOKIE_UNSET: 未配置 AGENTOS_BILI_COOKIE（含 SESSDATA 与 bili_jct）"
            )

    async def _envelope(
        self, path: str, form: dict[str, Any]
    ) -> dict[str, Any]:
        body = await self._request("POST", path, form=form)
        if body.get("code") != 0:
            raise BiliApiError(f"PLATFORM_ERROR:{body.get('code')} {body.get('message', '')}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise BiliApiError(f"BAD_ENVELOPE:{path} data 缺失")
        return data

    async def start_live(self) -> dict[str, Any]:
        """开播，返回 {addr, code}（RTMP 推流地址与推流码）。"""
        self._require_cookie()
        data = await self._envelope(_ENDPOINT_START, {
            "room_id": self._room_id,
            "platform": "pc",
            "csrf_token": self._csrf,
            "csrf": self._csrf,
        })
        rtmp = data.get("rtmp", {})
        return {"addr": str(rtmp.get("addr", "")), "code": str(rtmp.get("code", ""))}

    async def stop_live(self) -> None:
        """下播。"""
        self._require_cookie()
        await self._envelope(_ENDPOINT_STOP, {
            "room_id": self._room_id,
            "csrf_token": self._csrf,
        })

    async def update_title(self, title: str) -> None:
        """改标题（[AI] 前缀纪律：缺失自动补）。"""
        self._require_cookie()
        clean = title.strip()
        if not clean.startswith("[AI]"):
            clean = _AI_PREFIX + clean
        await self._envelope(_ENDPOINT_TITLE, {
            "room_id": self._room_id,
            "title": clean,
            "csrf_token": self._csrf,
        })

    async def get_popularity(self) -> int:
        """房间人气值（无需 cookie 的公开读面）。"""
        body = await self._request("GET", f"{_ENDPOINT_INFO}?room_id={self._room_id}")
        if body.get("code") != 0:
            raise BiliApiError(f"PLATFORM_ERROR:{body.get('code')} {body.get('message', '')}")
        room_info = body.get("data", {}).get("room_info", {})
        return int(room_info.get("popularity", 0))

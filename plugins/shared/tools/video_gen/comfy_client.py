"""ComfyUI HTTP 客户端（video_gen 适配层的传输面）。

协议（细化设计 §4.2）：
- POST /prompt {"prompt": <workflow API JSON>} -> {"prompt_id": str}
- GET /history/{prompt_id} -> {prompt_id: {"status": {"completed", "status_str"}, "outputs": ...}}
- GET /view?filename=&subfolder=&type=output -> 视频字节

可注入性（测试防拟合 + 时序测试纪律）：
- request_fn：异步传输函数 `(method, path, *, json_body) -> (status, body)`；
  /view 返回 bytes，其余返回已解析 JSON（dict）。
- sleep_fn / monotonic_fn：时钟注入，测试用 FakeClock 计数推进，禁止零延迟假等待。

显性妥协：轮询走短连接（每 poll 新建连接），P0-T2 真机联调后再评估
websocket 订阅；超时/重试语义由上层 tool.py 负责，本层只报事实。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx


class ComfyUIError(Exception):
    """ComfyUI 调用失败（code ∈ submit/poll/view 的失败语义）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class TransportFn(Protocol):
    """异步传输函数协议。"""

    async def __call__(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> tuple[int, Any]: ...


MonotonicFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


def _httpx_transport(
    base_url: str, token: str | None
) -> TransportFn:
    """默认 httpx 传输。/view 返回 (status, bytes)，其余返回 (status, json|text)。"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def call(
        method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        async with httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30.0
        ) as client:
            resp = await client.request(method, path, json=json_body)
            if path.startswith("/view"):
                return resp.status_code, resp.content
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text
            return resp.status_code, body

    return call


def _find_video(outputs: dict[str, Any]) -> dict[str, Any] | None:
    """从 history.outputs 中找第一个带 filename 的产物条目。

    ComfyUI 产物键名随节点不同（videos/images/gifs），此处按"值是列表且
    元素含 filename"的形状扫描，不绑定节点 id——真实图连通后 P0-T2 校准。
    """
    for node in outputs.values():
        if not isinstance(node, dict):
            continue
        for value in node.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                if "filename" in value[0]:
                    return value[0]
    return None


class ComfyUIClient:
    """ComfyUI 客户端：提交工作流 → 轮询 history → 取片。"""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        request_fn: TransportFn | None = None,
        sleep_fn: SleepFn | None = None,
        monotonic_fn: MonotonicFn | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._request: TransportFn = request_fn or _httpx_transport(base_url, token)
        self._sleep: SleepFn = sleep_fn or self._default_sleep
        self._monotonic: MonotonicFn = monotonic_fn or self._default_monotonic

    @staticmethod
    async def _default_sleep(seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    @staticmethod
    def _default_monotonic() -> float:
        import time

        return time.monotonic()

    async def submit(self, workflow: dict[str, Any]) -> str:
        """提交工作流，返回 prompt_id。非 200 或缺 prompt_id 视为失败。"""
        status, body = await self._request(
            "POST", "/prompt", json_body={"prompt": workflow, "client_id": "agentos-video-gen"}
        )
        if status != 200 or not isinstance(body, dict) or not body.get("prompt_id"):
            raise ComfyUIError("SUBMIT_REJECTED", str(body)[:500])
        return str(body["prompt_id"])

    async def wait(
        self, prompt_id: str, timeout_secs: float, poll_interval_secs: float = 2.0
    ) -> dict[str, Any]:
        """轮询 history 至完成，返回 outputs。超时/失败态抛 ComfyUIError。

        轮询间隔真实等待（sleep_fn 可注入），deadline 由 monotonic_fn 判定。
        """
        deadline = self._monotonic() + timeout_secs
        while True:
            status, body = await self._request("GET", f"/history/{prompt_id}")
            if status == 200 and isinstance(body, dict) and prompt_id in body:
                entry = body[prompt_id]
                status_info = entry.get("status", {})
                if status_info.get("status_str") == "error":
                    raise ComfyUIError("GENERATION_ERROR", str(status_info)[:500])
                if status_info.get("completed"):
                    return entry.get("outputs", {}) or {}
            if self._monotonic() >= deadline:
                raise ComfyUIError("TIMEOUT", f"prompt_id={prompt_id}")
            await self._sleep(poll_interval_secs)

    async def fetch_file(self, item: dict[str, Any]) -> bytes:
        """按 history 产物条目取文件字节。"""
        params = (
            f"filename={item['filename']}"
            f"&subfolder={item.get('subfolder', '')}"
            f"&type={item.get('type', 'output')}"
        )
        status, body = await self._request("GET", f"/view?{params}")
        if status != 200 or not isinstance(body, bytes):
            raise ComfyUIError("VIEW_FAILED", f"status={status}")
        return body

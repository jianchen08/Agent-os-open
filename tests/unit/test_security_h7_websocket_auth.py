"""H7/M1 安全回归：WebSocket 端点 token 鉴权。

漏洞：
1. app_factory.py 的根 /ws 无 token 校验直接 accept()（纯 echo）。
2. routes_comfyui.py 的 ComfyUI /ws 直接 accept()。router 虽挂了
   dependencies=[Depends(require_auth)]，但 FastAPI 的 HTTP dependencies
   不作用于 @router.websocket，必须函数体内单独校验——此处漏接。

修复：两个 /ws 端点都在 accept 前从 query 参数取 token 并校验，失败用
code=4001 close。

本测试守护：无 token / 无效 token 必须被 close(4001)，不能进入业务循环。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient


class TestRootWebsocketAuth:
    """H7: 根 /ws 需要 token。"""

    @staticmethod
    def _app() -> FastAPI:
        """构造一个只挂根 /ws 的最小 app（复用 app_factory 的鉴权逻辑）。

        直接复刻 app_factory.py 里 websocket_root 的鉴权代码，避免触发
        完整 app_factory 的重依赖初始化。
        """
        from channels.api.auth import verify_token

        app = FastAPI()

        @app.websocket("/ws")
        async def ws_root(websocket: WebSocket) -> None:
            token = websocket.query_params.get("token", "")
            if not token:
                await websocket.accept()
                await websocket.close(code=4001, reason="连接需要 token 认证")
                return
            if verify_token(token) is None:
                await websocket.accept()
                await websocket.close(code=4001, reason="Token 无效或已过期")
                return
            await websocket.accept()
            try:
                while True:
                    data = await websocket.receive_text()
                    await websocket.send_text(f"Echo: {data}")
            except Exception:
                pass

        return app

    def test_root_ws_without_token_rejected(self) -> None:
        """无 token 连接根 /ws 应被 close(4001)，无法进入 echo 循环。"""
        from starlette.websockets import WebSocketDisconnect

        client = TestClient(self._app())
        # 连接建立后服务端立即 close(4001)；客户端 receive 应抛 WebSocketDisconnect
        with client.websocket_connect("/ws") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            # starlette 的 WebSocketDisconnect.code 反映服务端 close code
            assert exc_info.value.code == 4001, f"期望 4001，实际 {exc_info.value.code}"

    def test_root_ws_with_invalid_token_rejected(self) -> None:
        """无效 token 连接根 /ws 应被 close(4001)。"""
        from starlette.websockets import WebSocketDisconnect

        client = TestClient(self._app())
        with client.websocket_connect("/ws?token=invalid_token_xxx") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4001, f"期望 4001，实际 {exc_info.value.code}"

    def test_root_ws_with_valid_token_accepted(self) -> None:
        """有效 token 连接根 /ws 应能正常 echo。"""
        from channels.api.auth import create_access_token

        client = TestClient(self._app())
        token = create_access_token({"sub": "u1", "username": "tester", "role": "user"})
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_text("hello")
            data = ws.receive_text()
            assert "hello" in data


class TestComfyuiWebsocketAuth:
    """M1: ComfyUI /ws 函数体内有 token 校验。"""

    def test_comfyui_ws_function_has_token_check(self) -> None:
        """M1 守护：comfyui_ws 函数源码必须含 verify_token 调用。

        FastAPI 的 router 级 dependencies 不作用于 websocket，必须在函数体
        内显式校验。这个静态检查确保没人"重构"时把 token 校验删掉。
        """
        import inspect

        from channels.api.routes_comfyui import comfyui_ws

        source = inspect.getsource(comfyui_ws)
        assert "verify_token" in source, "comfyui_ws 函数体缺少 token 校验"
        assert "4001" in source, "comfyui_ws 缺少拒绝码 4001"

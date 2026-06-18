"""
Agent OS Frontend Static Server
用 Python 托管前端静态文件，支持 SPA 路由回退和 API/WebSocket 反向代理
替代 nginx（因为 Docker Hub 无法拉取 nginx 镜像）
"""

import os
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
import httpx
import asyncio
import websockets

app = FastAPI()

# 静态文件目录
STATIC_DIR = Path("/app/dist")
INDEX_HTML = STATIC_DIR / "index.html"

# 后端服务地址
BACKEND_URL = os.environ.get("BACKEND_URL", "http://agent:8000")
BACKEND_WS_URL = os.environ.get("BACKEND_WS_URL", "ws://agent:8000")

# HTTP 客户端
client = httpx.AsyncClient(timeout=300.0)

# 代理后端时的连接重试次数（吸收后端启动竞态：前端启动后到后端 uvicorn
# listen 之间的窗口期，连不上应短暂重试而非直接 500）。
# 总尝试次数 = PROXY_CONNECT_RETRIES + 1，指数退避。
PROXY_CONNECT_RETRIES = int(os.environ.get("PROXY_CONNECT_RETRIES", "3"))
PROXY_CONNECT_BACKOFF = float(os.environ.get("PROXY_CONNECT_BACKOFF", "0.5"))


async def _request_with_connect_retry(
    method: str,
    url: str,
    *,
    content: bytes | None = None,
    headers: dict | None = None,
    params: dict | None = None,
    follow_redirects: bool = False,
) -> httpx.Response:
    """对后端 HTTP 请求做启动期连接重试。

    仅重试 httpx.ConnectError / httpx.ConnectTimeout（后端端口未就绪），
    一旦连接建立（拿到任何响应或非连接类错误）立即返回，不重试业务错误。
    """
    last_exc: Exception | None = None
    for attempt in range(PROXY_CONNECT_RETRIES + 1):
        try:
            return await client.request(
                method=method,
                url=url,
                content=content,
                headers=headers or {},
                params=params,
                follow_redirects=follow_redirects,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_exc = exc
            if attempt >= PROXY_CONNECT_RETRIES:
                break
            await asyncio.sleep(PROXY_CONNECT_BACKOFF * (2 ** attempt))
    # 重试用尽，抛出最后的连接异常
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# 健康检查（必须放在 SPA 回退路由之前）
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# 静态文件服务
# ---------------------------------------------------------------------------
# 挂载 assets 目录（Vite 编译产物含 hash，可长期缓存）
app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")


@app.get("/vite.svg")
async def vite_svg():
    return FileResponse(str(STATIC_DIR / "vite.svg"))


@app.get("/inject.html")
async def inject_html():
    return FileResponse(str(STATIC_DIR / "inject.html"))


# ---------------------------------------------------------------------------
# API 反向代理 → 后端容器
# 用 Response 直接返回原始 bytes，不经过 JSON 序列化
# ---------------------------------------------------------------------------
@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_api(request: Request, path: str):
    """将 /api/* 请求代理到后端"""
    url = f"{BACKEND_URL}/api/{path}"
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    resp = await _request_with_connect_retry(
        method=request.method,
        url=url,
        content=body,
        headers=headers,
        params=dict(request.query_params),
    )

    # 过滤 hop-by-hop headers
    resp_headers = {}
    for k, v in resp.headers.items():
        if k.lower() not in ("content-encoding", "transfer-encoding", "content-length"):
            resp_headers[k] = v

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# WebSocket 代理 → 后端容器
# ---------------------------------------------------------------------------
@app.websocket("/ws/{path:path}")
async def proxy_websocket(websocket: WebSocket, path: str):
    """将 /ws/* 请求代理到后端 WebSocket，保留路径和 query string"""
    await websocket.accept()

    # 构建后端 WS URL，保留完整路径和 query string（含 token）
    backend_ws_url = f"{BACKEND_WS_URL}/ws/{path}"
    query_string = websocket.url.query
    if query_string:
        backend_ws_url += f"?{query_string}"

    try:
        async with websockets.connect(backend_ws_url) as backend_ws:
            # 双向转发
            async def client_to_backend():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await backend_ws.send(data)
                except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                    pass

            async def backend_to_client():
                try:
                    async for message in backend_ws:
                        await websocket.send_text(message)
                except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                    pass

            await asyncio.gather(
                client_to_backend(),
                backend_to_client(),
            )
    except Exception as e:
        print(f"[WS Proxy] 连接后端失败: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 媒体文件反向代理 → 后端容器
# ---------------------------------------------------------------------------
@app.api_route("/media/{path:path}", methods=["GET"])
async def proxy_media(request: Request, path: str):
    """将 /media/* 请求代理到后端"""
    url = f"{BACKEND_URL}/media/{path}"
    resp = await _request_with_connect_retry(
        method="GET",
        url=url,
        follow_redirects=True,
    )

    resp_headers = {}
    for k, v in resp.headers.items():
        if k.lower() not in ("content-encoding", "transfer-encoding", "content-length"):
            resp_headers[k] = v

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# SPA 路由回退（必须放在最后！）
# ---------------------------------------------------------------------------
@app.get("/{path:path}")
async def spa_fallback(path: str):
    """
    SPA 路由回退：任何未匹配的路径都返回 index.html
    让前端 Router 处理页面导航
    """
    # 先尝试匹配实际的静态文件
    file_path = STATIC_DIR / path
    if file_path.is_file():
        return FileResponse(str(file_path))
    # 否则回退到 index.html（SPA 路由）
    return FileResponse(str(INDEX_HTML))


# ---------------------------------------------------------------------------
# 本地开发直接运行
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    import asyncio as _asyncio

    async def _wait_for_backend():
        """等待后端就绪后再启动，避免代理层 500。"""
        for i in range(60):
            try:
                async with httpx.AsyncClient(timeout=3.0) as _tmp:
                    r = await _tmp.get(f"{BACKEND_URL}/health")
                    if r.status_code == 200:
                        print(f"[frontend] 后端就绪 (attempt {i+1})")
                        return
            except Exception:
                pass
            await _asyncio.sleep(1)
        print("[frontend] 警告: 后端 60 秒未就绪，仍将启动")

    _asyncio.run(_wait_for_backend())

    port = int(os.environ.get("PORT", "5188"))
    uvicorn.run(app, host="0.0.0.0", port=port)

"""
Agent OS Frontend Static Server
用 Python 托管前端静态文件，支持 SPA 路由回退和 API/WebSocket 反向代理
替代 nginx（因为 Docker Hub 无法拉取 nginx 镜像）
"""

import os
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
import httpx
import asyncio
import websockets

app = FastAPI()

# 静态文件目录（容器内默认 /app/dist；支持环境变量覆盖便于本地/测试）
STATIC_DIR = Path(os.environ.get("FRONTEND_DIST_DIR", "/app/dist"))
INDEX_HTML = STATIC_DIR / "index.html"

# 后端服务地址
BACKEND_URL = os.environ.get("BACKEND_URL", "http://agent:8000")
BACKEND_WS_URL = os.environ.get("BACKEND_WS_URL", "ws://agent:8000")

# HTTP 客户端
client = httpx.AsyncClient(timeout=300.0)

# 后端启动竞态吸收：前端容器启动时后端 uvicorn 可能还没 listen，此时第一个
# 请求会 ConnectError。_stream_proxy 在 except 里返回 502，前端重试即可恢复，
# 不再做启动期重试（旧 _request_with_connect_retry 已随全量读取写法一起移除）。


# ---------------------------------------------------------------------------
# 流式反向代理（业界标准写法）
# ---------------------------------------------------------------------------
# 旧的写法用 Response(content=resp.content)，会在事件循环主线程一次性把整个
# 后端响应体同步读进内存，触发大块堆分配（brk）。长时间运行后 Python 堆碎片化，
# 单次 brk 在内核里退化到秒级，阻塞主线程（asyncio 协作式调度，不让出即停转），
# 导致 /health、静态文件、所有请求排队 → 前端容器卡死。
#
# 正确做法：stream=True 让 httpx 不预读响应体；StreamingResponse + aiter_bytes
# 分块转发（默认 64KB/块），主线程每次只处理一个小 chunk，永不持有完整响应体，
# 不触发大 brk。同时用 background_tasks 确保 resp 在流结束后关闭，避免连接泄漏。
# httpx 的流式响应必须显式 close，见 https://www.python-httpx.org/async/#streaming-responses
_HOP_BY_HOP_HEADERS = frozenset(("content-encoding", "transfer-encoding", "content-length", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade"))


async def _stream_proxy(request: Request, url: str, *, follow_redirects: bool = False):
    """流式转发请求到后端，主线程永不持有完整响应体。

    用 client.send(request, stream=True) 拿到流式响应（不预读响应体），
    StreamingResponse 边 aiter_bytes 边转发。主线程每次只持有一个 chunk，
    不触发大块堆分配（brk）。流结束/中断时 finally 关闭 resp 防连接泄漏。

    Args:
        request: 入站请求（取 method/body/headers/query）
        url: 后端完整 URL
        follow_redirects: 是否跟随重定向（媒体/上传需要）

    Returns:
        StreamingResponse，分块转发后端响应体。
    """
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    # 构造 httpx.Request，用 client.send(stream=True) 拿流式响应（不预读响应体）
    upstream_req = client.build_request(
        request.method,
        url,
        content=body or None,
        headers=headers,
        params=dict(request.query_params),
    )
    # stream=True：httpx 不预读响应体，aiter_bytes 时才按需读取
    resp = await client.send(upstream_req, stream=True, follow_redirects=follow_redirects)

    # 过滤 hop-by-hop headers（代理不能透传这些）
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}

    async def _iter_bytes():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        _iter_bytes(),
        status_code=resp.status_code,
        headers=resp_headers,
    )


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
    """将 /api/* 请求流式代理到后端（主线程不做大块内存分配）。"""
    url = f"{BACKEND_URL}/api/{path}"
    try:
        return await _stream_proxy(request, url)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return JSONResponse({"detail": "后端服务不可达"}, status_code=502)


# ---------------------------------------------------------------------------
# WebSocket 代理 → 后端容器
# ---------------------------------------------------------------------------
async def _relay_websocket(websocket: WebSocket, path: str):
    """WebSocket 双向转发核心（被 /ws 和 /ws/{path} 共享）"""
    await websocket.accept()

    # 构建后端 WS URL，保留完整路径和 query string（含 token）
    backend_ws_url = f"{BACKEND_WS_URL}/ws"
    if path:
        backend_ws_url += f"/{path}"
    query_string = websocket.url.query
    if query_string:
        backend_ws_url += f"?{query_string}"

    try:
        async with websockets.connect(backend_ws_url) as backend_ws:
            async def _client_to_backend():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await backend_ws.send(data)
                except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                    pass

            async def _backend_to_client():
                try:
                    async for message in backend_ws:
                        await websocket.send_text(message)
                except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                    pass

            await asyncio.gather(
                _client_to_backend(),
                _backend_to_client(),
            )
    except Exception as e:
        print(f"[WS Proxy] 连接后端失败: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.websocket("/ws")
async def proxy_websocket_root(websocket: WebSocket):
    """将 /ws 代理到后端 WebSocket（无额外路径——前端浏览器直连的就是这个）"""
    await _relay_websocket(websocket, "")


@app.websocket("/ws/{path:path}")
async def proxy_websocket(websocket: WebSocket, path: str):
    """将 /ws/{path} 代理到后端 WebSocket，保留路径和 query string"""
    await _relay_websocket(websocket, path)


# ---------------------------------------------------------------------------
# 媒体文件反向代理 → 后端容器
# ---------------------------------------------------------------------------
@app.api_route("/media/{path:path}", methods=["GET"])
async def proxy_media(request: Request, path: str):
    """将 /media/* 请求流式代理到后端。"""
    url = f"{BACKEND_URL}/media/{path}"
    try:
        return await _stream_proxy(request, url, follow_redirects=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return JSONResponse({"detail": "后端服务不可达"}, status_code=502)


# ---------------------------------------------------------------------------
# 上传文件反向代理 → 后端容器（图片/文件等多模态附件）
# ---------------------------------------------------------------------------
@app.api_route("/uploads/{path:path}", methods=["GET"])
async def proxy_uploads(request: Request, path: str):
    """将 /uploads/* 请求流式代理到后端（用户上传的图片/文件）。"""
    url = f"{BACKEND_URL}/uploads/{path}"
    try:
        return await _stream_proxy(request, url, follow_redirects=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return JSONResponse({"detail": "后端服务不可达"}, status_code=502)


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
        # index.html 必须 no-cache：它引用的 JS 文件名带 hash（内容变即重建），
        # 但若浏览器缓存了旧 index.html，会一直加载旧 hash 的 JS，导致部署后
        # 前端改动不生效（启发式缓存）。assets 里带 hash 的文件可长期缓存。
        if file_path.name == "index.html":
            return FileResponse(str(file_path), headers={"Cache-Control": "no-cache"})
        return FileResponse(str(file_path))
    # 否则回退到 index.html（SPA 路由），同样禁用缓存
    return FileResponse(str(INDEX_HTML), headers={"Cache-Control": "no-cache"})


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
    # 强制使用标准 asyncio 事件循环，避免 uvloop（C 扩展）在 WSL2 虚拟化
    # CPU 上触发 SIGSEGV（退出码 139）。uvloop 性能略高，但稳定性不如纯 Python。
    uvicorn.run(app, host="0.0.0.0", port=port, loop="asyncio")

#!/usr/bin/env python3
"""channel_api 独立 HTTP server 启动入口。

方案 B：channel_api 作为通道插件，自己启动 FastAPI HTTP server，
提供全部 /api/v1/* 端点。前端直接连接此端口（和 0.1 一样）。
管道执行时通过 MCP capability 调内核（9100）。

启动方式：
    python run_server.py                  # 默认 8988
    python run_server.py --port 8988      # 指定端口

环境变量：
    BACKEND_PORT  端口（默认 8988）
    BACKEND_HOST  绑定地址（默认 0.0.0.0）
"""

from __future__ import annotations

import argparse
import os
import sys

# 把 channel_api 目录加进 path（让 routes_*、deps、models 等平铺模块可 import）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

# 把项目根加进 path（让 src.auth.token 等 from src.xxx import 能工作——src/ 内部代码的引用约定）
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 切换工作目录到项目根（让 .env 能被读到——memory_store 从 .env 读 DEFAULT_ADMIN_PASSWORD）
os.chdir(_PROJECT_ROOT)

# 同时把 src/ 也加进（让不带 src 前缀的 import 如 from config.settings 也能工作）
_SRC_ROOT = os.path.join(_PROJECT_ROOT, "src")
if os.path.isdir(_SRC_ROOT) and _SRC_ROOT not in sys.path:
    sys.path.append(_SRC_ROOT)


def main() -> None:
    """启动 channel_api HTTP server。"""
    parser = argparse.ArgumentParser(description="AgentOS channel_api HTTP server")
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    parser.add_argument("--host", default=None, help="绑定地址")
    args = parser.parse_args()

    port = args.port or int(os.environ.get("BACKEND_PORT", "8988"))
    host = args.host or os.environ.get("BACKEND_HOST", "0.0.0.0")

    # 延迟 import（确保 sys.path 已设置）
    import uvicorn  # noqa: PLC0415

    from app import create_app  # noqa: PLC0415

    print(f"channel_api HTTP server starting on {host}:{port}")
    print(f"  API: http://127.0.0.1:{port}")
    print(f"  Health: http://127.0.0.1:{port}/health")
    print(f"  Docs: http://127.0.0.1:{port}/docs")
    print(f"  src: {_SRC_ROOT}")

    app = create_app()
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        timeout_keep_alive=120,
        ws_ping_interval=30.0,
        ws_ping_timeout=60.0,
    )


if __name__ == "__main__":
    main()

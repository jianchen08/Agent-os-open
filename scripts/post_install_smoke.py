#!/usr/bin/env python3
"""装后冒烟（部署冒烟范式：装完必自动验，替代人肉开 GUI 点一遍）。

设计：docs/working/打包发版循环问题与方案_20260929.md §6 #5（治装回裸奔、
A3 安装位形态漂移）。对已拉起的装机内核做三步 API 冒烟，退出码供安装脚本
消费（装回 checklist 末步）：

  1. 登录探活：POST /api/v1/auth/login（admin + 口令）→ access_token；
  2. 插件宿主面：GET /api/v1/plugins/hosts → 断言 200（合宿 venv 起不来的
     话这步即红——A3 结构漂移的业务面投影）；
  3. 纯聊天一轮：建会话 → WS user_input → 等 stream_end（--skip-chat 跳过；
     依赖 websockets 库与 LLM 密钥接线完整——「能登录但 LLM 全断」正是
     B3 裸拉事故面，冒烟必须覆盖）。

用法（装机内核默认 9101；dev 侧冒烟 --base http://127.0.0.1:9100）：
  python scripts/post_install_smoke.py [--base http://127.0.0.1:9101]
      [--password <口令>] [--skip-chat] [--chat-timeout 300]
口令来源：--password 或 AGENTOS_ADMIN_PASSWORD（内核口令无默认值——首启
随机播种；dispatch_task 同口径）。
退出码：0 = 全过；1 = 断言失败（探活过但 hosts/聊天红）；2 = 环境不可达
或口令缺失（安装脚本可区分「装坏了」与「没起来」）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:9101"
DEFAULT_CHAT_TIMEOUT = 300
#: 与前端 createSession 相同的标记头（session.ts）；缺省内核按普通请求处理
MAIN_AGENT_HEADER = {"X-Main-Agent-Request": "true"}
#: 冒烟消息：纯文本应答即收尾，禁调工具（工具循环会拉长等待且引入无关失败面）
SMOKE_PROMPT = "这是装后冒烟测试：请直接回复「收到」两个字，不要调用任何工具。"


def log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def _request(base: str, path: str, payload: dict | None = None, token: str | None = None,
             method: str | None = None, timeout: int = 15) -> tuple[int, dict | str]:
    """单次 HTTP 往返；返回 (status, body)。HTTPError 展开为元组返回（非异常）。"""
    data = json.dumps(payload or {}).encode("utf-8") if method == "POST" or payload else None
    headers = {"Content-Type": "application/json", **MAIN_AGENT_HEADER}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body
    except (urllib.error.URLError, OSError) as exc:
        raise ConnectionError(str(exc)) from exc
    try:
        return resp.status, json.loads(raw)
    except json.JSONDecodeError:
        return resp.status, raw


def smoke_chat(base: str, token: str, timeout_s: float) -> None:
    """纯聊天一轮：建会话 → ws-ticket → user_input → 等 stream_end。"""
    from websockets.exceptions import WebSocketException
    from websockets.sync.client import connect

    status, body = _request(base, "/api/v1/sessions", {"title": "post-install-smoke", "intent": "post-install-smoke"}, token, "POST")
    if status != 200:
        raise AssertionError(f"建会话失败：HTTP {status} {str(body)[:200]}")
    thread_id = body.get("thread_id") or (body.get("data") or {}).get("thread_id")
    if not thread_id:
        raise AssertionError(f"建会话响应无 thread_id：{str(body)[:200]}")

    status, body = _request(base, "/api/v1/ws-ticket", {}, token, "POST")
    if status != 200 or not body.get("ticket"):
        raise AssertionError(f"取 ws-ticket 失败：HTTP {status} {str(body)[:200]}")
    ws_url = base.replace("http", "ws", 1) + f"/ws/chat?ticket={body['ticket']}"
    message = {"type": "user_input", "thread_id": thread_id, "content": SMOKE_PROMPT,
               "pipeline_id": "", "attachments": [], "enable_thinking": False,
               "thinking_strength": "", "client_message_id": f"smoke-{int(time.time() * 1000)}"}
    try:
        with connect(ws_url, open_timeout=15) as ws:
            ws.send(json.dumps(message, ensure_ascii=False))
            log(f"已投递聊天（thread={thread_id}），等 stream_end（超时 {timeout_s:.0f}s）")
            deadline = time.time() + timeout_s
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    raise AssertionError(f"聊天超时 {timeout_s:.0f}s 未等到 stream_end（LLM 链路疑断——正是冒烟要抓的面）")
                try:
                    raw = ws.recv(timeout=remain)
                except TimeoutError as exc:
                    raise AssertionError(f"聊天超时 {timeout_s:.0f}s 未等到 stream_end（LLM 链路疑断）") from exc
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                ftype = frame.get("type", "?")
                if ftype == "stream_end":
                    log(f"PASS 纯聊天一轮：stream_end（thread={thread_id}）")
                    return
                if ftype in ("stream_error", "error"):
                    raise AssertionError(f"聊天流报错：{json.dumps(frame, ensure_ascii=False)[:400]}")
    except WebSocketException as exc:
        raise AssertionError(f"WS 链路异常（{ws_url}）：{exc}") from exc


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="装后冒烟：登录探活 + 插件宿主面 + 纯聊天一轮")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"内核基址（默认 {DEFAULT_BASE}）")
    parser.add_argument("--password", default=None, help="内核口令（默认环境变量 AGENTOS_ADMIN_PASSWORD）")
    parser.add_argument("--skip-chat", action="store_true", help="跳过纯聊天一轮（只验登录+宿主面）")
    parser.add_argument("--chat-timeout", type=float, default=DEFAULT_CHAT_TIMEOUT,
                        help=f"等 stream_end 的秒数（默认 {DEFAULT_CHAT_TIMEOUT}）")
    args = parser.parse_args()

    password = args.password or os.environ.get("AGENTOS_ADMIN_PASSWORD")
    if not password:
        log("FAIL 口令缺失：--password 或 AGENTOS_ADMIN_PASSWORD 未提供（内核口令无默认值，首启随机播种）")
        return 2

    # 1. 登录探活
    try:
        status, body = _request(args.base, "/api/v1/auth/login",
                                {"username": "admin", "password": password}, method="POST")
    except ConnectionError as exc:
        log(f"FAIL 内核不可达（{args.base}）：{exc}")
        return 2
    if status != 200 or not (isinstance(body, dict) and body.get("access_token")):
        log(f"FAIL 登录探活：HTTP {status} {str(body)[:200]}"
            "（口令不匹配——装机 user_root 口令与所给不符时即此形态）")
        return 1
    token = body["access_token"]
    log(f"PASS 登录探活（{args.base}）")

    # 2. 插件宿主面
    try:
        status, body = _request(args.base, "/api/v1/plugins/hosts", token=token, timeout=30)
    except ConnectionError as exc:
        log(f"FAIL 宿主面查询不可达：{exc}")
        return 2
    if status != 200:
        log(f"FAIL GET /api/v1/plugins/hosts：HTTP {status} {str(body)[:200]}"
            "（合宿 venv/插件面结构漂移的业务面投影，A3）")
        return 1
    log(f"PASS 插件宿主面：GET /api/v1/plugins/hosts 200"
        f"（hosts={len(body) if isinstance(body, list) else 'N/A'}）")

    # 3. 纯聊天一轮
    if args.skip_chat:
        log("SKIP 纯聊天（--skip-chat）")
        log("SMOKE PASS：登录 + 宿主面全绿（聊天面跳过）")
        return 0
    try:
        smoke_chat(args.base, token, args.chat_timeout)
    except AssertionError as exc:
        log(f"FAIL {exc}")
        return 1
    except (ConnectionError, OSError) as exc:
        log(f"FAIL 聊天链路不可达：{exc}")
        return 2
    except ImportError:
        log("FAIL 缺 websockets 库（纯聊天需要；装侧环境请用项目 venv 运行本脚本或 --skip-chat）")
        return 2
    log("SMOKE PASS：登录 + 宿主面 + 纯聊天全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())

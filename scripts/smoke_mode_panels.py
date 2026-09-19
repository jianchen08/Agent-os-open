"""六模式面板 live 冒烟（可重复车道，纯 stdlib 实现）。

背景（2026-09-18 模式面板战役，docs/working/模式面板复验_20260918.md）：
  1. 用户空间播种副本（%APPDATA%/agentos/plugins/modes/mode_*）曾落后仓内版本，
     `_parse_body` 缺内核 base64 契约（kernel/crates/api/src/http_dispatcher.rs 把
     请求原始字节 base64 后作 raw_body 传入插件），写动作全部解析为 {} → 缺参 400；
  2. 双源「用户副本赢」后合宿组 `_host/` 探测不达用户空间，宿主进程未拉起，
     六插件全部 /ext 路由 502（页面打不开排查_20260918.md）。

本脚本对运行中的栈做三段只读/无副作用探活：
  A. 六个面板页 GET /ext/mode_*/page/*-panel 全 200 且 body 以 <!DOCTYPE html> 开头；
  B. 核心数据端点每包抽 1 个 GET → 200 且 JSON 含预期顶层键；
  C. 写动作解析层探活：对 5 个写动作 POST 空 JSON {} → 断言 400（缺参）。
     C 段走内核 dispatcher：{} 会被 base64 后传给插件、由插件解回 {} 再走缺参 400
     路径——端到端证明 base64 解析层与路由都活着（旧副本在此同样 400，但 A/B 段
     数据形态不同可区分；配合仓内 test_parse_body_two_form_contract 锁实现）。
     缺参 400 不真派发任务，零 token 消耗。

用法：
  python scripts/smoke_mode_panels.py [--base-url http://127.0.0.1:9100]
                                      [--username admin] [--password admin12345]

退出码 0 = 全部断言通过；1 = 任一失败。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# A 段：面板页（page id 契约 = mode.describe panel_page_id，同 test_mode_panel_pages）
PANEL_PAGES = [
    ("mode_coding", "coding-panel"),
    ("mode_writing", "writing-panel"),
    ("mode_roleplay", "roleplay-panel"),
    ("mode_research", "research-panel"),
    ("mode_godot", "godot-panel"),
    ("mode_planning", "planning-panel"),
]

# B 段：每包抽 1 个免 query 的 GET 数据端点 → 预期顶层键
DATA_ENDPOINTS = [
    ("mode_roleplay", "/data/cards", "cards"),
    ("mode_writing", "/data/works", "works"),
    ("mode_coding", "/data/board", "columns"),
    ("mode_research", "/data/sessions", "sessions"),
    ("mode_godot", "/data/scene", "editor_online"),
    ("mode_planning", "/data/projects", "projects"),
]

# C 段：写动作（空 JSON → 缺参 400；不含 godot dispatch，避免任何派发面）
WRITE_ACTIONS = [
    ("mode_roleplay", "/data/actions/play"),
    ("mode_writing", "/data/actions/chapter_act"),
    ("mode_coding", "/data/actions/dispatch_issue"),
    ("mode_research", "/data/actions/start"),
    ("mode_planning", "/data/actions/plan"),
]


def _request(
    base: str,
    method: str,
    path: str,
    token: str | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    req = urllib.request.Request(base + path, method=method, data=body)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx：读回错误体供断言
        return exc.code, exc.read()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:9100")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin12345")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"{'✓' if ok else '✗'} {name}  {detail}", flush=True)

    # ── 登录取 token ────────────────────────────────────────────────────────────
    status, body = _request(
        base,
        "POST",
        "/api/v1/auth/login",
        body=json.dumps({"username": args.username, "password": args.password}).encode(),
    )
    if status != 200:
        print(f"✗ 登录失败 HTTP {status}: {body[:200]!r}", flush=True)
        return 1
    token = json.loads(body)["access_token"]
    print(f"✓ 登录 {args.username}@{base}", flush=True)

    # ── A 段：六面板页 ─────────────────────────────────────────────────────────
    for plugin_id, page_id in PANEL_PAGES:
        path = f"/ext/{plugin_id}/page/{page_id}"
        started = time.perf_counter()
        status, body = _request(base, "GET", path, token=token)
        elapsed = (time.perf_counter() - started) * 1000
        ok = status == 200 and body.startswith(b"<!DOCTYPE html>")
        check(
            f"A 面板页 {path}",
            ok,
            f"HTTP {status} {len(body)}B {elapsed:.0f}ms"
            + ("" if ok else "（期望 200 且 <!DOCTYPE html> 开头）"),
        )

    # ── B 段：核心 GET 数据端点 ────────────────────────────────────────────────
    for plugin_id, suffix, key in DATA_ENDPOINTS:
        path = f"/ext/{plugin_id}{suffix}"
        started = time.perf_counter()
        status, body = _request(base, "GET", path, token=token)
        elapsed = (time.perf_counter() - started) * 1000
        try:
            payload = json.loads(body)
            ok = status == 200 and key in payload
            detail = f"HTTP {status} 顶层键={sorted(payload)[:6]} {elapsed:.0f}ms"
        except ValueError:
            ok = False
            detail = f"HTTP {status} 非 JSON: {body[:120]!r}"
        check(f"B 数据 {path} 含 '{key}'", ok, detail)

    # ── C 段：写动作解析层探活（缺参 400 = 路由+base64 解析活着，零 token）──────
    for plugin_id, suffix in WRITE_ACTIONS:
        path = f"/ext/{plugin_id}{suffix}"
        started = time.perf_counter()
        status, body = _request(
            base, "POST", path, token=token, body=b"{}", timeout=35.0
        )
        elapsed = (time.perf_counter() - started) * 1000
        try:
            payload = json.loads(body)
            has_error = "error" in payload
        except ValueError:
            has_error = False
        ok = status == 400 and has_error
        check(
            f"C 写动作 {path} 缺参",
            ok,
            f"HTTP {status} {payload if has_error else body[:120]!r} {elapsed:.0f}ms"
            + ("" if ok else "（期望 400 + error 键）"),
        )

    failed = [r for r in results if not r[1]]
    print(
        f"\n摘要：{len(results) - len(failed)}/{len(results)} 通过"
        + (f"，失败 {len(failed)} 项" if failed else "，全绿"),
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

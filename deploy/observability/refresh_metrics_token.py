#!/usr/bin/env python3
"""AgentOS 观测栈令牌刷新：admin 登录内核，access token 落文件供 Prometheus bearer_token_file 用。

连接安全铁律：口令恒读 .env / 环境变量，不落本脚本、不落配置、不落输出。
access token 30min 过期（auth.rs ACCESS_TOKEN_TTL_SECS），定时任务每 25min 调一次；
--interval 常驻模式二选一。幂等：成功只重写令牌文件，失败非零退出（让调度方感知）。

用法：
  python refresh_metrics_token.py --base http://127.0.0.1:9101 --out secrets/metrics_token
  python refresh_metrics_token.py --interval 1500 --base http://host.docker.internal:9101
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:9101"
DEFAULT_OUT = "secrets/metrics_token"


def load_admin_password(env_file: str | None) -> str:
    """AGENTOS_ADMIN_PASSWORD 环境变量优先；否则从 .env 文件读（就近上溯 3 层）。"""
    val = os.environ.get("AGENTOS_ADMIN_PASSWORD")
    if val:
        return val
    candidates = (
        [Path(env_file)]
        if env_file
        else [Path.cwd() / ".env", Path.cwd().parent / ".env", Path.cwd().parent.parent / ".env"]
    )
    for p in candidates:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("AGENTOS_ADMIN_PASSWORD="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("未找到 AGENTOS_ADMIN_PASSWORD（环境变量或 .env）——拒绝猜口令")


def mint_token(base: str, password: str) -> str:
    body = json.dumps({"username": "admin", "password": password}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    token = data.get("access_token")
    if not token:
        raise SystemExit(f"登录响应缺 access_token：keys={sorted(data)}")
    return token


def write_token_file(out: Path, token: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(token, encoding="utf-8")
    try:
        out.chmod(0o600)
    except OSError:
        pass  # Windows FAT/挂载盘 chmod 可能不支持；令牌文件不入库即可


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE, help="内核 HTTP 基址（dev 9100 / 装机 9101）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="令牌输出文件（Prometheus bearer_token_file 指向它）")
    ap.add_argument("--env-file", default=None, help=".env 路径（默认 cwd 就近上溯找）")
    ap.add_argument("--interval", type=int, default=0, help="常驻刷新间隔秒（0=单次执行退出）")
    args = ap.parse_args()

    password = load_admin_password(args.env_file)
    out = Path(args.out)

    while True:
        try:
            write_token_file(out, mint_token(args.base, password))
            print(f"[ok] token written -> {out}")
        except urllib.error.HTTPError as exc:
            # 401/403 多为口令错——按铁律提示查 .env，别拿历史口令重试
            print(f"[fail] login http-{exc.code}（口令核对 .env 的 AGENTOS_ADMIN_PASSWORD）", file=sys.stderr)
            if args.interval <= 0:
                raise SystemExit(1) from exc
        except urllib.error.URLError as exc:
            print(f"[fail] kernel unreachable {args.base}: {exc.reason}", file=sys.stderr)
            if args.interval <= 0:
                raise SystemExit(1) from exc
        if args.interval <= 0:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

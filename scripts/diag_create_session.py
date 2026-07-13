#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断「创建会话慢」的压测脚本。

测量维度：
1. 单请求各段耗时（DNS / TCP / 首字节 / 总）
2. localhost vs 127.0.0.1 差异（排查 IPv6/DNS 回退）
3. 连续 N 次创建的耗时分布（排查冷启动 / 累积变慢）
4. 并发创建（排查同步 handler 线程池排队）
5. 单独测鉴权耗时（POST /auth/login vs 带 token 的 GET /threads）

用法：
    python scripts/diag_create_session.py
    python scripts/diag_create_session.py --base http://127.0.0.1:8988 --user admin --pwd admin123
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

DEFAULT_BASE = "http://127.0.0.1:8988"
DEFAULT_USER = "admin"
DEFAULT_PWD = "admin123"


def p50(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    xs2 = sorted(xs)
    return xs2[max(0, int(len(xs2) * 0.95) - 1)]


def fmt_stats(label: str, xs: list[float]) -> str:
    if not xs:
        return f"  {label}: (无样本)"
    return (
        f"  {label}: n={len(xs)} "
        f"min={min(xs)*1000:.0f}ms "
        f"p50={p50(xs)*1000:.0f}ms "
        f"p95={p95(xs)*1000:.0f}ms "
        f"max={max(xs)*1000:.0f}ms "
        f"mean={statistics.mean(xs)*1000:.0f}ms"
    )


def login(base: str, user: str, pwd: str) -> str:
    """登录拿 access_token。"""
    t0 = time.perf_counter()
    r = requests.post(
        f"{base}/api/v1/auth/login",
        json={"username": user, "password": pwd},
        timeout=10,
    )
    dt = time.perf_counter() - t0
    if r.status_code != 200:
        print(f"[login] 失败 {r.status_code} in {dt*1000:.0f}ms: {r.text[:200]}")
        sys.exit(1)
    token = r.json()["access_token"]
    print(f"[login] 成功 in {dt*1000:.0f}ms (user={user})")
    return token


def create_one(base: str, headers: dict, verbose: bool = False) -> dict:
    """创建一个会话，返回各段耗时（秒）。"""
    payload = {"title": "diag-test", "agent_id": "lingxi"}
    t_start = time.perf_counter()
    r = requests.post(
        f"{base}/api/v1/threads",
        json=payload,
        headers=headers,
        timeout=60,
    )
    t_total = time.perf_counter() - t_start
    ok = r.status_code == 201
    info = {"total": t_total, "status": r.status_code, "ok": ok}
    if verbose:
        info["elapsed_header"] = r.elapsed.total_seconds()
    if not ok:
        info["body"] = r.text[:200]
    return info


def test_sequential(base: str, headers: dict, n: int = 8) -> list[float]:
    """连续创建 N 个，返回每次耗时。"""
    print(f"\n=== 1) 连续创建 {n} 个会话（{base}） ===")
    times = []
    for i in range(n):
        info = create_one(base, headers)
        times.append(info["total"])
        flag = "OK " if info["ok"] else "ERR"
        print(f"  #{i+1:2d} {flag} {info['total']*1000:7.0f}ms  (status={info['status']})")
    print(fmt_stats("sequential", times))
    return times


def test_concurrent(base: str, headers: dict, n: int = 6, workers: int = 6) -> list[float]:
    """并发创建 N 个。"""
    print(f"\n=== 2) 并发创建 {n} 个会话（workers={workers}, {base}） ===")
    times = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(create_one, base, headers) for _ in range(n)]
        for i, fut in enumerate(as_completed(futures)):
            info = fut.result()
            times.append(info["total"])
            flag = "OK " if info["ok"] else "ERR"
            print(f"  #{i+1:2d} {flag} {info['total']*1000:7.0f}ms  (status={info['status']})")
    print(fmt_stats("concurrent", times))
    return times


def test_hosts(token: str, n: int = 5) -> None:
    """对比 localhost 与 127.0.0.1。"""
    print(f"\n=== 3) localhost vs 127.0.0.1 对比（各 {n} 次） ===")
    headers = {"Authorization": f"Bearer {token}"}
    for host in ["http://localhost:8988", "http://127.0.0.1:8988"]:
        ts = []
        for _ in range(n):
            info = create_one(host, headers)
            ts.append(info["total"])
        print(fmt_stats(host, ts))


def test_auth_cost(base: str, user: str, pwd: str, token: str, n: int = 5) -> None:
    """隔离鉴权耗时：登录 vs 带 token 的轻量 GET。"""
    print(f"\n=== 4) 鉴权耗时隔离（各 {n} 次） ===")
    # 4a 登录（含 bcrypt 校验）
    login_ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = requests.post(
            f"{base}/api/v1/auth/login",
            json={"username": user, "password": pwd},
            timeout=10,
        )
        login_ts.append(time.perf_counter() - t0)
    print(fmt_stats("login(bcrypt)", login_ts))

    # 4b 带 token 的轻量 GET（测 verify_token 路径，无业务逻辑）
    headers = {"Authorization": f"Bearer {token}"}
    get_ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = requests.get(f"{base}/api/v1/threads?limit=1", headers=headers, timeout=10)
        get_ts.append(time.perf_counter() - t0)
    print(fmt_stats("GET /threads(verify_token)", get_ts))


def test_dns_resolve(host: str = "localhost") -> None:
    """单独测 DNS 解析（排查 IPv6 回退）。"""
    import socket

    print(f"\n=== 5) DNS 解析 '{host}' ===")
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, 8988, type=socket.SOCK_STREAM)
        dt = time.perf_counter() - t0
        addrs = [info[4][0] for info in infos]
        print(f"  解析耗时: {dt*1000:.1f}ms  地址: {addrs}")
        if len(addrs) > 1 and any(":" in a for a in addrs) and any("." in a for a in addrs):
            print("  ⚠ 同时返回 IPv6+IPv4，且若 IPv6 不可达会有回退延迟")
    except Exception as e:
        print(f"  解析失败: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--pwd", default=DEFAULT_PWD)
    ap.add_argument("--seq", type=int, default=8, help="连续创建次数")
    ap.add_argument("--conc", type=int, default=6, help="并发数")
    args = ap.parse_args()

    print(f"目标: {args.base}  用户: {args.user}")

    # 0. DNS
    test_dns_resolve("localhost")

    # 1. 登录
    token = login(args.base, args.user, args.pwd)
    headers = {"Authorization": f"Bearer {token}"}

    # 2. 鉴权耗时隔离（先测，排除鉴权影响后续判断）
    test_auth_cost(args.base, args.user, args.pwd, token, n=4)

    # 3. 连续创建
    seq_times = test_sequential(args.base, headers, n=args.seq)

    # 4. 并发创建
    conc_times = test_concurrent(args.base, headers, n=args.conc, workers=args.conc)

    # 5. localhost 对比
    test_hosts(token, n=4)

    # 汇总结论
    print("\n" + "=" * 60)
    print("诊断结论:")
    sp50 = p50(seq_times) * 1000
    cp50 = p50(conc_times) * 1000
    print(f"  连续创建 p50: {sp50:.0f}ms")
    print(f"  并发创建 p50: {cp50:.0f}ms")
    if sp50 > 1000:
        print(f"  ⚠ 单次创建 > 1s，瓶颈在后端业务逻辑（看下方建议）")
    if cp50 > sp50 * 2:
        print(f"  ⚠ 并发退化严重（并发 p50 是连续的 {cp50/sp50:.1f}x），疑为线程池排队/锁竞争")
    print("=" * 60)


if __name__ == "__main__":
    main()

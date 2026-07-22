#!/usr/bin/env python3
"""隔离系统 CLI 入口 —— 启动脚本与 isolation 插件的桥接层。

从 start_web_cn.bat 收纳的 WSL/Docker/容器管理逻辑，
通过此 CLI 提供命令行调用入口，启动脚本只需调用本脚本。

用法:
    python3 isolation_cli.py probe         # WSL 存活探测
    python3 isolation_cli.py health <dir>   # WSL 内核健康检查
    python3 isolation_cli.py daemon <dir>   # 确保 dockerd 运行
    python3 isolation_cli.py containers <dir> <frontend_port> <redis_port> <backend_port>
    python3 isolation_cli.py ip             # 获取 WSL IP
    python3 isolation_cli.py portproxy <ip> <frontend_port> <redis_port>
    python3 isolation_cli.py pull <image>   # 拉取镜像（带回退）
"""
from __future__ import annotations

import os
import sys

# 将当前目录加入 sys.path 以便导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from container_orchestrator import (
    ensure_containers,
    get_container_status,
    pull_image_with_fallback,
)
from self_heal import wsl_shutdown
from wsl_health import (
    ProbeResult,
    check_wsl_kernel_health,
    ensure_dockerd,
    get_wsl_ip,
    probe_wsl_alive,
    setup_port_forward,
)


def _print_result(result: ProbeResult) -> int:
    """打印探测结果并返回退出码"""
    status = "OK" if result.rc == 0 else "FAIL"
    print(f"[{status}] rc={result.rc} {result.message}")
    return result.rc


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    cmd = sys.argv[1]

    if cmd == "probe":
        return _print_result(probe_wsl_alive())

    if cmd == "health":
        if len(sys.argv) < 3:
            print("Usage: isolation_cli.py health <wsl_dir>")
            return 1
        return _print_result(check_wsl_kernel_health(sys.argv[2]))

    if cmd == "daemon":
        if len(sys.argv) < 3:
            print("Usage: isolation_cli.py daemon <wsl_dir>")
            return 1
        return _print_result(ensure_dockerd(sys.argv[2]))

    if cmd == "containers":
        if len(sys.argv) < 6:
            print("Usage: isolation_cli.py containers <wsl_dir> <frontend_port> <redis_port> <backend_port>")
            return 1
        ok, msg = ensure_containers(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
        print(f"[{'OK' if ok else 'FAIL'}] {msg}")
        return 0 if ok else 1

    if cmd == "ip":
        ip = get_wsl_ip()
        if ip:
            print(ip)
            return 0
        print("[FAIL] Cannot get WSL IP")
        return 1

    if cmd == "portproxy":
        if len(sys.argv) < 5:
            print("Usage: isolation_cli.py portproxy <ip> <frontend_port> <redis_port>")
            return 1
        ok = setup_port_forward(sys.argv[2], sys.argv[3], sys.argv[4])
        print(f"[{'OK' if ok else 'WARN'}] Port forward {'configured' if ok else 'NOT set'}")
        return 0 if ok else 1

    if cmd == "pull":
        if len(sys.argv) < 3:
            print("Usage: isolation_cli.py pull <image>")
            return 1
        ok = pull_image_with_fallback(sys.argv[2])
        print(f"[{'OK' if ok else 'WARN'}] Image {sys.argv[2]} {'ready' if ok else 'not available'}")
        return 0 if ok else 1

    if cmd == "status":
        statuses = get_container_status()
        for s in statuses:
            print(f"  {s.name}\t{s.image}\trunning={s.running}\thealthy={s.healthy}")
        return 0

    if cmd == "shutdown":
        ok = wsl_shutdown()
        print(f"[{'OK' if ok else 'FAIL'}] wsl --shutdown {'done' if ok else 'failed'}")
        return 0 if ok else 1

    print(f"Unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())

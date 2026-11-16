#!/usr/bin/env python3
"""scratch 内核实例管理（开发工具箱技能自带脚本）——新插件联调用内核，不拖累现役内核。

方案依据（e2e sanctioned 模式，.github/workflows/e2e.yml「Start kernel」步）：
    AGENTOS_DB_PATH=<scratch> AGENTOS_KERNEL_PORT=<port> agentos-kernel
    轮询 /health 至就绪。

三隔离保证（不拖累现役内核/装机内核）：
- 端口：默认自动挑空闲端口，硬拒绝 9100（dev 默认）/9101（装机默认）；
- 数据面：DB 落 scratch 目录文件，密码经环境变量注入本实例——不读不改主仓
  .env/config，零文件写在 scratch 之外；
- 生命周期：实例档案 instance.json 记录 pid/port，stop 精确杀本实例并验证端口释放。

用法：
  python .../spawn_test_kernel.py spawn [--workspace W] [--port N] [--password PW]
                                        [--timeout S] [--scratch-root D]
  python .../spawn_test_kernel.py stop   [--scratch-root D] [--run-id ID]
  python .../spawn_test_kernel.py status [--scratch-root D] [--run-id ID]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

RESERVED_PORTS = (9100, 9101)
DEFAULT_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 0.5
DEFAULT_SCRATCH_ROOT = ".test_kernel"


class TestKernelError(Exception):
    """scratch 内核管理失败（二进制缺失/保留端口/实例不存在/就绪超时）。"""


def default_workspace() -> Path:
    """脚本位于 <workspace>/skills/skill-dev-toolkit/scripts/，上溯三级。"""
    return Path(__file__).resolve().parents[3]


def resolve_kernel_binary(workspace: Path) -> Path:
    """定位内核二进制（release 优先于 debug，Windows 带 .exe 后缀）。"""
    stem = "agentos-kernel"
    names = [f"{stem}.exe", stem] if os.name == "nt" else [stem, f"{stem}.exe"]
    for profile in ("release", "debug"):
        for name in names:
            candidate = workspace / "kernel" / "target" / profile / name
            if candidate.is_file():
                return candidate
    raise TestKernelError(
        "内核二进制缺失：先构建（kernel/ 下 cargo build --release --bin agentos-kernel；"
        "WSL 须挂 rust:1.88 容器）后重试"
    )


def validate_port(port: int) -> None:
    """9100=dev 默认、9101=装机默认，spawn 上去即撞现役内核，硬拒绝。"""
    if port in RESERVED_PORTS:
        raise TestKernelError(
            f"端口 {port} 为现役内核保留端口（dev/装机默认），scratch 实例禁用"
        )


def pick_free_port() -> int:
    """向 OS 要一个空闲 TCP 端口（绑定即释放，存在极小竞窗，撞上即 spawn 报错）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_kernel_env(port: int, db_path: Path, password: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """环境变量注入三件套（只在副本上改，不改调用方进程环境）。"""
    env = dict(os.environ if base is None else base)
    env["AGENTOS_KERNEL_PORT"] = str(port)
    env["AGENTOS_DB_PATH"] = str(db_path)
    env["AGENTOS_ADMIN_PASSWORD"] = password
    return env


def probe_health(port: int, timeout_s: float = 1.0) -> bool:
    """GET /health 无认证（e2e 同口径），可达即就绪。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout_s):
            return True
    except (urllib.error.URLError, OSError):
        return False


def await_ready(port: int, timeout_s: float, sleeper: Callable[[float], None] = time.sleep) -> None:
    """轮询 /health 至就绪，超时 TestKernelError。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if probe_health(port):
            return
        sleeper(POLL_INTERVAL_S)
    raise TestKernelError(f"内核就绪超时（{timeout_s}s，port {port}）")


def _scratch_root(workspace: Path, scratch_root: str | None) -> Path:
    return Path(scratch_root) if scratch_root else workspace / DEFAULT_SCRATCH_ROOT


def _write_instance(path: Path, record: dict[str, object]) -> None:
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_instance(scratch_root: Path, run_id: str | None) -> tuple[Path, dict[str, object]]:
    if run_id:
        instance_path = scratch_root / run_id / "instance.json"
    else:
        runs = sorted(
            (p for p in scratch_root.glob("*/instance.json")),
            key=lambda p: p.stat().st_mtime,
        )
        if not runs:
            raise TestKernelError(f"scratch 目录无实例档案: {scratch_root}")
        instance_path = runs[-1]
    if not instance_path.is_file():
        raise TestKernelError(f"实例档案不存在: {instance_path}")
    return instance_path, json.loads(instance_path.read_text(encoding="utf-8"))


def spawn(
    workspace: Path,
    port: int | None = None,
    password: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    scratch_root: str | None = None,
    kernel_bin: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> dict[str, object]:
    """拉起 scratch 内核实例并等待就绪，返回实例档案。

    kernel_bin 直指内核 exe（装机自举验证车道：resources/kernel/ 包内二进制，
    ADR 2026-11-03-plugin-dev-skill-and-staging-verify-pipeline §D3）；缺省按
    workspace 推导仓内 cargo 产物。env_extra 追加环境变量（如
    AGENTOS_USER_PLUGINS_DIR 用户根视图——内置根 AGENTOS_PLUGINS_DIR 是单值
    替换语义，勿经此设 staging，snippet_box 实测缺陷②）。
    """
    if kernel_bin:
        binary = Path(kernel_bin)
        if not binary.is_file():
            raise TestKernelError(f"--kernel-bin 指向的内核 exe 不存在: {binary}")
    else:
        binary = resolve_kernel_binary(workspace)
    if port is None:
        port = pick_free_port()
    validate_port(port)
    admin_password = password or uuid.uuid4().hex[:12]
    run_dir = _scratch_root(workspace, scratch_root) / f"run_{port}_{datetime.now():%Y%m%d_%H%M%S}"
    run_dir.mkdir(parents=True)
    db_path = run_dir / "kernel.db"
    log_path = run_dir / "kernel.log"
    env = build_kernel_env(port, db_path, admin_password)
    if env_extra:
        # 在完整进程环境之上追加（build_kernel_env 的 base 是底座替换语义，
        # 直传会把 PATH/SYSTEMROOT 砍掉→子进程 winsock 初始化失败 os error 10106，
        # snippet_box 跟进实测教训）
        env.update(env_extra)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [str(binary)],
            cwd=str(workspace),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    record: dict[str, object] = {
        "run_id": run_dir.name,
        "pid": process.pid,
        "port": port,
        "base_url": f"http://127.0.0.1:{port}",
        "db": str(db_path),
        "log": str(log_path),
        "admin_password": admin_password,
        "workspace": str(workspace),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        await_ready(port, timeout_s)
    except TestKernelError:
        process.terminate()
        raise
    _write_instance(run_dir / "instance.json", record)
    return record


def stop(scratch_dir: Path, run_id: str | None = None) -> dict[str, object]:
    """终止实例（Windows taskkill / POSIX SIGTERM），并验证端口释放。"""
    instance_path, record = _load_instance(scratch_dir, run_id)
    if record.get("stopped"):
        return record
    pid = cast(int, record["pid"])
    port = cast(int, record["port"])
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8", errors="replace",
            check=False,
        )  # taskkill 输出 GBK 中文（如"错误"），text 默认 utf-8 解码崩 reader thread
           # （snippet_box 实测缺陷④实锤），replace 显式吞不可解码字节
        if result.returncode != 0 and probe_health(port, timeout_s=0.5):
            raise TestKernelError(f"taskkill 失败且端口 {port} 仍可达: {result.stderr.strip()}")
        # taskkill 失败但端口已释放 = 实例先前已退出，按已停处理
    else:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and probe_health(port, timeout_s=0.5):
        time.sleep(0.2)
    if probe_health(port, timeout_s=0.5):
        raise TestKernelError(f"进程已杀但端口 {port} 仍可达，须人工排查")
    record["stopped"] = True
    record["stopped_at"] = datetime.now().isoformat(timespec="seconds")
    _write_instance(instance_path, record)
    return record


def status(scratch_dir: Path, run_id: str | None = None) -> dict[str, object]:
    """实例档案 + 实时健康/pid 探针。"""
    _, record = _load_instance(scratch_dir, run_id)
    port = cast(int, record["port"])
    record["health_ok"] = probe_health(port)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="scratch 内核实例管理")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("spawn", "stop", "status"):
        child = sub.add_parser(name)
        child.add_argument("--workspace", default=None, help="仓/工作空间根（默认按脚本位置推导）")
        child.add_argument("--scratch-root", default=None, help="scratch 根目录（默认 <workspace>/.test_kernel）")
        if name == "spawn":
            child.add_argument("--port", type=int, default=None, help="端口（默认自动挑空闲）")
            child.add_argument("--password", default=None, help="admin 口令（默认随机生成）")
            child.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="就绪超时秒数")
            child.add_argument("--kernel-bin", default=None, help="内核 exe 直指（装机自举验证：包内 resources/kernel/ 二进制；缺省按 workspace 推导 cargo 产物）")
        else:
            child.add_argument("--run-id", default=None, help="实例 run_id（默认取最近一次）")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace) if args.workspace else default_workspace()
    scratch_dir = _scratch_root(workspace, args.scratch_root)
    try:
        if args.command == "spawn":
            record = spawn(workspace, port=args.port, password=args.password, timeout_s=args.timeout, scratch_root=args.scratch_root, kernel_bin=args.kernel_bin)
        elif args.command == "stop":
            record = stop(scratch_dir, args.run_id)
        else:
            record = status(scratch_dir, args.run_id)
    except TestKernelError as exc:
        print(f"scratch 内核失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

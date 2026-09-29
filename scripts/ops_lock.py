#!/usr/bin/env python3
"""运维操作互斥锁：单写者 + 租约（本地单机退化版，O_EXCL 锁文件 + PID 探活）。

设计：docs/working/打包发版循环问题与方案_20260929.md §5.3（治 B1 双 agent 同时
装/卸/启、B3 裸拉的运维审计面）。electron 层已有同 userData 单实例锁 + 端口裁决，
但 **shell 操作层零互斥**——本工具补第三层：安装/重启/镜像同步类操作前置
acquire、收尾 release；持锁者被平台限额窗杀死后，锁由租约过期自动可抢占，
audit.log 留痕（B2 半途无收尾的可发现面）。

语义（取锁-抢占-审计三语义）：
  acquire : O_EXCL 原子建锁；已存在则读出三判——
            ① pid 活着且租约未过期 → 拒绝（报持有者与剩余秒，exit 1）；
            ② pid 已死 → 抢占（审计后重建锁）；
            ③ 租约过期（heartbeat_at 距今超 lease_s）→ 抢占（同上）。
  release : 按 holder 身份比对，仅持有人可删（防误删他人锁）。
  status  : 打印锁状态 free / held / stale（stale = 可抢占），纯观察恒 exit 0。
  锁文件 : <repo>/.zctmp/ops/<op>.lock（.zctmp 已入 .gitignore，不入仓）；
           审计    : <repo>/.zctmp/ops/audit.log（JSON 行 append）。
  锁内容 : {op, holder, pid, started_at, heartbeat_at, lease_s}（epoch 秒）。

接入用法（次周批次接线，本批只交付工具，不改现有流程）：
  # 推荐：带命令形态——锁 pid 挂到操作子进程，命令运行期互斥成立，
  # 结束（含被杀后下次抢占）自动收口
  python scripts/ops_lock.py acquire --op install --holder session-A [--lease 1800] -- <安装命令> [参数...]
  # 程序化/手动括弧形态：持锁 pid = acquire 命令自身，命令即退即死——
  # 互斥仅覆盖 lease 窗口，shell 长操作请用带命令形态
  python scripts/ops_lock.py acquire --op install --holder session-A
  python scripts/ops_lock.py release --op install --holder session-A
  长操作如需续租：操作内周期重写 heartbeat_at（本批未提供 heartbeat 子命令，
  短操作靠 lease 默认 1800s 覆盖）。

退出码：acquire 0=取锁成功（含抢占）/ 1=被拒；release 0=已释放 / 1=锁不存在
或非持有人；status 恒 0。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_REPO_DEFAULT = Path(__file__).resolve().parents[1]
DEFAULT_LEASE_S = 1800
_OP_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _now() -> float:
    return time.time()


def _lock_dir(raw: str | None) -> Path:
    return Path(raw).resolve() if raw else _REPO_DEFAULT / ".zctmp" / "ops"


def _lock_path(directory: Path, op: str) -> Path:
    return directory / f"{op}.lock"


def _audit(directory: Path, event: str, op: str, **extra: Any) -> None:
    """append 一行 JSON 审计（acquire/takeover/release/release-refused）。"""
    directory.mkdir(parents=True, exist_ok=True)
    line = {"ts": round(_now(), 3), "event": event, "op": op, "actor_pid": os.getpid(), **extra}
    with open(directory / "audit.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _read_lock(path: Path) -> dict[str, Any] | None:
    """读锁内容；不存在返回 None，损坏/不可解析返回 {}（调用方按可抢占处理）。"""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _pid_alive(pid: Any) -> bool:
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except (ImportError, ValueError, TypeError):
        return False


def _judge(prev: dict[str, Any], now: float) -> tuple[str, str]:
    """三判：held（拒绝）/ pid-dead / lease-expired（后两者 = 可抢占）。"""
    lease_s = float(prev.get("lease_s") or DEFAULT_LEASE_S)
    heartbeat = float(prev.get("heartbeat_at") or prev.get("started_at") or 0)
    if not _pid_alive(prev.get("pid")):
        return "pid-dead", "持锁 pid 已死（tasklist/psutil 探测）"
    if now - heartbeat > lease_s:
        return "lease-expired", f"租约过期（heartbeat 距今 {now - heartbeat:.0f}s > lease {lease_s:.0f}s）"
    return "held", ""


def _create_lock(path: Path, op: str, holder: str, lease_s: int, now: float) -> bool:
    """O_EXCL 原子建锁；已被并发抢占返回 False。"""
    payload = {"op": op, "holder": holder, "pid": os.getpid(),
               "started_at": round(now, 3), "heartbeat_at": round(now, 3), "lease_s": lease_s}
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False))
    return True


def cmd_acquire(directory: Path, op: str, holder: str, lease_s: int, command: list[str]) -> int:
    path = _lock_path(directory, op)
    now = _now()
    if _create_lock(path, op, holder, lease_s, now):
        _audit(directory, "acquire", op, holder=holder, lease_s=lease_s)
        print(f"[ops-lock] 取锁成功 op={op} holder={holder} lease={lease_s}s pid={os.getpid()}")
    else:
        prev = _read_lock(path)
        if prev is None:  # 竞态：刚被释放——再试一次原子建锁
            if _create_lock(path, op, holder, lease_s, _now()):
                _audit(directory, "acquire", op, holder=holder, lease_s=lease_s)
                print(f"[ops-lock] 取锁成功 op={op} holder={holder}（竞态重试）")
            else:
                print(f"[ops-lock] 拒绝：op={op} 抢占竞态失败（他人先到），重试或查 status")
                return 1
        else:
            verdict, why = _judge(prev, _now())
            if verdict == "held":
                remain = float(prev.get("lease_s") or DEFAULT_LEASE_S) - (_now() - float(
                    prev.get("heartbeat_at") or prev.get("started_at") or 0))
                print(f"[ops-lock] 拒绝：op={op} 已被持有 holder={prev.get('holder')} "
                      f"pid={prev.get('pid')} 剩余租约≈{max(0.0, remain):.0f}s")
                return 1
            # 抢占：删旧锁 → 原子重建 → 审计
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            if not _create_lock(path, op, holder, lease_s, _now()):
                print(f"[ops-lock] 拒绝：op={op} 抢占竞态失败（他人先到），重试或查 status")
                return 1
            _audit(directory, "takeover", op, holder=holder, reason=verdict, detail=why, prev=prev)
            print(f"[ops-lock] 抢占成功 op={op}（{verdict}：{why}），旧持有者={prev.get('holder')}")

    if not command:
        return 0
    # 带命令形态：锁 pid 改挂操作子进程（长命持有者，死了即可抢占），结束后自动 release
    proc = subprocess.Popen(command)
    data = _read_lock(path) or {}
    data["pid"] = proc.pid
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        rc = proc.wait()
    finally:
        cmd_release(directory, op, holder)
    _audit(directory, "exec-done", op, holder=holder, rc=rc)
    print(f"[ops-lock] 命令退出码 {rc}（已自动 release）")
    return rc


def cmd_release(directory: Path, op: str, holder: str) -> int:
    path = _lock_path(directory, op)
    prev = _read_lock(path)
    if prev is None:
        print(f"[ops-lock] 释放失败：op={op} 无锁文件")
        return 1
    if prev.get("holder") != holder:
        print(f"[ops-lock] 释放失败：op={op} 持有人是 {prev.get('holder')!r} 而非 {holder!r}"
              "（比对 holder 身份防误删他人锁）")
        _audit(directory, "release-refused", op, holder=holder, owner=prev.get("holder"))
        return 1
    path.unlink()
    _audit(directory, "release", op, holder=holder)
    print(f"[ops-lock] 已释放 op={op} holder={holder}")
    return 0


def cmd_status(directory: Path, op: str | None) -> int:
    if op is not None:
        targets = [op]
    else:
        targets = sorted(p.name[: -len(".lock")] for p in directory.glob("*.lock")) if directory.is_dir() else []
        if not targets:
            print(f"[ops-lock] {directory} 无任何锁（全部 free）")
            return 0
    for one in targets:
        prev = _read_lock(_lock_path(directory, one))
        if prev is None:
            print(f"[ops-lock] op={one}: free（无锁文件）")
            continue
        if not prev:
            print(f"[ops-lock] op={one}: stale（锁文件不可解析，可抢占重建）")
            continue
        verdict, _why = _judge(prev, _now())
        if verdict == "held":
            print(f"[ops-lock] op={one}: held holder={prev.get('holder')} pid={prev.get('pid')} "
                  f"lease_s={prev.get('lease_s')} started={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(prev.get('started_at', 0)))}")
        else:
            print(f"[ops-lock] op={one}: stale（{verdict}，可抢占）holder={prev.get('holder')} "
                  f"pid={prev.get('pid')}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="运维操作互斥锁（O_EXCL + PID 探活 + 租约抢占）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser, *, need_op: bool) -> None:
        p.add_argument("--op", required=need_op, metavar="NAME",
                       help="操作名（[A-Za-z0-9._-] 1-64，锁文件 <op>.lock）" + ("" if need_op else "（缺省列全部锁）"))
        p.add_argument("--lock-dir", default=None, help=f"锁目录（默认 {_REPO_DEFAULT / '.zctmp' / 'ops'}）")

    p_acq = sub.add_parser("acquire", help="取锁（活锁被拒 exit 1；死锁/过期锁抢占）；"
                            "带 `-- 命令` 时锁 pid 挂子进程并自动 release")
    common(p_acq, need_op=True)
    p_acq.add_argument("--holder", default=os.environ.get("USERNAME") or "unknown",
                       help="持锁者身份（release 比对用，默认环境变量 USERNAME）")
    p_acq.add_argument("--lease", type=int, default=DEFAULT_LEASE_S,
                       help=f"租约秒数（默认 {DEFAULT_LEASE_S}）")
    p_acq.add_argument("command", nargs="*", metavar="CMD",
                       help="带命令形态：`-- <命令> [参数...]`，取锁后执行、结束自动 release")

    p_rel = sub.add_parser("release", help="释放（仅持有人）")
    common(p_rel, need_op=True)
    p_rel.add_argument("--holder", default=os.environ.get("USERNAME") or "unknown", help="持锁者身份")

    p_st = sub.add_parser("status", help="查看锁状态（free/held/stale）")
    common(p_st, need_op=False)

    args = parser.parse_args()
    directory = _lock_dir(args.lock_dir)
    op = getattr(args, "op", None)
    if op is not None and not _OP_RE.match(op):
        print(f"[ops-lock] 非法操作名 {op!r}（须匹配 {_OP_RE.pattern}）")
        return 1
    directory.mkdir(parents=True, exist_ok=True)

    if args.cmd == "acquire":
        return cmd_acquire(directory, op, args.holder, args.lease, list(args.command))
    if args.cmd == "release":
        return cmd_release(directory, op, args.holder)
    return cmd_status(directory, op)


if __name__ == "__main__":
    sys.exit(main())

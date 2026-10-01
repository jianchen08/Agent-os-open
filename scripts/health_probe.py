#!/usr/bin/env python3
"""一键健康探针：五层 readiness 一页纸（K8s probe 分层范式的本地单机版）。

设计：docs/working/打包发版循环问题与方案_20260929.md §5.5（治 C3 人工三件套、
C1 UTC 找日志扑空、E3 资源位不可见、E1 handoff 盘点首步）。把散装信号聚合为
「进程 → 端口 → 日志 → 业务 → 资源」五面，只读零副作用（登录探活为纯 auth
往返，无数据写面；DB 经 mode=ro 连接）：

- L0 进程面：agentos-kernel / electron（灵汐助手.exe|electron.exe）进程树 +
  ParentProcessId 孤儿判定（内核父进程不在活进程表 = 裸拉/残留嫌疑，B3 面）；
- L1 端口面：9100（dev）/ 9101（装机）LISTEN + 归属 PID（netstat -ano）；
- L2 日志面：logs/kernel.liveness 权威判活（BUG-71 裁决产物；阈值 600s 同源
  内核 PRODUCTION_SILENCE_THRESHOLD）+ **直出当前实际日志路径**
  kernel.log.<UTC-今天>（操作者不再按本地日期猜，C1 根治面）；
- L3 业务面：9101 登录探活 + 长跑判定（复用 monitor_pipeline_watch 的
  WAITING_HUMAN/SUSPENDED/RUNNING_STALE/RUNNING 分类契约）；
- L4 资源面：系统内存水位（≥80% 告警，E3）+ 内核 RSS 合计。

两处根都扫（存在才报）：dev（repo logs/ 与 agentos_kernel.db）+ 装机
（%LOCALAPPDATA%\\Programs\\agent-os\\resources\\kernel\\logs 与 %APPDATA%\\agentos）。

用法：
  python scripts/health_probe.py [--json]
退出码：0 = 全绿；1 = 有红项；2 = 核心面不可达（无内核进程且 9100/9101 均无监听）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from io import TextIOWrapper
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]

#: 端口面探查的内核口（dev 9100 / 装机 9101；user 裁定口径，见 ADR 与排障文档）
KERNEL_PORTS = (9100, 9101)
#: 业务面登录探活目标（装机面；dev 形态下 9101 未起属合法，见 L3 判定）
LOGIN_BASE = "http://127.0.0.1:9101"
#: liveness 判活阈值：同源 kernel/crates/api/src/log_liveness.rs
#: PRODUCTION_SILENCE_THRESHOLD（10 分钟 = 常态静默窗实测 ≤48s 的一个数量级余量）
LIVENESS_FRESH_S = 600
#: 系统内存告警线（E3：2026-09-29 实录 81% 高位贴近发行目标）
MEM_WARN_PCT = 80

ELECTRON_NAMES = ("灵汐助手.exe", "electron.exe")
KERNEL_PROC = "agentos-kernel.exe"

_SPEC = importlib.util.spec_from_file_location(
    "monitor_pipeline_watch", str(Path(__file__).resolve().parent / "monitor_pipeline_watch.py")
)
assert _SPEC is not None
assert _SPEC.loader is not None
_mpw = importlib.util.module_from_spec(_SPEC)
sys.modules["monitor_pipeline_watch"] = _mpw
_SPEC.loader.exec_module(_mpw)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _human_mb(num_bytes: int | None) -> str:
    return f"{num_bytes / 1024 / 1024:.0f}MB" if num_bytes else "-"


# ── L0 进程面 ────────────────────────────────────────────────────────────────


def _probe_processes() -> dict[str, Any]:
    """psutil 扫内核/electron 进程树；孤儿 = 内核的 ParentProcessId 不在活进程表。"""
    import psutil

    procs: dict[int, Any] = {}
    for p in psutil.process_iter(["pid", "name", "ppid", "exe", "memory_info"]):
        try:
            procs[p.info["pid"]] = p.info
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    kernels, electrons = [], []
    for info in procs.values():
        name = (info["name"] or "").lower()
        if name == KERNEL_PROC:
            kernels.append(info)
        elif name in {n.lower() for n in ELECTRON_NAMES}:
            electrons.append(info)

    items: list[dict[str, Any]] = []
    red: list[str] = []
    for k in kernels:
        parent = procs.get(k["ppid"])
        rss = k["memory_info"].rss if k["memory_info"] else None
        entry = {
            "kind": "kernel",
            "pid": k["pid"],
            "exe": k["exe"] or "?",
            "rss_bytes": rss,
            "ppid": k["ppid"],
            "parent": parent["name"] if parent else None,
            "orphan": parent is None,
        }
        items.append(entry)
        if parent is None:
            red.append(
                f"内核 pid={k['pid']} 孤儿（ParentProcessId={k['ppid']} 不在活进程表，"
                f"exe={k['exe'] or '?'}）——裸拉/残留嫌疑（B3 面：缺密钥注入+占口毒化），"
                "收编动作见方案文档 §5.4"
            )
    for e in electrons:
        rss = e["memory_info"].rss if e["memory_info"] else None
        items.append({"kind": "electron", "pid": e["pid"], "exe": e["exe"] or "?", "rss_bytes": rss})
    return {"items": items, "kernel_count": len(kernels), "electron_count": len(electrons), "red": red}


# ── L1 端口面 ────────────────────────────────────────────────────────────────


def _probe_ports() -> dict[str, Any]:
    out = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout
    listeners: dict[int, list[int]] = {port: [] for port in KERNEL_PORTS}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING":
            for port in KERNEL_PORTS:
                if parts[1].rsplit(":", 1)[-1] == str(port):
                    listeners[port].append(int(parts[4]))

    import psutil

    def owner_name(pid: int) -> str:
        try:
            return psutil.Process(pid).name() or "?"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return "?"

    items = []
    for port in KERNEL_PORTS:
        pids = listeners[port]
        for pid in pids:
            items.append({"port": port, "pid": pid, "owner": owner_name(pid)})
        if not pids:
            items.append({"port": port, "pid": None, "owner": None})
    return {"items": items, "listening_ports": [p for p, v in listeners.items() if v]}


# ── L2 日志面 ────────────────────────────────────────────────────────────────


def _log_roots() -> list[Path]:
    roots = [_REPO / "logs"]
    if local := os.environ.get("LOCALAPPDATA"):
        roots.append(Path(local) / "Programs" / "agent-os" / "resources" / "kernel" / "logs")
    if appdata := os.environ.get("APPDATA"):
        roots.append(Path(appdata) / "agentos" / "logs")
    return roots


def _probe_logs(now: datetime) -> dict[str, Any]:
    utc_today = now.strftime("%Y-%m-%d")
    items, red = [], []
    any_fresh = False
    for root in _log_roots():
        if not root.is_dir():
            continue
        liveness = root / "kernel.liveness"
        age_s = None
        if liveness.is_file():
            age_s = max(0.0, now.timestamp() - liveness.stat().st_mtime)
        fresh = age_s is not None and age_s <= LIVENESS_FRESH_S
        any_fresh = any_fresh or fresh
        current_log = root / f"kernel.log.{utc_today}"
        item = {
            "root": str(root),
            "liveness_age_s": age_s,
            "fresh": fresh,
            "current_log": str(current_log),
            "current_log_exists": current_log.is_file(),
            "current_log_bytes": current_log.stat().st_size if current_log.is_file() else None,
        }
        items.append(item)
        if not fresh:
            why = "无 kernel.liveness 标记" if age_s is None else f"标记陈旧（{age_s:.0f}s 前刷新）"
            red.append(f"{root}：{why}（权威判活面不新鲜，阈值 {LIVENESS_FRESH_S}s）")
    if not items:
        red.append("找不到任何日志根（dev logs/ 与装机两处候选均不存在）")
    elif any_fresh and len(items) > 1:
        # 多根场景只要求至少一处权威判活面新鲜；不新鲜根（残留装位等）降为记录项
        red = []
    return {"utc_today": utc_today, "items": items, "red": red}


# ── L3 业务面 ────────────────────────────────────────────────────────────────


def _probe_login(base: str, listening_ports: list[int]) -> dict[str, Any]:
    password = os.environ.get("AGENTOS_ADMIN_PASSWORD")
    if not password:
        return {"status": "no-password", "note": "未设 AGENTOS_ADMIN_PASSWORD，登录探活无法执行（非红项）"}
    if 9101 not in listening_ports:
        if 9100 in listening_ports:
            return {"status": "dev-only", "note": "9101 无监听而 9100 在——dev 形态，装机业务面未起（非红项）"}
        return {"status": "unreachable", "note": "9100/9101 均无监听"}
    body = json.dumps({"username": "admin", "password": password}).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/v1/auth/login", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = resp.status == 200 and json.loads(resp.read().decode("utf-8")).get("access_token")
            return {"status": "ok" if ok else f"http-{resp.status}"}
    except urllib.error.HTTPError as exc:
        return {
            "status": f"http-{exc.code}",
            "red": f"登录探活失败：HTTP {exc.code}" + ("（口令不匹配）" if exc.code == 401 else ""),
        }
    except (urllib.error.URLError, OSError) as exc:
        return {"status": "unreachable", "red": f"登录探活不可达（{base}）：{exc}"}


def _probe_runs(db: Path, now: datetime) -> dict[str, Any] | None:
    """复用 monitor_pipeline_watch 分类契约；只读连接（mode=ro）。"""
    if not db.is_file():
        return None
    uri = f"file:{db.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return {"db": str(db), "error": str(exc)}
    try:
        cutoff = datetime.fromtimestamp(now.timestamp() - _mpw.DEFAULT_STUCK_MINS * 60, tz=UTC)
        verdicts = []
        for run in _mpw.load_active_runs(conn):
            verdicts.append(
                {
                    "run_id": run["run_id"],
                    "pipeline_id": run["pipeline_id"],
                    "verdict": _mpw.classify(run, cutoff),
                    "task_status": run["task_status"],
                }
            )
        return {"db": str(db), "runs": verdicts}
    except sqlite3.Error as exc:
        return {"db": str(db), "error": f"读活跃 run 失败：{exc}"}
    finally:
        conn.close()


def _probe_business(now: datetime, listening_ports: list[int]) -> dict[str, Any]:
    login = _probe_login(LOGIN_BASE, listening_ports)
    dbs = []
    dev_db = Path(os.environ.get("AGENTOS_DB_PATH") or (_REPO / "agentos_kernel.db"))
    if (runs := _probe_runs(dev_db, now)) is not None:
        dbs.append(runs)
    if appdata := os.environ.get("APPDATA"):
        if (runs := _probe_runs(Path(appdata) / "agentos" / "agentos_kernel.db", now)) is not None:
            dbs.append(runs)
    red = []
    if "red" in login:
        red.append(login["red"])
    for runs in dbs:
        for v in runs.get("runs", []):
            if v["verdict"] == "RUNNING_STALE":
                red.append(
                    f"长跑疑似 STUCK：pipeline={v['pipeline_id']} run={v['run_id']}"
                    f"（Running 无 trace 停滞超过 {_mpw.DEFAULT_STUCK_MINS} 分钟）"
                )
    return {"login": {k: v for k, v in login.items() if k != "red"}, "runs": dbs, "red": red}


# ── L4 资源面 ────────────────────────────────────────────────────────────────


def _probe_resources(proc_face: dict[str, Any]) -> dict[str, Any]:
    import psutil

    mem = psutil.virtual_memory()
    kernel_rss = sum(i["rss_bytes"] or 0 for i in proc_face["items"] if i["kind"] == "kernel")
    electron_rss = sum(i["rss_bytes"] or 0 for i in proc_face["items"] if i["kind"] == "electron")
    red = []
    if mem.percent >= MEM_WARN_PCT:
        red.append("内存水位越过告警线（E3 面：资源高位，检查 reasoning=max 档位与并发会话数）")
    return {
        "mem_percent": round(mem.percent, 1),
        "kernel_rss_bytes": kernel_rss,
        "electron_rss_bytes": electron_rss,
        "red": red,
    }


# ── 输出与退出码 ─────────────────────────────────────────────────────────────


def _render(faces: dict[str, Any], now: datetime) -> None:
    def mark(bad: bool) -> str:
        return "✗" if bad else "✓"

    print(f"==== AgentOS 健康探针（五层 readiness） UTC {now.isoformat(timespec='seconds')} ====")

    print("[L0 进程面]")
    pf = faces["L0_process"]
    for item in pf["items"]:
        if item["kind"] == "kernel":
            line = (
                f"  {mark(item['orphan'])} 内核 pid={item['pid']} rss={_human_mb(item['rss_bytes'])} exe={item['exe']}"
            )
            line += (
                f" 父={item['parent']}（pid={item['ppid']}）"
                if item["parent"]
                else f" 孤儿（ParentProcessId={item['ppid']} 已死）"
            )
            print(line)
        else:
            print(f"  ✓ 电子壳 pid={item['pid']} rss={_human_mb(item['rss_bytes'])} exe={item['exe']}")
    if not pf["items"]:
        print("  · 无内核/电子壳进程")
    for r in pf["red"]:
        print(f"  ✗ {r}")

    print("[L1 端口面]")
    for item in faces["L1_ports"]["items"]:
        if item["pid"] is not None:
            print(f"  ✓ :{item['port']} LISTEN pid={item['pid']} ({item['owner']})")
        else:
            print(f"  · :{item['port']} 无监听")

    print("[L2 日志面]")
    for item in faces["L2_logs"]["items"]:
        age = f"{item['liveness_age_s']:.0f}s 前" if item["liveness_age_s"] is not None else "无标记"
        print(f"  {mark(not item['fresh'])} {item['root']} kernel.liveness={age}（阈值 {LIVENESS_FRESH_S}s）")
        size = _human_mb(item["current_log_bytes"]) if item["current_log_bytes"] else "缺失"
        print(f"    当前 UTC 日志：{item['current_log']}（{size}）")
    for r in faces["L2_logs"]["red"]:
        print(f"  ✗ {r}")

    print("[L3 业务面]")
    login = faces["L3_business"]["login"]
    # no-password / dev-only 属无法判定或合法形态（·），非 ok 的其余状态为红（✗）
    login_soft = login["status"] in {"no-password", "dev-only"}
    login_sym = "·" if login_soft else ("✓" if login["status"] == "ok" else "✗")
    print(
        f"  {login_sym} 登录探活（{LOGIN_BASE}）：{login['status']}"
        + (f"——{login['note']}" if login.get("note") else "")
    )
    for runs in faces["L3_business"]["runs"]:
        if "error" in runs:
            print(f"  · {runs['db']}：{runs['error']}")
            continue
        for v in runs["runs"]:
            print(
                f"  {mark(v['verdict'] == 'RUNNING_STALE')} 长跑判定：[{v['verdict']}]"
                f" pipeline={v['pipeline_id']} task={v['task_status'] or '-'}（{runs['db']}）"
            )
        if not runs["runs"]:
            print(f"  ✓ 无活跃 run（{runs['db']}）")
    for r in faces["L3_business"]["red"]:
        print(f"  ✗ {r}")

    print("[L4 资源面]")
    rf = faces["L4_resources"]
    print(f"  {mark(bool(rf['red']))} 系统内存 {rf['mem_percent']}%（告警线 {MEM_WARN_PCT}%）")
    print(
        f"  · 内核 RSS 合计 {_human_mb(rf['kernel_rss_bytes'])}，电子壳 RSS 合计 {_human_mb(rf['electron_rss_bytes'])}"
    )
    for r in rf["red"]:
        print(f"  ✗ {r}")


def main() -> int:
    if isinstance(sys.stdout, TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="五层 readiness 一键健康探针（只读）")
    parser.add_argument("--json", action="store_true", help="输出机读 JSON（替代一页纸文本）")
    args = parser.parse_args()

    now = _utcnow()
    faces: dict[str, Any] = {}
    faces["L0_process"] = _probe_processes()
    faces["L1_ports"] = _probe_ports()
    faces["L2_logs"] = _probe_logs(now)
    faces["L3_business"] = _probe_business(now, faces["L1_ports"]["listening_ports"])
    faces["L4_resources"] = _probe_resources(faces["L0_process"])

    core_down = faces["L0_process"]["kernel_count"] == 0 and not faces["L1_ports"]["listening_ports"]
    all_red = [r for f in faces.values() for r in f.get("red", [])]
    exit_code = 2 if core_down else (1 if all_red else 0)

    if args.json:
        print(
            json.dumps(
                {"generated_at": now.isoformat(timespec="seconds"), "exit": exit_code, "faces": faces},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _render(faces, now)
        verdict = {0: "全绿", 1: "有红项（见 ✗ 行）", 2: "核心面不可达（无内核进程且 9100/9101 均无监听）"}
        print(f"==== 结论：{verdict[exit_code]}（exit={exit_code}） ====")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""kernel-metrics-smoke 门禁：真内核拉起，断言 /metrics 常用性能指标齐活。

性能监测补强 R1（docs/working/性能监测补强_20261028.md §四 O2）的 CI 执法面：
性能指标面（采集→聚合→导出→鉴权）每次内核面变更都在 CI 实测，不等装机取证。

链路：spawn_test_kernel scratch 拉起（三隔离：随机端口硬拒 9100/9101、scratch
DB、stop 验证端口释放）→ 登录取 access token（fresh DB 先试登录、失败走注册
兜底）→ 无 token 打 /metrics 必须被拒（鉴权面活着）→ 带 token 轮询 /metrics
至全部常用指标出现（proc_state poller 10s 周期 + 聚合器 1s flush，存在性等待
而非时序断言）→ stop 清场。

常用指标清单（单一真值=本清单，与 deploy/observability 看板、真值文档 §四 T5
三方对齐；改名须三处同刀）：
    kernel_proc_cpu_percent     内核进程 CPU%（sysinfo，R1 T5）
    kernel_proc_handle_count    内核进程句柄/FD 数（R1 T5）
    kernel_proc_memory_bytes    内核进程内存（R1 T5）
    kernel_mcp_reverse_queued   MCP 反向调用排队水位（main.rs agg_flush）
    kernel_mcp_reverse_active   MCP 反向调用执行中水位（main.rs agg_flush）

零第三方依赖（stdlib-only）；内核二进制缺失即红（fail-loud，CI job 前置
kernel-build；与 timing 真内核用例的 skipif 语义有意不同——本门禁是执法面
不是观察面）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "skill-dev-toolkit" / "scripts"

# 常用性能指标契约（见文件头，三方对齐：本清单 / 看板 JSON / 真值文档）
EXPECTED_METRICS: tuple[str, ...] = (
    "kernel_proc_cpu_percent",
    "kernel_proc_handle_count",
    "kernel_proc_memory_bytes",
    "kernel_mcp_reverse_queued",
    "kernel_mcp_reverse_active",
)

METRICS_WAIT_S = 120.0  # 就绪窗口：/health 先于内核引导完成就绪（tick 循环/插件
#   引导/proc poller 在后），构建后高负载首跑实证 25s 全空、空载实测 +2s/+10s
#   齐活——窗口须盖住慢引导而非指标节奏本身；齐活即提前返回，不付满额
METRICS_POLL_INTERVAL_S = 1.0


def _load_spawn_test_kernel():
    """加载 scratch 内核管理助手（与 tests/test_spawn_test_kernel.py 同款 importlib 形态）。"""
    spec = importlib.util.spec_from_file_location("spawn_test_kernel", SCRIPTS_DIR / "spawn_test_kernel.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 spawn_test_kernel: {SCRIPTS_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["spawn_test_kernel"] = module
    spec.loader.exec_module(module)
    return module


def _http_json(base: str, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"_raw": body[:200]}


def _login_token(base: str, password: str) -> str:
    """admin 登录取 access token；fresh scratch DB 种子未就位时注册兜底。"""
    code, body = _http_json(base, "/api/v1/auth/login", {"username": "admin", "password": password})
    token = body.get("access_token") if isinstance(body, dict) else None
    if token:
        return str(token)
    reg_code, reg_body = _http_json(base, "/api/v1/auth/register", {"username": "admin", "password": password})
    if reg_code not in (200, 201, 409):
        raise RuntimeError(f"登录失败 http-{code} 且注册兜底失败 http-{reg_code}: {reg_body}")
    code, body = _http_json(base, "/api/v1/auth/login", {"username": "admin", "password": password})
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise RuntimeError(f"注册兜底后登录仍失败 http-{code}: {body}")
    return str(token)


def _await_metrics_ready(base: str, password: str) -> dict[str, str]:
    """就绪窗口内：登录（重试——/health 早于引导完成就绪）→ 轮询 /metrics 至
    全部常用指标出现。返回 指标名 → 首行样例；超时 RuntimeError（fail-loud）。"""
    deadline = time.monotonic() + METRICS_WAIT_S
    token: str | None = None
    found: dict[str, str] = {}
    last_err = "未开始"
    while time.monotonic() < deadline:
        if token is None:
            try:
                token = _login_token(base, password)
                print("[kernel-metrics-smoke] ✓ admin 登录取到 access token")
            except Exception as exc:
                last_err = f"login: {exc}"
                time.sleep(METRICS_POLL_INTERVAL_S)
                continue
        status, text = _get_metrics(base, token)
        if status == 200:
            for line in text.splitlines():
                # exposition 行两形态：`name value`（无标签）/ `name{labels} value`
                # ——先切标签再取首 token，无标签行的值段必须剥掉
                parts = line.split("{", 1)[0].split()
                if not parts:
                    continue
                name = parts[0]
                if name in EXPECTED_METRICS and name not in found:
                    found[name] = line.strip()
            if len(found) == len(EXPECTED_METRICS):
                return found
            last_err = f"已见 {len(found)}/{len(EXPECTED_METRICS)}"
        else:
            last_err = f"/metrics http-{status}"
        time.sleep(METRICS_POLL_INTERVAL_S)
    missing = [m for m in EXPECTED_METRICS if m not in found]
    raise RuntimeError(f"就绪窗口 {METRICS_WAIT_S}s 内常用指标未齐（last: {last_err}）；缺: {missing}")


def _get_metrics(base: str, token: str | None) -> tuple[int, str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(base.rstrip("/") + "/metrics", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def _assert_auth_gate(base: str) -> None:
    """无 token 打 /metrics 必须被拒——鉴权面死了比指标缺更严重，先红在这里。"""
    status, _ = _get_metrics(base, token=None)
    if status not in (401, 403):
        raise RuntimeError(f"/metrics 无 token 期望 401/403，实得 {status}——鉴权面失效")


def main() -> int:
    stk = _load_spawn_test_kernel()
    try:
        stk.resolve_kernel_binary(REPO_ROOT)
    except stk.TestKernelError as exc:
        print(f"[kernel-metrics-smoke] 红：{exc}", file=sys.stderr)
        return 1

    record = stk.spawn(REPO_ROOT)
    port = int(record["port"])
    run_id = str(record["run_id"])
    base = str(record["base_url"])
    password = str(record["admin_password"])
    print(f"[kernel-metrics-smoke] scratch 内核就绪: run_id={run_id} port={port}")
    try:
        _assert_auth_gate(base)
        print("[kernel-metrics-smoke] ✓ /metrics 无 token 被拒（鉴权面活着）")
        found = _await_metrics_ready(base, password)
        for name in EXPECTED_METRICS:
            print(f"[kernel-metrics-smoke] ✓ {name}: {found[name]}")
    except Exception as exc:
        log_hint = record.get("log")
        print(f"[kernel-metrics-smoke] 红：{exc}", file=sys.stderr)
        print(f"[kernel-metrics-smoke] 内核日志: {log_hint}", file=sys.stderr)
        return 1
    finally:
        stopped = stk.stop(REPO_ROOT / stk.DEFAULT_SCRATCH_ROOT, run_id)
        print(
            f"[kernel-metrics-smoke] stop: stopped={stopped.get('stopped')} port_released={not stk.probe_health(port)}"
        )
    print(f"[kernel-metrics-smoke] 绿：常用性能指标 {len(EXPECTED_METRICS)}/{len(EXPECTED_METRICS)} 齐活")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

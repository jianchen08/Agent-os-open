#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P7 内核承载管道配置 — 功能验证可复现脚本。

复现 docs/working/p7_kernel_pipeline_config_function_verify_report.md 的全部验证动作：
  1. config 套件：cargo test -p agentos-config --test pipeline_definition_test（8 测试，含真实真相源）
  2. engine 套件：cargo test -p agentos-engine --test pipeline_execution_test（2 测试，含工具循环）
  3. api 套件：  cargo test -p agentos-api --test pipeline_config_endpoint_test（5 测试，GET/PUT 契约）
  4. 真实 HTTP：启动内核服务 → GET/PUT /api/v1/config/pipelines/{name} 契约验证
  5. 真相源字段：Python yaml 核对 default.yaml / agentos.yaml 关键字段

前置条件：
  - Rust 工具链（cargo 1.85+）
  - Python3 + pyyaml
  - 内核 debug bin 已编译（kernel/target/debug/agentos-kernel）；未编译时本脚本会用 cargo build 编译

用法：
    python3 docs/working/p7_kernel_pipeline_config_verify_reproduce.py

退出码：全部场景 PASS → 0；任一 FAIL → 1

[来源: docs/working/p7_kernel_pipeline_config_function_verify_report.md]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KERNEL_DIR = os.path.join(PROJECT_ROOT, "kernel")
PIPELINES_DIR = os.path.join(PROJECT_ROOT, "config", "pipelines")

passed = 0
failed = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    mark = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ── 1. 三套 cargo test ─────────────────────────────────────────────
def verify_cargo_tests() -> bool:
    print("\n===== 1. cargo 测试套件 =====")
    ok_all = True
    suites = [
        ("config pipeline_definition_test", ["-p", "agentos-config", "--test", "pipeline_definition_test"], 8),
        ("engine pipeline_execution_test", ["-p", "agentos-engine", "--test", "pipeline_execution_test"], 2),
        ("api pipeline_config_endpoint_test", ["-p", "agentos-api", "--test", "pipeline_config_endpoint_test"], 5),
    ]
    for name, args, expect in suites:
        p = run(["cargo", "test", *args], cwd=KERNEL_DIR, timeout=600)
        ok = p.returncode == 0 and f"{expect} passed" in p.stdout and "0 failed" in p.stdout
        tail = [l for l in p.stdout.strip().splitlines() if "test result" in l]
        report(f"cargo test {name} → 期望 {expect} passed", ok, tail[-1] if tail else f"rc={p.returncode}")
        if not ok:
            ok_all = False
            print(p.stdout[-2000:])
    return ok_all


# ── 2. 真相源字段核对（Python yaml）──────────────────────────────
def verify_truth_source() -> bool:
    print("\n===== 2. 真实真相源字段核对 =====")
    import yaml
    ok_all = True

    with open(os.path.join(PIPELINES_DIR, "default.yaml")) as f:
        d = yaml.safe_load(f)
    checks = [
        ("default.yaml name == agentos_agent", d.get("name") == "agentos_agent"),
        ("default.yaml input_routes ≥ 3", len(d.get("input_routes", [])) >= 3),
        ("default.yaml output_routes ≥ 4", len(d.get("output_routes", [])) >= 4),
        ("default.yaml plugins ≥ 10", len(d.get("plugins", [])) >= 10),
        ("default.yaml core_plugins 含 llm_call/tool_execute",
         "llm_call" in d.get("core_plugins", {}) and "tool_execute" in d.get("core_plugins", {})),
    ]
    for name, ok in checks:
        report(name, ok)
        ok_all = ok_all and ok

    # input_routes 字段齐备
    fields_ok = all(
        {"name", "condition", "target", "plugins", "priority"} <= set(r.keys())
        for r in d.get("input_routes", [])
    )
    report("input_routes 字段齐备 (name/condition/target/plugins/priority)", fields_ok)
    ok_all = ok_all and fields_ok

    # agentos.yaml
    with open(os.path.join(PROJECT_ROOT, "config", "agents", "main", "agentos.yaml")) as f:
        a = yaml.safe_load(f)
    agent_checks = [
        ("agentos.yaml config_id == agentos", a.get("config_id") == "agentos"),
        ("agentos.yaml system_prompt 非空", bool(a.get("system_prompt"))),
        ("agentos.yaml tool_ids ≥ 5", len(a.get("tool_ids", [])) >= 5),
        ("agentos.yaml model_tier 存在", bool(a.get("model_tier"))),
    ]
    for name, ok in agent_checks:
        report(name, ok)
        ok_all = ok_all and ok

    # 转换规则模拟（对齐 pipeline.rs to_engine_config）
    input_plugins: list[str] = []
    for r in d.get("input_routes", []):
        if r.get("target") == "core":
            for p in r.get("plugins", []):
                pid = f"pipeline_{p}"
                if pid not in input_plugins:
                    input_plugins.append(pid)
    referenced = {p for r in d.get("input_routes", []) for p in r.get("plugins", [])}
    post_plugins = [f"pipeline_{p['name']}" for p in d.get("plugins", []) if p["name"] not in referenced]
    routes_sorted = sorted(d.get("output_routes", []), key=lambda r: r["priority"])
    conv_checks = [
        ("prepare 插件并集去重且带 pipeline_ 前缀",
         all(s.startswith("pipeline_") for s in input_plugins) and "pipeline_tool_schema" in input_plugins),
        ("post 含 output 插件 stop_check/result_format",
         "pipeline_stop_check" in post_plugins and "pipeline_result_format" in post_plugins),
        ("路由按 priority 排序 next_tool→end",
         [r["route_type"] for r in routes_sorted] == ["next_tool", "wait", "next_llm", "end"]),
    ]
    for name, ok in conv_checks:
        report(name, ok)
        ok_all = ok_all and ok
    return ok_all


# ── 3. 真实 HTTP 契约验证（启动内核）─────────────────────────────
def ensure_kernel_bin() -> str:
    bin_path = os.path.join(KERNEL_DIR, "target", "debug", "agentos-kernel")
    if os.path.exists(bin_path):
        return bin_path
    print("未找到 debug bin，正在编译（cargo build -p agentos-api --bin agentos-kernel）…")
    p = run(["cargo", "build", "-p", "agentos-api", "--bin", "agentos-kernel"], cwd=KERNEL_DIR, timeout=900)
    assert p.returncode == 0, f"编译失败: {p.stderr[-1000:]}"
    return bin_path


def wait_health(port: int, tries: int = 10) -> bool:
    import urllib.request
    for _ in range(tries):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                return True
        except Exception:
            time.sleep(2)
    return False


def http_get(port: int, path: str) -> tuple[int, str]:
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def http_put(port: int, path: str, body: dict) -> tuple[int, str]:
    import urllib.request
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="PUT")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def verify_http(port: int = 19200) -> bool:
    print("\n===== 3. 真实 HTTP 端点契约 =====")
    bin_path = ensure_kernel_bin()
    env = dict(os.environ, AGENTOS_KERNEL_PORT=str(port), AGENTOS_DB_PATH=f"/tmp/p7_verify_{port}.db")
    proc = subprocess.Popen([bin_path], cwd=KERNEL_DIR, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok_all = True
    try:
        if not wait_health(port):
            report("内核服务就绪", False, "health 探测超时")
            return False

        # S6 GET default
        code, body = http_get(port, "/api/v1/config/pipelines/default")
        ok = code == 200 and '"name":"agentos_agent"' in body and '"etag"' in body
        report("GET default → 200 + name + etag", ok, f"code={code}")
        ok_all = ok_all and ok

        # B1 GET 非法 name
        code, _ = http_get(port, "/api/v1/config/pipelines/..%2F..%2Fetc%2Fpasswd")
        report("GET 非法 name（路径穿越）→ 400", code == 400, f"code={code}")
        ok_all = ok_all and code == 400 and ok_all

        # B2 GET 未知管道
        code, _ = http_get(port, "/api/v1/config/pipelines/nonexistent_pipeline_xyz")
        report("GET 未知管道 → 404", code == 404, f"code={code}")
        ok_all = ok_all and code == 404 and ok_all

        # S7 PUT 合法 body（临时管道名，验证后清理）
        body = {"data": {
            "name": "p7_verify_put",
            "input_routes": [{"name": "default", "target": "core", "plugins": ["tool_schema"], "priority": 30}],
            "output_routes": [{"route_type": "end", "condition": "True", "priority": 99}],
            "plugins": [{"name": "tool_schema", "config": {"enabled": True}}],
            "core_plugins": {"llm_call": {"class": "plugins.shared.core.llm_core.plugin.LLMCore", "config": {}}},
        }}
        code, _ = http_put(port, "/api/v1/config/pipelines/p7_verify_put", body)
        disk_ok = os.path.exists(os.path.join(PIPELINES_DIR, "p7_verify_put.yaml"))
        report("PUT 合法 body → 200 + 磁盘写回", code == 200 and disk_ok, f"code={code}, disk={disk_ok}")
        ok_all = ok_all and code == 200 and disk_ok and ok_all

        # S7 round-trip
        code, _ = http_get(port, "/api/v1/config/pipelines/p7_verify_put")
        report("PUT 后 GET round-trip → 200", code == 200, f"code={code}")
        ok_all = ok_all and code == 200 and ok_all

        # B1 PUT 非法 name
        code, _ = http_put(port, "/api/v1/config/pipelines/..%2F..%2Fetc%2Fpasswd", {"data": {"name": "x"}})
        report("PUT 非法 name（路径穿越）→ 400", code == 400, f"code={code}")
        ok_all = ok_all and code == 400 and ok_all

    finally:
        # 清理临时管道 + 终止内核
        tmp_file = os.path.join(PIPELINES_DIR, "p7_verify_put.yaml")
        if os.path.exists(tmp_file):
            os.remove(tmp_file)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("  临时管道文件已清理，内核已终止")
    return ok_all


def main() -> int:
    parser = argparse.ArgumentParser(description="P7 内核管道配置功能验证复现脚本")
    parser.add_argument("--skip-cargo", action="store_true", help="跳过 cargo 测试套件")
    parser.add_argument("--skip-http", action="store_true", help="跳过真实 HTTP 验证（内核启动）")
    parser.add_argument("--port", type=int, default=19200, help="内核 HTTP 端口（默认 19200）")
    args = parser.parse_args()

    print("=" * 60)
    print("P7 内核承载管道配置 — 功能验证复现")
    print("=" * 60)

    if not args.skip_cargo:
        verify_cargo_tests()
    if not args.skip_http:
        verify_http(args.port)
    verify_truth_source()

    print("\n" + "=" * 60)
    print(f"结果统计: PASS {passed} / FAIL {failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

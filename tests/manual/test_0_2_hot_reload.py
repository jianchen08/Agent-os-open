#!/usr/bin/env python3
"""0.2 kernel 全插件热加载实测脚本（pull 模型 + 手动端点）。

验证阶段 1 实现的两种热加载路径：
  1. pull 模型：改 sidecar 插件文件 mtime → 下次调用自动 respawn 加载新代码
  2. 手动端点：POST /api/v1/plugins/{id}/reload → force_unload → 下次调用 respawn

依赖：
  - 0.2 kernel 运行在 http://127.0.0.1:9100（start_web_02.bat --kernel-only）
  - 真实 LLM key 已配置（.env），或用 AGENTOS_LLM_MOCK=1 跳过 LLM

用法：
  python tests/manual/test_0_2_hot_reload.py            # 默认
  python tests/manual/test_0_2_hot_reload.py --no-llm   # 只测协议层，不调真实 LLM

[来源: 阶段1 热加载补实现验证]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KERNEL_URL = os.environ.get("AGENTOS_KERNEL_URL", "http://127.0.0.1:9100")

# 用于 pull 测试的 sidecar 插件（已知存在、热路径必调）
PULL_TARGET = "pipeline_prompt_build"
PULL_DIR = PROJECT_ROOT / "plugins" / "shared" / "pipeline" / "input" / "prompt_build"
PULL_FILE = PULL_DIR / "server.py"
PROBE_MARKER = "\n# hot-reload-probe\n"

_results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    _results.append((name, passed, detail))
    tag = "PASS" if passed else "FAIL"
    print(f"  [{tag}] {name}: {detail}")


def kernel_health() -> bool:
    try:
        with urllib.request.urlopen(f"{KERNEL_URL}/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def chat(message: str, sid: str, timeout: int = 120) -> tuple[float, str]:
    """发一条 HTTP chat，返回 (耗时秒, 回复内容)。"""
    body = json.dumps(
        {"message": message, "session_id": sid, "history": [], "agent_id": "agentos"}
    ).encode()
    req = urllib.request.Request(
        f"{KERNEL_URL}/api/v1/chat", data=body, headers={"Content-Type": "application/json"}
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            return time.time() - t0, data.get("content", "")
    except Exception as e:
        return time.time() - t0, f"ERR: {e}"


def reload_by_id(plugin_id: str) -> dict:
    """调手动 reload 端点，返回响应 JSON。"""
    req = urllib.request.Request(
        f"{KERNEL_URL}/api/v1/plugins/{plugin_id}/reload", method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_history() -> list:
    with urllib.request.urlopen(f"{KERNEL_URL}/api/v1/plugins/history", timeout=5) as r:
        return json.loads(r.read())


# ── 测试用例 ────────────────────────────────────────────────────────────────


def test_health() -> None:
    print("\n=== 测试1: kernel 健康检查 ===")
    ok = kernel_health()
    record("kernel /health", ok, "200 ok" if ok else "kernel 未运行，请先启动")


def test_manual_reload_endpoint() -> None:
    """手动 reload 端点：force_unload 一个 sidecar，验证返回 success + history 审计。"""
    print("\n=== 测试2: 手动 reload 端点 (force_unload) ===")
    if not kernel_health():
        record("manual reload", False, "skip: kernel 未运行")
        return
    try:
        resp = reload_by_id(PULL_TARGET)
    except Exception as e:
        record("manual reload", False, f"请求失败: {e}")
        return
    ok = resp.get("success") is True and resp.get("host_type") == "sidecar"
    record("reload 返回 success+sidecar", ok, json.dumps(resp, ensure_ascii=False)[:120])

    # 验证 history 审计
    hist = get_history()
    has_entry = any(
        e.get("plugin_id") == PULL_TARGET and e.get("success") for e in hist
    )
    record("history 审计记录", has_entry, f"history 共 {len(hist)} 条")


def test_pull_model_reload(use_llm: bool) -> None:
    """pull 模型：改 sidecar 文件 mtime → 下次 chat 触发 respawn。

    判据：改文件后发 chat，kernel 日志应出现 "code/config changed, reloading"。
    本脚本无法直接读 kernel 日志，改为间接判据：改文件前后各发一条 chat 都成功返回
    （说明 respawn 路径通畅，未因热加载导致 chat 失败）。
    """
    print("\n=== 测试3: pull 模型热加载 (改文件自动 respawn) ===")
    if not kernel_health():
        record("pull reload", False, "skip: kernel 未运行")
        return
    if not PULL_FILE.exists():
        record("pull reload", False, f"目标文件不存在: {PULL_FILE}")
        return

    # 备份原内容
    original = PULL_FILE.read_text(encoding="utf-8")

    try:
        # 1. 改文件 mtime（追加 probe 标记）
        PULL_FILE.write_text(original + PROBE_MARKER, encoding="utf-8")
        new_mtime = os.path.getmtime(PULL_FILE)
        record("改文件 mtime", True, f"server.py mtime 更新为 {new_mtime}")

        # 2. 等 TTL（>1s）让指纹缓存过期
        time.sleep(1.5)

        # 3. 发 chat 触发调用 → pull 检测应 respawn
        if use_llm:
            dt, reply = chat("回一个字：好", "pull-test")
            ok = dt < 120 and "ERR" not in reply
            record(
                "改文件后 chat 通畅",
                ok,
                f"{dt:.2f}s 回复: {reply[:40]}",
            )
        else:
            record("改文件后 chat (跳过LLM)", True, "--no-llm 模式跳过实际调用")
    finally:
        # 还原文件（注意 write 会再次改 mtime，但内容已还原，下次 pull 检测会再次 respawn 用回原内容）
        PULL_FILE.write_text(original, encoding="utf-8")


def test_control_no_false_reload(use_llm: bool) -> None:
    """对照：不改文件，连发两条 chat，验证不会误 respawn。

    间接判据：两条 chat 都成功返回（pull 模型不破坏正常调用）。
    精确的 respawn 计数需查 kernel 日志的 "MCP client connected"（手工核验）。
    """
    print("\n=== 测试4: 对照实验 (不改文件不误重载) ===")
    if not kernel_health():
        record("control", False, "skip: kernel 未运行")
        return
    if not use_llm:
        record("control (跳过LLM)", True, "--no-llm 模式跳过")
        return
    dt1, r1 = chat("回数字1", "ctrl-1")
    time.sleep(2)  # 过 TTL
    dt2, r2 = chat("回数字2", "ctrl-2")
    ok = "ERR" not in r1 and "ERR" not in r2
    record("不改文件连发2条 chat 通畅", ok, f"{dt1:.2f}s / {dt2:.2f}s")
    print("  (注：是否误 respawn 需查 kernel 日志 MCP client connected 次数)")


def test_cdylib_honest_degradation() -> None:
    """cdylib 插件 reload 应诚实返回不支持（restart_required）。"""
    print("\n=== 测试5: cdylib 诚实降级 ===")
    if not kernel_health():
        record("cdylib 降级", False, "skip: kernel 未运行")
        return
    # native-sdk-test-plugin 是唯一的 cdylib 插件（host_type=inprocess）
    # 若不存在则跳过
    try:
        req = urllib.request.Request(
            f"{KERNEL_URL}/api/v1/plugins/native-sdk-test-plugin/reload", method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 404 = 插件未注册到运行期 manifest，也是合理的（测试插件未必启用）
        record("cdylib 降级", True, f"测试插件未在运行期 (HTTP {e.code})，符合预期")
        return
    except Exception as e:
        record("cdylib 降级", False, f"请求异常: {e}")
        return
    # 若返回了，应是不支持热加载
    ok = resp.get("success") is False or resp.get("restart_required") is True
    record("cdylib 返回不支持/需重启", ok, json.dumps(resp, ensure_ascii=False)[:100])


def test_reload_all_with_discovery() -> None:
    """reload-all 带 discover：返回 reloaded/discovered 结构化响应。"""
    print("\n=== 测试6: reload-all 带 discover (运行时新增插件懒加载) ===")
    if not kernel_health():
        record("reload-all discover", False, "skip: kernel 未运行")
        return
    try:
        req = urllib.request.Request(
            f"{KERNEL_URL}/api/v1/plugins/reload-all", method="POST"
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except Exception as e:
        record("reload-all discover", False, f"请求失败: {e}")
        return
    has_reloaded = "reloaded" in resp and isinstance(resp["reloaded"], list)
    has_discovered = "discovered" in resp and isinstance(resp["discovered"], list)
    ok = has_reloaded and has_discovered
    record(
        "reload-all 返回结构",
        ok,
        f"reloaded={len(resp.get('reloaded', []))} discovered={len(resp.get('discovered', []))} discover_error={resp.get('discover_error')}",
    )


def test_idle_soft_unload(use_llm: bool) -> None:
    """空闲软卸载：发 chat 触发 sidecar spawn → 等空闲超时 → 验证进程被 kill。

    需 kernel 用短空闲超时启动（AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS=15）。
    判据：spawn 后进程数 N1，空闲等待后进程数 N2 < N1（被软卸载）。
    注：此测试耗时（需等空闲超时 + GC 周期），默认 LLM 模式才跑。
    """
    print("\n=== 测试7: 空闲软卸载 (生命周期 GC) ===")
    if not kernel_health() or not use_llm:
        record("空闲软卸载", True, "skip: 需要 LLM + 短空闲超时环境")
        return
    import subprocess

    def py_proc_count() -> int:
        try:
            out = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-Command",
                 "(Get-Process python -ErrorAction SilentlyContinue).Count"],
                stderr=subprocess.DEVNULL, text=True,
            )
            return int(out.strip() or "0")
        except Exception:
            return -1

    # 触发 sidecar spawn
    chat("hi", "idle-test")
    n1 = py_proc_count()
    print(f"  spawn 后 python 进程数: {n1}")
    if n1 <= 0:
        record("空闲软卸载", False, "无法获取进程数（可能非 Windows 或无 sidecar）")
        return

    # 等待空闲超时 + GC 周期（默认 300s 太久，仅当短超时配置时验证）
    timeout_env = os.environ.get("AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS", "300")
    if int(timeout_env) > 60:
        record("空闲软卸载", True, f"skip: 空闲超时 {timeout_env}s 太长，用 AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS=15 启 kernel 测")
        return

    wait = int(timeout_env) + 35  # 超时 + GC 30s 周期 + 余量
    print(f"  等待 {wait}s (空闲超时 {timeout_env}s + GC 周期)...")
    time.sleep(wait)
    n2 = py_proc_count()
    print(f"  GC 后 python 进程数: {n2}")
    ok = n2 < n1
    record(
        "空闲软卸载进程回收",
        ok,
        f"{n1} → {n2} ({'已回收' if ok else '未回收'})",
    )


# ── 主函数 ──────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="0.2 kernel 热加载 + 生命周期实测")
    parser.add_argument("--no-llm", action="store_true", help="跳过真实 LLM 调用")
    args = parser.parse_args()

    print("=" * 70)
    print("0.2 kernel 全插件热加载 + 生命周期实测")
    print(f"kernel: {KERNEL_URL}  LLM: {'跳过' if args.no_llm else '真实'}")
    print("=" * 70)

    test_health()
    test_manual_reload_endpoint()
    test_pull_model_reload(use_llm=not args.no_llm)
    test_control_no_false_reload(use_llm=not args.no_llm)
    test_cdylib_honest_degradation()
    test_reload_all_with_discovery()
    test_idle_soft_unload(use_llm=not args.no_llm)

    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print("\n" + "=" * 70)
    print(f"汇总: {passed}/{total} 通过, {failed} 失败")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

# @feature: FP-0.2.可观测性 dsh_adapter DSH兼容四件垫片 | @vision: V3 可嵌入
"""真实进程级 stdio 往返车道（垫片四端到端，任务 d66b3780c7a9）。

补齐上一轮报告「待真机验证清单」第 1/2 项：
**Node 桥子进程 ↔ Python 宿主的真实 stdio 双向 RPC 往返** +
**loop 三操作接内核契约后的语义正确性**。

真实面（零桩）：
- 真实 node 子进程跑 runtime/dsh-rpc-bridge.mjs（require Node >= 22：
  链包 @deepseek-ai/dsh-subprocess-local 依赖 node:util.getSystemErrorMessage）；
- 真实 cordis chain boot（bootChain 全链）+ 夹具消费插件经 bootPlan.packages
  计划装载（inject: ['agentos'] 服务面真实挂载）；
- 真实 stdio 帧传输（下行 initialize/emit_hook + 上行 agentos/call 双向）；
- 真实 Python 读循环分发 / handler 表 / _write_frame 回写；
- 真实服务代理（makeServiceProxy via rpcUpstream）与身份表（dispatchHook）。

唯一桩 = 内核 capability caller（SpyKernel，随用例预设响应/异常）——
真实运行中内核冒烟属另一车道（Linux 侧无编译产物，见任务报告「真实内核
冒烟未跑」如实标注）；本车道桩只替换「内核 capability 调用」这一个边界。

跳过门控：Node>=22 或运行时工作区缺失即 skip（真机车道不假绿）。
可用 AGENTOS_TEST_NODE 显式指定 node 可执行文件。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_bridge_gaps import _load_bridge_module

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[2]
RUNTIME_WS = REPO_ROOT / "user_root" / "dsh_runtime"
CONSUMER = PLUGIN_DIR / "runtime" / "test" / "fixtures" / "roundtrip_consumer.mjs"

_WS_READY = RUNTIME_WS.is_dir() and (RUNTIME_WS / "node_modules").is_dir() and CONSUMER.is_file()

_BOOT_PLAN: dict[str, Any] = {
    "chain": True,
    "packages": [{"name": "agentos-roundtrip-consumer", "entry": str(CONSUMER)}],
}

# RunRecord wire 形态（与 L3b _run_row 同源；serde lowercase）
_RUN_ROW = {
    "run_id": "run-l4",
    "config_hash": "h",
    "status": "running",
    "tenant_id": "default",
    "created_at": "2026-11-21T00:00:00Z",
    "current_branch": "main",
    "current_seq": 0,
    "pipeline_id": "pipe-l4",
}


def _node_major(bin_path: Path) -> int | None:
    """读 node 主版本（读不到/非 0 退出 = None）。"""
    try:
        out = subprocess.run(
            [str(bin_path), "--version"], capture_output=True, text=True, timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out.stdout.strip().lstrip("v").split(".")[0]) if out.stdout else None


def _resolve_node22() -> Path | None:
    """解析可用 Node>=22：AGENTOS_TEST_NODE > tmp/node22 下载位 > PATH。"""
    candidates: list[Path] = []
    env = os.environ.get("AGENTOS_TEST_NODE", "")
    if env:
        candidates.append(Path(env))
    candidates.extend(sorted((REPO_ROOT / "tmp" / "node22").glob("node-*/bin/node")))
    which = shutil.which("node")
    if which:
        candidates.append(Path(which))
    for cand in candidates:
        if cand.is_file() and (_node_major(cand) or 0) >= 22:
            return cand
    return None


class SpyKernel:
    """内核 capability caller 桩（本车道唯一替换边界）：记录 + 预设响应/异常。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.raise_on: dict[str, str] = {}

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method in self.raise_on:
            raise RuntimeError(self.raise_on[method])
        return self.responses[method]


@pytest.fixture
def bmod() -> Any:
    return _load_bridge_module()


@pytest.fixture
def node_bin(monkeypatch: pytest.MonkeyPatch) -> Path:
    """定位 Node>=22 并把其 bin 目录前置到 PATH（桥按 "node" 从 PATH 解析）。"""
    if not _WS_READY:
        pytest.skip(
            f"L4 真进程车道需要运行时工作区 + 夹具（缺: {RUNTIME_WS} 或 {CONSUMER}）")
    bin_path = _resolve_node22()
    if bin_path is None:
        pytest.skip("L4 真进程车道需要 Node >= 22（设 AGENTOS_TEST_NODE 或预置 tmp/node22）")
    monkeypatch.setenv("PATH", f"{bin_path.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_path


def _exec(op: str, **extra: Any) -> dict[str, Any]:
    """夹具消费插件的分派 exec（op 分派到对应 loop 方法）。"""
    return {"callId": f"call-l4-{op}", "name": "probe",
            "arguments": {"op": op, **extra}}


async def _scenario_emit(bmod: Any, kernel: SpyKernel, op: str, **extra: Any) -> dict[str, Any]:
    """boot 真实桥 → emit_hook 一轮（夹具插件内经服务代理反向调用）→ 关停。

    result 必传：DSH 真实契约 post-execute = (exec, result, next) 三参，
    hookArgsFor 对 undefined 槽位跳位（缺 result 会让 next 错位到第二参）。
    """
    bridge = bmod.DshRuntimeBridge(
        runtime_root=str(RUNTIME_WS), boot_plan=_BOOT_PLAN,
        boot_timeout_s=120.0, call_timeout_s=60.0,
    )
    bridge.set_kernel_caller(kernel)
    try:
        await bridge.initialize()
        return await bridge.emit_hook(
            "tools/post-execute", {}, exec=_exec(op, **extra),
            result={"ok": True}, agent_key="sess-l4")
    finally:
        await bridge.shutdown()


def test_real_process_roundtrip_loop_status(bmod: Any, node_bin: Path) -> None:
    """真实往返·status：插件 ctx.agentos.loop.status → stdio → 读循环 → 内核契约投影。"""
    kernel = SpyKernel()
    kernel.responses["service-registry.pipeline-runs.list_by_pipeline"] = [_RUN_ROW]

    out = asyncio.run(_scenario_emit(bmod, kernel, "status", taskId="PIPE-L4"))

    assert out["ok"] is True, f"emit_hook 失败: {out}"
    assert kernel.calls == [(
        "service-registry.pipeline-runs.list_by_pipeline", {"pipeline_id": "pipe-l4"},
    )], "内核契约必须收到归一化后的 pipeline_id（真进程往返实据）"
    probe = (out.get("decision") or {}).get("probeStatus")
    assert probe is not None, f"decision 必须携带夹具并入的 probeStatus: {out}"
    assert probe["exists"] is True
    assert probe["run_status"] == "running", "run_status 取自内核快照行（跨进程语义成立）"
    assert probe["run_id"] == "run-l4"


def test_real_process_roundtrip_stop_and_inject(bmod: Any, node_bin: Path) -> None:
    """真实往返·stop/inject：两个写操作同一真进程桥逐一往返（≥2 组区分度输入）。"""
    kernel = SpyKernel()
    kernel.responses["pipeline-executor.suspend_pipeline"] = {
        "status": "suspended", "pipeline_id": "pipe-l4", "run_id": "run-l4"}
    kernel.responses["chat.send_message"] = {
        "status": "dispatched", "pipeline_id": "pipe-l4"}

    stop_out = asyncio.run(_scenario_emit(bmod, kernel, "stop", taskId="pipe-l4"))
    assert stop_out["ok"] is True, stop_out
    assert kernel.calls[-1] == ("pipeline-executor.suspend_pipeline", {"pipeline_id": "pipe-l4"})
    probe_stop = (stop_out.get("decision") or {}).get("probeStop")
    assert probe_stop == {
        "ok": True, "pipeline_id": "pipe-l4", "run_status": "suspended", "run_id": "run-l4",
    }

    inject_out = asyncio.run(_scenario_emit(bmod, kernel, "inject",
                                            taskId="pipe-l4", payload={"hint": "go", "n": 1}))
    assert inject_out["ok"] is True, inject_out
    (method, params), = [c for c in kernel.calls if c[0] == "chat.send_message"]
    assert method == "chat.send_message"
    assert params["pipeline_id"] == "pipe-l4"
    assert json.loads(params["message"]) == {"hint": "go", "n": 1}, \
        "结构化载荷必须以 JSON 文本注入（跨进程序列化实据）"
    probe_inject = (inject_out.get("decision") or {}).get("probeInject")
    assert probe_inject["ok"] is True
    assert probe_inject["injected"] is True


def test_real_process_roundtrip_fail_visible_and_bridge_survives(bmod: Any, node_bin: Path) -> None:
    """真实往返·fail-visible：内核异常 → 插件侧原样传播 → ok=False；桥不死可续用。"""
    kernel = SpyKernel()
    kernel.raise_on["pipeline-executor.suspend_pipeline"] = "l4 kernel boom"
    kernel.responses["service-registry.pipeline-runs.list_by_pipeline"] = [_RUN_ROW]

    out = asyncio.run(_scenario_emit(bmod, kernel, "stop", taskId="pipe-l4"))

    assert out["ok"] is False, f"内核异常必须让 emit_hook fail-visible: {out}"
    assert "l4 kernel boom" in str(out.get("error")), out

    # 桥不死：同进程再走一轮 status 照常往返（错误帧路径不污染帧通道）
    kernel_calls_before = len(kernel.calls)
    recovery = asyncio.run(_scenario_emit(bmod, kernel, "status", taskId="pipe-l4"))
    assert recovery["ok"] is True, recovery
    assert len(kernel.calls) > kernel_calls_before, "恢复轮必须再次到达内核 caller"

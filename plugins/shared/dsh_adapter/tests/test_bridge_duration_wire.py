# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""桥信封 ``duration_ms`` 的线格式契约：整数毫秒。

事故（内核库 blobs，5 条）：``failed to parse MCP response as
ToolExecutionResult: invalid type: floating point ``0.0``, expected u64``
（``dsh_glob`` 3 条 / ``dsh_read`` 2 条）——``runtime/dsh-rpc-bridge.mjs`` 以
``Math.round(dt*10)/10`` 产出一位小数，撞 ``kernel/crates/core/src/types.rs``
的 ``duration_ms: Option<u64>``（``invoker/src/invoker/mcp_result.rs`` 严格
serde，失败即 PARSE_ERROR）。

契约（修复后）：桥两侧——mjs ``tool/call`` 信封与 ``bridge.py`` 合成失败
信封——``duration_ms`` 一律整数毫秒；带小数位会被内核整条拒绝。

真机用例依赖外部 DSH checkout 与 node，缺失时 skip（不静默假绿）；
CI 安全用例只覆盖 ``bridge.py`` 合成信封。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from runtime_workspace import (  # noqa: E402
    NPM_REGISTRY_DEFAULT,
    bootstrap_spec,
    ensure_workspace,
    resolve_npm_binary,
    resolve_runtime_root,
)

_BRIDGE_MODULE = "dsh_adapter_duration_wire_test"
_RUNTIME_ROOT_ENV = "AGENTOS_DSH_RUNTIME_ROOT"
def _load_bridge_module() -> Any:
    """显式文件级加载（唯一模块名）：防裸 ``import bridge`` 被其它插件目录劫持。"""
    spec = importlib.util.spec_from_file_location(_BRIDGE_MODULE, PLUGIN_DIR / "bridge.py")
    assert spec is not None, "cannot load dsh_adapter bridge.py"
    assert spec.loader is not None, "cannot load dsh_adapter bridge.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_BRIDGE_MODULE] = module
    spec.loader.exec_module(module)
    return module


def _resolve_workspace() -> Path | None:
    """运行时工作区解析（真机车道）：env 优先，其次用户空间默认；
    需已 bootstrap（node_modules 含 dsh-tools 基础包）才可用。"""
    import subprocess  # noqa: PLC0415

    configured = os.environ.get(_RUNTIME_ROOT_ENV, "").strip()
    if configured and Path(configured).is_dir():
        return Path(configured)
    import shutil  # noqa: PLC0415

    ws = resolve_runtime_root(configured or None)
    if ws.is_dir() and (ws / "node_modules" / "@deepseek-ai" / "dsh-tools").is_dir():
        return ws
    if shutil.which("npm") is None:
        return None
    ws = ensure_workspace(ws)
    cmd = [resolve_npm_binary(), "install", "--prefix", str(ws), "--registry", NPM_REGISTRY_DEFAULT,
           "--no-audit", "--no-fund", *bootstrap_spec()]
    subprocess.run(cmd, check=False, capture_output=True, timeout=600)
    if (ws / "node_modules" / "@deepseek-ai" / "dsh-tools").is_dir():
        return ws
    return None


def test_boot_failure_envelope_duration_is_integer_ms(tmp_path: Path) -> None:
    """仓库根缺失的合成失败信封：duration_ms 必须是 int（0.0 会撞内核 u64）。"""
    mod = _load_bridge_module()
    bridge = mod.DshRuntimeBridge(runtime_root=str(tmp_path / "no-such-repo"))
    out = asyncio.run(bridge.call_tool("read", {"file_path": "a.txt"}))
    assert out["success"] is False
    assert isinstance(out["duration_ms"], int), f"duration_ms 必须是整数，实际 {out['duration_ms']!r}"


def test_real_bridge_envelope_duration_is_integer_ms(tmp_path: Path) -> None:
    """真机：真实 node 桥从运行时工作区 boot DSH 后调用 read，信封 duration_ms 无小数位。"""
    if shutil.which("node") is None:
        pytest.skip("node 不可用（外部依赖）")
    ws = _resolve_workspace()
    if ws is None:
        pytest.skip(f"DSH 运行时工作区不可用（设 {_RUNTIME_ROOT_ENV} 或允许 npm bootstrap 用户空间工作区）")
    mod = _load_bridge_module()
    probe = tmp_path / "probe.txt"
    probe.write_text("hello\n", encoding="utf-8")

    async def scenario() -> dict[str, Any]:
        bridge = mod.DshRuntimeBridge(runtime_root=str(ws), cwd=str(tmp_path))
        try:
            tools = await bridge.initialize()
            names = {t.get("name") for t in tools if isinstance(t, dict)}
            if "read" not in names:
                pytest.skip(f"DSH 工具面缺 read（拿到 {sorted(n for n in names if n)[:5]}）")
            return await bridge.call_tool("read", {"file_path": str(probe)})
        finally:
            await bridge.shutdown()

    out = asyncio.run(scenario())
    assert out["success"] is True, f"调用失败：{out.get('error')!r}"
    # JSON 侧 "1.0" 回读为 float——isinstance 即线格式判别（int 才通过内核 u64）
    assert isinstance(out["duration_ms"], int), f"duration_ms 必须是整数，实际 {out['duration_ms']!r}"
    assert out["duration_ms"] >= 0

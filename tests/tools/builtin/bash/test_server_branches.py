# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""bash 工具 sidecar 接口适配层（server.py）分支补测。

覆盖缺口（2026-09-13 实测 32/83）补齐：
- bash_execute：无 _call_context 直通、失败结果 → {error, error_code} 映射、
  进度前向器构建失败降级（执行不受阻）、带 call_context 的全链路进度推送
  （真实短命令 + fake emitter）；
- _progress_forwarder：路由键缺失降级、frontend capability 缺失降级、
  节流缓冲 → 消费者 emit 的完整 payload、emit 故障下 finalize 吞异常；
- 生命周期：_shutdown_all 吞 shutdown 异常、on_unload 钩子、
  _atexit_cleanup 正常与工具构造失败两条路。

外部依赖打桩：FrontendEmitter.from_plugin（内核 frontend capability 通道）；
命令执行走真实 BashTool/ProcessManager（短命 echo，log_dir 指 tmp）。
server.py 以唯一模块名加载（跨插件 server.py 同名，同
tests/test_isolation_service_server.py 的加载纪律）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BASH_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "bash"
_SERVER_PATH = _BASH_DIR / "server.py"

# 平铺裸名逐出（server/tool/process_manager/bash_types 跨插件同名，仅逐出
# 指向 bash 目录之外的缓存）：保证 server.py 的 `from tool import BashTool`
# 在本文件模块加载期确定解析到 bash 插件目录。
sys.path.insert(0, str(_BASH_DIR))
for _bare in ("server", "tool", "process_manager", "bash_types", "progress_reporter"):
    _mod = sys.modules.get(_bare)
    _file = getattr(_mod, "__file__", None)
    if _mod is not None and _file is not None:
        try:
            _foreign = not Path(_file).resolve().is_relative_to(_BASH_DIR.resolve())
        except (OSError, ValueError):
            _foreign = True
        if _foreign:
            sys.modules.pop(_bare, None)

_spec = importlib.util.spec_from_file_location("bash_tool_server_under_test", _SERVER_PATH)
assert _spec is not None and _spec.loader is not None
server = importlib.util.module_from_spec(_spec)
sys.modules["bash_tool_server_under_test"] = server
_spec.loader.exec_module(server)

from process_manager import ProcessManager  # noqa: E402
from tool import BashTool  # noqa: E402


# ─────────────────────────── 桩 ───────────────────────────


class _StubProcessManager:
    """记录 shutdown 调用的假进程管理器（可注入故障）。"""

    def __init__(self, *, raise_on_shutdown: bool = False) -> None:
        self.shutdown_calls = 0
        self._raise = raise_on_shutdown

    async def shutdown_all(self, force: bool = True) -> int:
        self.shutdown_calls += 1
        if self._raise:
            raise RuntimeError("shutdown exploded")
        return 0


class _FakeEmitter:
    """frontend capability 的假出口：记录 emit 事件（可注入故障）。"""

    def __init__(self, *, fail_on_emit: bool = False) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail_on_emit

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._fail:
            raise RuntimeError("frontend channel down")
        self.events.append((event, payload))


def _patch_emitter(monkeypatch: pytest.MonkeyPatch, emitter: Any) -> None:
    import agentos_plugin_sdk.capability as cap_mod

    monkeypatch.setattr(
        cap_mod.FrontendEmitter,
        "from_plugin",
        classmethod(lambda cls, plugin: emitter),  # type: ignore[method-assign]
    )


def _real_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BashTool:
    """真实 BashTool 单例注入 server 模块（log_dir 指 tmp，不污染仓库）。"""
    tool = BashTool()
    tool.process_manager = ProcessManager(log_dir=tmp_path / "logs")
    monkeypatch.setattr(server, "_tool", tool)
    return tool


# ─────────────────────────── bash_execute 入口 ───────────────────────────


async def test_bash_execute_maps_failure_result_to_error_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _real_tool(monkeypatch, tmp_path)

    out = await server.bash_execute(**{"command": "rm -rf /"})  # type: ignore[arg-type]

    assert out["error_code"] == "SECURITY_CHECK_FAILED"
    assert "安全检查失败" in out["error"]


async def test_bash_execute_success_with_progress_forwarding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """带 call_context 的真实短命令：结果成功 + 收尾冲刷出 tool_progress 事件。"""
    emitter = _FakeEmitter()
    _patch_emitter(monkeypatch, emitter)
    _real_tool(monkeypatch, tmp_path)

    out = await server.bash_execute(
        **{
            "command": "echo bash_execute_progress_e2e",
            "working_dir": str(tmp_path),
            "timeout": 15,
            "_call_context": {
                "call_id": "c-e2e",
                "thread_id": "t-e2e",
                "pipeline_id": "p-e2e",
                "message_id": "m-e2e",
            },
        }
    )

    assert out["status"] == "completed"
    assert "bash_execute_progress_e2e" in out["output"]
    assert out["exit_code"] == 0
    forwarded = [payload for evt, payload in emitter.events if evt == "tool_progress"]
    assert forwarded, "finalize 冲刷后应有 tool_progress 事件推给前端"
    assert any("bash_execute_progress_e2e" in p["delta"] for p in forwarded)
    assert all(p["call_id"] == "c-e2e" for p in forwarded)


async def test_bash_execute_degrades_when_forwarder_build_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """前向器构建抛错 → 降级继续执行，工具结果不受影响。"""

    def _boom(cls: type, plugin: Any) -> Any:
        raise RuntimeError("capability lookup exploded")

    import agentos_plugin_sdk.capability as cap_mod

    monkeypatch.setattr(
        cap_mod.FrontendEmitter,
        "from_plugin",
        classmethod(_boom),  # type: ignore[method-assign]
    )
    _real_tool(monkeypatch, tmp_path)

    out = await server.bash_execute(
        **{
            "command": "echo still_runs",
            "working_dir": str(tmp_path),
            "timeout": 15,
            "_call_context": {"call_id": "c-3", "thread_id": "t-3"},
        }
    )

    assert out["status"] == "completed"
    assert "still_runs" in out["output"]


# ─────────────────────────── _progress_forwarder ───────────────────────────


async def test_progress_forwarder_degrades_without_routing_keys() -> None:
    """call_context 缺 thread_id → (None, None)，工具照常执行。"""
    report, finalize = server._progress_forwarder({"call_id": "c-1"})
    assert report is None
    assert finalize is None


async def test_progress_forwarder_degrades_without_frontend_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内核未注入 frontend capability → (None, None) 优雅降级。"""
    _patch_emitter(monkeypatch, None)
    report, finalize = server._progress_forwarder(
        {"call_id": "c-1", "thread_id": "t-1"}
    )
    assert report is None
    assert finalize is None


async def test_progress_forwarder_throttles_and_flushes_via_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1KB 阈值前缓冲不推；达标 flush → 消费者推完整路由键的 tool_progress。"""
    emitter = _FakeEmitter()
    _patch_emitter(monkeypatch, emitter)

    report, finalize = server._progress_forwarder(
        {
            "call_id": "c-1",
            "thread_id": "t-1",
            "pipeline_id": "p-1",
            "message_id": "m-1",
        }
    )
    assert report is not None
    assert finalize is not None

    report("hello " * 100)  # 600 字符：低于 1KB 阈值，暂不推送
    assert emitter.events == []  # 同步上报不调度事件循环，消费者未运行
    report("x" * 500)  # 累计 1100 ≥ 1KB → flush 入队

    await finalize()

    assert len(emitter.events) == 1
    event, payload = emitter.events[0]
    assert event == "tool_progress"
    assert payload["call_id"] == "c-1"
    assert payload["thread_id"] == "t-1"
    assert payload["pipeline_id"] == "p-1"
    assert payload["message_id"] == "m-1"
    assert payload["tool_name"] == "bash_execute"
    assert "hello" in payload["delta"] and "xxxxx" in payload["delta"]
    assert payload["bytes_read"] == len(("hello " * 100 + "x" * 500).encode("utf-8"))
    assert payload["elapsed_ms"] >= 0


async def test_progress_forwarder_finalizer_swallows_consumer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """消费者 emit 抛错 → finalize 吞掉，不向工具调用方传播。"""
    emitter = _FakeEmitter(fail_on_emit=True)
    _patch_emitter(monkeypatch, emitter)

    report, finalize = server._progress_forwarder(
        {"call_id": "c-2", "thread_id": "t-2"}
    )
    assert report is not None and finalize is not None
    report("y" * 1100)  # 触发 flush → 消费者 emit 抛错

    await finalize()  # 不抛

    assert emitter.events == []


# ─────────────────────────── 生命周期 ───────────────────────────


async def test_shutdown_all_swallows_process_manager_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubProcessManager(raise_on_shutdown=True)
    monkeypatch.setattr(server, "_tool", SimpleNamespace(process_manager=stub))

    await server._shutdown_all()  # 不抛

    assert stub.shutdown_calls == 1


async def test_on_unload_lifecycle_terminates_all_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubProcessManager()
    monkeypatch.setattr(server, "_tool", SimpleNamespace(process_manager=stub))

    await server._on_unload({})

    assert stub.shutdown_calls == 1


def test_atexit_cleanup_runs_shutdown_in_fresh_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubProcessManager()
    monkeypatch.setattr(server, "_tool", SimpleNamespace(process_manager=stub))

    server._atexit_cleanup()

    assert stub.shutdown_calls == 1


def test_atexit_cleanup_swallows_tool_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单例未初始化且 BashTool 构造失败 → 兜底清理吞掉，进程照常退出。"""

    class _BrokenTool:
        def __init__(self) -> None:
            raise RuntimeError("tool bootstrap exploded")

    monkeypatch.setattr(server, "_tool", None)
    monkeypatch.setattr(server, "BashTool", _BrokenTool)

    server._atexit_cleanup()  # 不抛

    assert server._tool is None

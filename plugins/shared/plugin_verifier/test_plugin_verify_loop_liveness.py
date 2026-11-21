# @feature: FP-0.2.〇 插件开发 | @ci: python-coverage
"""plugin_verify 工具的事件循环活性锁（异步审计 P2-7）。

根因：``server.py`` 的 ``plugin_verify`` 是 async handler，却直接调
``run_plugin_verification``——同步体内 ``subprocess.Popen`` + ``communicate
(timeout=timeout_s)``（默认 240s，上限 600s）与 ``_cleanup_scratch`` 的
``time.sleep`` 退避全在宿主事件循环里跑（独占进程插件不连坐，但验证期间
循环整体停摆）。

锁面（断行为）：
- 验证全程出事件循环：假身验证体同步阻塞 0.25s，心跳间隔不得出现 ≥0.1s
  空窗；假身线程 ≠ 事件循环线程；
- 语义不变：结果 dict 原样返回；``VerifierError`` 仍在工具边界收口成
  结构化错误（``status=error``，不向 MCP 抛裸异常）。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _PLUGIN_DIR.parent
for _d in (_PLUGIN_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker  # noqa: E402

_MODULE_NAME = "plugin_verifier_server_loop_liveness"

_RESULT: dict[str, Any] = {
    "success": True,
    "status": "completed",
    "plugin_id": "demo",
    "scope": "backend",
    "all_pass": True,
    "checks": [{"name": "tool_surface", "pass": True, "detail": ""}],
    "screenshots": [],
    "evidence_path": None,
    "error": None,
}


def _load_module() -> Any:
    """动态加载 server.py（独立模块名，避免与其他测试文件装载互相覆盖）。"""
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""
    async with LoopTicker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：验证执行出事件循环
# ============================================================


async def test_plugin_verify_keeps_event_loop_ticking(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证体 0.25s 阻塞期间心跳持续打点；结果 dict 原样返回。"""
    module = _load_module()
    threads: list[int] = []
    calls: list[tuple[Any, ...]] = []

    def _slow_verify(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        calls.append(args)
        threads.append(threading.get_ident())
        time.sleep(0.25)
        return dict(_RESULT)

    monkeypatch.setattr(module, "run_plugin_verification", _slow_verify)
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        result = await module.plugin_verify(
            plugin_path="plugins/shared/demo", workspace="/tmp/ws", timeout_s=90
        )

    assert ticker.max_gap < MAX_TICK_GAP
    assert result == _RESULT
    assert calls == [("/tmp/ws", "plugins/shared/demo", "backend", 90)]
    assert len(threads) == 1
    assert threads[0] != loop_thread, "验证脚本必须在事件循环线程之外执行"


async def test_plugin_verify_error_still_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    """VerifierError 仍在工具边界收口（下放不改变结构化错误语义）。"""
    module = _load_module()

    def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise module.VerifierError("插件目录越界")

    monkeypatch.setattr(module, "run_plugin_verification", _boom)

    result = await module.plugin_verify(plugin_path="../escape", workspace="/tmp/ws")

    assert result["success"] is False
    assert result["status"] == "error"
    assert result["error"] == "插件目录越界"
    assert result["all_pass"] is False

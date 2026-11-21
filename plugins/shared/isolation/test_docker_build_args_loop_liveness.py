# @feature: FP-0.2.五 隔离执行(docker provider) | @ci: python-coverage
"""docker provider 容器创建参数构建的事件循环活性锁（异步审计 P2-9）。

根因：``_build_run_args``（同步）经 ``_bridge_url_for_container`` 调
``_detect_host_gateway_ip``——后者 ``os.popen("ip route show default")``。
单命令毫秒级且仅 Linux 下执行（其余平台早退），本身不构成风险；但
``create_environment``（async）在事件循环里直调，探测一旦变慢（WSL 首次
拉起 ``ip``/PATH 冷缓存）就整体冻循环。修法 = 调用侧整体 ``to_thread``，
``_build_run_args`` 保持同步契约（既有同步调用面与测试不受影响）。

锁面（断行为）：
- 参数构建出事件循环：假身网关探测同步阻塞 0.25s，心跳间隔不得出现
  ≥0.1s 空窗；假身线程 ≠ 事件循环线程；
- 语义不变：容器创建成功信封（env_id/status/container_id）与命令面一致。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _PLUGIN_DIR.parent
for _d in (_PLUGIN_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

# 同插件既有 docker 车道替身/夹具（单仓单源，禁复制漂移）
import test_providers_docker as tpd  # noqa: E402
from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker  # noqa: E402


async def _not_found(container_id: str, timeout: float = 15) -> tuple[bool, str]:
    del container_id, timeout
    return False, "Error: No such container: unknown"


async def _created(name: str, run_args: list[str]) -> tuple[str, str]:
    del name, run_args
    return "abc123", ""


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""
    async with LoopTicker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：网关探测（参数构建）出事件循环
# ============================================================


async def test_create_environment_keeps_event_loop_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """网关探测 0.25s 阻塞期间心跳持续打点；创建成功信封语义照常。"""
    provider = tpd._provider()
    monkeypatch.setattr(provider, "_is_wsl_docker", lambda: False)
    monkeypatch.delenv("AGENTOS_BRIDGE_URL", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(provider, "_ensure_image", tpd._fake_ensure_image())
    monkeypatch.setattr(provider, "_start_one", _not_found)
    monkeypatch.setattr(provider, "_create_and_start", _created)
    monkeypatch.setattr(provider, "_run_cmd", tpd._CmdScript([(0, b"running", b"")]))

    threads: list[int] = []

    def _slow_gateway_probe() -> str:
        threads.append(threading.get_ident())
        time.sleep(0.25)
        return ""

    monkeypatch.setattr(provider, "_detect_host_gateway_ip", _slow_gateway_probe)
    loop_thread = threading.get_ident()

    async with LoopTicker() as ticker:
        env = await provider.create_environment(tpd._ctx(workspace=str(ws)), "c1")

    assert ticker.max_gap < MAX_TICK_GAP
    assert threads
    assert all(tid != loop_thread for tid in threads), (
        "容器参数构建（含网关探测）必须在事件循环线程之外"
    )
    assert env.env_id == "c1"
    assert env.status == tpd.EnvironmentStatus.READY.value
    assert env.provider_info["container_id"] == "abc123"
    assert env.provider_info["container_name"] == "c1"

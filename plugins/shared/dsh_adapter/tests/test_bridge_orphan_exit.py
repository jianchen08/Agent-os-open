# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""桥进程「父死子退」契约：stdin 关闭时桥必须自行退出。

事故现场（2026-11-12，dev 内核 PID 20280）：sidecar 多次 respawn 后遗留 5 个
``dsh-rpc-bridge.mjs`` 孤儿 node 进程，每个 ~110MB；其中 5 个孤儿桥的父
python 进程已消失（``ppid=<gone>``）。

根因：sidecar 被强制回收（respwan/崩溃）时 ``on_unload`` 不执行，
``bridge.py`` 的 ``shutdown()`` 不会发 ``shutdown`` 帧；Node 侧此前只认
``shutdown`` 帧，stdin EOF 无处理器 → 已 boot 的 DSH context 持有的定时器/
句柄让进程永久常驻。

契约（修复后）：stdin 关闭（= 宿主进程死亡，管道被 OS 关闭）⇒ 桥执行与
``shutdown`` 帧相同的 dispose 后退出 0。真机用例：node 缺失时 skip，不静默假绿。
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
BRIDGE_SCRIPT = PLUGIN_DIR / "runtime" / "dsh-rpc-bridge.mjs"

_NODE = shutil.which("node")


def _wait_ready(proc: subprocess.Popen[bytes], timeout: float = 20.0) -> None:
    """等桥在 stderr 打出 ready 行（stdout 是协议帧通道，不可用于判活）。"""
    assert proc.stderr is not None
    lines: queue.Queue[bytes] = queue.Queue()

    def _reader() -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            lines.put(line)

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            if proc.poll() is not None:
                raise AssertionError(f"桥进程提前退出 rc={proc.returncode}")
            continue
        if b"ready" in line:
            return
    raise AssertionError("桥未在超时内就绪（stderr 无 ready 行）")


@pytest.mark.skipif(_NODE is None, reason="node 不可用（真机车道）")
def test_bridge_exits_when_stdin_closes(tmp_path: Path) -> None:
    """stdin 关闭 ⇒ 桥退出。

    ``--require`` 预载一个常驻 interval，模拟已 boot 的 DSH context 持有的
    句柄——没有该句柄时 node 在 stdin EOF 后会自然退出，测不出差异。
    """
    preload = tmp_path / "hold-handle.cjs"
    preload.write_text("setInterval(() => {}, 1000)\n", encoding="utf-8")
    env = {**os.environ, "AGENTOS_DSH_RUNTIME_ROOT": ""}
    proc = subprocess.Popen(
        [_NODE, "--require", str(preload), str(BRIDGE_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
    )
    try:
        _wait_ready(proc)
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "stdin 关闭后桥仍未退出（父死子不退，孤儿进程）"
            ) from None
        assert rc == 0, f"桥退出码应为 0，实际 {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


if __name__ == "__main__":  # pragma: no cover - 手工调试入口
    sys.exit(pytest.main([__file__, "-q"]))

# @feature: FP-0.2.dev_toolkit | @ci: python-coverage
"""spawn_test_kernel.py 单测：二进制定位/保留端口/环境注入/就绪轮询/实例档案。

行为契约（断输入→输出）：
- 内核二进制缺失 → 带构建提示的 TestKernelError；release 优先于 debug；
- 9100/9101 保留端口硬拒绝（dev/装机现役端口不可占用）；
- 环境注入只改副本不改调用方环境；
- /health 轮询：活服务秒回，死端口按超时报错；
- 集成：真内核 scratch 拉起（自动空闲端口+scratch DB）→ 健康探针 → stop 后端口释放。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "skill-dev-toolkit" / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("spawn_test_kernel", SCRIPTS_DIR / "spawn_test_kernel.py")
stk = importlib.util.module_from_spec(_spec)
sys.modules["spawn_test_kernel"] = stk
_spec.loader.exec_module(stk)

pytestmark = pytest.mark.unit

_KERNEL_EXE = "agentos-kernel.exe" if sys.platform == "win32" else "agentos-kernel"


def test_resolve_missing_binary_hints_build(tmp_path: Path) -> None:
    with pytest.raises(stk.TestKernelError, match="缺失"):
        stk.resolve_kernel_binary(tmp_path)


def test_resolve_prefers_release_over_debug(tmp_path: Path) -> None:
    release = tmp_path / "kernel" / "target" / "release" / _KERNEL_EXE
    debug = tmp_path / "kernel" / "target" / "debug" / _KERNEL_EXE
    release.parent.mkdir(parents=True)
    release.write_bytes(b"fake")
    assert stk.resolve_kernel_binary(tmp_path) == release
    debug.parent.mkdir(parents=True)
    debug.write_bytes(b"fake")
    assert stk.resolve_kernel_binary(tmp_path) == release
    release.unlink()
    assert stk.resolve_kernel_binary(tmp_path) == debug


@pytest.mark.parametrize("reserved", [9100, 9101])
def test_validate_port_rejects_reserved(reserved: int) -> None:
    with pytest.raises(stk.TestKernelError, match="保留"):
        stk.validate_port(reserved)
    stk.validate_port(5233)


def test_build_kernel_env_pure_copy() -> None:
    base = {"PATH": "unchanged"}
    env = stk.build_kernel_env(5233, Path("scratch/k.db"), "pw123", base=base)
    assert env["AGENTOS_KERNEL_PORT"] == "5233"
    assert env["AGENTOS_DB_PATH"] == str(Path("scratch/k.db"))
    assert env["AGENTOS_ADMIN_PASSWORD"] == "pw123"
    assert base == {"PATH": "unchanged"}


def test_pick_free_port_in_range() -> None:
    port = stk.pick_free_port()
    assert 0 < port < 65536
    stk.validate_port(port)


def test_await_ready_with_local_health_server() -> None:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stk.await_ready(server.server_address[1], 5.0)
    finally:
        server.shutdown()
        server.server_close()


def test_await_ready_times_out_on_dead_port() -> None:
    with pytest.raises(stk.TestKernelError, match="就绪超时"):
        stk.await_ready(stk.pick_free_port(), 0.5)


def test_stop_without_instance_archive_raises(tmp_path: Path) -> None:
    with pytest.raises(stk.TestKernelError, match="实例"):
        stk.stop(tmp_path)


def test_stop_tolerates_already_dead_instance(tmp_path: Path) -> None:
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    victim.kill()
    victim.wait()
    run_dir = tmp_path / "run_dead"
    run_dir.mkdir()
    port = stk.pick_free_port()
    (run_dir / "instance.json").write_text(
        json.dumps({"pid": victim.pid, "port": port}), encoding="utf-8"
    )
    record = stk.stop(tmp_path, "run_dead")
    assert record["stopped"] is True


@pytest.mark.integration
def test_spawn_status_stop_real_kernel(tmp_path: Path) -> None:
    try:
        stk.resolve_kernel_binary(REPO_ROOT)
    except stk.TestKernelError as exc:
        pytest.skip(f"内核二进制未构建，跳过真拉起: {exc}")
    record = stk.spawn(REPO_ROOT, timeout_s=120.0, scratch_root=str(tmp_path))
    port = cast(int, record["port"])
    run_id = cast(str, record["run_id"])
    try:
        assert stk.probe_health(port) is True
        live = stk.status(tmp_path, run_id)
        assert live["health_ok"] is True
    finally:
        stopped = stk.stop(tmp_path, run_id)
    assert stopped["stopped"] is True
    assert stk.probe_health(port) is False

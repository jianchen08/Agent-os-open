# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""wsl_native 真机冒烟（后台进程生命周期全链）。

锁定契约（需求：bash 工具后台进程的二次访问——continue/input/terminate）：
- 起：start_process 经 wsl 传输起长跑进程，`echo $$` pid 协议捕获 Linux 侧 pid；
- 续：进程保持 running，宿主侧可反复 send_input 喂 stdin；
- 通：stdin 数据真实到达 Linux 侧进程（落盘文件证明，绕开日志攒批）；
- 杀：terminate 走 WslNativeProcessBackend，按 container_pid 杀，Linux 侧确认死亡。

门控：AGENTOS_WSL_NATIVE_E2E=1 且平台为 Windows 且 WSL 可用才执行——
真实 wsl.exe 依赖，默认车道跳过。环境配方见 ADR
2026-09-14-wsl-native-isolation-provider.md。

注意：全生命周期必须在单一事件循环内执行——subprocess 管道绑定出生 loop，
跨 loop 调 send_input 会落到已关闭的 loop 上（ok=False 假失败）。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

_BASH_DIR = str(Path(__file__).resolve().parents[1] / "plugins" / "shared" / "tools" / "bash")
if _BASH_DIR not in sys.path:
    sys.path.insert(0, _BASH_DIR)


def _wsl_ubuntu_available() -> bool:
    import subprocess

    try:
        result = subprocess.run(  # noqa: PLW1510
            ["wsl.exe", "-l", "-q"], capture_output=True, timeout=15
        )
        if result.returncode != 0:
            return False
        out = result.stdout or b""
        text = (
            out.decode("utf-16-le", errors="replace")
            if b"\x00" in out
            else out.decode("utf-8", errors="replace")
        )
        return "Ubuntu" in [x.strip() for x in text.replace("\r\n", "\n").split("\n") if x.strip()]
    except Exception:
        return False


_REQUIREMENTS_MET = (
    os.environ.get("AGENTOS_WSL_NATIVE_E2E") == "1"
    and sys.platform == "win32"
    and _wsl_ubuntu_available()
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _REQUIREMENTS_MET,
        reason="真机依赖：Windows + Ubuntu 发行版 + AGENTOS_WSL_NATIVE_E2E=1",
    ),
]


def _to_wsl_path(path: Path) -> str:
    normalized = str(path).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", normalized)
    assert m is not None
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}"


def test_background_process_full_lifecycle(tmp_path: Path) -> None:
    from process_manager import ProcessManager

    marker = tmp_path / "stdin_proof.txt"
    log_dir = tmp_path / "logs"
    pm = ProcessManager(log_dir=log_dir)
    backend = {
        "backend": "wsl_native",
        "wsl_exe": "wsl",
        "distro": "Ubuntu",
        "user": None,
        "sandbox_cmd": [],
        "workspace_wsl": _to_wsl_path(tmp_path),
    }
    cmd = (
        "echo READY; "
        f"read line; echo \"$line\" > {_to_wsl_path(marker)}; echo WROTE; "
        "sleep 60"
    )

    async def _lifecycle() -> None:
        pid, _ = await pm.start_process(
            command=cmd,
            working_dir="/workspace",
            log_dir=log_dir,
            container_id="cua-ws1",
            exec_backend=backend,
        )

        # pid 协议：container_pid 是 Linux 侧 pid（非宿主 wsl.exe pid）
        info = pm.active_processes[pid]
        assert info.metadata["container_pid"] != pid
        assert info.metadata["exec_backend"]["backend"] == "wsl_native"

        # 二次访问：喂 stdin → 落盘证明数据真实到达 Linux 侧进程
        await asyncio.sleep(2)
        ok, input_err = await pm.send_input(pid, "stdin-proof-12345\n")
        assert ok is True, (
            f"send_input 失败: {input_err} | "
            f"status={pm.active_processes[pid].status if pid in pm.active_processes else 'GONE'}"
        )
        await asyncio.sleep(2)
        assert marker.exists(), "stdin 数据未到达 Linux 侧进程"
        assert "stdin-proof-12345" in marker.read_text(encoding="utf-8", errors="replace")

        # terminate：按 container_pid 杀
        linux_pid = info.metadata["container_pid"]
        terminated, term_err = await pm.terminate_process(pid, force=True)
        assert terminated is True, f"terminate 失败: {term_err}"
        await asyncio.sleep(1)

        proc = await asyncio.create_subprocess_exec(
            "wsl.exe", "--exec", "sh", "-c",
            f"kill -0 {linux_pid} 2>/dev/null && echo ALIVE || echo DEAD",
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        assert out.decode(errors="replace").strip() == "DEAD", (
            "terminate 后 Linux 侧进程仍存活"
        )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_lifecycle())
    finally:
        loop.close()

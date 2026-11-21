# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""wsl_native 沙箱包装器 profile 测试（ADR 2026-11-10-workspace-only-sandbox）。

钉的是**沙箱可见面的不变式**，不是脚本实现细节：
1. 整机遮蔽：`--tmpfs /mnt` 必须在，工作空间按真实路径 `--bind` 挂回；
2. 逃逸面：docker socket 存在时必须有 `/dev/null` 遮蔽；
3. pid 协议：不得出现 `--unshare-pid`（`echo $$` 上报宿主 pid 的前提）；
4. 能力面：`--cap-drop ALL`；
5. fail-closed：bwrap 缺失时非零退出且命令不执行；
6. 真跑：沙箱内 `/mnt/c` 不可见、工作空间可写。

依赖 WSL 发行版（`wsl.exe`）与已装的 bwrap；两者缺一即 skip——本文件是
本地车道（与 test_providers_wsl_native.py 同口径 @ci: none-local）。
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPER = REPO_ROOT / "plugins" / "shared" / "isolation" / "scripts" / "aos-sandbox.sh"
DISTRO = "Ubuntu"
SANDBOX_DIR = "/tmp/aos-sandbox-selftest"


def _wsl(script: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["wsl.exe", "-d", DISTRO, "--exec", "bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


@pytest.fixture(scope="module")
def sandbox_path() -> str:
    """把仓内包装脚本装进发行版临时目录，返回发行版侧路径。"""
    probe = _wsl("echo ok")
    if probe.returncode != 0 or "ok" not in (probe.stdout or ""):
        pytest.skip(f"WSL 发行版 {DISTRO} 不可用")
    if not WRAPPER.is_file():
        pytest.skip(f"包装脚本缺失: {WRAPPER}")
    payload = base64.b64encode(WRAPPER.read_bytes()).decode("ascii")
    script = (
        f"mkdir -p {SANDBOX_DIR} && "
        f"printf %s {payload} | base64 -d > {SANDBOX_DIR}/aos-sandbox && "
        f"chmod 0755 {SANDBOX_DIR}/aos-sandbox && echo ok"
    )
    proc = _wsl(script)
    if proc.returncode != 0:
        pytest.skip(f"包装脚本投放失败: {proc.stderr}")
    return f"{SANDBOX_DIR}/aos-sandbox"


@pytest.fixture(scope="module")
def has_bwrap() -> bool:
    proc = _wsl('command -v bwrap || "$HOME"/.agentos/bin/bwrap --version')
    return proc.returncode == 0


def _groups(args: list[str], flag: str, width: int) -> list[list[str]]:
    """收集 `flag a [b]` 形态的参数组（同一 flag 可多次出现，不能只看首个）。"""
    return [args[i + 1 : i + 1 + width] for i, token in enumerate(args) if token == flag]


class TestProfileInvariants:
    """dry-run argv 的可见面不变式。"""

    def _dry_run(self, sandbox_path: str) -> list[str]:
        proc = _wsl(f"cd {SANDBOX_DIR} && AOS_SANDBOX_DRY_RUN=1 {sandbox_path} bash -c true")
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.splitlines()

    def test_mnt_is_masked_and_workspace_rebound(self, sandbox_path: str) -> None:
        args = self._dry_run(sandbox_path)

        # 整机遮蔽：/mnt 整体换成空 tmpfs
        assert ["/mnt"] in _groups(args, "--tmpfs", 1)

        # 工作空间按真实路径挂回可写
        assert [SANDBOX_DIR, SANDBOX_DIR] in _groups(args, "--bind", 2)

    def test_root_is_read_only(self, sandbox_path: str) -> None:
        args = self._dry_run(sandbox_path)
        assert ["/", "/"] in _groups(args, "--ro-bind", 2)

    def test_no_pid_namespace(self, sandbox_path: str) -> None:
        """--unshare-pid 会让 echo $$ 上报 namespace 内 pid，宿主杀不掉后台进程。"""
        assert "--unshare-pid" not in self._dry_run(sandbox_path)

    def test_capabilities_dropped(self, sandbox_path: str) -> None:
        args = self._dry_run(sandbox_path)
        assert ["ALL"] in _groups(args, "--cap-drop", 1)

    def test_docker_socket_masked_when_present(self, sandbox_path: str) -> None:
        exists = _wsl("[ -S /run/docker.sock ] && echo yes")
        if "yes" not in (exists.stdout or ""):
            pytest.skip("发行版内无 docker socket")
        args = self._dry_run(sandbox_path)
        assert ["/dev/null", "/run/docker.sock"] in _groups(args, "--ro-bind", 2)

    def test_toolchain_home_is_writable_carveout(self, sandbox_path: str) -> None:
        """根只读会堵死 cargo/rustup 写面（ADR 2026-11-07 工具链持久层）。"""
        args = self._dry_run(sandbox_path)
        assert any(pair[0].endswith("/.cargo") for pair in _groups(args, "--bind", 2))


class TestFailClosed:
    """bwrap 不可用时必须拒跑，不静默落回宿主。"""

    def test_missing_bwrap_exits_nonzero(self, sandbox_path: str) -> None:
        proc = _wsl(
            f"cd {SANDBOX_DIR} && AOS_SANDBOX_BWRAP=/nonexistent/bwrap "
            f"{sandbox_path} bash -c 'echo SHOULD_NOT_RUN'"
        )
        assert proc.returncode != 0
        assert "SHOULD_NOT_RUN" not in (proc.stdout or "")
        assert "bubblewrap" in (proc.stderr or "")

    def test_refuses_root_workdir(self, sandbox_path: str) -> None:
        proc = _wsl(f"cd / && {sandbox_path} bash -c 'echo SHOULD_NOT_RUN'")
        assert proc.returncode != 0
        assert "SHOULD_NOT_RUN" not in (proc.stdout or "")


class TestRealSandbox:
    """真跑：需要发行版内可用的 bwrap。"""

    def test_host_drives_invisible_and_workspace_writable(
        self, sandbox_path: str, has_bwrap: bool
    ) -> None:
        if not has_bwrap:
            pytest.skip("发行版内无 bwrap")
        proc = _wsl(
            f"cd {SANDBOX_DIR} && {sandbox_path} bash -c "
            "'ls /mnt/c >/dev/null 2>&1 && echo C_VISIBLE || echo C_HIDDEN; "
            "touch .probe && echo WRITE_OK; rm -f .probe'"
        )
        assert proc.returncode == 0, proc.stderr
        assert "C_HIDDEN" in proc.stdout
        assert "WRITE_OK" in proc.stdout

# @feature: FP-DEPLOY deploy-manifest 回读校验（diff 更新是否落地） | @ci: python-test
"""deploy/verify_deploy.sh 端到端：Linux 部署位回读（真实 bash）。

行为契约（临时树 + 真实脚本，不碰真部署）：
- 部署面与清单一致 → 退出 0（清单回读 + 内核/cdylib 断言全绿）；
- 篡改关键项（plugin.json）→ 退出 1（关键项未落地）；
- --warn → 同样篡改仍退出 0（手工维护放行逃生口）；
- 旧包无清单 → 退出 0 且 1/3 打印 SKIP（不阻断历史部署）。

bash 缺失环境跳过（CI 覆盖车道均有 bash）。仅 Linux 跑：脚本契约含
``[ -x 内核二进制 ]``，Windows/MSYS 的 NTFS 无 exec 位（chmod 不生效），
在 Windows 上会恒判「内核二进制缺失」，属平台假阴性而非脚本缺陷。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform != "linux", reason="部署脚本契约依赖 Linux exec 位（MSYS/NTFS 无 -x）"),
    pytest.mark.skipif(BASH is None, reason="bash 不可用（部署脚本车道需 bash）"),
]

PLUGINS = ("tool_core", "sensitive_checker", "spill_guard")


def _write(root: Path, rel: str, payload: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _make_tree(tmp_path: Path) -> Path:
    """造 linux profile 部署根：部署面 + 服务器现编产物（kernel-bin/cdylib）。"""
    deploy = tmp_path / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "deploy" / "verify_deploy.sh", deploy / "verify_deploy.sh")
    shutil.copy(REPO_ROOT / "deploy" / "verify_deploy_manifest.py", deploy / "verify_deploy_manifest.py")

    _write(tmp_path, "plugins/shared/alpha/plugin.json", b'{"id":"alpha"}')
    _write(tmp_path, "plugins/shared/alpha/main.py", b"print('alpha')")
    for name in PLUGINS:
        _write(tmp_path, f"plugins/shared/{name}/lib{name}.so", b"elf")
    _write(tmp_path, "plugins/sdk/src/__init__.py", b"")
    _write(tmp_path, "frontend/dist/index.html", b"<html></html>")
    _write(tmp_path, "config/kernel/instance.yaml", b"schema: 1")
    _write(tmp_path, "skills/demo/SKILL.md", b"# demo")

    kernel = tmp_path / "kernel-bin" / "agentos-kernel"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"kernel")
    kernel.chmod(0o755)
    return tmp_path


def _build_manifest(root: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(root / "deploy" / "verify_deploy_manifest.py"),
            "build",
            "--root", str(root),
            "--out", str(root / "deploy" / "deploy-manifest.json"),
            "--profile", "linux",
            "--version", "0.2.2",
            "--build-head", "deadbeef",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _run_verify(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(root / "deploy" / "verify_deploy.sh"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(root),
        check=False,
        env={**os.environ, "AGENTOS_DEPLOY_VERIFY": ""},
    )


def test_green_deployment_passes(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    _build_manifest(root)
    result = _run_verify(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS 部署位与发版清单一致" in result.stdout
    assert "PASS 内核二进制" in result.stdout
    assert "PASS cdylib tool_core" in result.stdout


def test_tampered_critical_file_red(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    _build_manifest(root)
    (root / "plugins" / "shared" / "alpha" / "plugin.json").write_bytes(b'{"id":"alpha","stale":true}')
    result = _run_verify(root)
    assert result.returncode == 1
    assert "FAIL" in result.stderr
    assert "plugins/shared/alpha/plugin.json" in result.stdout


def test_warn_mode_lets_manual_maintenance_through(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    _build_manifest(root)
    (root / "plugins" / "shared" / "alpha" / "plugin.json").write_bytes(b'{"id":"alpha","manual":1}')
    result = _run_verify(root, "--warn")
    assert result.returncode == 0
    assert "WARN" in result.stdout


def test_legacy_package_without_manifest_skips(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)  # 不生成清单 = 旧包形态
    result = _run_verify(root)
    assert result.returncode == 0
    assert "SKIP 无 deploy/deploy-manifest.json" in result.stdout

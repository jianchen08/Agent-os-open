# @feature: FP-DEPLOY deploy-manifest 回读校验（diff 更新是否落地） | @ci: python-coverage
"""scripts/electron-after-pack.cjs 钩子契约（Windows 安装包清单生成）。

行为契约：
- win32：对 <appOutDir>/resources 生成 deploy-manifest.json，version 取
  packager.appInfo.version，build_head 取 AGENTOS_BUILD_HEAD（发布链注入）；
- 非 win32：跳过且不产出清单（mac/linux 桌面包不是本仓交付面）；
- fail-closed：资源根缺失 → 抛错中止出包（清单缺失会让装后校验静默降级）。

node 缺失的环境跳过（CI 覆盖车道有 node；纯 python 沙箱不红）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "electron-after-pack.cjs"

NODE = shutil.which("node")

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(NODE is None, reason="node 不可用（本仓 CI 覆盖车道有 node）"),
]

_DRIVER = """
const hook = require(process.argv[2]);
hook({
  appOutDir: process.argv[3],
  electronPlatformName: process.argv[4],
  packager: { appInfo: { version: "0.2.2" } },
})
  .then(() => process.exit(0))
  .catch((err) => {
    console.error(String(err && err.message ? err.message : err));
    process.exit(1);
  });
"""


def _write(root: Path, rel: str, payload: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _packed_tree(app_out: Path) -> Path:
    """最小 win32 打包产物树（resources 七根 + app.asar + external MCP 受管目录）。"""
    resources = app_out / "resources"
    _write(resources, "app.asar", b"asar")
    _write(resources, "kernel/agentos-kernel.exe", b"kernel")
    _write(resources, "frontend/dist/index.html", b"<html></html>")
    _write(resources, "plugins/shared/alpha/plugin.json", b'{"id":"alpha"}')
    _write(resources, "plugins/shared/_host/host.py", b"host")
    _write(resources, "plugins/shared/mcp_windows_mcp/plugin.json", b'{"id":"mcp_windows_mcp"}')
    _write(resources, "plugins/shared/mcp_windows_mcp/.mcp-managed", b"generated")
    _write(resources, "config/kernel/instance.yaml", b"schema: 1")
    _write(resources, "skills/demo/SKILL.md", b"# demo")
    _write(resources, "prereqs/uv.exe", b"uv")
    _write(resources, "bin/rclone.exe", b"rclone")
    return resources


def _run_hook(tmp_path: Path, app_out: Path, platform: str, build_head: str = "deadbeef") -> subprocess.CompletedProcess[str]:
    assert NODE is not None  # 模块级 skipif 已保证非 None；此处收窄类型供 mypy
    driver = tmp_path / "driver.cjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    env = dict(os.environ)
    env["AGENTOS_BUILD_HEAD"] = build_head
    return subprocess.run(
        [NODE, str(driver), str(HOOK), str(app_out), platform],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def test_hook_generates_manifest_with_build_head(tmp_path: Path) -> None:
    app_out = tmp_path / "win-unpacked"
    resources = _packed_tree(app_out)

    result = _run_hook(tmp_path, app_out, "win32")
    assert result.returncode == 0, result.stderr

    manifest = json.loads((resources / "deploy-manifest.json").read_text(encoding="utf-8"))
    assert manifest["profile"] == "windows"
    assert manifest["version"] == "0.2.2"
    assert manifest["build_head"] == "deadbeef"
    assert "kernel/agentos-kernel.exe" in manifest["critical"]
    assert "deploy-manifest.json" not in manifest["files"]  # 清单不进自身


def test_hook_strips_managed_external_mcp_dir(tmp_path: Path) -> None:
    """external MCP 受管生成目录（.mcp-managed 标记）出包前剥离，不进清单。

    派生面不随包（2026-11-20 启动优化 C）：升级带回陈旧受管副本会在装机 tier1
    与用户根副本间构成同 id 双源抖动。手写插件（无标记）不受影响。
    """
    app_out = tmp_path / "win-unpacked"
    resources = _packed_tree(app_out)

    result = _run_hook(tmp_path, app_out, "win32")
    assert result.returncode == 0, result.stderr

    assert not (resources / "plugins/shared/mcp_windows_mcp").exists()
    assert (resources / "plugins/shared/alpha/plugin.json").is_file()
    manifest = json.loads((resources / "deploy-manifest.json").read_text(encoding="utf-8"))
    assert all(not f.startswith("plugins/shared/mcp_windows_mcp") for f in manifest["files"])


def test_hook_skips_non_windows(tmp_path: Path) -> None:
    app_out = tmp_path / "linux-unpacked"
    resources = _packed_tree(app_out)

    result = _run_hook(tmp_path, app_out, "linux")
    assert result.returncode == 0, result.stderr
    assert not (resources / "deploy-manifest.json").exists()


def test_hook_fail_closed_without_resources(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, tmp_path / "win-unpacked", "win32")
    assert result.returncode != 0
    assert "resources 目录不存在" in result.stderr

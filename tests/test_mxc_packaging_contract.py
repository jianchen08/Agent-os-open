# @feature: FP-0.2.PACK MXC 运行时随包 | @ci: python-coverage
"""MXC 运行时打包契约：取件脚本 → build 产物 → resources/mxc → 内核 env。

锁点（打包链任何一环断掉都应在单测层直接红）：
1. ``scripts/mxc_runtime.json`` pin 版本/资产/成员 SHA-256；
2. ``electron:build`` 在 electron-builder 之前跑取件脚本；
3. ``build.win.extraResources`` 把 ``build/mxc-runtime`` 复制到 ``resources/mxc``；
4. electron 侧 kernel env 注入 ``AGENTOS_MXC_EXECUTOR``（ambient 优先）；
5. 打包布局门禁要求 Windows 产物携带 ``mxc/x64/wxc-exec.exe``；
6. 许可证随包（MIT）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = _ROOT / "package.json"
_MANIFEST = _ROOT / "scripts" / "mxc_runtime.json"
_FETCHER = _ROOT / "scripts" / "fetch_mxc_runtime.py"
_LAYOUT_GATE = _ROOT / "scripts" / "check_packaged_layout_gate.py"
_KERNEL_MANAGER = _ROOT / "electron" / "kernel-manager.ts"
_LICENSE = _ROOT / "third_party" / "mxc" / "LICENSE.md"

_REQUIRED_MEMBERS = {
    "x64/wxc-exec.exe",
    "x64/mxc_ffi.dll",
    "x64/wslcsdk.dll",
    "x64/wxc-wslc-daemon.exe",
    "x64/wxc-windows-sandbox-guest.exe",
    "x64/wxc-windows-sandbox-daemon.exe",
}


def test_manifest_pins_version_asset_and_members() -> None:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["version"] == "1.1.0"
    assert manifest["source"]["asset_id"] == 749925569
    assert manifest["source"]["size"] == 533829612
    members = {item["path"]: item for item in manifest["members"]}
    assert set(members) >= _REQUIRED_MEMBERS
    for path in _REQUIRED_MEMBERS:
        assert members[path]["size"] > 0
        assert re.fullmatch(r"[0-9a-f]{64}", members[path]["sha256"]), path


def test_build_chain_fetches_before_packaging() -> None:
    package = json.loads(_PACKAGE.read_text(encoding="utf-8"))
    build = package["scripts"]["electron:build"]
    assert "python scripts/fetch_mxc_runtime.py" in build
    assert build.index("fetch_mxc_runtime.py") < build.index("electron-builder")


def test_extra_resources_maps_mxc_runtime_to_resources_mxc() -> None:
    package = json.loads(_PACKAGE.read_text(encoding="utf-8"))
    resources = package["build"]["win"]["extraResources"]
    assert any(
        entry.get("from") == "build/mxc-runtime" and entry.get("to") == "mxc"
        for entry in resources
    )


def test_kernel_env_injects_packaged_executor() -> None:
    text = _KERNEL_MANAGER.read_text(encoding="utf-8")
    assert "mxcExe" in text
    assert "AGENTOS_MXC_EXECUTOR" in text
    assert 'path.join(resourcesPath, "mxc", "x64", "wxc-exec.exe")' in text


def test_packaged_layout_gate_requires_mxc_executor() -> None:
    text = _LAYOUT_GATE.read_text(encoding="utf-8")
    assert "_check_mxc_runtime" in text
    assert "MXC_EXECUTOR_MISSING" in text


def test_fetcher_pins_sha_and_is_idempotent_capable() -> None:
    text = _FETCHER.read_text(encoding="utf-8")
    assert "SHA-256 不符" in text
    assert "--check" in text
    assert "--force" in text


def test_license_is_shipped_with_runtime() -> None:
    assert _LICENSE.is_file()
    text = _LICENSE.read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Microsoft Corporation" in text


def _load_layout_gate() -> Any:
    import importlib.util
    import sys

    name = "check_packaged_layout_gate_mxc_test_mod"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _LAYOUT_GATE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_layout_gate_requires_executor_and_manifest(tmp_path: Path) -> None:
    gate = _load_layout_gate()
    (tmp_path / "kernel").mkdir()
    (tmp_path / "kernel" / "agentos-kernel.exe").write_bytes(b"")
    problems = gate._check_mxc_runtime(tmp_path)
    assert any("MXC_EXECUTOR_MISSING" in item for item in problems)
    assert any("MXC_MANIFEST_MISSING" in item for item in problems)
    (tmp_path / "mxc" / "x64").mkdir(parents=True)
    (tmp_path / "mxc" / "x64" / "wxc-exec.exe").write_bytes(b"")
    (tmp_path / "mxc" / "MXC_RUNTIME_MANIFEST.json").write_text("{}", encoding="utf-8")
    assert gate._check_mxc_runtime(tmp_path) == []


def test_layout_gate_skips_non_windows_products(tmp_path: Path) -> None:
    gate = _load_layout_gate()
    assert gate._check_mxc_runtime(tmp_path) == []

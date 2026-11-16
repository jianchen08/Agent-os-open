# @feature: FP-RELEASE 发布待检器 | @ci: python-coverage
"""check_release_pending.py 单测（统一发布管线 CI 载体消费口）。

行为契约（断输入→输出，不钉实现）：
- 无 release 目录 / manifest 缺失 / semver 不一致 / manifest 不可解析 → 待发布（10）；
- manifest 存在且 semver 与 VERSION 一致 → 已发布（0）；
- VERSION 缺失 / 格式非法 → 环境不可判（2），不当成待发布静默放过（fail-closed）；
- 真仓负控制：退出码与 release/<VERSION>/ 实况一致（不硬编码期望值）。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location("check_release_pending", SCRIPTS / "check_release_pending.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _make_repo(root: Path, version: str = "0.2.1") -> None:
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")


def _write_manifest(root: Path, version: str, semver: str | None = None) -> Path:
    manifest_dir = root / "release" / version
    manifest_dir.mkdir(parents=True, exist_ok=True)
    version_entry: dict = {"version_code": 24}
    version_entry["semver"] = semver if semver is not None else version
    if semver == "__missing__":
        del version_entry["semver"]  # 坏形态：semver 键整个缺失
    payload = {
        "schema_version": 1,
        "build_head": "a" * 40,
        "version": version_entry,
        "artifacts": {},
    }
    path = manifest_dir / "delivery_manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_no_release_dir_pending(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    version, pending, reason = mod.check_pending(tmp_path)
    assert version == "0.2.1"
    assert pending is True
    assert "不存在" in reason


def test_manifest_match_up_to_date(tmp_path: Path) -> None:
    _make_repo(tmp_path, "1.0.0")
    _write_manifest(tmp_path, "1.0.0")
    _, pending, reason = mod.check_pending(tmp_path)
    assert pending is False
    assert "已发布" in reason


@pytest.mark.parametrize(
    ("written", "needle"),
    [
        ("0.2.0", "!="),  # manifest 版本与 VERSION 漂移
        ("__missing__", "!="),  # manifest semver 键缺失
    ],
)
def test_semver_mismatch_pending(tmp_path: Path, written: str, needle: str) -> None:
    _make_repo(tmp_path, "0.2.1")
    _write_manifest(tmp_path, "0.2.1", semver=written)
    _, pending, reason = mod.check_pending(tmp_path)
    assert pending is True
    assert needle in reason


def test_unparseable_manifest_pending(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    manifest_dir = tmp_path / "release" / "0.2.1"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "delivery_manifest.json").write_text("{not json", encoding="utf-8")
    _, pending, reason = mod.check_pending(tmp_path)
    assert pending is True
    assert "不可解析" in reason


@pytest.mark.parametrize("bad", ["", "0.2", "v1"])
def test_malformed_version_error(tmp_path: Path, bad: str) -> None:
    (tmp_path / "VERSION").write_text(f"{bad}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="三段式"):
        mod.check_pending(tmp_path)


def test_missing_version_file_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="缺失"):
        mod.check_pending(tmp_path)


def test_real_repo_consistent_with_release_dir() -> None:
    """真仓负控制：退出码与 release/<VERSION>/ 实况一致（防实现钉死某态）。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_release_pending.py")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    manifest = REPO_ROOT / "release" / version / "delivery_manifest.json"
    if manifest.is_file():
        semver = json.loads(manifest.read_text(encoding="utf-8"))["version"]["semver"]
        expected = 0 if semver == version else 10
    else:
        expected = 10
    assert proc.returncode == expected, f"stdout={proc.stdout} stderr={proc.stderr}"
    assert proc.returncode in (0, 10)

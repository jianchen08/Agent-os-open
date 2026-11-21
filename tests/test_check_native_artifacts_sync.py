# @feature: FP-0.2.〇 插件生态 | @ci: python-coverage
"""check_native_artifacts_sync 判定逻辑单测。

覆盖：artifact_staleness 四象限（产物缺失 / 仅 ABI 过期 / 仅插件源码过期 /
双新）；latest_source_mtime 只认 rs/toml/Cargo.lock，且跳过 target/.venv/
node_modules（否则一次构建/同步就把产物误判过期）。

运行面真身口径（2026-11-09 事故）：内核加载的是插件目录根那份 cdylib，改过
插件源码不重编 = 跑旧逻辑；门禁必须按插件自身源码判过期，不能只比 native-sdk。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_native_artifacts_sync.py"
_spec = importlib.util.spec_from_file_location("check_native_artifacts_sync", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_native_artifacts_sync"] = mod
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_staleness_matrix(tmp_path: Path) -> None:
    dll = _touch(tmp_path / "x.dll", 200.0)
    assert mod.artifact_staleness(dll, 100.0, 150.0) == (False, False), "双新不判过期"
    assert mod.artifact_staleness(dll, 250.0, 150.0) == (True, False), "早于 native-sdk"
    assert mod.artifact_staleness(dll, 100.0, 250.0) == (False, True), "早于插件源码"
    assert mod.artifact_staleness(dll, 250.0, 250.0) == (True, True), "双旧"
    assert mod.artifact_staleness(tmp_path / "missing.dll", 100.0, 150.0) == (True, True), "缺失"


def test_latest_source_mtime_scope(tmp_path: Path) -> None:
    src = tmp_path / "plugin"
    _touch(src / "src" / "lib.rs", 100.0)
    _touch(src / "Cargo.toml", 120.0)
    _touch(src / "Cargo.lock", 110.0)
    # 依赖/产物目录不算源码变更。
    _touch(src / "target" / "release" / "junk.rs", 900.0)
    _touch(src / ".venv" / "Lib" / "junk.rs", 900.0)
    _touch(src / "node_modules" / "junk.toml", 900.0)
    assert mod.latest_source_mtime(src) == 120.0
    assert mod.latest_source_mtime(tmp_path / "absent") == 0.0

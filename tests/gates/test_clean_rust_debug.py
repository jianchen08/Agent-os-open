# @feature: FP-0.2.CFG 门禁脚本（Rust 编译产物回收） | @ci: python-coverage
# @feature: rust-artifact-sweep 编译产物增量回收 | @ci: python-coverage
"""clean_rust_debug 增量回收（--older-than，per-crate hash 簇策略）单测。

覆盖可单测面：
1. 簇判定与保留方向（同 crate 旧 hash 簇删、最新簇留、伴生文件同簇）；
2. 单副本 crate 保护（无论多老都不删——lockfile 未变的活依赖缓存）；
3. 阈值缓冲（冷置未满 N 天的旧簇保留，parametrize 多档）；
4. 目录簇（build/.fingerprint 的 {stem}-{hash}/ 形态）；
5. 无 hash 命名保守不动（顶层链接产物、build 共享输出目录）；
6. 占用容错（unlink 失败计 skipped、不中断、其余照删）；
7. main 端到端（ROOT 注入、kernel + plugins 双范围、llvm-cov-target 的
   profile 下探、恒 exit 0、非正阈值拒绝）。
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


crd = _load("clean_rust_debug")

HOUR = 3600
DAY = 86400
H1 = "aaaa1111bbbb2222"
H2 = "cccc3333dddd4444"


def _make_file(path: Path, size: int, age_sec: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    ts = time.time() - age_sec
    os.utime(path, (ts, ts))
    return path


def _make_dir(path: Path, age_sec: float) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _make_file(path / "inner.bin", 8, age_sec)
    ts = time.time() - age_sec
    os.utime(path, (ts, ts))
    return path


def test_old_hash_cluster_removed_new_kept_with_companions(tmp_path: Path) -> None:
    deps = tmp_path / "deps"
    old = [
        _make_file(deps / f"libagentos_api-{H1}.rlib", 100, age_sec=10 * DAY),
        _make_file(deps / f"agentos_api-{H1}.d", 2, age_sec=10 * DAY),
        _make_file(deps / f"agentos_api-{H1}.pdb", 50, age_sec=10 * DAY),
    ]
    new = [
        _make_file(deps / f"libagentos_api-{H2}.rlib", 100, age_sec=1 * HOUR),
        _make_file(deps / f"agentos_api-{H2}.pdb", 50, age_sec=1 * HOUR),
    ]

    stats = crd.SweepStats()
    crd.sweep_old_fingerprints(tmp_path, time.time() - 3 * DAY, stats)

    assert all(not p.exists() for p in old)
    assert all(p.exists() for p in new)
    assert stats.removed == 3
    assert stats.freed == 152


def test_single_cluster_survives_regardless_of_age(tmp_path: Path) -> None:
    """lockfile 未变的第三方依赖只有单 hash 簇：无论多老都是活缓存，不许删。"""
    deps = tmp_path / "deps"
    ancient = _make_file(deps / "libtokio-1234abcd5678ef90.rlib", 500, age_sec=30 * DAY)

    stats = crd.SweepStats()
    crd.sweep_old_fingerprints(tmp_path, time.time() - 3 * DAY, stats)

    assert ancient.exists()
    assert stats.removed == 0 and stats.freed == 0


@pytest.mark.parametrize("old_age_days", [0.5, 1, 2])
def test_threshold_grace_period_keeps_recently_replaced(tmp_path: Path, old_age_days: float) -> None:
    """冷置未满 3 天的旧 hash 簇保留（给源码回退留缓冲窗口）。"""
    deps = tmp_path / "deps"
    stale = _make_file(deps / f"libserde-{H1}.rlib", 10, age_sec=old_age_days * DAY)
    current = _make_file(deps / f"libserde-{H2}.rlib", 10, age_sec=1 * HOUR)

    stats = crd.SweepStats()
    crd.sweep_old_fingerprints(tmp_path, time.time() - 3 * DAY, stats)

    assert stale.exists() and current.exists()
    assert stats.removed == 0


def test_dir_clusters_in_build_and_fingerprint(tmp_path: Path) -> None:
    profile = tmp_path / "debug"
    old_build = _make_dir(profile / "build" / f"libsqlite3-sys-{H1}", age_sec=9 * DAY)
    new_build = _make_dir(profile / "build" / f"libsqlite3-sys-{H2}", age_sec=1 * HOUR)
    shared_out = _make_dir(profile / "build" / "libsqlite3-sys", age_sec=9 * DAY)
    old_fp = _make_dir(profile / ".fingerprint" / f"libsqlite3-sys-{H1}", age_sec=9 * DAY)
    new_fp = _make_dir(profile / ".fingerprint" / f"libsqlite3-sys-{H2}", age_sec=1 * HOUR)

    stats = crd.SweepStats()
    crd.sweep_old_fingerprints(profile, time.time() - 3 * DAY, stats)

    assert not old_build.exists() and not old_fp.exists()
    assert new_build.exists() and new_fp.exists()
    assert shared_out.exists()  # 无 hash 的共享输出目录保守不动


def test_unhashed_entries_untouched(tmp_path: Path) -> None:
    """无 hash 命名的条目（顶层链接产物等）不参与簇判定，一律不动。"""
    profile = tmp_path / "debug"
    top_exe = _make_file(profile / "agentos-kernel.exe", 1000, age_sec=9 * DAY)
    top_d = _make_file(profile / "agentos-kernel.d", 4, age_sec=9 * DAY)
    deps = profile / "deps"
    plain_d = _make_file(deps / "libagentos_api.d", 3, age_sec=9 * DAY)

    stats = crd.SweepStats()
    crd.sweep_old_fingerprints(profile, time.time() - 3 * DAY, stats)

    assert top_exe.exists() and top_d.exists() and plain_d.exists()
    assert stats.removed == 0


def test_locked_file_skipped_without_aborting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deps = tmp_path / "deps"
    locked = _make_file(deps / f"libold-{H1}.rlib", 7, age_sec=9 * DAY)
    mortal = _make_file(deps / f"libold-{H1}.d", 3, age_sec=9 * DAY)
    live = _make_file(deps / f"libold-{H2}.rlib", 2, age_sec=1 * HOUR)

    original_unlink = Path.unlink

    def _deny(self: Path, missing_ok: bool = False) -> None:
        if self.name == f"libold-{H1}.rlib":
            raise PermissionError(13, "拒绝访问")
        return original_unlink(self)

    monkeypatch.setattr(Path, "unlink", _deny)

    stats = crd.SweepStats()
    crd.sweep_old_fingerprints(tmp_path, time.time() - 3 * DAY, stats)

    assert locked.exists() and stats.skipped == 1
    assert not mortal.exists()
    assert live.exists() and stats.removed == 1 and stats.freed == 3


def test_profile_discovery_llcov_shape(tmp_path: Path) -> None:
    """llvm-cov-target 形态：profile 在 target 根的 debug/ 下，簇同样被处理。"""
    deps = tmp_path / "debug" / "deps"
    old = _make_file(deps / f"libagentos_invoker-{H1}.rlib", 20, age_sec=9 * DAY)
    new = _make_file(deps / f"libagentos_invoker-{H2}.rlib", 20, age_sec=1 * HOUR)

    assert crd._profile_dirs(tmp_path) == [tmp_path / "debug"]
    stats = crd.SweepStats()
    crd.sweep_old_fingerprints(tmp_path / "debug", time.time() - 3 * DAY, stats)

    assert not old.exists() and new.exists()


def test_profile_discovery_debug_root_shape(tmp_path: Path) -> None:
    """target/debug 即 profile 的形态：root 自身被识别为 profile。"""
    (tmp_path / "deps").mkdir()
    assert crd._profile_dirs(tmp_path) == [tmp_path]
    assert crd._profile_dirs(tmp_path / "nonexistent") == []


def test_collect_targets_fixed_depth_glob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """插件 target 只按固定深度命中；release 仅 --with-release 时入列。"""
    kernel_debug = tmp_path / "kernel" / "target" / "debug"
    kernel_cov = tmp_path / "kernel" / "target" / "llvm-cov-target"
    kernel_release = tmp_path / "kernel" / "target" / "release"
    plugin_debug4 = tmp_path / "plugins" / "shared" / "tools" / "x" / "target" / "debug"
    plugin_debug3 = tmp_path / "plugins" / "sdk" / "target" / "debug"
    for d in (kernel_debug, kernel_cov, kernel_release, plugin_debug4, plugin_debug3):
        d.mkdir(parents=True)
    # `**` 禁用语义哨兵：更深层树里的同名目录不得靠全树递归命中（20万+文件会卡死）
    deep_decoy = tmp_path / "plugins" / "shared" / "tools" / "x" / "target" / "debug" / "venv" / "target" / "debug"
    deep_decoy.mkdir(parents=True)

    monkeypatch.setattr(crd, "ROOT", tmp_path)
    monkeypatch.setattr(crd, "KERNEL_TARGET", tmp_path / "kernel" / "target")

    default = crd.collect_targets(with_release=False)
    assert kernel_debug in default and kernel_cov in default
    assert plugin_debug4 in default and plugin_debug3 in default
    assert kernel_release not in default
    assert deep_decoy not in default

    with_release = crd.collect_targets(with_release=True)
    assert kernel_release in with_release


def test_main_incremental_mode_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_kernel = _make_file(
        tmp_path / "kernel" / "target" / "debug" / "deps" / f"libagentos_api-{H1}.rlib", 50, age_sec=9 * DAY
    )
    new_kernel = _make_file(
        tmp_path / "kernel" / "target" / "debug" / "deps" / f"libagentos_api-{H2}.rlib", 60, age_sec=1 * HOUR
    )
    ancient_dep = _make_file(
        tmp_path / "kernel" / "target" / "debug" / "deps" / "libserde-1111222233334444.rlib", 70, age_sec=30 * DAY
    )
    old_plugin = _make_file(
        tmp_path / "plugins" / "shared" / "tools" / "x" / "target" / "debug" / "deps" / f"libx-{H1}.rlib",
        7,
        age_sec=9 * DAY,
    )
    new_plugin = _make_file(
        tmp_path / "plugins" / "shared" / "tools" / "x" / "target" / "debug" / "deps" / f"libx-{H2}.rlib",
        7,
        age_sec=1 * HOUR,
    )

    monkeypatch.setattr(crd, "ROOT", tmp_path)
    monkeypatch.setattr(crd, "KERNEL_TARGET", tmp_path / "kernel" / "target")
    monkeypatch.setattr("sys.argv", ["clean_rust_debug.py", "--older-than", "3"])

    assert crd.main() == 0
    assert not old_kernel.exists() and not old_plugin.exists()
    assert new_kernel.exists() and new_plugin.exists()
    assert ancient_dep.exists()  # 单副本老依赖：活缓存保护


def test_main_rejects_non_positive_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["clean_rust_debug.py", "--older-than", "0"])
    with pytest.raises(SystemExit) as exc_info:
        crd.main()
    assert exc_info.value.code != 0

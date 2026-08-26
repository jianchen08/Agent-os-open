# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation hardware_profile.py 硬件检测与资源配额测试（A5.3 补）。

覆盖：
1. compute_resource_profile 三档分级（低/中/高内存阈值）与 CPU 约束；
2. 环境变量覆盖（AO_MAX_ENVIRONMENTS 等,含非法值忽略）；
3. detect_hardware 退化路径（cgroup/sysconf/windows/fallback）；
4. cgroup 读取边界（max 无限制/超大值/损坏文件）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_mod() -> Any:
    mod_name = "isolation_hardware_profile_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "hardware_profile.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()
compute_resource_profile = _MOD.compute_resource_profile
detect_hardware = _MOD.detect_hardware
get_resource_profile = _MOD.get_resource_profile
_apply_env_overrides = _MOD._apply_env_overrides
_PROFILES = _MOD._PROFILES


class TestResourceProfileTiers:
    def test_low_tier_below_12gb(self) -> None:
        profile = compute_resource_profile({"total_memory_gb": 8.0, "cpu_count": 4})
        assert profile["tier"] == "low"
        assert profile["max_environments"] <= 3
        assert profile["container_memory"].endswith("m")
        # CPU 约束：4 核 - 预留 1 = 3 → 并发 ≤ 3
        assert profile["max_concurrent_tasks"] <= 3
        # 总量约束：单容器内存 ≥ 128m
        assert int(profile["container_memory"][:-1]) >= 128
        # swap = memory（禁止放大）
        assert profile["memory_swap"] == profile["container_memory"]

    def test_mid_tier_12_to_24gb(self) -> None:
        profile = compute_resource_profile({"total_memory_gb": 16.0, "cpu_count": 8})
        assert profile["tier"] == "mid"
        assert profile["max_environments"] <= 6
        assert profile["max_concurrent_tasks"] <= 6

    def test_high_tier_above_24gb(self) -> None:
        profile = compute_resource_profile({"total_memory_gb": 32.0, "cpu_count": 16})
        assert profile["tier"] == "high"
        assert profile["max_environments"] <= 12
        assert profile["max_concurrent_tasks"] <= 12

    def test_cpu_bound_constrains_environments(self) -> None:
        """2 核低配：并发被 CPU 约束压到 1。"""
        profile = compute_resource_profile({"total_memory_gb": 8.0, "cpu_count": 2})
        assert profile["max_environments"] == 1
        assert profile["max_concurrent_tasks"] == 1

    def test_per_memory_at_least_128mb(self) -> None:
        profile = compute_resource_profile({"total_memory_gb": 8.0, "cpu_count": 32})
        assert int(profile["container_memory"][:-1]) >= 128
        assert float(profile["container_cpus"]) >= 0.25


class TestEnvOverrides:
    def test_valid_overrides_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AO_MAX_ENVIRONMENTS", "9")
        monkeypatch.setenv("AO_CONTAINER_MEMORY", "1g")
        monkeypatch.setenv("AO_CONTAINER_CPUS", "2.0")
        monkeypatch.setenv("AO_MAX_CONCURRENT_TASKS", "5")
        profile = compute_resource_profile({"total_memory_gb": 8.0, "cpu_count": 4})
        assert profile["max_environments"] == 9
        assert profile["container_memory"] == "1g"
        assert profile["container_cpus"] == "2.0"
        assert profile["max_concurrent_tasks"] == 5

    def test_invalid_override_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AO_MAX_ENVIRONMENTS", "not-an-int")
        profile = _apply_env_overrides(dict(_PROFILES["low"]))
        # 非法值忽略，保持原值
        assert profile["max_environments"] == _PROFILES["low"]["max_environments"]

    def test_no_env_no_change(self) -> None:
        profile = _apply_env_overrides(dict(_PROFILES["mid"]))
        assert profile == dict(_PROFILES["mid"])


class TestDetectHardwareFallback:
    def test_fallback_default_when_all_detection_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "_read_cgroup_memory_limit_gb", lambda: None)
        monkeypatch.setattr(_MOD, "_read_sysconf_memory_gb", lambda: None)
        monkeypatch.setattr(_MOD, "_read_windows_memory_gb", lambda: None)
        monkeypatch.setattr(_MOD.os, "cpu_count", lambda: None)
        hw = detect_hardware()
        assert hw["total_memory_gb"] == 8.0
        assert hw["source"] == "default(fallback)"
        assert hw["cpu_count"] == 4  # os.cpu_count() or 4

    def test_sysconf_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "_read_cgroup_memory_limit_gb", lambda: None)
        monkeypatch.setattr(_MOD, "_read_sysconf_memory_gb", lambda: 16.0)
        hw = detect_hardware()
        assert hw["total_memory_gb"] == 16.0
        assert hw["source"] == "sysconf"

    def test_windows_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "_read_cgroup_memory_limit_gb", lambda: None)
        monkeypatch.setattr(_MOD, "_read_sysconf_memory_gb", lambda: None)
        monkeypatch.setattr(_MOD, "_read_windows_memory_gb", lambda: 24.0)
        hw = detect_hardware()
        assert hw["total_memory_gb"] == 24.0
        assert hw["source"] == "windows"


class TestCgroupV2Reader:
    def test_max_unlimited_returns_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        f = tmp_path / "memory.max"
        f.write_text("max", encoding="utf-8")
        _V2 = "/sys/fs/cgroup/memory.max"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) == _V2)
        monkeypatch.setattr("builtins.open", _open_at(_V2, f))
        assert _MOD._read_cgroup_memory_limit_gb() is None

    def test_bad_content_returns_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        f = tmp_path / "memory.max"
        f.write_text("not-a-number", encoding="utf-8")
        _V2 = "/sys/fs/cgroup/memory.max"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) == _V2)
        monkeypatch.setattr("builtins.open", _open_at(_V2, f))
        assert _MOD._read_cgroup_memory_limit_gb() is None


_real_open = open  # 捕获原始 builtins.open（monkeypatch 后闭包内不递归）


def _open_only(f: Path):
    """把 builtins.open 重定向到指定文件（仅测试用，模块级函数便于引用）。"""
    return _open_at(str(f), f)


def _open_at(target: str, f: Path):
    """builtins.open 重定向:目标路径读取指定文件,其余原样。"""

    def _opener(path, *a, **k):
        if str(path) == str(target):
            return _real_open(f, *a, **k)
        return _real_open(path, *a, **k)

    return _opener


class TestCgroupV2Numeric:
    def test_numeric_limit_converted_to_gb(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        f = tmp_path / "memory.max"
        f.write_text(str(4 * 1024**3), encoding="utf-8")
        _V2 = "/sys/fs/cgroup/memory.max"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) == _V2)
        monkeypatch.setattr("builtins.open", _open_at(_V2, f))
        assert _MOD._read_cgroup_memory_limit_gb() == 4.0

    def test_v2_not_found_falls_to_v1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        v1 = tmp_path / "memory.limit_in_bytes"
        v1.write_text(str(2 * 1024**3), encoding="utf-8")
        _V1 = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        # v2 存在但内容非法 → 落入 v1 分支
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) in (_V1, "/sys/fs/cgroup/memory.max"))
        monkeypatch.setattr("builtins.open", _open_at(_V1, v1))
        assert _MOD._read_cgroup_memory_limit_gb() == 2.0

    def test_v1_unlimited_huge_value_returns_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        v1 = tmp_path / "memory.limit_in_bytes"
        v1.write_text(str(1024**4 * 2), encoding="utf-8")  # >1TB
        _V1 = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) == _V1)
        monkeypatch.setattr("builtins.open", _open_at(_V1, v1))
        assert _MOD._read_cgroup_memory_limit_gb() is None

    def test_cgroup_cpu_v2_count(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        f = tmp_path / "cpu.max"
        f.write_text("800000 100000", encoding="utf-8")
        _CPU = "/sys/fs/cgroup/cpu.max"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) == _CPU)
        monkeypatch.setattr("builtins.open", _open_at(_CPU, f))
        assert _MOD._read_cgroup_cpu_count() == 8

    def test_cgroup_cpu_v2_max_unlimited(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        f = tmp_path / "cpu.max"
        f.write_text("max 100000", encoding="utf-8")
        _CPU = "/sys/fs/cgroup/cpu.max"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) == _CPU)
        monkeypatch.setattr("builtins.open", _open_at(_CPU, f))
        assert _MOD._read_cgroup_cpu_count() is None

    def test_cgroup_cpu_v1_count(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        quota = tmp_path / "cpu.cfs_quota_us"
        period = tmp_path / "cpu.cfs_period_us"
        quota.write_text("500000", encoding="utf-8")
        period.write_text("100000", encoding="utf-8")
        _Q = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
        _P = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) in (_Q, _P))
        monkeypatch.setattr("builtins.open", _open_at_map({_Q: quota, _P: period}))
        assert _MOD._read_cgroup_cpu_count() == 5


def _open_at_map(mapping: dict):
    """多文件重定向:路径→临时文件映射。"""

    def _opener(path, *a, **k):
        if str(path) in mapping:
            return _real_open(mapping[str(path)], *a, **k)
        return _real_open(path, *a, **k)

    return _opener

class TestSysconfReader:
    def test_sysconf_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Windows 的 os 无 sysconf 属性,整体替换模块 os 引用(fake 仅实现用到的接口)
        class _FakeOs:
            @staticmethod
            def sysconf(name: str) -> int:
                return 4096 if name == "SC_PAGE_SIZE" else 2 * 1024**2

        monkeypatch.setattr(_MOD, "os", _FakeOs())
        assert _MOD._read_sysconf_memory_gb() == 8.0

    def test_sysconf_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakeOs:
            @staticmethod
            def sysconf(_name: str) -> int:
                raise OSError("no sysconf")

        monkeypatch.setattr(_MOD, "os", _FakeOs())
        assert _MOD._read_sysconf_memory_gb() is None


class TestWindowsMemoryReader:
    def test_non_windows_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os, "name", "posix")
        assert _MOD._read_windows_memory_gb() is None

    def test_ctypes_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """fake ctypes:GlobalMemoryStatusEx 写回 ullTotalPhys,走成功分支。"""
        monkeypatch.setattr(_MOD.os, "name", "nt")

        class _FakeStatus:
            ullTotalPhys = 0

        class _FakeKernel32:
            @staticmethod
            def GlobalMemoryStatusEx(stat: _FakeStatus) -> bool:
                stat.ullTotalPhys = 16 * 1024**3
                return True

        class _FakeWindll:
            kernel32 = _FakeKernel32()

        class _FakeCtypes:
            c_ulong = int  # MEMORYSTATUSEX 类体引用,值无关紧要
            c_ulonglong = int

            class Structure:  # noqa: N801
                _fields_: list = []

            @staticmethod
            def sizeof(_x: Any) -> int:
                return 8

            @staticmethod
            def byref(x: Any) -> Any:
                return x

            windll = _FakeWindll()

        monkeypatch.setitem(sys.modules, "ctypes", _FakeCtypes())
        assert _MOD._read_windows_memory_gb() == 16.0

    def test_ctypes_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD.os, "name", "nt")

        class _FakeKernel32:
            @staticmethod
            def GlobalMemoryStatusEx(_stat: Any) -> bool:
                raise OSError("API failed")

        class _FakeWindll:
            kernel32 = _FakeKernel32()

        class _FakeCtypes:
            c_ulong = int  # MEMORYSTATUSEX 类体引用,值无关紧要
            c_ulonglong = int

            class Structure:  # noqa: N801
                _fields_: list = []

            @staticmethod
            def sizeof(_x: Any) -> int:
                return 8

            @staticmethod
            def byref(x: Any) -> Any:
                return x

            windll = _FakeWindll()

        monkeypatch.setitem(sys.modules, "ctypes", _FakeCtypes())
        assert _MOD._read_windows_memory_gb() is None


class TestCgroupExceptionPaths:
    def test_memory_v1_bad_content(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        f = tmp_path / "memory.limit_in_bytes"
        f.write_text("garbage", encoding="utf-8")
        _V1 = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) == _V1)
        monkeypatch.setattr("builtins.open", _open_at(_V1, f))
        assert _MOD._read_cgroup_memory_limit_gb() is None

    def test_memory_v1_open_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _V1 = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) == _V1)

        def _boom(_path, *_a, **_k):
            raise OSError("EACCES")

        monkeypatch.setattr("builtins.open", _boom)
        assert _MOD._read_cgroup_memory_limit_gb() is None

    def test_cpu_v2_bad_content(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        f = tmp_path / "cpu.max"
        f.write_text("not-a-number", encoding="utf-8")
        _CPU = "/sys/fs/cgroup/cpu.max"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) == _CPU)
        monkeypatch.setattr("builtins.open", _open_at(_CPU, f))
        assert _MOD._read_cgroup_cpu_count() is None

    def test_cpu_v1_bad_content(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        quota = tmp_path / "cpu.cfs_quota_us"
        quota.write_text("garbage", encoding="utf-8")
        period = tmp_path / "cpu.cfs_period_us"
        period.write_text("100000", encoding="utf-8")
        _Q = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
        _P = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) in (_Q, _P))
        monkeypatch.setattr("builtins.open", _open_at_map({_Q: quota, _P: period}))
        assert _MOD._read_cgroup_cpu_count() is None


class TestEntryPoints:
    def test_compute_profile_with_none_hardware(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            _MOD, "detect_hardware", lambda: {"total_memory_gb": 16.0, "cpu_count": 8}
        )
        profile = compute_resource_profile(None)
        assert profile["tier"] == "mid"

    def test_get_resource_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            _MOD, "detect_hardware", lambda: {"total_memory_gb": 8.0, "cpu_count": 4}
        )
        profile = get_resource_profile()
        assert profile["tier"] == "low"
        assert profile["max_environments"] >= 1

    def test_cpu_v1_open_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """v1 quota 读取抛 OSError(权限) → 返回 None。"""
        _Q = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
        _P = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) in (_Q, _P))

        def _boom(path, *_a, **_k):
            if str(path) == _Q:
                raise OSError("EACCES")
            return _real_open(path, *_a, **_k)

        monkeypatch.setattr("builtins.open", _boom)
        assert _MOD._read_cgroup_cpu_count() is None
    def test_cpu_v2_bad_quota_token(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """cpu.max 首 token 非数字 → int() 抛 ValueError → None。"""
        f = tmp_path / "cpu.max"
        f.write_text("abc 100000", encoding="utf-8")
        _CPU = "/sys/fs/cgroup/cpu.max"
        monkeypatch.setattr(_MOD.os.path, "exists", lambda p: str(p) == _CPU)
        monkeypatch.setattr("builtins.open", _open_at(_CPU, f))
        assert _MOD._read_cgroup_cpu_count() is None

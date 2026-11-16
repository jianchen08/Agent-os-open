# @feature: FP-0.2.移动端适配 GM1 | @ci: sdk-test
"""device_profile 单测：设备档案探测与档位计划的 SDK 本地面。

档位表行为（手机/桌面/云边界、单调性）由仓级契约测试
tests/test_mobile_gm1_contracts.py::TestCohostProfileContract 验收，
本文件只补契约面不覆盖的探测与显式钉档路径。
"""

from __future__ import annotations

import pytest

from agentos_plugin_sdk.device_profile import (
    DeviceProfile,
    _probe_memory_gb,
    detect_device_profile,
    plan_hosts,
    resolve_tier,
)


class TestDetectDeviceProfile:
    def test_detect_reads_probes_and_explicit_tier(self) -> None:
        """探测值进档案；AGENTOS_DEVICE_TIER 显式钉档优先于硬件推断。"""
        profile = detect_device_profile(
            {"AGENTOS_DEVICE_TIER": "cloud"},
            probe_cores=lambda: 4,
            probe_memory_gb=lambda: 4.0,
        )

        assert profile.cores == 4
        assert profile.memory_gb == 4.0
        assert profile.tier == "cloud"
        # 显式云档硬件无关指宿主数：4c4g 与 64c256g 同宿主计划（同档同计划）；
        # spawn_concurrency 是核数投影（min(8, cores)），随核自适应属 P2 语义。
        assert plan_hosts(profile).host_count == 4
        assert (
            plan_hosts(profile).host_count
            == plan_hosts(DeviceProfile(cores=64, memory_gb=256.0, tier="cloud")).host_count
        )

    def test_detect_without_tier_env_is_auto(self) -> None:
        """无钉档环境变量 → tier=None 按硬件推断（4c4g → phone）。"""
        profile = detect_device_profile({}, probe_cores=lambda: 4, probe_memory_gb=lambda: 4.0)

        assert profile.tier is None
        assert resolve_tier(profile) == "phone"

    def test_detect_invalid_tier_env_falls_back_to_auto(self) -> None:
        """非法档位值不阻塞启动：回退自动探测（tier=None）。"""
        profile = detect_device_profile(
            {"AGENTOS_DEVICE_TIER": "watch"},
            probe_cores=lambda: 16,
            probe_memory_gb=lambda: 32.0,
        )

        assert profile.tier is None
        assert resolve_tier(profile) == "desktop"

    def test_detect_probe_failure_is_conservative_desktop(self) -> None:
        """探测不可得（记 0）不降档：按桌面档保守处理，维持现役宿主数。"""
        profile = detect_device_profile({}, probe_cores=lambda: 0, probe_memory_gb=lambda: 0.0)

        assert resolve_tier(profile) == "desktop"
        assert plan_hosts(profile).host_count == 4


class TestPlanHostsExplicitTier:
    def test_explicit_phone_tier_overrides_big_hardware(self) -> None:
        """显式 phone 钉档在越界硬件上仍出手机计划（显式档整体覆盖推断）。"""
        plan = plan_hosts(DeviceProfile(cores=16, memory_gb=32.0, tier="phone"))

        assert plan.tier == "phone"
        assert 1 <= plan.host_count <= 2

    def test_unknown_tier_value_rejected(self) -> None:
        """档位词表外的显式值报错（fail-closed，不静默换档）。"""
        with pytest.raises(ValueError, match="词表"):
            resolve_tier(DeviceProfile(cores=4, memory_gb=4.0, tier="watch"))


class TestPhoneSingleHostBoundary:
    @pytest.mark.parametrize(
        ("memory_gb", "expected_hosts"),
        [(6.0, 1), (6.5, 2)],
        ids=["memory-at-single-host-bound", "memory-above-single-host-bound"],
    )
    def test_single_host_step_follows_memory_budget(self, memory_gb: float, expected_hosts: int) -> None:
        """手机档内 1/2 宿主分界按内存预算（钉文档化的分界值，防漂移）。"""
        plan = plan_hosts(DeviceProfile(cores=8, memory_gb=memory_gb, tier="phone"))

        assert plan.host_count == expected_hosts


class TestProbes:
    def test_memory_probe_positive_on_host_platform(self) -> None:
        """真依赖：本机内存探测为正数（Windows GlobalMemoryStatusEx / Linux sysconf）。"""
        assert _probe_memory_gb() > 0

"""设备档案与合宿宿主计划（原则 P2：合宿数=f(核数,内存档)，启动时探测）。

方案出处：docs/working/LLM网关迁移与多端部署方案_20261013.md §Phase1 / §1.2 P2。
三端（手机/桌面/云）插件集合一致（P1），差的是进程预算：本模块把
「逻辑核数 + 内存容量」的设备档案映射为合宿宿主数与 spawn 并发度计划，
供启动装配按档案取值（配置化 + 启动探测，不改装箱机制本身）。

档位表（契约钉点，详见 tests/test_mobile_gm1_contracts.py::TestCohostProfileContract）：
- phone：cores ≤ 8 且 memory_gb ≤ 8（边界含于手机档）→ 1-2 个宿主；
- desktop：任一维度越过手机上界 → 4（桌面合宿收敛现实：槽容量 20 +
  pipeline 组归并 light → 2×light + light_stable + longwait；运行时执法在
  内核 invoker host_key 按同一档位表取槽容量/装箱域，本模块为计划投影）；
- cloud：显式 tier="cloud" → 硬件无关（同档同计划，现役云与桌面同值）。

并发度单源：运行时 spawn 并行度由内核
``agentos_core::types::bounded_spawn_concurrency()``（min(8, 逻辑核数)）执法，
本模块的 ``HostPlan.spawn_concurrency`` 只是同一公式在计划面的投影
（供配置预填与观测），不在 Python 侧另起执法；手机档并行 spawn 天然被
宿主数（1-2）上界约束。

配置面：环境变量 ``AGENTOS_DEVICE_TIER``（"phone"/"desktop"/"cloud"）显式
钉档，优先于硬件推断（云主机等无法按内存判档的场景）。非法值回退自动
探测并告警（与内核 light_host_max_members 同款容错惯例，不阻塞启动）。
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: 档位词表（与能力门禁 min_profile 同一词表）。
TIERS: tuple[str, ...] = ("phone", "desktop", "cloud")

#: 手机档上界（P2 原则原文「≤8核且≤8GB」，边界含于手机档）。
PHONE_MAX_CORES = 8
PHONE_MAX_MEMORY_GB = 8.0

#: 手机档内 1 宿主的内存上界（GB）：更紧的内存预算收敛到单宿主，
#: 降低多进程底座驻留；之上的手机档开 2 宿主换故障隔离半径。
PHONE_SINGLE_HOST_MEMORY_GB = 6.0

#: 桌面档宿主数：桌面合宿收敛现实（2026-10-15 rollout：槽容量 20 + pipeline
#: 组归并 light → 2×light + light_stable + longwait），契约钉值对齐部署面。
DESKTOP_HOST_COUNT = 4

#: 显式云档宿主数：云是独立档、硬件无关；现役云与桌面同值故同数。
CLOUD_HOST_COUNT = 4

#: spawn 并发度上界：内核 bounded_spawn_concurrency() 的公式上界（计划面投影）。
SPAWN_CONCURRENCY_CAP = 8

#: 显式钉档环境变量。
TIER_ENV = "AGENTOS_DEVICE_TIER"


@dataclass(frozen=True)
class DeviceProfile:
    """设备档案：逻辑核数 + 内存容量（GB）+ 可选显式档位。

    ``tier=None`` 表示按硬件推断；显式档位（含 "phone"/"desktop"）整体
    覆盖硬件推断——档案数值此时仅作观测记录。
    """

    cores: int
    memory_gb: float
    tier: str | None = None


@dataclass(frozen=True)
class HostPlan:
    """合宿宿主计划：档位、宿主数与 spawn 并发度（计划面投影）。"""

    tier: str
    host_count: int
    spawn_concurrency: int


def _spawn_concurrency_projection(cores: int) -> int:
    """bounded_spawn_concurrency() 公式的计划面投影：min(8, 核数)，下限 1。

    执法单源在内核（agentos_core::types）；此处同公式仅供计划可见性，
    两处漂移由各自单测锚定同一公式。
    """
    return max(1, min(SPAWN_CONCURRENCY_CAP, max(cores, 1)))


def resolve_tier(profile: DeviceProfile) -> str:
    """解析设备档位：显式 tier 优先；否则按硬件推断（任一维度越界即桌面档）。

    硬件维度非法（cores ≤ 0 或 memory_gb ≤ 0，探测失败的降级形态）时按
    桌面档保守处理：无法证实手机档约束就不做降档，维持现役行为。
    """
    if profile.tier is not None:
        if profile.tier not in TIERS:
            raise ValueError(f"未知档位 {profile.tier!r}（合法词表：{TIERS}）")
        return profile.tier
    if profile.cores <= 0 or profile.memory_gb <= 0:
        return "desktop"
    if profile.cores <= PHONE_MAX_CORES and profile.memory_gb <= PHONE_MAX_MEMORY_GB:
        return "phone"
    return "desktop"


def plan_hosts(profile: DeviceProfile) -> HostPlan:
    """设备档案 → 宿主计划（纯函数，档位表见模块 docstring）。

    资源单调性：同档位内资源升档宿主数不回退（低配不比高配排更多宿主）；
    显式 tier 时计划与硬件维度无关（同档同计划）。
    """
    tier = resolve_tier(profile)
    if tier == "phone":
        host_count = 1 if profile.memory_gb <= PHONE_SINGLE_HOST_MEMORY_GB else 2
        # 手机档并行 spawn 被宿主数上界约束（只有这么多宿主可拉起）。
        spawn_concurrency = host_count
    else:
        host_count = CLOUD_HOST_COUNT if tier == "cloud" else DESKTOP_HOST_COUNT
        spawn_concurrency = _spawn_concurrency_projection(profile.cores)
    return HostPlan(tier=tier, host_count=host_count, spawn_concurrency=spawn_concurrency)


def _probe_cores() -> int:
    """逻辑核数探测；不可得返回 0（调用方按桌面档保守处理）。"""
    return os.cpu_count() or 0


def _probe_memory_gb() -> float:
    """内存容量探测（GB）；不可得返回 0.0（调用方按桌面档保守处理）。

    Linux/macOS 走 sysconf 物理页换算；Windows 走 GlobalMemoryStatusEx。
    """
    if sys.platform == "win32":

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MemoryStatus()
        stat.dwLength = ctypes.sizeof(_MemoryStatus)
        ok: int = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        if ok:
            return float(stat.ullTotalPhys) / (1024**3)
        return 0.0
    page_size = os.sysconf("SC_PAGE_SIZE")
    page_count = os.sysconf("SC_PHYS_PAGES")
    if page_size <= 0 or page_count <= 0:
        return 0.0
    return page_size * page_count / (1024**3)


def detect_device_profile(
    env: Mapping[str, str] | None = None,
    *,
    probe_cores: Callable[[], int] | None = None,
    probe_memory_gb: Callable[[], float] | None = None,
) -> DeviceProfile:
    """启动时设备档案探测：逻辑核数 + 内存容量 + 显式钉档环境变量。

    ``AGENTOS_DEVICE_TIER`` 非法值告警回退自动探测（不阻塞启动）；探测
    不可得的维度记 0，由 :func:`plan_hosts` 按桌面档保守处理。探针参数
    供测试注入（平台相关 syscall 无法跨平台真跑）。
    """
    env_map = os.environ if env is None else env
    tier_raw = env_map.get(TIER_ENV, "").strip().lower()
    tier: str | None = None
    if tier_raw:
        if tier_raw in TIERS:
            tier = tier_raw
        else:
            logger.warning(
                "%s=%r 不在词表 %s 内，回退自动探测",
                TIER_ENV,
                env_map.get(TIER_ENV),
                TIERS,
            )
    cores = (probe_cores or _probe_cores)()
    memory_gb = (probe_memory_gb or _probe_memory_gb)()
    if cores <= 0 or memory_gb <= 0:
        logger.warning(
            "设备档案探测不完整（cores=%s memory_gb=%s），plan_hosts 将按桌面档保守处理 | platform=%s",
            cores,
            memory_gb,
            platform.platform(),
        )
    return DeviceProfile(cores=cores, memory_gb=memory_gb, tier=tier)

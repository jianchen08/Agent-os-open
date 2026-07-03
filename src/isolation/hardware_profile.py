"""
硬件自适应资源计算

根据运行环境（容器内/裸机）的实际可用资源，计算隔离容器的资源配额。
目标：同一套代码、同一份配置，在 8GB 低配机自动轻量、在 32GB 高配机自动放开。

分级标准：
  - 低配 (<12GB):  最多 3 个隔离容器，每个 256m / 0.25 CPU
  - 中配 (12-24GB): 最多 6 个隔离容器，每个 384m / 0.5 CPU
  - 高配 (>24GB):  最多 12 个隔离容器，每个 512m / 1.0 CPU

环境变量覆盖（高级用户可强制指定）：
  - AO_MAX_ENVIRONMENTS    覆盖最大隔离容器数
  - AO_CONTAINER_MEMORY    覆盖单容器内存（如 "256m"）
  - AO_CONTAINER_CPUS      覆盖单容器 CPU（如 "0.5"）
  - AO_MAX_CONCURRENT_TASKS 覆盖最大并发任务数
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 硬件检测
# ---------------------------------------------------------------------------


def _read_cgroup_memory_limit_gb() -> float | None:
    """读取 cgroup v1/v2 的内存限制（容器内更准确）。

    容器内读 os.sysconf('SC_PHYS_PAGES') 会读到宿主机全部内存，
    而非容器实际可用配额。cgroup limit 才是容器真实能用的内存。
    返回 GB 单位的限制值，读不到返回 None。
    """
    # cgroup v2（新版 Linux、Docker Desktop 默认）
    v2_path = "/sys/fs/cgroup/memory.max"
    if os.path.exists(v2_path):  # noqa: PTH110
        try:
            with open(v2_path, encoding="utf-8") as f:
                content = f.read().strip()
            # v2 里 "max" 表示无限制
            if content == "max":
                return None
            return int(content) / (1024**3)
        except (ValueError, OSError):
            pass

    # cgroup v1（旧版）
    v1_path = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    if os.path.exists(v1_path):  # noqa: PTH110
        try:
            with open(v1_path, encoding="utf-8") as f:
                limit = int(f.read().strip())
            # cgroup v1 无限制时是一个超大值（>1TB）
            if limit > 1024**4:
                return None
            return limit / (1024**3)
        except (ValueError, OSError):
            pass

    return None


def _read_sysconf_memory_gb() -> float | None:
    """退化路径：用 os.sysconf 读物理内存（Linux/macOS 裸机）。"""
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        return (page_size * phys_pages) / (1024**3)
    except (ValueError, OSError, AttributeError):
        return None


def _read_windows_memory_gb() -> float | None:
    """Windows 内存检测：通过 ctypes 调 GlobalMemoryStatusEx。

    SC_PHYS_PAGES 是 POSIX 接口，Windows 上不可用，需用 Windows API。
    返回 GB 单位的物理内存总量，失败返回 None。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes  # noqa: PLC0415

        class MEMORYSTATUSEX(ctypes.Structure):
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

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return stat.ullTotalPhys / (1024**3)
    except Exception:
        return None


def _read_cgroup_cpu_count() -> int | None:
    """读取 cgroup 限制的 CPU 数（容器内更准确）。

    容器内 os.cpu_count() 可能读到宿主机全部 CPU，
    cgroup quota/period 才是容器实际能用的 CPU 配额。
    """
    # cgroup v2
    v2_cpu_max = "/sys/fs/cgroup/cpu.max"
    if os.path.exists(v2_cpu_max):  # noqa: PTH110
        try:
            with open(v2_cpu_max, encoding="utf-8") as f:
                parts = f.read().strip().split()
            # 格式："quota period" 或 "max period"
            if len(parts) == 2 and parts[0] != "max":
                quota, period = int(parts[0]), int(parts[1])
                if period > 0:
                    return max(1, math.ceil(quota / period))
        except (ValueError, OSError):
            pass

    # cgroup v1
    v1_quota = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
    v1_period = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
    if os.path.exists(v1_quota) and os.path.exists(v1_period):  # noqa: PTH110
        try:
            with open(v1_quota, encoding="utf-8") as f:
                quota = int(f.read().strip())
            with open(v1_period, encoding="utf-8") as f:
                period = int(f.read().strip())
            if quota > 0 and period > 0:
                return max(1, math.ceil(quota / period))
        except (ValueError, OSError):
            pass

    return None


def detect_hardware() -> dict[str, Any]:
    """检测运行环境的实际可用资源。

    优先级：cgroup limit > sysconf > 默认值
    容器内会用 cgroup（更准），裸机会退化到 sysconf。

    Returns:
        {
            "total_memory_gb": float,    # 实际可用内存（GB）
            "cpu_count": int,            # 实际可用 CPU 核数
            "source": str,               # 检测来源（用于日志诊断）
            "is_container": bool,        # 是否在容器内运行
        }
    """
    # 检测是否在容器内
    is_container = os.path.exists("/.dockerenv") or os.path.exists("/proc/1/cgroup")  # noqa: PTH110

    # 内存：优先 cgroup，再 sysconf（Linux/macOS），再 Windows API
    mem_gb = _read_cgroup_memory_limit_gb()
    source = "cgroup"
    if mem_gb is None:
        mem_gb = _read_sysconf_memory_gb()
        source = "sysconf"
    if mem_gb is None:
        mem_gb = _read_windows_memory_gb()
        source = "windows"
    if mem_gb is None:
        # 全部检测失败，给保守默认值（按低配处理，最安全）
        mem_gb = 8.0
        source = "default(fallback)"

    # CPU：优先 cgroup
    cpu_count = _read_cgroup_cpu_count()
    if cpu_count is None:
        cpu_count = os.cpu_count() or 4

    result = {
        "total_memory_gb": round(mem_gb, 1),
        "cpu_count": max(1, cpu_count),
        "source": source,
        "is_container": is_container,
    }
    logger.info(f"[hardware_profile] 硬件检测: {result}")
    return result


# ---------------------------------------------------------------------------
# 资源配额计算
# ---------------------------------------------------------------------------

# 分级阈值（GB）
LOW_MEM_THRESHOLD = 12.0  # < 12GB 视为低配
MID_MEM_THRESHOLD = 24.0  # 12-24GB 视为中配，>24GB 视为高配

# 三档配置（纯数据，便于测试和调整）
_PROFILES = {
    "low": {  # 低配机（如 8GB 笔记本）
        "max_environments": 3,
        "container_memory": "256m",
        "container_cpus": "0.25",
        "memory_swap": "256m",  # = memory，禁止 swap 放大
        "pids_limit": 64,  # 更严格的进程数限制
        "max_concurrent_tasks": 3,
    },
    "mid": {  # 中配机（如 16GB 主流笔记本）
        "max_environments": 6,
        "container_memory": "384m",
        "container_cpus": "0.5",
        "memory_swap": "384m",
        "pids_limit": 100,
        "max_concurrent_tasks": 6,
    },
    "high": {  # 高配机（如 32GB+ 工作站/服务器）
        "max_environments": 12,
        "container_memory": "512m",
        "container_cpus": "1.0",
        "memory_swap": "512m",
        "pids_limit": 200,
        "max_concurrent_tasks": 12,
    },
}


def _apply_env_overrides(profile: dict[str, Any]) -> dict[str, Any]:
    """应用环境变量覆盖（高级用户可强制指定）。

    支持的环境变量：
      - AO_MAX_ENVIRONMENTS     (int)
      - AO_CONTAINER_MEMORY     (str, 如 "256m")
      - AO_CONTAINER_CPUS       (str, 如 "0.5")
      - AO_MAX_CONCURRENT_TASKS (int)
      - AO_MEMORY_SWAP          (str)
      - AO_PIDS_LIMIT           (int)
    """
    overrides = {
        "max_environments": ("AO_MAX_ENVIRONMENTS", int),
        "container_memory": ("AO_CONTAINER_MEMORY", str),
        "container_cpus": ("AO_CONTAINER_CPUS", str),
        "memory_swap": ("AO_MEMORY_SWAP", str),
        "pids_limit": ("AO_PIDS_LIMIT", int),
        "max_concurrent_tasks": ("AO_MAX_CONCURRENT_TASKS", int),
    }

    result = dict(profile)
    for key, (env_name, cast) in overrides.items():
        env_val = os.environ.get(env_name)
        if env_val is not None:
            try:
                result[key] = cast(env_val)
                logger.info(f"[hardware_profile] 环境变量覆盖: {key}={result[key]} (来自 {env_name})")
            except (ValueError, TypeError):
                logger.warning(f"[hardware_profile] 环境变量 {env_name}={env_val} 无法转为 {cast.__name__}，忽略")
    return result


def compute_resource_profile(hardware: dict[str, Any] | None = None) -> dict[str, Any]:
    """根据硬件检测结果计算资源配额。

    Args:
        hardware: detect_hardware() 的返回值。None 时内部自动检测。

    Returns:
        资源配额字典，包含：
        - max_environments: 最大隔离容器数
        - container_memory: 单容器内存限制（如 "256m"）
        - container_cpus: 单容器 CPU 限制（如 "0.5"）
        - memory_swap: swap 限制（= container_memory，禁止放大）
        - pids_limit: 进程数上限（防 fork 炸弹）
        - max_concurrent_tasks: 最大并发任务数
        - tier: 分级标识（"low"/"mid"/"high"，用于日志）
    """
    if hardware is None:
        hardware = detect_hardware()

    mem = hardware["total_memory_gb"]
    cpu = hardware["cpu_count"]

    # 按内存分级
    if mem < LOW_MEM_THRESHOLD:
        tier = "low"
    elif mem < MID_MEM_THRESHOLD:
        tier = "mid"
    else:
        tier = "high"

    profile = dict(_PROFILES[tier])

    # CPU 数会进一步约束并发：不能超过 (CPU总数 - 预留给系统的核数)
    # 低配预留 1 核，中配预留 2 核，高配预留 4 核给系统
    reserved_cpu = {"low": 1, "mid": 2, "high": 4}[tier]
    cpu_based_concurrency = max(1, cpu - reserved_cpu)
    profile["max_environments"] = min(profile["max_environments"], cpu_based_concurrency)
    profile["max_concurrent_tasks"] = min(profile["max_concurrent_tasks"], cpu_based_concurrency)

    # 总量约束：所有容器资源总和不超过宿主机的一半。
    # 单容器配额 = (宿主机一半) / max_environments，满载时总和恰好 = 一半。
    # 用实际检测值动态计算，覆盖 _PROFILES 里写死的档位值。
    max_env = max(1, profile["max_environments"])
    half_mem_mb = int(mem * 1024 / 2)
    half_cpu = cpu / 2
    per_mem_mb = max(128, half_mem_mb // max_env)  # 单容器至少 128m
    per_cpu = round(half_cpu / max_env, 2)
    per_cpu = max(0.25, per_cpu)  # 单容器至少 0.25 cpu
    profile["container_memory"] = f"{per_mem_mb}m"
    profile["container_cpus"] = str(per_cpu)
    profile["memory_swap"] = profile["container_memory"]  # 禁止 swap 放大

    profile["tier"] = tier

    # 应用环境变量覆盖（最后一步，优先级最高）
    profile = _apply_env_overrides(profile)

    logger.info(
        f"[hardware_profile] 资源配额: tier={tier} mem={mem}GB cpu={cpu} "
        f"→ max_env={profile['max_environments']} "
        f"mem/c={profile['container_memory']} cpu/c={profile['container_cpus']} "
        f"max_tasks={profile['max_concurrent_tasks']}"
    )
    return profile


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def get_resource_profile() -> dict[str, Any]:
    """便捷入口：检测硬件 + 计算配额，一步到位。

    模块级不缓存（detect_hardware 本身很轻），由调用方决定是否缓存。
    IsolationManager 在初始化时调用一次即可。
    """
    return compute_resource_profile(detect_hardware())


if __name__ == "__main__":
    # 手动测试入口：python -m isolation.hardware_profile
    import json

    hw = detect_hardware()
    profile = compute_resource_profile(hw)
    print("=== 硬件检测 ===")
    print(json.dumps(hw, indent=2, ensure_ascii=False))
    print("\n=== 资源配额 ===")
    print(json.dumps(profile, indent=2, ensure_ascii=False))

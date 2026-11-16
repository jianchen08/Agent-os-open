"""win32 Job Object 钉死/恢复——由 server.py 平移（文件长度机械闸触碰即拆，
ADR 2026-10-12-file-length-mechanical-gate 纪律：存量触线拆或缩，不放宽基线）。
server.py 经 re-import 保持原符号面（测试与生产引用不变）。"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

# Job Object 句柄登记（模块级防提前关闭——句柄被 GC 关闭会连带 KILL_ON_JOB_CLOSE
# 把还活着的子进程整树杀掉，语义反转）。进程生命周期与宿主 sidecar 一致。
_JOB_HANDLES: list[int] = []


def _close_job_handle(job: int) -> None:
    """关闭半成品 Job 句柄（失败路径专用：无成员 Job，Close 无副作用）。

    不关则内核对象泄漏且语义悬空（句柄活着但没人被钉进去）；关闭失败仅告警
    ——钉死未生效已在主路径告警在案，此处不再重复放大。
    """
    try:  # pragma: no cover —— win32 API 路径（Linux CI 不可达；Windows 由 test_hindsight_job_pin_win32 行为实证）
        import ctypes  # noqa: PLC0415

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)
    except Exception:  # noqa: BLE001 —— 关闭失败不阻断 spawn（纵深非前置）
        logger.warning("[hindsight] 半成品 Job 句柄关闭失败 job=%s", job, exc_info=True)


def _resume_suspended_process(process: subprocess.Popen[bytes]) -> None:
    """恢复 CREATE_SUSPENDED 起停的子进程主线程（win32，Toolhelp 定位首线程）。

    钉死次序契约（见 _pin_process_to_job docstring）依赖「挂起起停→Assign→
    恢复」；恢复在钉死成败路径都必须尽力执行（调用方 finally）——进程吊死在
    挂起态比孤儿更糟，故恢复失败仅告警不抛。
    """
    try:  # pragma: no cover —— win32 API 路径（Linux CI 不可达；Windows 由 test_hindsight_job_pin_win32 行为实证）
        import ctypes  # noqa: PLC0415

        class _ThreadEntry32(ctypes.Structure):
            """THREADENTRY32（tlhelp32.h，ctypes 算对齐）。"""

            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ThreadID", ctypes.c_uint32),
                ("th32OwnerProcessID", ctypes.c_uint32),
                ("tpBasePri", ctypes.c_long),
                ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", ctypes.c_uint32),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snapshot = kernel32.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if not snapshot or snapshot == -1:
            logger.warning(
                "[hindsight] CreateToolhelp32Snapshot 失败，子进程主线程恢复未遂 PID=%s",
                process.pid,
            )
            return
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(_ThreadEntry32)
        tid: int | None = None
        if kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            while True:
                if entry.th32OwnerProcessID == process.pid:
                    tid = entry.th32ThreadID
                    break
                if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(snapshot)
        if tid is None:
            logger.warning(
                "[hindsight] 快照内未找到子进程线程，主线程恢复未遂 PID=%s",
                process.pid,
            )
            return
        thread = kernel32.OpenThread(0x0002, 0, tid)  # THREAD_SUSPEND_RESUME
        if not thread:
            logger.warning(
                "[hindsight] OpenThread 失败，主线程恢复未遂 PID=%s tid=%s",
                process.pid,
                tid,
            )
            return
        try:
            if kernel32.ResumeThread(thread) == -1:
                logger.warning(
                    "[hindsight] ResumeThread 失败，子进程可能停留在挂起态 PID=%s tid=%s",
                    process.pid,
                    tid,
                )
        finally:
            kernel32.CloseHandle(thread)
    except Exception:  # noqa: BLE001 —— 恢复失败不抛：spawn 已成事实，必须尽力放行
        logger.warning(
            "[hindsight] 恢复子进程主线程异常 PID=%s", process.pid, exc_info=True
        )


def _pin_process_to_job(process: subprocess.Popen[bytes], suspended: bool = False) -> None:
    """win32 Job Object 钉死子进程树（P0-3：孤儿结构性归零）。

    KILL_ON_JOB_CLOSE：宿主/内核无论优雅还是崩溃死亡，hindsight-api（含 uv
    跳板子进程，经父子创建自然入 job）整树陪葬；优雅路径既有 kill_process_tree
    保留双保险。非 Windows 平台 no-op。失败仅告警不阻断 spawn（钉死是纵深而非
    前置条件）。

    结构契约：JOBOBJECT_EXTENDED_LIMIT_INFORMATION 必须全 7 字段（Basic 64B +
    IO_COUNTERS 48B + ProcessMemoryLimit/JobMemoryLimit/PeakProcessMemoryUsed/
    PeakJobMemoryUsed 4×SIZE_T 32B，x64 共 144B）——内核按期望尺寸精确校验，
    缺一字节即 ERROR_BAD_LENGTH(24)，钉死全程静默失效（10-20 装机双孤儿归因
    实锤：IoInfo 误写 8 quadword + 缺尾 4 字段 → sizeof=128 → SetInfo 恒败 →
    Assign 从未执行）。

    次序契约（suspended=True，win32 spawn 面配套 CREATE_SUSPENDED 起停）：跳板
    被钉入 Job 前无法产生真身（uv cpython），真身经父子创建原子入 Job——无
    挂起时跳板启动即 spawn 真身，Assign 前真身可能已出生并永远留在 Job 外
    （关 Job 只杀跳板，真身成新孤儿）。
    """
    if sys.platform != "win32":
        return
    job: int | None = None
    pinned = False
    try:  # pragma: no cover —— win32 API 路径（Linux CI 不可达；Windows 由 test_hindsight_job_pin_win32 行为实证）
        import ctypes  # noqa: PLC0415

        class _JobBasicLimitInfo(ctypes.Structure):
            """JOBOBJECT_BASIC_LIMIT_INFORMATION（winbase.h，ctypes 算对齐）。"""

            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _IoCounters(ctypes.Structure):
            """IO_COUNTERS（winnt.h：6 个 ULONGLONG，不是 8）。"""

            _fields_ = [("Quadword", ctypes.c_uint64 * 6)]

        class _JobExtendedLimitInfo(ctypes.Structure):
            """JOBOBJECT_EXTENDED_LIMIT_INFORMATION（InfoClass=9，winnt.h 全 7 字段）。

            尺寸必须与内核期望逐字节一致（x64=144），见函数 docstring 结构契约。
            """

            _fields_ = [
                ("BasicLimitInformation", _JobBasicLimitInfo),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning("[hindsight] CreateJobObjectW 失败，Job 钉死未生效")
            return
        info = _JobExtendedLimitInfo()
        info.BasicLimitInformation.LimitFlags |= 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            logger.warning(
                "[hindsight] SetInformationJobObject 失败（err=%s），Job 钉死未生效",
                ctypes.get_last_error(),
            )
            return
        if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
            logger.warning("[hindsight] AssignProcessToJobObject 失败 PID=%s", process.pid)
            return
        _JOB_HANDLES.append(job)
        pinned = True
        logger.info("[hindsight] 子进程已钉入 Job Object PID=%s job=%s", process.pid, job)
    except Exception:  # noqa: BLE001 —— 钉死失败不阻断 spawn（纵深非前置）  # pragma: no cover
        logger.warning("[hindsight] Job Object 钉死异常，退化为仅 kill_process_tree 双保险", exc_info=True)
    finally:  # pragma: no cover
        if job and not pinned:  # 0/None=句柄从未建立（create 失败），无需关
            _close_job_handle(job)
        if suspended:
            _resume_suspended_process(process)

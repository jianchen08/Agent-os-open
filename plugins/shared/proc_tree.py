"""进程树终止共享件（超时清理/插件卸载收尾共用，D1/D4/D5 消费）。

契约：``kill_process_tree(pid)`` 终止以 pid 为根的整棵进程树，**失败不吞**——
返回失败清单（str 列表）供调用方记日志/回填错误消息；空列表 = 树已清。
根进程已不存在的视为成功（终止目标已达成，非故障）。

实现：
- 双平台统一走 psutil 先收集子树再叶→根杀（先收集后杀，收窄杀过程中
  新子进程逃逸窗口）；杀后 ``wait_procs`` 收敛等待，仍未退出的进失败清单。
- Windows 追加 ``taskkill /T /F`` 兜底：psutil 按父子快照杀，与真实进程树
  之间存在的竞态窗口由 taskkill 的内核遍历补网（正常路径下进程已死，
  taskkill 报"找不到进程"属预期，非失败）。

psutil 已在根 pyproject 声明。
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

__all__ = ["kill_process_tree"]

# taskkill 兜底超时（秒）：taskkill 挂死时不能拖住调用方
_TASKKILL_TIMEOUT_SECONDS = 10
# 杀后收敛等待（秒）：超时仍未退出的进程进失败清单
_REAP_TIMEOUT_SECONDS = 5.0


def kill_process_tree(pid: int) -> list[str]:
    """终止 pid 为根的进程树，返回失败清单（空 = 全清）。

    失败清单条目形如 ``"pid=123 kill 失败: ..."``，调用方按需记日志或
    附进错误消息；本函数只收集不上报（日志口径归调用方）。
    """
    import psutil  # noqa: PLC0415 — 延迟导入：非进程管理调用方零开销

    failures: list[str] = []
    if pid <= 0:
        # 非法目标（负 pid；POSIX pid 0 = 调用方进程组，绝不可杀）：
        # 视为无可终止对象，不抛异常。
        return []
    try:
        parent = psutil.Process(pid)
    except (psutil.NoSuchProcess, ValueError):
        return []  # 已退出 / 非法 pid：终止目标已达成
    except psutil.Error as exc:
        return [f"pid={pid} psutil 访问失败: {exc}"]

    # 先收集子树再叶→根杀：收集是一次快照，杀从叶子开始，根最后退
    # （根活着期间可能 spawn 的子进程窗口由 Windows taskkill 兜底收窄）。
    children: list[psutil.Process] = []
    try:
        children = parent.children(recursive=True)
    except psutil.Error as exc:
        failures.append(f"pid={pid} 子进程枚举失败: {exc}")

    for proc in reversed(children):
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass  # 杀的过程中自己先退了，目标已达成
        except psutil.Error as exc:
            failures.append(f"pid={proc.pid} kill 失败: {exc}")
    try:
        parent.kill()
    except psutil.NoSuchProcess:
        pass
    except psutil.Error as exc:
        failures.append(f"pid={pid} kill 失败: {exc}")

    # 收敛等待：kill 是异步信号，不等待则调用方立即检查存活可能误判。
    # 受保护进程（如 Windows System）连 wait 都 AccessDenied——按杀失败
    # 同口径进失败清单，不向上抛。
    procs = [*children, parent]
    try:
        _, alive = psutil.wait_procs(procs, timeout=_REAP_TIMEOUT_SECONDS)
    except psutil.Error as exc:
        failures.append(f"pid={pid} 终止收敛等待失败: {exc}")
        alive = []
    for proc in alive:
        failures.append(f"pid={proc.pid} 终止后 {int(_REAP_TIMEOUT_SECONDS)}s 仍存活")

    if os.name == "nt":
        _taskkill_fallback(pid, failures)
    return failures


def _taskkill_fallback(pid: int, failures: list[str]) -> None:
    """Windows 兜底：taskkill /T /F 按内核父子关系再杀一遍。

    psutil 已杀干净时 taskkill 非零退出（找不到进程）属预期，不算失败；
    只有调用本身异常（超时/无法启动）才记入失败清单。
    """
    try:
        subprocess.run(
            ["taskkill", "/pid", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — 兜底失败必须留痕，不吞
        failures.append(f"pid={pid} taskkill 兜底失败: {exc}")
        logger.warning("[proc_tree] taskkill 兜底失败 | pid=%s | error=%s", pid, exc)

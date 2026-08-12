"""主事件循环冻结看门狗。

独立 OS 线程监控 asyncio 主 loop 心跳：通过 ``loop.call_soon_threadsafe`` 投递
时间戳回调，若 loop 连续 ``freeze_threshold`` 秒都没处理回调（被同步代码冻住），
主动 dump 所有线程栈到 ``log_dir/loop_freeze.log``，精确定位冻在哪个
subprocess.run / socket.read / SSL 握手 / C 扩展。

为什么需要它（和应用层 asyncio 日志的区别）
------------------------------------------
此前所有可观测性（task_notifier 日志、``_await_with_escape`` 诊断线程、
payload_diag）都是**应用层 asyncio 日志**——依赖事件循环调度协程才会打出。
当主 loop 被同步代码冻住时，loop 不调度、协程不让出，应用层日志只能停在
"冻住前最后一条"，中间空白，永远抓不到真正的元凶。

本看门狗走完全不同的维度：独立 OS 线程 + ``faulthandler``（C 级，不依赖 GIL
也不依赖事件循环），loop 冻住时照常 dump，是抓"同步阻塞冻 loop"的唯一手段。

定位的问题：生产反复出现的"子任务终态通知上级后，上级首次 LLM 调用永久卡死"。
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_FREEZE_THRESHOLD = 15.0  # loop 停滞超过此秒数判定冻结
_DEFAULT_CHECK_INTERVAL = 2.0  # 看门狗检查频率
_DEFAULT_DUMP_COOLDOWN = 60.0  # 冻结 dump 冷却，避免冻结期间刷屏


def dump_all_threads(file_path: Path, *, tag: str = "LOOP FROZEN", loop: Any = None) -> None:
    """dump 所有 OS 线程栈到文件。

    组合两套栈源，互相印证：
    - ``faulthandler.dump_traceback(all_threads=True)``：C 级栈，能看到
      subprocess.run / socket.read / SSL 等 C 层同步阻塞点（不依赖 GIL）。
    - ``sys._current_frames()`` + ``traceback.print_stack``：Python 级栈，
      带行号、更易读，定位到具体 .py 文件的调用链。

    Args:
        file_path: dump 目标文件（追加写）。
        tag: dump 块的标题标记。
    """
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n{'=' * 72}\n{time.strftime('%Y-%m-%d %H:%M:%S')}  {tag}\n{'=' * 72}\n")
            fh.write("--- faulthandler (C-level, all threads) ---\n")
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.write("\n--- Python frames (sys._current_frames) ---\n")
            frames = sys._current_frames()
            for t in threading.enumerate():
                fh.write(f"\nThread {t.name!r} ident={t.ident} alive={t.is_alive()} daemon={t.daemon}:\n")
                # t.ident 在线程未启动时为 None，无法作为帧表 key
                if t.ident is None:
                    fh.write("  (no ident, thread not started)\n")
                    continue
                frame = frames.get(t.ident)
                if frame is None:
                    fh.write("  (no frame available)\n")
                    continue
                traceback.print_stack(frame, file=fh)

            # asyncio 协程栈：OS 线程栈只能看到主 loop 在 select/_run_once，
            # 看不到「哪个协程在等什么 event」——而这正是定位协程级卡死
            # （主 loop 活着、某协程卡在 await）的关键。如 f81e12cac33d 卡死：
            # OS 栈显示 select（误判没冻），但某个协程实际卡在等 LLM/lock/queue。
            if loop is not None:
                fh.write("\n--- asyncio tasks (协程栈，定位协程级卡死) ---\n")
                try:
                    import asyncio  # noqa: PLC0415

                    tasks = asyncio.all_tasks(loop)
                    fh.write(f"共 {len(tasks)} 个未完成 task\n")
                    for _t in tasks:
                        try:
                            fh.write(f"\nTask {_t!r} done={_t.done()}:\n")
                            _t.print_stack(file=fh, limit=20)
                        except Exception as _te:  # noqa: BLE001
                            fh.write(f"  (print_stack 失败: {_te})\n")
                except Exception as _ae:  # noqa: BLE001
                    fh.write(f"(asyncio tasks dump 失败: {_ae})\n")
        logger.error("[LoopWatchdog] 已 dump 线程栈到 %s（%s）", file_path, tag)
    except Exception as exc:  # noqa: BLE001 - 诊断工具自身绝不能拖垮进程
        logger.error("[LoopWatchdog] dump 线程栈失败: %s", exc)


def attach_loop_watchdog(
    loop: Any,
    log_dir: str | Path,
    *,
    freeze_threshold: float = _DEFAULT_FREEZE_THRESHOLD,
    check_interval: float = _DEFAULT_CHECK_INTERVAL,
    dump_cooldown: float = _DEFAULT_DUMP_COOLDOWN,
) -> threading.Thread:
    """启动看门狗线程监控给定 loop 的心跳。

    工作原理：看门狗线程每 ``check_interval`` 秒用
    ``loop.call_soon_threadsafe`` 向主 loop 投递一个心跳回调（更新时间戳）。
    loop 正常运行时会立即调度该回调，时间戳持续刷新；若 loop 被同步代码
    冻住（subprocess.run / 阻塞 IO / C 扩展），回调无法被调度，时间戳停滞。
    停滞超过 ``freeze_threshold`` 秒即判定冻结，dump 所有线程栈。

    正常运行时零业务影响（仅每 2 秒一次轻量回调 + 时间戳比较）。
    看门狗线程为 daemon，进程退出时自动结束。

    Args:
        loop: 被监控的 asyncio 事件循环。
        log_dir: dump 文件目录（``loop_freeze.log`` 写入此处）。
        freeze_threshold: 判定冻结的停滞阈值（秒）。
        check_interval: 心跳检查间隔（秒）。
        dump_cooldown: 连续 dump 的冷却（秒），冻结期间避免刷屏。

    Returns:
        看门狗线程实例（daemon）。
    """
    dump_path = Path(log_dir) / "loop_freeze.log"
    # last_heartbeat 初始化为当前时刻，避免启动瞬间（首次回调尚未执行）误判冻结。
    state: dict[str, float] = {"last_heartbeat": time.monotonic(), "last_dump": 0.0}

    def _heartbeat() -> None:
        state["last_heartbeat"] = time.monotonic()

    def _watch() -> None:
        logger.info(
            "[LoopWatchdog] 已启动 | freeze_threshold=%.0fs check_interval=%.1fs dump=%s",
            freeze_threshold,
            check_interval,
            dump_path,
        )
        while True:
            try:
                loop.call_soon_threadsafe(_heartbeat)
            except RuntimeError:
                # loop 已关闭（进程退出），停止看门狗
                logger.info("[LoopWatchdog] loop 已关闭，看门狗退出")
                break
            except Exception:  # noqa: BLE001
                # 其它异常（loop 未就绪等）也终止，避免看门狗空转吞错
                break

            time.sleep(check_interval)
            stalled = time.monotonic() - state["last_heartbeat"]
            if stalled > freeze_threshold:
                now = time.monotonic()
                if now - state["last_dump"] >= dump_cooldown:
                    state["last_dump"] = now
                    dump_all_threads(
                        dump_path,
                        tag=f"LOOP FROZEN (stalled {stalled:.0f}s)",
                        loop=loop,
                    )

    thread = threading.Thread(target=_watch, name="LoopWatchdog", daemon=True)
    thread.start()
    return thread


__all__ = ["attach_loop_watchdog", "dump_all_threads"]

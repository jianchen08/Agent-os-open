"""主事件循环冻结看门狗回归测试。

验证 attach_loop_watchdog 能在主 loop 被同步代码冻住时 dump 所有线程栈，
精确定位冻结源。这是定位"通知上级后上级卡死"类问题的关键诊断手段——
应用层 asyncio 日志在 loop 冻住时不调度、抓不到元凶，本看门狗走独立 OS
线程 + faulthandler，loop 冻住时照常 dump。
"""

import asyncio
import sys
import time

sys.path.insert(0, "src")

from monitoring.loop_watchdog import attach_loop_watchdog, dump_all_threads  # noqa: E402


def test_dump_all_threads_writes_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """dump_all_threads 写入文件，含 faulthandler 与 Python 帧两段。"""
    out = tmp_path / "freeze.log"
    dump_all_threads(out, tag="TEST_TAG")
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "TEST_TAG" in content
    assert "faulthandler" in content
    assert "Python frames" in content


def test_watchdog_dumps_on_loop_freeze(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """主 loop 被同步 sleep 冻住时，watchdog 触发 dump 并定位到冻结点。"""

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        attach_loop_watchdog(
            loop,
            tmp_path,
            freeze_threshold=0.3,
            check_interval=0.1,
            dump_cooldown=0.0,
        )
        # 同步阻塞冻住 loop（模拟 subprocess.run / 阻塞 IO 占据事件循环，
        # 即生产中 worktree git 合并 / rmtree 冻住 loop 的场景）
        time.sleep(1.0)
        # 让出 loop，给 watchdog 线程完成文件写入的时间
        await asyncio.sleep(0.3)

    asyncio.run(_main())

    log_file = tmp_path / "loop_freeze.log"
    assert log_file.exists(), "loop 冻结后应生成 loop_freeze.log"
    content = log_file.read_text(encoding="utf-8")
    assert "LOOP FROZEN" in content
    # Python 帧应定位到冻结源（time.sleep 出现在本测试调用栈中）
    assert "_main" in content or "time.sleep" in content or "sleep" in content


def test_watchdog_quiet_on_healthy_loop(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """主 loop 正常（不断让出）时，watchdog 不 dump。"""

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        attach_loop_watchdog(
            loop,
            tmp_path,
            freeze_threshold=0.5,
            check_interval=0.1,
            dump_cooldown=0.0,
        )
        # 正常协程：不断 await 让出 loop，心跳回调持续被调度刷新
        for _ in range(20):
            await asyncio.sleep(0.05)

    asyncio.run(_main())

    log_file = tmp_path / "loop_freeze.log"
    assert not log_file.exists(), "健康 loop 不应触发 dump"

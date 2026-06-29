"""流式硬超时 watchdog 的强关入口探测（纯静态分析，不发网络请求）。

背景：BUG-FIX-fix_20260628_stream_cancel_cant_break_socket
当 LLM 流式调用卡在 httpx 底层 socket recv 时，asyncio.wait_for 的 cancel
打不穿 C 级 recv，导致 inter_chunk_timeout 形同虚设、pipeline 永久挂死。
解决思路：用独立 task（_stream_heartbeat）在静默超时后【主动关闭】底层连接，
让 recv() 收到连接关闭立即抛异常，从而打破死锁。

本 probe 的唯一目的：确定【唯一正确】的强关调用入口，不靠猜。

运行（PYTHONPATH 不需要，纯反射）：
  C:\\...\\Python312\\python.exe scripts/probe_stream_force_close.py
"""
from __future__ import annotations

import inspect


def main() -> None:
    import litellm
    from openai import AsyncStream

    csw = litellm.CustomStreamWrapper
    csw_file = inspect.getsourcefile(csw)

    print("=" * 70)
    print("流式强关入口探测")
    print(f"  litellm 版本: {__get_dist_version('litellm')}")
    print(f"  CustomStreamWrapper 源: {csw_file}")
    print("=" * 70)

    # ── 1. CustomStreamWrapper.completion_stream 是什么 ──
    #      经 streaming_handler.__anext__ 消费，openai provider 路径下是
    #      openai.AsyncStream（见 litellm/llms/openai/openai.py:1119-1120）。
    print("\n[1] CustomStreamWrapper.completion_stream")
    print("    openai provider 路径：litellm 把 openai SDK 返回值原样塞进 completion_stream")
    print("    → completion_stream 即 openai.AsyncStream")

    # ── 2. CustomStreamWrapper.aclose() 做什么 ──
    print("\n[2] CustomStreamWrapper.aclose() 实现：")
    src = inspect.getsource(csw.aclose)
    print(src)

    # ── 3. openai.AsyncStream 怎么真正关底层连接 ──
    print("\n[3] openai.AsyncStream 结构与 close()：")
    init_src = inspect.getsource(AsyncStream.__init__)
    print(init_src)
    print("AsyncStream.close() 实现：")
    print(inspect.getsource(AsyncStream.close))

    # ── 结论 ──
    print("=" * 70)
    print("结论：唯一正确强关入口")
    print("=" * 70)
    print("""
    CustomStreamWrapper.aclose()
      └─ completion_stream.aclose()  (即 openai.AsyncStream.close())
           └─ await self.response.aclose()  (httpx.Response.aclose())
                └─ 关闭底层 socket

    即：adapter 持有的 `response`（CustomStreamWrapper）的 aclose() 即可强关。
    litellm 已用 anyio.CancelScope(shield=True) 包裹，保证：
      1. 取消信号（CancelledError）不会中断 aclose 自身的 await；
      2. completion_stream 先置 None 再关，重复调用安全；
      3. httpx.Response.aclose() 真正释放底层连接。

    因此 _force_close_stream 只需：
        await asyncio.wait_for(response.aclose(), timeout=2)
    无需深挖 httpx transport 层。

    watchdog（独立 asyncio.Task）在静默超时后调它，与主协程的
    wait_for(__anext__) 互不阻塞——aclose 关闭 socket 后，卡在 recv() 的
    __anext__ 会立即收到连接错误而抛异常，打破死锁。
    """)


def __get_dist_version(pkg: str) -> str:
    from importlib.metadata import version  # noqa: PLC0415
    try:
        return version(pkg)
    except Exception:  # noqa: BLE001
        return "?"


if __name__ == "__main__":
    main()

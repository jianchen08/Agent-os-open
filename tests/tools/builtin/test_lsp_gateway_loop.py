"""LSPGateway 事件循环生命周期回归测试。

回归场景：
    工具执行框架（tool_core）会为每次异步工具调用新建并关闭一个事件循环，
    每个 task 管道也各自 asyncio.run 一个新循环。LSPClient 的子进程 transport
    绑定在某个循环上，循环关闭后 transport 失效，下一次复用该 client 时在
    Windows ProactorEventLoop 上抛
    ``AttributeError: 'NoneType' object has no attribute 'send'``
    （``self._loop._proactor`` 随循环关闭被置 None）。

修复后：LSPGateway 自管常驻专用事件循环，所有 LSP 调用 marshal 进专用循环，
client 缓存绑定在永不关闭的循环上，跨调用方循环复用不再崩溃。

本测试不依赖真实 LSP 服务器（pylsp 等），通过注入 FakeLSPClient 验证：
- 调用方循环关闭后，gateway 仍能复用同一 client 实例；
- 每次调用使用的专用循环是同一个、且不随调用方循环关闭而失效；
- client 上看到的 running loop == gateway 专用循环（而非调用方循环）。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from lsp.gateway import LSPGateway

# 用平台无关的绝对路径构造 file URI（Windows 上 /tmp 不是绝对路径）
_TMPDIR = Path(__file__).resolve().parent


def _py(name: str) -> str:
    """返回测试目录下的绝对 .py 路径，便于跨平台构造 file URI。"""
    return str(_TMPDIR / name)


class FakeLSPClient:
    """模拟真实 LSPClient 的 loop-bound 语义。

    真实 client 在 start() 时创建子进程，其 stdin/stdout 的 StreamWriter/
    StreamReader transport **绑定到调用 start() 时所在的 running loop**。后续
    业务方法（get_diagnostics 等）通过 transport.write 发送数据时，会命中
    该绑定 loop；若该 loop 已关闭（Windows ProactorEventLoop 上
    ``self._loop._proactor`` 被置 None），write 抛
    ``AttributeError: 'NoneType' object has no attribute 'send'``。

    本替身复现这一关键语义：start() 锁定"绑定 loop"，业务方法前检查绑定 loop
    是否仍存活，若已关闭则抛出与生产环境一致的错误。这样无需真实 LSP 服务器，
    即可锁住"client 跨已关闭循环复用"的回归。
    """

    instances: list[FakeLSPClient] = []

    def __init__(self, server_info):
        self.server_info = server_info
        self.initialized = False
        self.bound_loop: asyncio.AbstractEventLoop | None = None
        self.call_loops: list[int] = []  # 记录每次业务调用所在的 loop id
        self.stopped = False
        FakeLSPClient.instances.append(self)

    async def start(self) -> bool:
        # 真实 client 的 transport 在此刻绑定到 running loop
        self.bound_loop = asyncio.get_running_loop()
        self.initialized = True
        return True

    def _check_transport_alive(self):
        """模拟 transport.write 命中已关闭 loop 的行为。"""
        if self.bound_loop is not None and self.bound_loop.is_closed():
            raise AttributeError("'NoneType' object has no attribute 'send'")

    async def stop(self):
        self.stopped = True
        self.initialized = False

    async def get_diagnostics(self, uri: str):
        self._check_transport_alive()
        self.call_loops.append(id(asyncio.get_running_loop()))
        return [f"diag-for:{uri}"]

    async def go_to_definition(self, uri: str, position):
        self._check_transport_alive()
        self.call_loops.append(id(asyncio.get_running_loop()))
        return []

    async def find_references(self, uri: str, position):
        self._check_transport_alive()
        self.call_loops.append(id(asyncio.get_running_loop()))
        return []


@pytest.fixture
def gateway(monkeypatch):
    """构造一个用 FakeLSPClient 替换真实 client 的 gateway 并初始化。"""
    FakeLSPClient.instances.clear()
    monkeypatch.setattr("lsp.gateway.LSPClient", FakeLSPClient)
    gw = LSPGateway()
    asyncio.run(gw.initialize())
    yield gw
    asyncio.run(gw.shutdown())


def _run_in_fresh_loop(coro):
    """模拟工具执行框架：新建一个事件循环跑完协程后立即关闭。

    对应 src/plugins/core/tool_core/plugin.py:_asyncio_tool_runner 的行为：
        loop = asyncio.new_event_loop()
        loop.run_until_complete(coro)
        loop.close()
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestGatewayCrossLoopReuse:
    """跨调用方循环复用 client 的回归测试。"""

    def test_client_reused_across_closed_caller_loops(self, gateway):
        """两次调用分别用各自独立的、调用后关闭的循环，client 应被复用且不崩。"""
        diags1 = _run_in_fresh_loop(gateway.get_diagnostics(_py("a.py")))
        diags2 = _run_in_fresh_loop(gateway.get_diagnostics(_py("b.py")))

        assert diags1 == [f"diag-for:{Path(_py('a.py')).as_uri()}"]
        assert diags2 == [f"diag-for:{Path(_py('b.py')).as_uri()}"]
        # 同一 client 被复用（未重新创建第二个实例）
        assert len(FakeLSPClient.instances) == 1

    def test_calls_run_on_dedicated_loop_not_caller_loop(self, gateway):
        """client 业务方法必须运行在 gateway 专用循环上，而非调用方循环。

        这是修复的核心：client transport 绑定在专用循环，不随调用方循环关闭而死。
        """
        _run_in_fresh_loop(gateway.get_diagnostics(_py("x.py")))

        client = FakeLSPClient.instances[0]
        dedicated_loop_id = id(gateway._loop)  # noqa: SLF001
        assert client.call_loops, "client.get_diagnostics 应至少被调用一次"
        assert all(loop_id == dedicated_loop_id for loop_id in client.call_loops)

    def test_dedicated_loop_stays_alive_after_many_caller_loops(self, gateway):
        """多次（各自关闭的）调用方循环后，专用循环仍存活、可继续服务。"""
        dedicated_loop = gateway._loop  # noqa: SLF001
        assert not dedicated_loop.is_closed()

        for i in range(5):
            _run_in_fresh_loop(gateway.get_diagnostics(_py(f"{i}.py")))
            # 每次调用方循环关闭后，专用循环必须仍然存活
            assert not dedicated_loop.is_closed()

    def test_get_diagnostics_detects_unavailable_language(self, gateway):
        """未配置服务器（或启动失败）时返回空列表而非抛异常。"""
        # _detect_language 对未知扩展名默认返回 "python"，这里直接传 language
        # 强制走未配置的 "cobol"，验证 ensure_client 找不到 server 时优雅返回 []
        result = _run_in_fresh_loop(gateway.get_diagnostics(_py("a.unknownext"), language="cobol"))
        assert result == []


class TestGatewayLoopThreading:
    """专用循环线程模型验证。"""

    def test_dedicated_loop_runs_in_own_daemon_thread(self, gateway):
        """专用循环应在独立 daemon 线程中 run_forever。"""
        assert gateway._loop_thread is not None  # noqa: SLF001
        assert gateway._loop_thread.is_alive()
        assert gateway._loop_thread.daemon
        assert gateway._loop_thread.name == "lsp-gateway-loop"

    def test_concurrent_caller_loops_serialize_safely(self, gateway):
        """两个调用方线程各自在自己的循环里并发调用，gateway 不应崩。"""
        errors: list[BaseException] = []

        def caller(name: str):
            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(gateway.get_diagnostics(_py(f"concurrent_{name}.py")))
                finally:
                    loop.close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=caller, args=(str(i),)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"并发调用出现错误: {errors}"

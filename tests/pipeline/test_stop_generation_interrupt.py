"""停止生成信号中断测试。

验证 stop_generation 信号能立即中断 engine_task（打断进行中的 LLM await）。
核心修复点：deliver_signal 只写 state 时，正在 await 的 LLM 调用不会被中断，
用户点停止要等当前轮跑完才生效。修复后 stop_generation 额外 cancel engine_task。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.engine import PipelineEngine
from pipeline.registry import PluginRegistry
from pipeline.route import InputRouteTable, OutputRouteTable
from pipeline.engine_registry import get_engine_registry


def _make_engine(pipeline_id: str = "stop-test-1") -> PipelineEngine:
    """构造一个真实 PipelineEngine（用空路由表/插件注册表）。"""
    e = PipelineEngine(
        input_route_table=InputRouteTable(),
        output_route_table=OutputRouteTable(),
        plugin_registry=PluginRegistry(),
    )
    e.pipeline_id = pipeline_id
    return e


@pytest.fixture(autouse=True)
def clean_registry():
    """每个测试前后清空全局 EngineRegistry。"""
    reg = get_engine_registry()
    reg._engines.clear()
    yield
    reg._engines.clear()


class TestStopGenerationInterrupt:
    """stop_generation 信号必须 cancel engine_task（立即中断 await）。"""

    def test_interrupt_cancels_engine_task(self) -> None:
        """stop_generation 信号投递后，engine_task 被 cancel。"""
        engine = _make_engine("stop-cancel-1")
        # 注册到 registry，挂一个模拟的 engine_task
        entry = get_engine_registry().register(
            "stop-cancel-1", engine, thread_id="t1",
        )
        mock_task = MagicMock()
        mock_task.done.return_value = False  # 正在运行
        entry.engine_task = mock_task

        # 模拟 state 已初始化（run 过一轮）
        engine._last_state = {"iteration": 1}

        # 投递 stop_generation 信号
        engine.deliver_signal({"signal_type": "stop_generation"})

        # 验证：engine_task 被 cancel（立即中断 LLM await）
        mock_task.cancel.assert_called_once(), (
            "stop_generation 信号必须 cancel engine_task，否则停止无效"
        )

    def test_interrupt_skips_done_task(self) -> None:
        """engine_task 已 done 时，stop_generation 不重复 cancel。"""
        engine = _make_engine("stop-done-1")
        entry = get_engine_registry().register("stop-done-1", engine)
        mock_task = MagicMock()
        mock_task.done.return_value = True  # 已完成
        entry.engine_task = mock_task
        engine._last_state = {"iteration": 1}

        engine.deliver_signal({"signal_type": "stop_generation"})

        mock_task.cancel.assert_not_called(), "已完成的 task 不应再 cancel"

    def test_interrupt_no_task_does_nothing(self) -> None:
        """engine_task 为 None 时，stop_generation 不报错。"""
        engine = _make_engine("stop-notask-1")
        get_engine_registry().register("stop-notask-1", engine)
        engine._last_state = {"iteration": 1}

        # 不应抛异常
        engine.deliver_signal({"signal_type": "stop_generation"})

    def test_non_stop_signal_does_not_interrupt(self) -> None:
        """非 stop_generation 信号（如自定义信号）只写 state，不 interrupt。"""
        engine = _make_engine("stop-other-1")
        entry = get_engine_registry().register("stop-other-1", engine)
        mock_task = MagicMock()
        mock_task.done.return_value = False
        entry.engine_task = mock_task
        engine._last_state = {"iteration": 1}

        # 投递非 stop 信号
        engine.deliver_signal({"signal_type": "custom_signal", "data": "x"})

        mock_task.cancel.assert_not_called(), (
            "非 stop_generation 信号不应 cancel engine_task（只写 state）"
        )
        # 但 state 里应有信号记录
        assert "custom_signal" in engine._last_state["pending_signals"]

    def test_stop_signal_also_writes_state(self) -> None:
        """stop_generation 既 cancel task，也写 state（供插件下一轮检查）。"""
        engine = _make_engine("stop-state-1")
        entry = get_engine_registry().register("stop-state-1", engine)
        mock_task = MagicMock()
        mock_task.done.return_value = False
        entry.engine_task = mock_task
        engine._last_state = {"iteration": 1}

        engine.deliver_signal({"signal_type": "stop_generation"})

        mock_task.cancel.assert_called_once()
        # state 里也记录了 stop 信号
        assert "stop_generation" in engine._last_state["pending_signals"]


class TestStopSignalStateNone:
    """stop_generation 不依赖 last_state 判定是否中断（修复运行中首次 run 停止失效）。

    _last_state 仅在 _run_loop 的 finally（run 结束时）写入，引擎运行中——尤其首次
    run——其值为 None。是否有 await 在进行应以 engine_task 存活与否为准，而非 last_state。
    """

    def test_stop_signal_running_first_run_cancels(self) -> None:
        """运行中首次 run（last_state=None 但 engine_task 存活）时，stop 仍必须 cancel。

        回归测试：原先 deliver_signal 以 `state is None` 提前 return，导致首次 run
        进行中点停止永远不中断（LLM 跑到自然结束）。
        """
        engine = _make_engine("stop-prerun-1")
        entry = get_engine_registry().register("stop-prerun-1", engine)
        mock_task = MagicMock()
        mock_task.done.return_value = False  # 正在运行
        entry.engine_task = mock_task
        # last_state 保持 None（首次 run 进行中）

        engine.deliver_signal({"signal_type": "stop_generation"})

        mock_task.cancel.assert_called_once(), (
            "首次 run 进行中（last_state=None）时 stop 必须取消 engine_task"
        )

    def test_stop_signal_truly_idle_no_task(self) -> None:
        """engine_task 为 None（真 idle，无 await）时，stop 不报错也不 cancel。"""
        engine = _make_engine("stop-prerun-2")
        get_engine_registry().register("stop-prerun-2", engine)
        # engine_task 保持 None（未启动 run）；last_state 保持 None

        engine.deliver_signal({"signal_type": "stop_generation"})  # 不应抛异常



class TestEngineKeepsEntryAfterRun:
    """引擎正常结束后：entry 保留 + is_idle=True（可重发消息走 idle 重启）。

    回归点：原逻辑引擎正常结束会 unregister 自身 + 不复位 _run_started，
    导致任务标签页续发消息时报"未注册"（entry 没了或 is_idle=False）。
    修复：I3 引擎不主动 unregister + finally 复位 _run_started。
    """

    def test_engine_idle_after_run_complete(self) -> None:
        """引擎 run 正常结束后 is_idle 返回 True（_run_started 复位）。"""
        from pipeline.engine import PipelineEngine
        from pipeline.registry import PluginRegistry
        from pipeline.route import InputRouteTable, OutputRouteTable

        engine = PipelineEngine(
            input_route_table=InputRouteTable(),
            output_route_table=OutputRouteTable(),
            plugin_registry=PluginRegistry(),
        )
        # 模拟 run 过程：_run_started=True（run 开始时设）
        engine._run_started = True
        assert engine.is_idle is False  # run 中不是 idle

        # 模拟 run finally 复位（直接调属性，验证复位逻辑本身）
        engine._run_started = False
        engine._running = False

        assert engine.is_idle is True, (
            "引擎结束后 _run_started 复位，is_idle 应为 True（供 send 走 idle 重启）"
        )

    def test_find_engine_returns_idle_for_completed_engine(self) -> None:
        """_find_engine 对已结束（idle）引擎返回 idle（而非 None/未注册）。"""
        from pipeline.message_bus import _find_engine
        from pipeline.registry import get_engine_registry

        engine = MagicMock()
        # 已结束引擎的状态：非 running/suspended，idle=True
        engine.is_running = False
        engine.is_suspended = False
        engine.is_idle = True

        reg = get_engine_registry()
        reg._engines.clear()
        reg.register("completed-1", engine)

        try:
            found, state = _find_engine("completed-1")
            assert found is engine, "应找到引擎"
            assert state == "idle", f"已结束引擎应返回 idle，得到 {state}"
        finally:
            reg._engines.clear()


class TestCooperativeStopViaOnChunk:
    """协作式停止：on_chunk 回调检测到 stop 信号时 raise CancelledError。

    cancel engine_task 在 httpx 底层 socket recv 卡死时无法打断 LLM await，
    此时 on_chunk 是唯一可靠的停止检查点（每个流式 chunk 都同步触发它）。
    raise CancelledError 冒泡到 _run_loop 的 except CancelledError，复用既有
    停止清理路径，无需改 adapter/llm_core/LLMResponse。
    """

    def test_is_stop_signal_active_reads_current_state(self) -> None:
        """_is_stop_signal_active 读运行中实时 _current_state（非 last_state）。"""
        engine = _make_engine("coop-1")
        # 模拟 run 进行中：_current_state 有值，last_state 仍为 None
        engine._current_state = {"iteration": 1}
        assert engine._is_stop_signal_active() is False

        # deliver_signal 写入信号
        engine._current_state.setdefault("pending_signals", {})["stop_generation"] = {
            "signal_type": "stop_generation",
        }
        assert engine._is_stop_signal_active() is True

    def test_on_chunk_raises_cancelled_when_stop_signal_active(self) -> None:
        """信号命中时，on_chunk raise CancelledError 中断流式。

        on_chunk 现位于流式输出口 StreamingOutput（engine._streaming），
        通过 stop_check 回调注入的 _is_stop_signal_active 做协作式中断判定。
        """
        engine = _make_engine("coop-2")
        engine._current_state = {
            "iteration": 1,
            "pending_signals": {"stop_generation": {"signal_type": "stop_generation"}},
        }

        with pytest.raises(asyncio.CancelledError):
            engine._streaming._on_chunk({"type": "text", "content": "x"})

    def test_on_chunk_no_signal_enqueues_normally(self) -> None:
        """无停止信号时，on_chunk 正常入队（不破坏原有流式功能）。"""
        engine = _make_engine("coop-3")
        engine._current_state = {"iteration": 1}  # 无 pending_signals
        # 装配 chunk queue + bridge（on_chunk 依赖它们，均在 StreamingOutput 内）
        engine._streaming._chunk_queue = asyncio.Queue()
        engine._streaming._bridge = MagicMock()  # 非空 bridge

        engine._streaming._on_chunk({"type": "text", "content": "hello"})

        assert engine._streaming._chunk_queue.qsize() == 1, "无停止信号时 chunk 应正常入队"

    def test_on_chunk_signal_only_read_not_consumed(self) -> None:
        """协作式停止只读信号不删除（消费/清理由插件下一轮自治处理）。"""
        engine = _make_engine("coop-4")
        engine._current_state = {
            "iteration": 1,
            "pending_signals": {"stop_generation": {"signal_type": "stop_generation"}},
        }

        with pytest.raises(asyncio.CancelledError):
            engine._streaming._on_chunk({"type": "text", "content": "x"})

        # 信号仍在（未被协作式路径消费）
        assert "stop_generation" in engine._current_state["pending_signals"]


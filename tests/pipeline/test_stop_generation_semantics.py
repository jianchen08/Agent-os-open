"""停止生成（stop_generation）"只打断输出流"语义回归测试。

修复背景（BUG-20260716_stop_generation_kills_task_and_pipeline）：
用户点"停止生成"语义上只是想让管道暂时别输出（可能要纠正/插话），与任务、
管道死活无关。但此前代码有两处把"停输出"越权升级成"杀任务 + 毁管道 + 判失败 +
触发重试"：

  路径A（WS 入口）: app_factory.py 的 stop_generation 分支在投递 CONTROL 信号之外，
    还冗余调用 fail_task + cancel_pipeline。cancel_pipeline → message_bus.stop() →
    unregister() 删 entry；fail_task 把任务判 FAILED 并触发 1/6 重试。
  路径B（引擎内部，更隐蔽）: deliver_signal→_interrupt_engine_task→engine_task.cancel()
    或 _on_chunk 协作式 raise CancelledError，二者冒泡到 run() 的
    except CancelledError，该分支【不区分】"用户主动停"与"管道真崩溃"，一律
    ENDED + RAW_ERROR + emit_error + _mark_task_failed_on_engine_exit(→fail_task)。

修复后：
  - 路径A: app_factory.py 删掉 fail_task/cancel_pipeline 级联，只留信号投递。
  - 路径B: 引擎引入 _user_stop_requested 标志。stop_generation 经
    _interrupt_engine_task（cancel 前）或 _on_chunk（raise 前，经 on_user_stop 回调）
    先置该标志；run() 的 CancelledError 分支据此走"安静退出"——不写 RAW_ERROR、
    不 emit_error、不 fail_task。entry 保留、_run_started 复位为 idle，重发即继续。

本测试锁定三条核心契约 + 一条对照组（真崩溃仍失败）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.engine import PipelineEngine
from pipeline.engine_registry import get_engine_registry
from pipeline.message_bus import _find_engine
from pipeline.registry import PluginRegistry
from pipeline.route import InputRouteTable, OutputRouteTable
from pipeline.types import StateKeys


@pytest.fixture(autouse=True)
def clean_registry():
    """每个测试前后清空全局 EngineRegistry。"""
    reg = get_engine_registry()
    reg._engines.clear()
    yield
    reg._engines.clear()


def _make_engine(pipeline_id: str = "sem-1") -> PipelineEngine:
    """构造真实 PipelineEngine（空路由表/插件注册表，对齐 test_stop_generation_interrupt）。"""
    e = PipelineEngine(
        input_route_table=InputRouteTable(),
        output_route_table=OutputRouteTable(),
        plugin_registry=PluginRegistry(),
    )
    e.pipeline_id = pipeline_id
    return e


# 直接驱动 _run_loop（绕过 run() 的 agent_config 守卫与模型 tier 刷新），
# 用最小 state + patch run_iteration 抛 CancelledError，精准测试 except 分支。
def _min_state(pipeline_id: str) -> dict:
    return {
        StateKeys.PIPELINE_ID: pipeline_id,
        StateKeys.ITERATION: 0,
        StateKeys.ENDED: False,
        "messages": [{"role": "user", "content": "hi"}],
    }


async def _drive_run_loop(
    engine: PipelineEngine,
    state: dict,
    *,
    set_user_stop: bool,
) -> dict:
    """驱动引擎 _run_loop 到 run_iteration 抛 CancelledError。

    set_user_stop=True 模拟停止生成路径在迭代中置标志（_interrupt_engine_task /
    _on_chunk 的行为）；False 模拟真崩溃（标志未置）。run_iteration 被 patch 成
    抛 CancelledError，冒泡到 _run_loop 的 except CancelledError。
    """
    async def _cancel_iter(_eng, _st, _iter):  # noqa: ANN001
        if set_user_stop:
            _eng._user_stop_requested = True
        raise asyncio.CancelledError("user stop" if set_user_stop else "real cancel")

    with patch("pipeline.engine.run_iteration", _cancel_iter):
        return await engine._run_loop(state, resumed=False)


def _register_with_running_task(engine: PipelineEngine, pipeline_id: str) -> None:
    """把引擎注册进 registry，并挂一个"正在运行"的模拟 engine_task。"""
    entry = get_engine_registry().register(pipeline_id, engine, thread_id="t-sem")
    mock_task = MagicMock()
    mock_task.done.return_value = False  # 正在运行
    entry.engine_task = mock_task


class TestStopGenerationSetsUserStopFlag:
    """契约1：stop_generation 经任一路径都必须置 _user_stop_requested=True。

    这是 run() 安静退出分支的唯一判据，漏置会把"用户停输出"判成崩溃。
    """

    def test_interrupt_path_sets_flag(self) -> None:
        """路径B-1: deliver_signal → _interrupt_engine_task cancel 前 置标志。"""
        engine = _make_engine("sem-int-1")
        _register_with_running_task(engine, "sem-int-1")

        assert engine._user_stop_requested is False  # 初始未置
        engine.deliver_signal({"signal_type": "stop_generation"})

        assert engine._user_stop_requested is True, (
            "stop_generation 经 interrupt 路径必须置 _user_stop_requested，"
            "否则 run() 的 CancelledError 会把用户停止判成崩溃而 fail_task"
        )

    def test_cooperative_path_sets_flag(self) -> None:
        """路径B-2: _on_chunk 命中 stop 信号、raise 前经 on_user_stop 回调置标志。"""
        engine = _make_engine("sem-coop-1")
        engine._current_state = {
            "iteration": 1,
            "pending_signals": {"stop_generation": {"signal_type": "stop_generation"}},
        }

        with pytest.raises(asyncio.CancelledError):
            engine._streaming._on_chunk({"type": "text", "content": "x"})

        assert engine._user_stop_requested is True, (
            "stop_generation 经协作式 _on_chunk 路径必须置 _user_stop_requested，"
            "否则流式中断会被 run() 的 CancelledError 分支判成崩溃而 fail_task"
        )

    def test_non_stop_signal_does_not_set_flag(self) -> None:
        """对照组: 非停止信号（如 custom_signal）不置 _user_stop_requested。"""
        engine = _make_engine("sem-nostop-1")
        _register_with_running_task(engine, "sem-nostop-1")

        engine.deliver_signal({"signal_type": "custom_signal", "data": "x"})

        assert engine._user_stop_requested is False, (
            "非 stop_generation 信号不应置用户停止标志——那会吞掉真正的崩溃/取消"
        )

    def test_flag_starts_false(self) -> None:
        """契约: 引擎构造后 _user_stop_requested 初始为 False（防止误判静默退出）。"""
        engine = _make_engine("sem-reset-1")
        assert engine._user_stop_requested is False, (
            "新建引擎的用户停止标志必须为 False，否则首轮非用户取消会被误判为静默退出"
        )

    def test_streaming_on_user_stop_callback_wired(self) -> None:
        """StreamingOutput 构造时注入的 on_user_stop 指向引擎标志设置方法。"""
        engine = _make_engine("sem-wire-1")
        # 回调存在且调用后置标志
        assert engine._streaming._on_user_stop is not None
        engine._streaming._on_user_stop()
        assert engine._user_stop_requested is True


class TestStopGenerationKeepsEntryAndIdle:
    """契约2: 停止生成后 entry 保留 + 引擎可被 _find_engine 命中为 idle（重发即继续）。"""

    def test_entry_remains_after_user_stop_run(self) -> None:
        """用户停止触发的 CancelledError 退出后 entry 仍在（未被 unregister）。"""
        engine = _make_engine("sem-entry-1")
        _register_with_running_task(engine, "sem-entry-1")

        asyncio.run(_drive_run_loop(engine, _min_state("sem-entry-1"), set_user_stop=True))

        # entry 必须保留——安静退出不调 message_bus.stop，不 unregister
        assert get_engine_registry().get("sem-entry-1") is not None, (
            "用户停止生成不应注销管道 entry：丢失会导致续发消息报'未注册'"
        )
        # 引擎复位为 idle（_run_started 在 finally 复位），重发能命中 idle 重启
        assert engine.is_idle is True, (
            "用户停止后引擎应为 idle（_run_started 复位），供续发走 _start_idle_engine"
        )
        found, state = _find_engine("sem-entry-1")
        assert found is not None and state == "idle"


class TestUserStopQuietExit:
    """契约3: 用户停止的 CancelledError 走安静分支——不写 RAW_ERROR、不 fail_task。

    这是修复的核心：原 _run_loop 的 except CancelledError 不分青红皂白一律判失败。
    """

    def test_user_stop_no_raw_error_no_fail(self) -> None:
        """用户停止退出：state 无 RAW_ERROR、_mark_task_failed_on_engine_exit 不被调用。"""
        engine = _make_engine("sem-quiet-1")
        fail_spy = AsyncMock()
        with patch.object(engine, "_mark_task_failed_on_engine_exit", fail_spy):
            state = asyncio.run(
                _drive_run_loop(engine, _min_state("sem-quiet-1"), set_user_stop=True)
            )

        # 安静分支：不写 RAW_ERROR（区别于真崩溃分支）
        assert state.get(StateKeys.RAW_ERROR) in (None, ""), (
            "用户停止生成不应写 RAW_ERROR——那会把停止判成管道异常退出并触发重试"
        )
        # 安静分支：不调 fail_task
        fail_spy.assert_not_called(), (
            "用户停止生成不应 fail_task——这是'只打断输出'语义的核心保证"
        )

    def test_user_stop_no_emit_error(self) -> None:
        """用户停止退出：不给前端推 emit_error（停止不是错误）。"""
        engine = _make_engine("sem-noerr-1")
        # 装一个 bridge spy；_run_loop 的 finally 清理会复位 _streaming._bridge，
        # 故先持住 spy 引用再断言（不依赖运行后 _bridge 仍非空）。
        bridge_spy = MagicMock()
        engine._streaming._bridge = bridge_spy

        asyncio.run(_drive_run_loop(engine, _min_state("sem-noerr-1"), set_user_stop=True))

        bridge_spy.emit_error.assert_not_called(), (
            "用户停止生成不应给前端推 emit_error（停止不是错误）"
        )


class TestRealCancelStillFails:
    """对照组: 真正的（非用户）取消仍走失败分支——修复不能吞掉真崩溃。

    这是防止过度修复的护栏：_user_stop_requested=False 的 CancelledError 必须保持
    原行为（ENDED + RAW_ERROR + fail_task），否则超时硬墙、容器销毁、显式 cancel
    等正常终态路径会被静默吞掉。
    """

    def test_non_user_cancel_writes_raw_error(self) -> None:
        """非用户停止的 CancelledError 仍写 RAW_ERROR（真崩溃路径不变）。"""
        engine = _make_engine("sem-real-1")
        state = asyncio.run(_drive_run_loop(engine, _min_state("sem-real-1"), set_user_stop=False))

        assert state.get(StateKeys.RAW_ERROR), (
            "真崩溃/真取消的 CancelledError 必须写 RAW_ERROR（修复不得吞掉真失败）"
        )
        assert "Pipeline engine cancelled" in state[StateKeys.RAW_ERROR]

    def test_non_user_cancel_fails_task(self) -> None:
        """非用户停止的 CancelledError 仍 fail_task（真崩溃路径不变）。"""
        engine = _make_engine("sem-real-2")
        fail_spy = AsyncMock()
        with patch.object(engine, "_mark_task_failed_on_engine_exit", fail_spy):
            asyncio.run(_drive_run_loop(engine, _min_state("sem-real-2"), set_user_stop=False))

        fail_spy.assert_called_once(), (
            "真崩溃/真取消必须 fail_task——这是超时硬墙、容器销毁等终态路径的依赖"
        )

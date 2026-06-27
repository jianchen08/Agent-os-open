"""管道引擎日志 Handler 生命周期回归测试。

BUG-FIX-fix_20260627_log_missing_after_restart:

问题根因: _setup_pipeline_logging 有防重复守卫(_logging_pipeline_id == pipeline_run_id),
  但 _cleanup_run_loop 关闭移除所有 FileHandler 后未重置该守卫。停止生成只 cancel
  engine_task 不删 entry，register 复用同一 engine 实例；下次发消息走 idle 重启时
  pipeline_id 不变，守卫命中 return，handler 不重建 → 重启后日志不写文件。

本测试验证契约：cleanup 销毁 handler 后，守卫标志必须同步失效，
使同一 engine 重启时 _setup_pipeline_logging 重新创建 handler。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from pipeline.engine import PipelineEngine
from pipeline.types import StateKeys, create_initial_state


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _make_engine(tmp_path: Path) -> PipelineEngine:
    """构造一个仅满足日志配置所需依赖的最小 PipelineEngine。"""
    engine = PipelineEngine(
        input_route_table=MagicMock(),
        output_route_table=MagicMock(),
        plugin_registry=MagicMock(),
    )
    # 让日志文件落到临时目录，避免污染真实 logs/
    engine._pipeline_id = "test_log_abc123"
    return engine


# ---------------------------------------------------------------------------
# 守卫标志生命周期
# ---------------------------------------------------------------------------


class TestLoggingGuardLifecycle:
    """守卫标志 _logging_pipeline_id 必须与 FileHandler 生命周期一致。"""

    def test_guard_set_after_setup(self, tmp_path: Path, monkeypatch) -> None:
        """_setup_pipeline_logging 后守卫标志被置为 pipeline_id。"""
        engine = _make_engine(tmp_path)
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))

        loggers: list[logging.Logger] = []
        engine._setup_pipeline_logging(engine._pipeline_id, resumed=False, pipeline_loggers=loggers)

        assert engine._logging_pipeline_id == engine._pipeline_id

    def test_guard_reset_after_cleanup(self, tmp_path: Path, monkeypatch) -> None:
        """cleanup 销毁 handler 后守卫标志必须重置，否则重启不重建 handler。

        这是 BUG-FIX-fix_20260627_log_missing_after_restart 的回归点。
        """
        engine = _make_engine(tmp_path)
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))

        loggers: list[logging.Logger] = []
        engine._setup_pipeline_logging(engine._pipeline_id, resumed=False, pipeline_loggers=loggers)
        assert engine._logging_pipeline_id == engine._pipeline_id

        # 取出 setup 创建的 FileHandler，供 cleanup 关闭
        handler = engine._get_last_file_handler(loggers)
        assert handler is not None

        # cleanup 内部需要 _current_pipeline_id 的真实 token，且 token 必须在
        # 同一个 asyncio Context 内创建/重置，故 set 放进协程。
        from pipeline.engine_state import _current_pipeline_id

        import asyncio

        async def _run() -> None:
            token = _current_pipeline_id.set(engine._pipeline_id)
            state = create_initial_state(user_input="", agent_config=None)
            state[StateKeys.PIPELINE_ID] = engine._pipeline_id
            from pipeline.registry import get_engine_registry

            # register 一个 entry，cleanup 内部 get 会命中
            get_engine_registry().register(engine._pipeline_id, engine)
            await engine._cleanup_run_loop(state, handler, loggers, token)

        asyncio.run(_run())

        # 核心断言：守卫标志已失效，重启时可重建 handler
        assert engine._logging_pipeline_id is None

    def test_restart_recreates_handler(self, tmp_path: Path, monkeypatch) -> None:
        """同一 engine 首次 setup → cleanup → 再次 setup 必须产生新 FileHandler。

        复现重启复用 engine 场景：停止生成(cancel) → idle 重启(同一 engine.run)。
        修复前第二次 setup 会因守卫命中直接 return，不创建 handler。
        """
        engine = _make_engine(tmp_path)
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))

        # 第一次 run：正常创建 handler
        loggers_1: list[logging.Logger] = []
        engine._setup_pipeline_logging(engine._pipeline_id, resumed=False, pipeline_loggers=loggers_1)
        handler_1 = engine._get_last_file_handler(loggers_1)
        assert handler_1 is not None

        # run 结束：cleanup 销毁 handler 并重置守卫（修复后）
        for h in list(loggers_1[0].handlers):
            if isinstance(h, logging.FileHandler):
                h.close()
                for lg in loggers_1:
                    lg.removeHandler(h)
        engine._logging_pipeline_id = None  # 模拟修复后的 cleanup

        # 第二次 run（重启）：必须创建新的 FileHandler
        loggers_2: list[logging.Logger] = []
        engine._setup_pipeline_logging(engine._pipeline_id, resumed=False, pipeline_loggers=loggers_2)
        handler_2 = engine._get_last_file_handler(loggers_2)
        assert handler_2 is not None, "重启后未重建日志 handler（BUG-FIX-fix_20260627）"
        assert handler_2 is not handler_1, "重启后复用了已关闭的旧 handler"

    def test_restart_appends_same_log_file(self, tmp_path: Path, monkeypatch) -> None:
        """同一 pipeline_id 重启后日志追加到同一文件尾部，不覆盖。

        需求：日志按注册表 ID 归档，只要引擎为该 ID 运行，日志就持续写到
        pipeline_{id}.log 后面。修复前重启用 "w" 模式会清空同 ID 的历史日志。
        """
        engine = _make_engine(tmp_path)
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))

        log_file = tmp_path / "logs" / "pipeline" / f"pipeline_{engine._pipeline_id}.log"

        # _PipelineLogFilter 按 _current_pipeline_id 过滤，必须设上才能落盘
        from pipeline.engine_state import _current_pipeline_id
        token = _current_pipeline_id.set(engine._pipeline_id)

        try:
            # 第一次 run：写一条日志
            loggers_1: list[logging.Logger] = []
            engine._setup_pipeline_logging(engine._pipeline_id, resumed=False, pipeline_loggers=loggers_1)
            lg = logging.getLogger("pipeline.engine")
            lg.info("[TEST] first-run-line")
            for h in lg.handlers:
                h.flush()
            first_size = log_file.stat().st_size
            assert first_size > 0, "首次运行应写入日志"

            # 模拟 cleanup：关闭并移除 handler，重置守卫
            for h in list(lg.handlers):
                if isinstance(h, logging.FileHandler):
                    h.close()
                    lg.removeHandler(h)
            engine._logging_pipeline_id = None

            # 第二次 run（重启，resumed=False）：必须追加而非覆盖
            loggers_2: list[logging.Logger] = []
            engine._setup_pipeline_logging(engine._pipeline_id, resumed=False, pipeline_loggers=loggers_2)
            lg.info("[TEST] second-run-line")
            for h in lg.handlers:
                h.flush()

            content = log_file.read_text(encoding="utf-8")
            # 两次运行的日志都应在同一文件里（追加）
            assert "[TEST] first-run-line" in content, "重启覆盖了首次运行的日志"
            assert "[TEST] second-run-line" in content, "重启后日志未追加"
            assert content.index("[TEST] first-run-line") < content.index("[TEST] second-run-line"), \
                "重启日志未追加到首次日志之后"

            # 清理本次测试新增的 handler，避免污染其它测试
            for h in list(lg.handlers):
                if isinstance(h, logging.FileHandler):
                    h.close()
                    lg.removeHandler(h)
        finally:
            _current_pipeline_id.reset(token)

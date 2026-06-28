"""管道引擎日志 Handler 生命周期回归测试。

BUG-FIX-fix_20260627_log_missing_after_restart:

问题根因: 日志配置有防重复守卫(_logging_pipeline_id == pipeline_run_id),
  但 teardown 关闭移除所有 FileHandler 后未重置该守卫。停止生成只 cancel
  engine_task 不删 entry，register 复用同一 engine 实例；下次发消息走 idle 重启时
  pipeline_id 不变，守卫命中 return，handler 不重建 → 重启后日志不写文件。

日志系统已从 PipelineEngine 拆出为独立模块 pipeline/engine_logging.py
（PipelineLogger 类）。本测试改为针对 PipelineLogger 验证同一组契约：
setup 后守卫置位、teardown 后守卫失效、同 ID 重启追加同一文件不覆盖。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pipeline.engine_logging import PipelineLogger
from pipeline.engine_state import _current_pipeline_id


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _make_logger(tmp_path: Path) -> PipelineLogger:
    """构造一个把日志落到临时目录的 PipelineLogger，避免污染真实 logs/。"""
    return PipelineLogger(log_base=tmp_path / "logs")


# ---------------------------------------------------------------------------
# 守卫标志生命周期
# ---------------------------------------------------------------------------


class TestLoggingGuardLifecycle:
    """守卫标志必须与 FileHandler 生命周期一致。"""

    def test_guard_set_after_setup(self, tmp_path: Path) -> None:
        """setup 后守卫标志被置为 pipeline_id。"""
        pipeline_logger = _make_logger(tmp_path)
        pipeline_logger.setup("test_log_abc123")
        assert pipeline_logger._logging_pipeline_id == "test_log_abc123"

    def test_guard_reset_after_teardown(self, tmp_path: Path) -> None:
        """teardown 销毁 handler 后守卫标志必须重置，否则重启不重建 handler。

        这是 BUG-FIX-fix_20260627_log_missing_after_restart 的回归点。
        """
        pipeline_logger = _make_logger(tmp_path)
        pipeline_logger.setup("test_log_def456")
        assert pipeline_logger._logging_pipeline_id == "test_log_def456"
        assert pipeline_logger.log_handler is not None

        pipeline_logger.teardown()

        # 核心断言：守卫标志已失效，重启时可重建 handler
        assert pipeline_logger._logging_pipeline_id is None
        assert pipeline_logger.log_handler is None

    def test_restart_recreates_handler(self, tmp_path: Path) -> None:
        """首次 setup → teardown → 再次 setup 必须产生新 FileHandler。

        复现重启复用 engine 场景：停止生成(cancel) → idle 重启(同一 engine.run)。
        修复前第二次 setup 会因守卫命中直接 return，不创建 handler。
        """
        pipeline_logger = _make_logger(tmp_path)

        # 第一次 run：正常创建 handler
        pipeline_logger.setup("test_log_restart1")
        handler_1 = pipeline_logger.log_handler
        assert handler_1 is not None

        # run 结束：teardown 销毁 handler 并重置守卫（修复后）
        pipeline_logger.teardown()

        # 第二次 run（重启）：必须创建新的 FileHandler
        pipeline_logger.setup("test_log_restart1")
        handler_2 = pipeline_logger.log_handler
        assert handler_2 is not None, "重启后未重建日志 handler（BUG-FIX-fix_20260627）"
        assert handler_2 is not handler_1, "重启后复用了已关闭的旧 handler"

        pipeline_logger.teardown()

    def test_restart_appends_same_log_file(self, tmp_path: Path) -> None:
        """同一 pipeline_id 重启后日志追加到同一文件尾部，不覆盖。

        需求：日志按注册表 ID 归档，只要引擎为该 ID 运行，日志就持续写到
        pipeline_{id}.log 后面。修复前重启用 "w" 模式会清空同 ID 的历史日志。
        """
        pipeline_id = "test_log_append"
        pipeline_logger = _make_logger(tmp_path)
        log_file = tmp_path / "logs" / "pipeline" / f"pipeline_{pipeline_id}.log"

        # _PipelineLogFilter 按 _current_pipeline_id 过滤，必须设上才能落盘
        token = _current_pipeline_id.set(pipeline_id)

        try:
            # 第一次 run：写一条日志
            pipeline_logger.setup(pipeline_id)
            lg = logging.getLogger("pipeline.engine")
            lg.info("[TEST] first-run-line")
            for h in lg.handlers:
                h.flush()
            first_size = log_file.stat().st_size
            assert first_size > 0, "首次运行应写入日志"

            # 模拟 teardown：关闭移除 handler，重置守卫
            pipeline_logger.teardown()

            # 第二次 run（重启）：必须追加而非覆盖
            pipeline_logger.setup(pipeline_id)
            lg.info("[TEST] second-run-line")
            for h in lg.handlers:
                h.flush()

            content = log_file.read_text(encoding="utf-8")
            # 两次运行的日志都应在同一文件里（追加）
            assert "[TEST] first-run-line" in content, "重启覆盖了首次运行的日志"
            assert "[TEST] second-run-line" in content, "重启后日志未追加"
            assert content.index("[TEST] first-run-line") < content.index("[TEST] second-run-line"), \
                "重启日志未追加到首次日志之后"

            pipeline_logger.teardown()
        finally:
            _current_pipeline_id.reset(token)


class TestContextVarBinding:
    """contextvar 绑定/重置（_current_pipeline_id，供过滤器按 pipeline_id 区分）。"""

    def test_bind_and_reset_context(self, tmp_path: Path) -> None:
        """bind_context 设置 contextvar，reset_context 恢复。"""
        pipeline_logger = _make_logger(tmp_path)
        assert _current_pipeline_id.get() is None

        token = pipeline_logger.bind_context("ctx_pid")
        assert _current_pipeline_id.get() == "ctx_pid"

        pipeline_logger.reset_context(token)
        assert _current_pipeline_id.get() is None

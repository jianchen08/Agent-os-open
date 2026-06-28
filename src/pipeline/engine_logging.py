"""管道引擎的日志基础设施（per-pipeline 文件日志）。

把"为每个管道建独立日志文件"这套基础设施从引擎核心拆出来：引擎只管
"执行循环"，本模块负责按 pipeline_run_id 创建/关闭 FileHandler，并把
contextvar（当前 pipeline_id）的生命周期收口在这里。

为什么单独成模块（架构理由）：
- 日志是【横切基础设施】，与"执行管道"零内聚。FileHandler 创建、目录布局、
  日志器清单、过滤器、contextvar 绑定/重置，都是传输/采集层细节，不属于
  引擎领域核心。混在 engine.py 是基础设施侵入领域层。
- 引擎通过 self._pipeline_logger 委托，依赖方向单向：logger 不反向依赖引擎
  内部状态（model_info 需要的 plugin_registry 作为参数注入，不持有引擎引用）。

职责边界：
- setup(pipeline_run_id)：按目录（logs/pipeline|error|task）建 3 个 FileHandler
  挂到相关 logger，带防重复守卫（同 ID resume 不重建）。
- bind_context / reset_context：_current_pipeline_id contextvar 的 set/reset，
  供 _TaskLogFilter / _PipelineLogFilter 按 pipeline_id 过滤。
- log_handler：取当前管道的最后一个 FileHandler（供调用方判断/透传）。
- model_info(plugin_registry)：打印当前 LLM 模型（plugin_registry 参数注入）。
- teardown()：关闭所有 FileHandler 并从 logger 移除，重置防重复守卫。
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from pathlib import Path
from typing import Any

from pipeline.engine_state import _PipelineLogFilter, _current_pipeline_id

logger = logging.getLogger(__name__)


# 需要挂 per-pipeline FileHandler 的日志器清单（引擎核心 + 插件 + 工具 + LLM 等）。
# 放模块级常量，与引擎类解耦。
_PIPELINE_LOGGERS: tuple[str, ...] = (
    "pipeline.engine", "pipeline.chain", "pipeline.event_bus",
    "pipeline.route", "pipeline.config", "pipeline.registry",
    "pipeline.stream_bridge",
    "plugins.core", "plugins.input", "plugins.output",
    "infrastructure.task_worker", "tasks",
    "tools.builtin", "evaluation",
    "llm.adapter", "llm.adapter._stream",
    "triggers.manager",
    "pipeline.message_bus",
    "src.core.event_bus",
)

# 任务执行日志放行的 logger 前缀（task_submit/manage/evaluate/worker）。
_TASK_LOG_LOGGERS: tuple[str, ...] = (
    "tools.builtin.task_submit",
    "tools.builtin.task_manage",
    "tools.builtin.task_evaluate",
    "infrastructure.task_worker",
    "tasks",
)


class _TaskLogFilter(logging.Filter):
    """只放行任务执行相关的日志（task_submit/task_manage/task_evaluate/worker）。

    按 _current_pipeline_id contextvar 过滤，确保多管道并行时各管道任务日志不串。
    """

    def __init__(self, pipeline_id: str) -> None:
        super().__init__()
        self.pipeline_id = pipeline_id

    def filter(self, record: logging.LogRecord) -> bool:
        if _current_pipeline_id.get() != self.pipeline_id:
            return False
        return any(record.name.startswith(prefix) for prefix in _TASK_LOG_LOGGERS)


def _create_log_handler(
    file_path: str,
    mode: str,
    level: int,
    formatter: logging.Formatter,
    filters: list[Any],
) -> logging.FileHandler:
    """创建配置好的 FileHandler（统一日志 Handler 创建逻辑）。

    Args:
        file_path: 日志文件路径
        mode: 文件打开模式（"w" 覆盖 / "a" 追加）
        level: 日志级别
        formatter: 日志格式化器
        filters: 过滤器列表

    Returns:
        配置完成的 FileHandler 实例
    """
    handler = logging.FileHandler(file_path, encoding="utf-8", mode=mode)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    for f in filters:
        handler.addFilter(f)
    return handler


class PipelineLogger:
    """单个管道的文件日志管理器。

    引擎在 _run_loop 开始时 setup + bind_context，结束时 teardown + reset_context。
    持有当前管道的 logger 列表（供 teardown 关闭 handler），不持有引擎引用。

    Args:
        log_base: 日志根目录（默认 logs/，测试可注入临时目录）。
    """

    def __init__(self, *, log_base: Path | str = "logs") -> None:
        self._log_base = Path(log_base)
        # 防重复守卫：同 pipeline_run_id resume 时不重建 handler（见 setup）。
        self._logging_pipeline_id: str | None = None
        # 当前 setup 挂了 handler 的 logger 列表（teardown 用）。
        self._pipeline_loggers: list[logging.Logger] = []

    # ------------------------------------------------------------------
    # contextvar 绑定（_current_pipeline_id，供过滤器按 pipeline_id 区分）
    # ------------------------------------------------------------------

    def bind_context(self, pipeline_run_id: str) -> contextvars.Token[str | None]:
        """绑定当前 pipeline_id 到 contextvar，返回 token 供 reset_context 用。"""
        return _current_pipeline_id.set(pipeline_run_id)

    def reset_context(self, token: contextvars.Token[str | None]) -> None:
        """重置 contextvar（必须用 bind_context 返回的 token）。"""
        _current_pipeline_id.reset(token)

    # ------------------------------------------------------------------
    # setup：按目录建 3 个 FileHandler 挂到相关 logger
    # ------------------------------------------------------------------

    def setup(self, pipeline_run_id: str, resumed: bool = False) -> None:
        """为当前管道设置独立日志文件，按类型分文件夹存储。

        目录结构：
          logs/pipeline/  — 主日志（DEBUG~INFO，不含 WARNING+）
          logs/error/     — 错误日志（WARNING 及以上）
          logs/task/      — 任务执行日志（task_submit/manage/evaluate/worker）

        防重复守卫：同 pipeline_run_id（resume/停止后重启同 ID）直接 return，
        避免重复挂 handler 导致日志重复写。teardown 会重置守卫。

        Args:
            pipeline_run_id: 管道运行 ID（用作文件名与过滤 key）
            resumed: 预留参数（历史用于决定覆盖/追加，现统一追加，已不影响行为）
        """
        try:
            # 防止 resume 时重复添加 Handler
            if self._logging_pipeline_id == pipeline_run_id:
                return
            self._logging_pipeline_id = pipeline_run_id

            pipeline_dir = self._log_base / "pipeline"
            error_dir = self._log_base / "error"
            task_dir = self._log_base / "task"
            pipeline_dir.mkdir(parents=True, exist_ok=True)
            error_dir.mkdir(parents=True, exist_ok=True)
            task_dir.mkdir(parents=True, exist_ok=True)

            # 文件名按 pipeline_run_id 唯一命名，同引擎无论首次/resume/重启都追加
            # 同一文件（resumed 历史 "w" 覆盖会丢历史日志，已废弃）。
            log_mode = "a"
            log_fmt = logging.Formatter(
                "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                datefmt="%H:%M:%S",
            )
            pipeline_filter = _PipelineLogFilter(pipeline_run_id)

            # 1. 主日志（DEBUG~INFO，排除 WARNING+，避免与 error 重复）
            main_handler = _create_log_handler(
                str(pipeline_dir / f"pipeline_{pipeline_run_id}.log"),
                log_mode, logging.DEBUG, log_fmt,
                [pipeline_filter, lambda r: r.levelno < logging.WARNING],
            )
            # 2. 错误/警告日志（WARNING+，独立文件夹）
            error_handler = _create_log_handler(
                str(error_dir / f"pipeline_{pipeline_run_id}.log"),
                log_mode, logging.WARNING, log_fmt,
                [pipeline_filter],
            )
            # 3. 任务执行日志（独立文件夹）
            task_handler = _create_log_handler(
                str(task_dir / f"pipeline_{pipeline_run_id}.log"),
                log_mode, logging.DEBUG, log_fmt,
                [_TaskLogFilter(pipeline_run_id)],
            )

            # 挂到所有相关 logger，并收集到 _pipeline_loggers 供 teardown
            self._pipeline_loggers = []
            for name in _PIPELINE_LOGGERS:
                lg = logging.getLogger(name)
                if lg.level == logging.NOTSET:
                    lg.setLevel(logging.DEBUG)
                lg.addHandler(main_handler)
                lg.addHandler(error_handler)
                lg.addHandler(task_handler)
                self._pipeline_loggers.append(lg)
        except Exception as exc:
            logger.info("管道日志器配置失败（非致命）: %s", exc)

    # ------------------------------------------------------------------
    # log_handler：取当前管道最后一个 FileHandler
    # ------------------------------------------------------------------

    @property
    def log_handler(self) -> logging.FileHandler | None:
        """当前 setup 挂载的最后一个 FileHandler（调用方判断/透传用）。

        从 _pipeline_loggers 反向遍历找第一个 FileHandler（即最近 setup 挂的）。
        """
        for lg in reversed(self._pipeline_loggers):
            for h in reversed(lg.handlers):
                if isinstance(h, logging.FileHandler):
                    return h
        return None

    # ------------------------------------------------------------------
    # model_info：打印当前 LLM 模型（plugin_registry 参数注入）
    # ------------------------------------------------------------------

    def model_info(self, plugin_registry: Any) -> None:
        """显示当前 LLM 模型信息。

        Args:
            plugin_registry: 引擎的插件注册表，用于取 llm_call 核心插件的模型/provider。
        """
        llm_core = plugin_registry.get_core("llm_call")
        if llm_core and hasattr(llm_core, "_model"):
            model_info = f"{llm_core._model} (provider={llm_core._provider}"
            if getattr(llm_core, "_api_base", None):
                model_info += f", api_base={llm_core._api_base}"
            model_info += ")"
            logger.debug("Model: %s", model_info)

    # ------------------------------------------------------------------
    # teardown：关闭 FileHandler 并从 logger 移除
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        """关闭所有为当前管道创建的 FileHandler 并从 logger 移除，防泄漏。

        BUG-FIX-fix_20260627_log_missing_after_restart:
        handler 全部关闭移除后必须重置守卫标志，否则引擎复用重启时 setup 的防重复
        守卫会误判"已配置"直接 return，不重建 handler → 日志不写文件。
        触发路径：停止生成只 cancel engine_task 不删 entry（复用同一 engine），下次
        发消息走 _start_idle_engine → 同一 engine.run() → pipeline_id 不变 → 守卫命中。
        """
        # 收集去重后的 FileHandler（同一 handler 挂在多个 logger 上）
        handlers_to_close: list[logging.FileHandler] = []
        seen: set[int] = set()
        for lg in self._pipeline_loggers:
            for h in lg.handlers:
                if isinstance(h, logging.FileHandler) and id(h) not in seen:
                    seen.add(id(h))
                    handlers_to_close.append(h)

        for h in handlers_to_close:
            with contextlib.suppress(Exception):
                h.close()
            for lg in self._pipeline_loggers:
                with contextlib.suppress(Exception):
                    lg.removeHandler(h)

        self._pipeline_loggers = []
        # 重置守卫，允许下次 setup 重建（见上方 BUG 注释）
        self._logging_pipeline_id = None

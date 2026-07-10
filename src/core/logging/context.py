"""日志上下文追踪。

通过 ``contextvars`` 在日志中注入 request_id / task_id / session_id 等追踪字段。
线程安全且支持 asyncio。

典型用法::

    from src.core.logging import LogContext

    LogContext.bind(request_id="abc123", task_id="t-001")
    LogContext.bind(session_id="sess-42")
    # 后续所有日志自动携带这些字段
    LogContext.unbind()  # 清除全部
"""

from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager


class LogContext:
    """请求级日志上下文（基于 contextvars，线程安全）。

    所有字段都是可选的；未设置的字段在日志中输出 ``-``。

    追踪字段：
    - request_id / task_id / session_id: 通用请求级追踪
    - trace_id: 请求链路追踪（贯穿 L1→L2→L3 委托链）
    - pipeline_id / thread_id: 管道引擎执行追踪（用于关联一次 pipeline 执行的全部日志）
    - agent_name: 当前执行 Agent 名称
    """

    _vars: dict[str, contextvars.ContextVar[str]] = {
        "request_id": contextvars.ContextVar("log_request_id", default="-"),
        "task_id": contextvars.ContextVar("log_task_id", default="-"),
        "session_id": contextvars.ContextVar("log_session_id", default="-"),
        "trace_id": contextvars.ContextVar("log_trace_id", default="-"),
        "pipeline_id": contextvars.ContextVar("log_pipeline_id", default="-"),
        "thread_id": contextvars.ContextVar("log_thread_id", default="-"),
        "agent_name": contextvars.ContextVar("log_agent_name", default="-"),
    }

    # ── 读取 ──────────────────────────────────────────────

    @classmethod
    def get(cls, key: str) -> str:
        """获取单个上下文字段，不存在返回 ``'-'``。"""
        var = cls._vars.get(key)
        if var is None:
            return "-"
        return var.get()

    @classmethod
    def snapshot(cls) -> dict[str, str]:
        """返回所有追踪字段的当前值快照。"""
        return {name: var.get() for name, var in cls._vars.items()}

    @classmethod
    def format_context(cls) -> str:
        """格式化为日志行中可用的短字符串。

        示例输出: ``rid=abc123 tid=t-001 sid=sess-42``
        """
        parts: list[str] = []
        short = {"request_id": "rid", "task_id": "tid", "session_id": "sid"}
        for name, var in cls._vars.items():
            val = var.get()
            if val != "-":
                label = short.get(name, name)
                parts.append(f"{label}={val}")
        return " ".join(parts) if parts else "-"

    # ── 写入 ──────────────────────────────────────────────

    @classmethod
    def bind(cls, **kwargs: str) -> None:
        """设置上下文字段（覆盖已有值）。"""
        for key, value in kwargs.items():
            var = cls._vars.get(key)
            if var is not None:
                var.set(value)

    @classmethod
    def unbind(cls) -> None:
        """清除全部上下文字段，恢复默认值 ``-``。"""
        for var in cls._vars.values():
            var.set("-")

    # ── 上下文管理器 ──────────────────────────────────────

    @classmethod
    @contextmanager
    def scoped(cls, **kwargs: str) -> Generator[None, None, None]:
        """临时绑定上下文字段，退出时自动恢复。

        ::

            with LogContext.scoped(request_id="abc"):
                # 日志携带 rid=abc
                ...
            # 自动恢复
        """
        saved: dict[str, str] = {}
        for key, var in cls._vars.items():
            saved[key] = var.get()

        cls.bind(**kwargs)
        try:
            yield
        finally:
            for key, var in cls._vars.items():
                var.set(saved[key])

    # ── 注册自定义字段 ────────────────────────────────────

    @classmethod
    def register_field(cls, name: str) -> None:
        """注册自定义追踪字段。

        注意：应在进程启动阶段调用，不宜在请求处理中动态注册。
        """
        if name not in cls._vars:
            cls._vars[name] = contextvars.ContextVar(f"log_{name}", default="-")


__all__ = ["LogContext"]

"""日志上下文过滤器。

``ContextFilter`` 将 ``LogContext`` 中的链路追踪字段注入到每条
``LogRecord``，使得标准 ``logging.Formatter`` 也能通过
``%(request_id)s``、``%(task_id)s`` 等占位符引用这些字段。

用法::

    import logging
    from src.core.logging import ContextFilter

    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s "
        "rid=%(request_id)s tid=%(task_id)s %(message)s"
    ))

与 ``StructuredFormatter`` / ``JsonFormatter`` 的区别：

- Formatter 在 ``format()`` 中直接读取 ``LogContext``
- Filter 在日志分发阶段注入字段，对所有 Formatter 通用
- 二者可共存；Formatter 的优先级更高（会覆盖 Filter 注入的同名字段）
"""

from __future__ import annotations

import logging

from src.core.logging.context import LogContext


class ContextFilter(logging.Filter):
    """将 LogContext 追踪字段注入 LogRecord。

    注入的字段与 ``LogContext`` 管理的 7 个标准字段一致：
    ``request_id``, ``task_id``, ``session_id``, ``trace_id``,
    ``pipeline_id``, ``thread_id``, ``agent_name``。

    注入值就是 ``LogContext.get(key)`` 的当前值（未设置时为 ``'-'``）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """注入上下文字段到 record，始终返回 True（不拦截日志）。

        Args:
            record: 待处理的日志记录。

        Returns:
            始终返回 ``True``，不拦截任何日志。
        """
        for key, value in LogContext.snapshot().items():
            # 仅在 record 尚未有同名属性时注入，
            # 避免覆盖调用方通过 extra= 显式设置的值。
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


__all__ = ["ContextFilter"]

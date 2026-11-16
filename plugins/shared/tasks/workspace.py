"""任务工作空间解析器。

统一的入口：从任务数据（task.metadata.ws_meta）获取实际工作空间路径。
不做 resolve_workspace 链计算，ws_meta 是唯一可信来源。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

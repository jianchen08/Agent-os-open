"""llm.complete_stream 流式协议翻译——re-export shim（真源已迁共享根）。

真源单源 = ``plugins/shared/_llm_streaming.py``（2026-11-04 网关化第二步迁入，
provider 插件契约方同源消费）。本文件保留模块名 ``streaming`` 兼容既有
import 面（llm 内部 + 测试）；出口经共享根显式 API（``_root_api``，E2 闸
唯一合法消费面），不再裸名导入实现模块。
"""

from __future__ import annotations

from _root_api import (  # noqa: F401 —— 兼容 shim 全量转出
    StreamEvent,
    StreamTranslator,
    map_finish_reason,
)

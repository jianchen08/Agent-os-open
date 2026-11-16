# @feature: FP-0.2.可观测性 E-2 trace_ingestion | @ci: python-plugins-test
"""测试装配：trace_ingestion 插件目录 + plugins/shared 根注入 sys.path。

与 server.py 的 bootstrap_plugin 注入保持一致（builtin_tools/tests 先例），
使测试在本地与 CI 上均可收集运行。
"""

import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 平铺布局（ADR 2026-10-13）：插件目录的直接父目录即 plugins/shared 根
_SHARED_ROOT = os.path.dirname(_PLUGIN_DIR)
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

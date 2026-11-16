# @feature: FP-0.2.Fmin 本机 MCP server | @ci: local
"""F-min 本机 MCP server 测试基建：把组件目录挂进 sys.path。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

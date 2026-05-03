"""连接器测试 conftest — 将 src 加入 sys.path。"""

import sys
from pathlib import Path

# 将项目 src 目录加入 Python 搜索路径
_src = str(Path(__file__).resolve().parent.parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

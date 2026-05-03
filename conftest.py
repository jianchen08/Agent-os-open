"""测试公共配置。

将 src 目录添加到 Python 路径，确保所有测试能正确导入项目模块。
"""

import sys
import os

# 将 src 目录添加到 Python 路径
_src_dir = os.path.join(os.path.dirname(__file__), "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

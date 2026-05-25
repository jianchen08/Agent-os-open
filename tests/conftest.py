"""测试公共配置。"""

import sys
import os

# 将 src 目录添加到 Python 路径，确保测试能正确导入项目模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

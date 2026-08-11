"""快速测试生成脚本"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 直接调用 main 函数
import generate_resume

# 测试中文版
generate_resume.main.__wrapped__ if hasattr(generate_resume.main, '__wrapped__') else None
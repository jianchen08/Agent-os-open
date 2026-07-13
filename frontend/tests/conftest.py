"""pytest 配置：为 server.py 测试准备静态文件目录。

server.py 在 import 时挂载 StaticFiles(directory=STATIC_DIR/assets)，
STATIC_DIR 默认是容器内 /app/dist。测试在宿主机跑，需要指向临时目录。
"""
import os
import sys
import tempfile
from pathlib import Path

_FRONTEND_DIR = Path(__file__).resolve().parent.parent

# 在 import server 之前设置环境变量，指向临时 dist 目录
_tmp_dist = Path(tempfile.mkdtemp(prefix="frontend_test_dist_"))
(_tmp_dist / "assets").mkdir(parents=True, exist_ok=True)
(_tmp_dist / "index.html").write_text("<html></html>", encoding="utf-8")
os.environ["FRONTEND_DIST_DIR"] = str(_tmp_dist)

# 让 import server 能找到 frontend 目录
if str(_FRONTEND_DIR) not in sys.path:
    sys.path.insert(0, str(_FRONTEND_DIR))

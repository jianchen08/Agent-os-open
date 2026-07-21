"""
0.2 版本端到端测试 - 共享配置和 Fixture

环境说明:
- Kernel (Rust) 运行在 http://localhost:9100
- 前端 (Vite) 运行在 http://localhost:5290
- Chromium 已安装在 /usr/bin/chromium
"""
import os
import sys
import pytest

# ============================================================
# 路径修复：确保项目根目录在 sys.path 中
# 这样父目录 tests/conftest.py 中的 pytest_sessionstart hook
# 能正确导入 tests.test_utils.report_generator
# ============================================================
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 从 e2e_helpers 导入服务地址配置
from e2e_helpers import KERNEL_URL, FRONTEND_URL, WS_URL, CHROMIUM_BIN


# ============================================================
# pytest Fixture
# ============================================================
@pytest.fixture(scope="session")
def kernel_url():
    """Kernel 服务地址。"""
    return KERNEL_URL


@pytest.fixture(scope="session")
def frontend_url():
    """前端服务地址。"""
    return FRONTEND_URL


@pytest.fixture(scope="session")
def ws_url():
    """WebSocket 服务地址。"""
    return WS_URL


@pytest.fixture(scope="session")
def chromium_bin():
    """Chromium 可执行文件路径。"""
    return CHROMIUM_BIN

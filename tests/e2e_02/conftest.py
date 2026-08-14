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


@pytest.fixture(scope="session")
def auth_token():
    """登录 admin/admin12345 获取 access_token（session 级复用，仅依赖内核）。

    供审批闭环 / 管道 chat / WS 流式等需要登录态的 e2e 测试使用。
    """
    from e2e_helpers import login_admin

    return login_admin()


@pytest.fixture(scope="module")
def cleanup_sessions(auth_token):
    """e2e 数据清理：测试内创建的会话在此注册，teardown 时逐个删除。

    所有会产生数据的 e2e 测试必须把创建的 session 注册进来——
    避免测试残留的会话/消息落到内核 SQLite（本地反复跑会越积越多，
    CI 内存库虽无持久影响，但保持一致卫生习惯）。
    删除是 best-effort：失败只告警不阻塞测试结论。
    """
    from e2e_helpers import delete_session

    created: list[str] = []

    def _register(session_id: str) -> str:
        created.append(session_id)
        return session_id

    yield _register

    for sid in created:
        try:
            delete_session(auth_token, sid)
        except Exception as exc:  # noqa: BLE001 —— teardown 尽力而为
            print(f"[e2e-cleanup] 删除会话失败（忽略）: {sid} | {exc}")

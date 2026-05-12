"""
测试设置工具

提供统一的测试环境设置，减少重复代码
"""

import sys
from pathlib import Path


def setup_test_environment():
    """
    设置测试环境

    自动添加项目根目录到Python路径，并返回配置对象
    """
    # 获取项目根目录
    current_file = Path(__file__).resolve()
    project_root = current_file.parent.parent.parent

    # 添加到Python路径
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # 导入并返回配置
    from src.config.settings import get_settings

    return get_settings()


def setup_test_database():
    """
    设置测试数据库

    Returns:
        数据库会话管理器
    """
    from src.db.connection import get_async_session

    return get_async_session


def create_test_client():
    """
    创建测试客户端

    Returns:
        FastAPI测试客户端
    """
    from fastapi.testclient import TestClient

    from src.api.main import app

    return TestClient(app)


def create_async_test_client():
    """
    创建异步测试客户端

    Returns:
        异步HTTP客户端
    """
    import httpx

    from src.core.constants import Timeout

    settings = setup_test_environment()

    return httpx.AsyncClient(
        base_url=f"http://localhost:{settings.backend_port}",
        timeout=Timeout.API_REQUEST,
    )


class TestEnvironment:
    """测试环境管理器"""

    def __init__(self):
        self.settings = setup_test_environment()
        self._client = None
        self._async_client = None
        self._db_session = None

    @property
    def client(self):
        """获取同步测试客户端"""
        if self._client is None:
            self._client = create_test_client()
        return self._client

    @property
    def async_client(self):
        """获取异步测试客户端"""
        if self._async_client is None:
            self._async_client = create_async_test_client()
        return self._async_client

    async def get_db_session(self):
        """获取数据库会话"""
        if self._db_session is None:
            db_generator = setup_test_database()
            self._db_session = await db_generator.__anext__()
        return self._db_session

    async def cleanup(self):
        """清理资源"""
        if self._async_client:
            await self._async_client.aclose()

        if self._db_session:
            await self._db_session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()


# 便捷函数
def get_test_settings():
    """获取测试配置"""
    return setup_test_environment()


def get_test_urls():
    """获取测试URL"""
    settings = get_test_settings()
    return {
        "backend": f"http://localhost:{settings.backend_port}",
        "frontend": f"http://localhost:{settings.frontend_port}",
        "api_base": f"http://localhost:{settings.backend_port}/api/v1",
        "websocket": f"ws://localhost:{settings.backend_port}/ws",
        "sse": f"http://localhost:{settings.backend_port}/api/v1/sse",
    }


def print_test_info(test_name: str):
    """打印测试信息"""
    print(f"\n{'=' * 60}")
    print(f"🧪 开始测试: {test_name}")
    print(f"{'=' * 60}")

    settings = get_test_settings()
    urls = get_test_urls()

    print("📊 测试配置:")
    print(f"  - 后端端口: {settings.backend_port}")
    print(f"  - 前端端口: {settings.frontend_port}")
    print(f"  - 数据库: {settings.database_url}")
    print(f"  - API地址: {urls['api_base']}")
    print(f"{'=' * 60}\n")


def print_test_result(test_name: str, success: bool, message: str = ""):
    """打印测试结果"""
    status = "[成功] 通过" if success else "[错误] 失败"
    print(f"\n[测试] 测试结果: {test_name} - {status}")
    if message:
        print(f"[详情] {message}")
    print(f"{'=' * 60}\n")

"""
端口配置一致性测试 — 验证端口冲突修复后各配置文件端口正确且一致

变更背景：
  三个端口统一 +100 偏移以避免冲突：
    后端    : 8888 → 8988
    前端(宿主机): 5189 → 5289（容器内部 5188 不变）
    Redis(宿主机): 6380 → 6480（容器内部 6379 不变）

验证范围：
  1. docker-compose.yml 端口映射正确
  2. docker-compose.yml 中 BACKEND_URL / BACKEND_WS_URL 端口为 8988
  3. start_web_cn.bat 中引用的端口正确
  4. config/system/api_config.yaml 中 base_url 端口为 8988
  5. 容器内部端口未被修改（5188 / 6379）
  6. 旧端口不再作为服务端口出现
"""
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 常量：新端口 / 旧端口 / 容器内部端口
# ---------------------------------------------------------------------------
NEW_BACKEND_PORT = "8988"
NEW_FRONTEND_HOST_PORT = "5289"
NEW_REDIS_HOST_PORT = "6480"

OLD_BACKEND_PORT = "8888"
OLD_FRONTEND_HOST_PORT = "5189"
OLD_REDIS_HOST_PORT = "6380"

CONTAINER_FRONTEND_PORT = "5188"
CONTAINER_REDIS_PORT = "6379"

# 项目根目录（tests/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _read_file(rel_path: str) -> str:
    """读取项目根目录下指定相对路径的文件全文。"""
    full = PROJECT_ROOT / rel_path
    assert full.exists(), f"配置文件不存在: {full}"
    return full.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. docker-compose.yml — 端口映射
# ---------------------------------------------------------------------------
class TestDockerComposePortMapping:
    """验证 docker-compose.yml 中的端口映射配置"""

    @pytest.fixture
    def content(self):
        return _read_file("docker-compose.yml")

    def test_frontend_port_mapping(self, content):
        """前端端口映射应为 5289:5188（宿主机:容器）"""
        assert '"5289:5188"' in content, "前端端口映射应为 5289:5188"

    def test_redis_port_mapping(self, content):
        """Redis 端口映射应为 6480:6379（宿主机:容器）"""
        assert '"6480:6379"' in content, "Redis 端口映射应为 6480:6379"

    def test_no_old_frontend_host_port_in_mapping(self, content):
        """旧的前端宿主机端口 5189 不应出现在端口映射中"""
        assert '"5189:' not in content, "旧的 5189: 端口映射仍然存在"

    def test_no_old_redis_host_port_in_mapping(self, content):
        """旧的 Redis 宿主机端口 6380 不应出现在端口映射中"""
        assert '"6380:' not in content, "旧的 6380: 端口映射仍然存在"


# ---------------------------------------------------------------------------
# 2. docker-compose.yml — BACKEND_URL / BACKEND_WS_URL
# ---------------------------------------------------------------------------
class TestDockerComposeBackendUrl:
    """验证 docker-compose.yml 中后端 URL 的端口"""

    @pytest.fixture
    def content(self):
        return _read_file("docker-compose.yml")

    def test_backend_url_port(self, content):
        """BACKEND_URL 应使用端口 8988"""
        # compose 中实际形式为 BACKEND_URL=http://${BACKEND_HOST_IP:-host.docker.internal}:8988
        assert re.search(r"BACKEND_URL=http://\$\{BACKEND_HOST_IP:-host\.docker\.internal\}:8988", content), (
            "BACKEND_URL 应指向后端端口 8988"
        )

    def test_backend_ws_url_port(self, content):
        """BACKEND_WS_URL 应使用端口 8988"""
        assert re.search(r"BACKEND_WS_URL=ws://\$\{BACKEND_HOST_IP:-host\.docker\.internal\}:8988", content), (
            "BACKEND_WS_URL 应指向后端端口 8988"
        )

    def test_no_old_backend_port_in_urls(self, content):
        """旧后端端口 8888 不应出现在 URL 配置中"""
        assert ":8888" not in content, "旧的 8888 后端端口仍存在于 docker-compose.yml"


# ---------------------------------------------------------------------------
# 3. start_web_cn.bat — 端口引用
# ---------------------------------------------------------------------------
class TestStartWebCnBat:
    """验证 start_web_cn.bat 中引用的端口"""

    @pytest.fixture
    def content(self):
        return _read_file("start_web_cn.bat")

    def test_redis_url_port(self, content):
        """REDIS_URL 应使用端口 6480"""
        assert "REDIS_URL=redis://localhost:6480" in content, (
            "REDIS_URL 应使用端口 6480"
        )

    def test_display_backend_url(self, content):
        """启动完成提示中的后端 URL 应使用端口 8988"""
        # 脚本实际用 127.0.0.1（避免 IPv6 慢回退），非 localhost
        assert "http://127.0.0.1:8988" in content, (
            "启动脚本中后端 URL 应使用端口 8988"
        )

    def test_display_frontend_url(self, content):
        """启动完成提示中的前端 URL 应使用端口 5289"""
        assert "http://127.0.0.1:5289" in content, (
            "启动脚本中前端 URL 应使用端口 5289"
        )

    def test_no_old_redis_port(self, content):
        """旧 Redis 端口 6380 不应出现在启动脚本中"""
        assert "localhost:6380" not in content, "旧的 6380 Redis 端口仍存在于启动脚本"

    def test_no_old_backend_port(self, content):
        """旧后端端口 8888 不应出现在启动脚本中"""
        assert "localhost:8888" not in content, "旧的 8888 后端端口仍存在于启动脚本"

    def test_no_old_frontend_port(self, content):
        """旧前端端口 5189 不应出现在启动脚本中"""
        assert "localhost:5189" not in content, "旧的 5189 前端端口仍存在于启动脚本"


# ---------------------------------------------------------------------------
# 4. config/system/api_config.yaml — base_url 端口
# ---------------------------------------------------------------------------
class TestApiConfig:
    """验证 api_config.yaml 中 base_url 的端口"""

    @pytest.fixture
    def content(self):
        return _read_file("config/system/api_config.yaml")

    def test_base_url_port(self, content):
        """base_url 应使用端口 8988"""
        assert "base_url: http://localhost:8988" in content, (
            "api_config.yaml 中 base_url 应使用端口 8988"
        )

    def test_no_old_backend_port(self, content):
        """旧后端端口 8888 不应出现在 api_config.yaml 中"""
        assert "8888" not in content, "旧的 8888 端口仍存在于 api_config.yaml"


# ---------------------------------------------------------------------------
# 5. 容器内部端口未被修改
# ---------------------------------------------------------------------------
class TestContainerInternalPorts:
    """验证 Docker 容器内部端口保持不变"""

    @pytest.fixture
    def content(self):
        return _read_file("docker-compose.yml")

    def test_frontend_container_port_unchanged(self, content):
        """前端容器内部端口仍为 5188"""
        # 端口映射中的容器端口
        assert '"5289:5188"' in content, "前端端口映射中容器端口应为 5188"
        # healthcheck 中的 localhost 端口（容器内部访问）
        assert "localhost:5188" in content, "healthcheck 中前端容器端口应为 5188"

    def test_redis_container_port_unchanged(self, content):
        """Redis 容器内部端口仍为 6379"""
        assert '"6480:6379"' in content, "Redis 端口映射中容器端口应为 6379"


# ---------------------------------------------------------------------------
# 5.5 frontend/.env.example — 前端环境变量端口
# ---------------------------------------------------------------------------
class TestFrontendEnvExample:
    """验证 frontend/.env.example 中前端连接后端的端口正确"""

    @pytest.fixture
    def content(self):
        return _read_file("frontend/.env.example")

    def test_api_base_url_port(self, content):
        """VITE_API_BASE_URL 应使用端口 8988"""
        assert "VITE_API_BASE_URL=http://127.0.0.1:8988" in content, (
            ".env.example 中 VITE_API_BASE_URL 应使用后端端口 8988"
        )

    def test_ws_base_url_port(self, content):
        """VITE_WS_BASE_URL 应使用端口 8988"""
        assert "VITE_WS_BASE_URL=ws://127.0.0.1:8988" in content, (
            ".env.example 中 VITE_WS_BASE_URL 应使用后端端口 8988"
        )

    def test_no_old_backend_port(self, content):
        """旧后端端口 8888 不应作为服务端口出现在 .env.example 中"""
        # 用带上下文的精确匹配（:8888），避免裸 8888 子串误报
        assert ":8888" not in content, ".env.example 中仍残留旧后端端口 8888"


# ---------------------------------------------------------------------------
# 6. 旧端口在关键配置文件中不再作为服务端口出现
# ---------------------------------------------------------------------------
class TestNoOldPortsInKeyConfigs:
    """验证旧端口（8888/5189/6380）不再作为服务端口出现在关键配置文件中"""

    @pytest.mark.parametrize("rel_path", [
        "docker-compose.yml",
        "start_web_cn.bat",
        "config/system/api_config.yaml",
    ])
    def test_no_old_backend_port_as_service(self, rel_path):
        """旧后端端口 8888 不应作为服务端口出现在关键配置中"""
        content = _read_file(rel_path)
        # 检查 8888 出现在端口上下文中（:8888 或 =8888 或 localhost:8888）
        port_patterns = [":8888", "8888", "port.*8888"]
        matches = [p for p in port_patterns if p in content]
        assert not matches, (
            f"{rel_path} 中仍然包含旧后端端口 8888（匹配模式: {matches}）"
        )

    @pytest.mark.parametrize("rel_path", [
        "docker-compose.yml",
        "start_web_cn.bat",
        "frontend/.env.example",
    ])
    def test_no_old_frontend_host_port(self, rel_path):
        """旧前端宿主机端口 5189 不应出现在关键配置中"""
        content = _read_file(rel_path)
        assert "5189" not in content, (
            f"{rel_path} 中仍然包含旧前端宿主机端口 5189"
        )

    @pytest.mark.parametrize("rel_path", [
        "docker-compose.yml",
        "start_web_cn.bat",
    ])
    def test_no_old_redis_host_port(self, rel_path):
        """旧 Redis 宿主机端口 6380 不应出现在关键配置中"""
        content = _read_file(rel_path)
        assert "6380" not in content, (
            f"{rel_path} 中仍然包含旧 Redis 宿主机端口 6380"
        )

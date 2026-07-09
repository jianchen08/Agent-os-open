"""
启动脚本与配置的环境适配测试

变更背景：
  审查发现启动脚本/配置存在设备/环境耦合，会导致换机器/换目录/换 OS 时失效：
    1. docker-compose.yml / start_web_cn.bat / wsl_ensure_containers.sh 三处
       硬编码容器名后缀 -22404（来自目录名前 5 位 hex），换部署目录即三处不一致
    2. wsl_ensure_containers.sh 裸跑默认路径写死 /mnt/d/myproject/container_224042d3b925
    3. .env（含真实凭据）需确保被 .gitignore 忽略

修复策略：
  - 容器名/网络/卷去掉 -22404 后缀，改由 COMPOSE_PROJECT_NAME 参数化（默认 agentos）
  - wsl 脚本裸跑时用 wslpath 动态推导项目目录
  - 确保 .gitignore 持续忽略 .env

验证范围：
  1. 三脚本均不含 22404 字面量（硬编码后缀已清除）
  2. 三脚本容器名引用一致（无后缀统一命名）
  3. docker-compose.yml 顶部有 name 字段并引用 COMPOSE_PROJECT_NAME（多实例可参数化）
  4. compose 容器名/网络/卷声明一致
  5. wsl 脚本用 wslpath 动态推导项目目录（不写死挂载路径）
  6. .gitignore 持续忽略 .env（防凭据入库回归）
"""
from pathlib import Path

import pytest

# 项目根目录（tests/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_file(rel_path: str) -> str:
    """读取项目根目录下指定相对路径的文件全文。"""
    full = PROJECT_ROOT / rel_path
    assert full.exists(), f"配置文件不存在: {full}"
    return full.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 硬编码后缀 -22404 已从三处清除
# ---------------------------------------------------------------------------
class TestNoHardcodedDirSuffix:
    """验证目录哈希后缀 22404 不再硬编码到任何启动脚本/配置中。

    22404 是目录名 container_224042d3b925 的前 5 位 hex，硬编码会导致
    换部署目录后 compose 生成的容器名与脚本探测的名字对不上。
    """

    @pytest.mark.parametrize("rel_path", [
        "docker-compose.yml",
        "start_web_cn.bat",
        "wsl_ensure_containers.sh",
    ])
    def test_no_22404_literal(self, rel_path):
        """三脚本均不应含 22404 字面量"""
        content = _read_file(rel_path)
        assert "22404" not in content, (
            f"{rel_path} 仍含硬编码目录哈希后缀 22404，换部署目录会失效"
        )


# ---------------------------------------------------------------------------
# 2. 容器名引用跨脚本一致（无后缀统一命名）
# ---------------------------------------------------------------------------
class TestContainerNameConsistency:
    """验证 redis/frontend 容器名在 compose 与探测脚本间一致。"""

    @pytest.fixture
    def compose(self):
        return _read_file("docker-compose.yml")

    @pytest.fixture
    def bat(self):
        return _read_file("start_web_cn.bat")

    @pytest.fixture
    def wsl_sh(self):
        return _read_file("wsl_ensure_containers.sh")

    def test_compose_redis_container_name(self, compose):
        """compose 中 redis 容器名为 agent-os-redis（无后缀）"""
        assert "container_name: agent-os-redis\n" in compose

    def test_compose_frontend_container_name(self, compose):
        """compose 中 frontend 容器名为 agent-os-frontend（无后缀）"""
        assert "container_name: agent-os-frontend\n" in compose

    def test_bat_references_unsuffixed_names(self, bat):
        """bat 探测/启动用无后缀容器名，与 compose 一致"""
        assert "agent-os-redis" in bat
        assert "agent-os-frontend" in bat

    def test_wsl_references_unsuffixed_names(self, wsl_sh):
        """wsl 脚本状态轮询用无后缀容器名，与 compose 一致"""
        assert "agent-os-redis" in wsl_sh
        assert "agent-os-frontend" in wsl_sh


# ---------------------------------------------------------------------------
# 3. compose project name 参数化（多实例隔离）
# ---------------------------------------------------------------------------
class TestComposeProjectName:
    """验证 docker-compose.yml 通过 COMPOSE_PROJECT_NAME 支持多实例隔离。"""

    @pytest.fixture
    def content(self):
        return _read_file("docker-compose.yml")

    def test_has_name_field(self, content):
        """compose 顶部应有 name 字段（显式声明 project name）"""
        assert "name:" in content, "docker-compose.yml 缺少 name 字段"

    def test_name_uses_env_with_default(self, content):
        """name 字段应引用 COMPOSE_PROJECT_NAME 环境变量并带默认值"""
        assert "COMPOSE_PROJECT_NAME" in content, (
            "name 字段未引用 COMPOSE_PROJECT_NAME，无法多实例隔离"
        )
        # 形如 name: ${COMPOSE_PROJECT_NAME:-agentos}
        assert "${COMPOSE_PROJECT_NAME:-" in content, (
            "name 字段缺少默认值兜底（:- 语法），单实例无法开箱即用"
        )

    def test_network_and_volume_unsuffixed(self, content):
        """网络名 agent-net、卷名 redis-data 无目录哈希后缀"""
        assert "agent-net" in content
        assert "redis-data" in content
        assert "agent-net-22404" not in content
        assert "redis-data-22404" not in content


# ---------------------------------------------------------------------------
# 4. wsl 脚本裸跑路径动态推导（不写死挂载点）
# ---------------------------------------------------------------------------
class TestWslProjectDirDynamic:
    """验证 wsl_ensure_containers.sh 不写死 /mnt/d/myproject/... 默认路径。

    裸跑脚本（未经 start_web_cn.bat 传参）时应能适配任意部署目录。
    """

    @pytest.fixture
    def content(self):
        return _read_file("wsl_ensure_containers.sh")

    def test_uses_wslpath(self, content):
        """脚本应使用 wslpath 动态推导当前目录的 WSL 路径"""
        assert "wslpath" in content, (
            "wsl 脚本未用 wslpath 动态推导项目目录，仍依赖写死路径"
        )

    def test_no_hardcoded_project_path(self, content):
        """不应写死 container_224042d3b925 目录名"""
        assert "container_224042d3b925" not in content, (
            "wsl 脚本仍写死 container_224042d3b925 目录名，换目录即失效"
        )


# ---------------------------------------------------------------------------
# 5. .gitignore 持续忽略 .env（防凭据入库回归）
# ---------------------------------------------------------------------------
class TestEnvIgnored:
    """验证 .env 被 .gitignore 忽略，防止含真实凭据的 .env 被误提交。"""

    def test_env_in_gitignore(self):
        """.gitignore 应含 .env 规则"""
        content = _read_file(".gitignore")
        # 逐行检查，避免 .env.local / .env.example 等变体混淆判断
        lines = [ln.strip() for ln in content.splitlines()]
        assert ".env" in lines, ".gitignore 缺少 .env 忽略规则"

"""
启动脚本与配置的环境适配测试

变更背景：
  审查发现启动脚本/配置存在设备/环境耦合，会导致换机器/换目录/换 OS 时失效：
    1. docker-compose.yml / start_web_cn.bat / wsl_ensure_containers.sh 三处
       硬编码容器名后缀 -22404（来自目录名前 5 位 hex），换部署目录即三处不一致
    2. docker-compose.yml 的 name 字段 + 显式 container_name 把 compose project
       锁死成固定字面量，导致 compose "看不见"既有容器，下次 up 重建→端口冲突
    3. wsl_ensure_containers.sh 裸跑默认路径写死 /mnt/d/myproject/container_224042d3b925

修复策略（回归 compose 标准命名）：
  - 移除 docker-compose.yml 的 name 字段和显式 container_name，让 compose 用标准
    {project}-{service} 命名；project 默认取自目录名，不同目录天然隔离
  - 脚本（bat / wsl_ensure_containers.sh）改用 `docker compose ps -q <service>`
    动态获取容器，不依赖固定容器名（update_frontend.ps1 已验证的范式）
  - wsl 脚本裸跑时用 wslpath 动态推导项目目录

验证范围：
  1. 三脚本均不含 22404 字面量（硬编码后缀已清除）
  2. compose 不设 name/container_name（让 project 跟随目录）
  3. 脚本用 docker compose ps -q 动态获取容器（不写死容器名）
  4. wsl 脚本用 wslpath 动态推导项目目录（不写死挂载路径）
  5. .gitignore 持续忽略 .env（防凭据入库回归）
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
# 2. compose 回归标准命名（project 跟随目录）
# ---------------------------------------------------------------------------
class TestComposeStandardNaming:
    """验证 docker-compose.yml 让 compose 用标准 {project}-{service} 命名。

    不设 name 字段和 container_name，project name 默认取自所在目录名，
    这样既有容器能被 compose 识别，不同目录的实例天然隔离。
    """

    @pytest.fixture
    def content(self):
        return _read_file("docker-compose.yml")

    def test_no_explicit_name_field(self, content):
        """不应有顶层 name 字段（否则 project name 被锁死成字面量）"""
        # 逐行检查，避免误命中注释里的 "name:" 或 service 内字段
        for line in content.splitlines():
            stripped = line.lstrip()
            # 顶层 name: 字段（无缩进），非注释
            if stripped.startswith("name:") and not line.startswith(" "):
                pytest.fail(f"docker-compose.yml 含顶层 name 字段，会锁死 project name: {line}")

    def test_no_container_name(self, content):
        """不应有显式 container_name（否则多实例容器名冲突）"""
        assert "container_name:" not in content, (
            "docker-compose.yml 含 container_name，会锁死容器名导致多实例冲突"
        )

    def test_network_and_volume_unsuffixed(self, content):
        """网络名 agent-net、卷名 redis-data 无目录哈希后缀"""
        assert "agent-net" in content
        assert "redis-data" in content
        assert "agent-net-22404" not in content
        assert "redis-data-22404" not in content


# ---------------------------------------------------------------------------
# 3. 脚本动态获取容器（不写死容器名）
# ---------------------------------------------------------------------------
class TestDynamicContainerLookup:
    """验证 bat / wsl 脚本用 docker compose ps -q 动态获取容器，不依赖固定名。"""

    @pytest.fixture
    def bat(self):
        return _read_file("start_web_cn.bat")

    @pytest.fixture
    def wsl_sh(self):
        return _read_file("wsl_ensure_containers.sh")

    def test_bat_uses_compose_ps(self, bat):
        """bat 用 docker compose ps -q <service> 获取容器 ID"""
        assert "docker compose ps -q" in bat, (
            "bat 应使用 docker compose ps -q 动态获取容器，而非写死容器名"
        )

    def test_bat_no_hardcoded_container_start(self, bat):
        """bat 不应写死 docker start agent-os-frontend/redis 容器名"""
        assert "docker start agent-os-frontend" not in bat, (
            "bat 仍写死 docker start agent-os-frontend，换目录会失配"
        )
        assert "docker start agent-os-redis" not in bat, (
            "bat 仍写死 docker start agent-os-redis，换目录会失配"
        )

    def test_wsl_uses_compose_ps(self, wsl_sh):
        """wsl 脚本用 docker compose ps -q <service> 获取容器 ID"""
        assert "docker compose ps -q" in wsl_sh, (
            "wsl 脚本应使用 docker compose ps -q 动态获取容器，而非写死容器名"
        )

    def test_wsl_no_hardcoded_name_filter(self, wsl_sh):
        """wsl 脚本不应写死 --filter name=agent-os-* 容器名"""
        assert "name=agent-os-frontend" not in wsl_sh, (
            "wsl 脚本仍写死 name=agent-os-frontend 过滤，换目录会失配"
        )
        assert "name=agent-os-redis" not in wsl_sh, (
            "wsl 脚本仍写死 name=agent-os-redis 过滤，换目录会失配"
        )


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
# 4.5 安装脚本不写死项目路径 / 容器名前缀
# ---------------------------------------------------------------------------
class TestInstallScriptsPortability:
    """验证安装脚本可跨设备移植，不写死项目绝对路径或旧容器名前缀。"""

    def test_install_wsl_docker_no_hardcoded_path(self):
        """install_wsl_docker.sh 不应写死 /mnt/d/myproject/container_... 路径"""
        content = _read_file("install_wsl_docker.sh")
        assert "/mnt/d/myproject/container_" not in content, (
            "install_wsl_docker.sh 仍写死项目挂载路径，换部署目录挂载测试必失败"
        )
        # 挂载测试应改为动态推导脚本所在目录
        assert "dirname" in content or "pwd" in content, (
            "install_wsl_docker.sh 挂载测试应动态推导项目路径（dirname/pwd）"
        )

    def test_install_sh_uses_compose_for_redis_check(self):
        """install.sh 健康检查应用 docker compose ps 查 service，而非旧容器名前缀"""
        content = _read_file("install.sh")
        # 旧写法 grep agent-os-redis 与 compose 标准命名（{project}-redis）不匹配
        assert "grep -q agent-os-redis" not in content, (
            "install.sh 仍用 grep agent-os-redis 检查容器，与 compose 标准命名不匹配"
        )
        assert "docker compose ps -q redis" in content, (
            "install.sh 应改用 docker compose ps -q redis 检查 service"
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

"""
容器编排与生命周期管理

从 start_web_cn.bat 提取的容器编排逻辑，
通过 wsl_ensure_containers.sh 委托执行容器创建和状态检查。

暴露接口：
- ensure_containers: 确保项目容器就绪（前端/Redis）
- get_container_status: 获取容器运行状态
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# WSL 发行版名称
_WSL_DISTRO = os.environ.get("WSL_DISTRO", "Ubuntu")
# 容器编排超时（秒）
_ORCHESTRATE_TIMEOUT = 240


@dataclass
class ContainerStatus:
    """容器状态"""

    name: str
    """容器名称"""
    running: bool
    """是否在运行"""
    healthy: bool
    """是否健康"""
    image: str
    """镜像名称"""


def ensure_containers(
    wsl_dir: str,
    frontend_port: str,
    redis_port: str,
    backend_port: str,
) -> tuple[bool, str]:
    """确保项目容器就绪。

    通过 wsl_ensure_containers.sh 执行真实状态检查和容器创建。
    对标 start_web_cn.bat 第 138-188 行的容器编排逻辑。

    Args:
        wsl_dir: WSL 内项目目录路径
        frontend_port: 前端端口
        redis_port: Redis 端口
        backend_port: 后端端口

    Returns:
        (成功与否, 消息描述)
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ensure_script = os.path.join(script_dir, "scripts", "wsl_ensure_containers.sh")

    if not os.path.exists(ensure_script):
        logger.warning("[container_orchestrator] wsl_ensure_containers.sh not found at %s", ensure_script)
        return False, "ensure_containers script not found"

    try:
        result = subprocess.run(
            [
                "wsl",
                "-d",
                _WSL_DISTRO,
                "-u",
                "root",
                "--",
                "bash",
                "-c",
                f"export FRONTEND_HOST_PORT={frontend_port} "
                f"REDIS_HOST_PORT={redis_port} "
                f"BACKEND_PORT={backend_port}; "
                f"timeout {_ORCHESTRATE_TIMEOUT} {ensure_script} {wsl_dir}",
            ],
            capture_output=True,
            text=True,
                check=False,
        )
        rc = result.returncode
        if rc == 0:
            return True, "Containers started"
        if rc == 7:
            return False, "cgroup_stuck (container cleanup/start blocked by cgroup/task residue)"
        stderr_snippet = result.stderr[:500] if result.stderr else ""
        return False, f"container start failed (rc={rc}): {stderr_snippet}"
    except subprocess.TimeoutExpired:
        return False, "container orchestration timeout"
    except Exception as e:
        logger.warning("[container_orchestrator] ensure_containers exception: %s", e)
        return False, str(e)


def get_container_status(wsl_ip: str | None = None) -> list[ContainerStatus]:
    """获取当前运行的容器状态。

    通过 docker ps 检查容器运行情况。

    Args:
        wsl_ip: WSL IP 地址（用于设置 DOCKER_HOST）。None 表示使用本地 Docker。

    Returns:
        容器状态列表
    """
    env = os.environ.copy()
    if wsl_ip:
        env["DOCKER_HOST"] = f"tcp://{wsl_ip}:2375"

    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--format",
                "{{.Names}}\t{{.Image}}\t{{.Status}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
                check=False,
        )
        statuses: list[ContainerStatus] = []
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) >= 3:
                    name, image, status_str = parts[0], parts[1], parts[2]
                    statuses.append(
                        ContainerStatus(
                            name=name,
                            running="Up" in status_str,
                            healthy="healthy" in status_str.lower(),
                            image=image,
                        )
                    )
        return statuses
    except Exception as e:
        logger.warning("[container_orchestrator] get_container_status exception: %s", e)
        return []


def pull_image_with_fallback(image: str, wsl_ip: str | None = None) -> bool:
    """拉取 Docker 镜像，带 daocloud 镜像回退。

    对标 start_web_cn.bat 的 pull_image_with_fallback 逻辑。
    Docker Hub 拉取失败时尝试 daocloud 镜像源。

    Args:
        image: 镜像名称（如 redis:7-alpine）
        wsl_ip: WSL IP 地址（用于设置 DOCKER_HOST）。None 表示使用本地 Docker。

    Returns:
        True 表示镜像可用（本地已有或拉取成功）
    """
    env = os.environ.copy()
    if wsl_ip:
        env["DOCKER_HOST"] = f"tcp://{wsl_ip}:2375"

    # 检查本地是否已有
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=15,
                check=False,
        )
        if result.returncode == 0:
            logger.info("[container_orchestrator] Image exists locally: %s", image)
            return True
    except Exception:
        pass

    # 尝试从 Docker Hub 拉取
    try:
        result = subprocess.run(
            ["docker", "pull", image],
            capture_output=True,
            timeout=120,
                check=False,
        )
        if result.returncode == 0:
            logger.info("[container_orchestrator] Pull success from Docker Hub: %s", image)
            return True
    except Exception:
        pass

    # 尝试 daocloud 镜像
    daocloud_image = f"docker.m.daocloud.io/library/{image}"
    try:
        result = subprocess.run(
            ["docker", "pull", daocloud_image],
            capture_output=True,
            timeout=120,
                check=False,
        )
        if result.returncode == 0:
            # 重命名为原始名称
            subprocess.run(
                ["docker", "tag", daocloud_image, image],
                capture_output=True,
                timeout=15,
                    check=False,
            )
            logger.info("[container_orchestrator] Pull success from daocloud: %s", image)
            return True
    except Exception:
        pass

    logger.warning("[container_orchestrator] Image pull failed (Docker Hub + daocloud): %s", image)
    return False

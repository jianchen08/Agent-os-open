"""
WSL 健康探测与 Docker daemon 管理

从 start_web_cn.bat 提取的 WSL/Docker 基础设施管理逻辑，
提供可编程调用的接口而非嵌入在启动脚本中。

暴露接口：
- probe_wsl_alive: WSL 存活探测（对标 wsl_alive_probe.ps1）
- check_wsl_kernel_health: WSL 内核健康检查（对标 wsl_health_probe.sh）
- ensure_dockerd: 确保 dockerd 运行（对标 wsl_start_daemon.sh）
- setup_port_forward: 设置 netsh portproxy 端口转发
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# WSL 发行版名称
_WSL_DISTRO = os.environ.get("WSL_DISTRO", "Ubuntu")
# 探测超时（秒）
_PROBE_TIMEOUT = 20
# daemon 启动超时（秒）
_DAEMON_TIMEOUT = 150


@dataclass
class ProbeResult:
    """探测结果"""

    rc: int
    """返回码: 0=正常, 124=超时/死锁, 2=磁盘丢失, 其他=异常"""
    message: str
    """结果描述"""


def probe_wsl_alive(timeout: int = _PROBE_TIMEOUT) -> ProbeResult:
    """WSL 存活探测。

    通过 wsl_alive_probe.ps1 执行带超时的存活检测，
    判断 WSL 是否处于响应状态。

    Args:
        timeout: 探测超时秒数

    Returns:
        ProbeResult: rc=0 表示 WSL 正常响应
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    probe_script = os.path.join(script_dir, "scripts", "wsl_alive_probe.ps1")

    if not os.path.exists(probe_script):
        logger.warning("[wsl_health] wsl_alive_probe.ps1 not found at %s", probe_script)
        return ProbeResult(rc=1, message="probe script not found")

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                probe_script,
                "-Timeout",
                str(timeout),
            ],
            capture_output=True,
            text=True,
                check=False,
        )
        rc = result.returncode
        if rc == 0:
            return ProbeResult(rc=0, message="WSL responding OK")
        if rc == 124:
            return ProbeResult(rc=124, message="WSL probe timeout (kernel deadlock?)")
        if rc == 2:
            return ProbeResult(rc=2, message="WSL disk lost")
        return ProbeResult(rc=rc, message=f"WSL unavailable rc={rc}")
    except subprocess.TimeoutExpired:
        return ProbeResult(rc=124, message="WSL probe timeout (external)")
    except Exception as e:
        logger.warning("[wsl_health] probe_wsl_alive exception: %s", e)
        return ProbeResult(rc=1, message=str(e))


def check_wsl_kernel_health(wsl_dir: str) -> ProbeResult:
    """WSL 内核健康检查。

    通过 wsl_health_probe.sh 检测 D-state 死锁污染。

    Args:
        wsl_dir: WSL 内项目目录路径

    Returns:
        ProbeResult: rc=0 表示健康, rc=8 表示内核污染
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    health_script = os.path.join(script_dir, "scripts", "wsl_health_probe.sh")

    if not os.path.exists(health_script):
        logger.warning("[wsl_health] wsl_health_probe.sh not found at %s", health_script)
        return ProbeResult(rc=1, message="health probe script not found")

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
                f"timeout 30 {health_script} {wsl_dir}",
            ],
            capture_output=True,
            text=True,
                check=False,
        )
        rc = result.returncode
        if rc == 0:
            return ProbeResult(rc=0, message="WSL kernel healthy")
        if rc == 8:
            return ProbeResult(rc=8, message="WSL kernel polluted by D-state deadlock")
        return ProbeResult(
            rc=rc,
            message=f"health probe abnormal (rc={rc}), treat as pollution",
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(rc=8, message="health probe timeout, treat as pollution")
    except Exception as e:
        logger.warning("[wsl_health] check_wsl_kernel_health exception: %s", e)
        return ProbeResult(rc=8, message=str(e))


def ensure_dockerd(wsl_dir: str) -> ProbeResult:
    """确保 dockerd 运行（绕过 systemd 直接启动）。

    通过 wsl_start_daemon.sh 启动 dockerd，
    所有 pgrep/pkill/docker 调用都包裹在 timeout 中。

    Args:
        wsl_dir: WSL 内项目目录路径

    Returns:
        ProbeResult: rc=0 表示 dockerd 运行正常
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    daemon_script = os.path.join(script_dir, "scripts", "wsl_start_daemon.sh")

    if not os.path.exists(daemon_script):
        logger.warning("[wsl_health] wsl_start_daemon.sh not found at %s", daemon_script)
        return ProbeResult(rc=1, message="daemon script not found")

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
                f"timeout {_DAEMON_TIMEOUT} {daemon_script}",
            ],
            capture_output=True,
            text=True,
                check=False,
        )
        rc = result.returncode
        if rc == 0:
            return ProbeResult(rc=0, message="dockerd running OK")
        if rc == 7:
            return ProbeResult(rc=7, message="kernel polluted")
        return ProbeResult(rc=rc, message=f"dockerd start failed (rc={rc})")
    except subprocess.TimeoutExpired:
        return ProbeResult(rc=7, message="daemon start timeout")
    except Exception as e:
        logger.warning("[wsl_health] ensure_dockerd exception: %s", e)
        return ProbeResult(rc=7, message=str(e))


def get_wsl_ip() -> str | None:
    """获取 WSL IP 地址（NAT 模式）。

    Returns:
        WSL IP 地址字符串，获取失败返回 None
    """
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
                "hostname -I 2>/dev/null",
            ],
            capture_output=True,
            text=True,
                check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
    except Exception as e:
        logger.warning("[wsl_health] get_wsl_ip exception: %s", e)
    return None


# ── 引擎自愈（插件运行时维护 docker/WSL 引擎存活）─────────────────────
# WSL2 VM 在最后一个 wsl.exe 客户端退出后按 vmIdleTimeout（默认 60s）回收
# （microsoft/WSL#9968：配置大值也拦不住回收），dockerd 随 systemd 关闭 →
# Windows 侧 DOCKER_HOST=tcp://localhost:2375 拒绝连接。唯一可靠维护手段 =
# 常驻保活会话（wsl.exe ... sleep infinity）持有 VM。本段提供幂等拉起，
# 供 DockerProvider.is_available / isolation_guard 复检调用：插件在自身
# 运行周期内维护引擎存活，不依赖外部计划任务/Startup。
_ENGINE_ENSURE_COOLDOWN = 30.0
# 上一次真正拉起/等待的单调时钟（模块级：跨插件实例共享，防多实例风暴）
_last_engine_ensure: float = 0.0
# Windows 隐藏窗口标志（sidecar 拉起 wsl.exe 时不闪控制台）
_CREATE_NO_WINDOW = 0x08000000


def is_docker_reachable(timeout: float = 5.0) -> bool:
    """docker daemon 是否可经当前 DOCKER_HOST 配置访问（docker version）。"""
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except Exception as e:
        logger.warning("[wsl_health] docker 可达性探测异常: %s", e)
        return False


def _keepalive_running() -> bool:
    """是否已有 WSL 保活会话（wsl.exe 命令行含 sleep infinity）。"""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name='wsl.exe'\" | "
                "Where-Object { $_.CommandLine -match 'sleep infinity' } | "
                "Measure-Object).Count",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            logger.warning(
                "[wsl_health] 保活会话探测失败: %s", result.stderr.strip()[:200]
            )
            return False
        return int(result.stdout.strip()) >= 1
    except Exception as e:
        logger.warning("[wsl_health] 保活会话探测异常: %s", e)
        return False


def ensure_docker_engine(timeout: float = 30.0) -> bool:
    """确保 docker 引擎可达（幂等，供插件运行时自愈调用）。

    幂等语义：
    - docker 已可达 → 直接 True（零成本快路径）；
    - 冷却窗口（_ENGINE_ENSURE_COOLDOWN）内不重复拉起，防多插件/多轮次风暴；
    - 保活会话已存在 → 只等待 daemon 就绪（dockerd 由 WSL systemd 随 VM 启动）；
    - 无保活会话 → detached 拉起一个持有 VM 后等待就绪。

    返回最终可达性；阻塞最长 timeout 秒（Windows 专用：wsl.exe/powershell，
    与模块既有函数同假设；调用方应放线程池或依赖冷却保证低成本）。
    """
    global _last_engine_ensure
    if is_docker_reachable():
        return True
    now = time.monotonic()
    if now - _last_engine_ensure < _ENGINE_ENSURE_COOLDOWN:
        logger.info("[wsl_health] 引擎自愈冷却中，跳过拉起")
        return False
    _last_engine_ensure = now
    if _keepalive_running():
        logger.info("[wsl_health] 保活会话存在但 docker 不可达，等待 daemon 就绪")
    else:
        subprocess.Popen(
            ["wsl.exe", "-d", _WSL_DISTRO, "--exec", "/bin/sh", "-c", "sleep infinity"],
            creationflags=_CREATE_NO_WINDOW,
        )
        logger.info("[wsl_health] 已拉起 WSL 保活会话（%s），等待 docker 就绪", _WSL_DISTRO)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_docker_reachable(timeout=3.0):
            return True
        time.sleep(2)
    return False


def setup_port_forward(
    wsl_ip: str,
    frontend_port: str,
    redis_port: str,
) -> bool:
    """设置 netsh portproxy 端口转发（Windows localhost → WSL 容器端口）。

    先 reset 清除旧规则，再添加新规则，避免重复。

    Args:
        wsl_ip: WSL IP 地址
        frontend_port: 前端端口
        redis_port: Redis 端口

    Returns:
        True 表示端口转发配置成功
    """
    import tempfile

    # 写入临时 bat 文件避免引号嵌套问题
    portproxy_bat = os.path.join(tempfile.gettempdir(), "agent_portproxy.bat")
    lines = [
        "@echo off",
        "netsh interface portproxy reset",
        f"netsh interface portproxy add v4tov4 listenport={frontend_port} listenaddress=0.0.0.0 connectport={frontend_port} connectaddress={wsl_ip}",
        f"netsh interface portproxy add v4tov4 listenport={redis_port} listenaddress=0.0.0.0 connectport={redis_port} connectaddress={wsl_ip}",
    ]
    with open(portproxy_bat, "w") as f:
        f.write("\n".join(lines))

    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Start-Process cmd -Verb RunAs -Wait -ArgumentList '/c','{portproxy_bat}'",
            ],
            capture_output=True,
                check=False,
        )
        # 验证 portproxy 是否设置成功
        verify = subprocess.run(
            ["netsh", "interface", "portproxy", "show", "v4tov4"],
            capture_output=True,
            text=True,
                check=False,
        )
        return frontend_port in verify.stdout
    except Exception as e:
        logger.warning("[wsl_health] setup_port_forward exception: %s", e)
        return False

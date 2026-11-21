"""容器探测（container_probe）：从 Harbor 环境对象解析任务容器与工作目录。

harbor-free（对 environment 只鸭子式消费 ``exec`` 与 ``task_env_config``），
可独立单测。解析策略（两级，全失败显式 raise，绝不猜）：

1. 容器内自证：``environment.exec("cat /etc/hostname")`` —— Docker 默认把
   容器短 id 写进 /etc/hostname（compose 未显式设 hostname 时成立，TB 任务
   镜像不设）→ 用短 id 走 ``docker inspect`` 换取正式容器名；inspect 失败时
   短 id 本身就是合法 exec 目标（docker exec/inspect 均收短 id）。
2. workdir：``environment.task_env_config.workdir``（Harbor 任务环境配置的
   容器内工作目录，与 Harbor 自己 exec 用的 cwd 同源）；缺失回退 "/workspace"
   （external provider 收养时保证存在）。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any


class ContainerProbeError(RuntimeError):
    """容器解析失败（环境不可执行/hostname 不可得等）。"""


@dataclass
class ContainerRef:
    """任务容器坐标（供外部绑定面使用）。"""

    name: str  # 容器名或短 id（docker exec/inspect 均可定位）
    workdir: str  # 容器内任务工作目录


def _env_exec(env: Any, command: str, timeout_sec: int = 15) -> tuple[int, str, str]:
    """鸭子式 environment.exec 调用（桩测试用同步桩；真环境用 _env_exec_async）。

    Harbor ExecResult 形状 {stdout, stderr, return_code}；stdout/stderr 可为
    None（模型允许缺省），统一归一为空串。
    """
    result = env.exec(command, timeout_sec=timeout_sec)
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    rc = getattr(result, "return_code", 1)
    return int(rc), str(stdout or ""), str(stderr or "")


async def _env_exec_async(env: Any, command: str, timeout_sec: int = 15) -> tuple[int, str, str]:
    """async 版 environment.exec（真 Harbor 环境的调用形态）。"""
    result = await env.exec(command, timeout_sec=timeout_sec)
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    rc = getattr(result, "return_code", 1)
    return int(rc), str(stdout or ""), str(stderr or "")


def _docker_inspect_name(ref: str) -> str | None:
    """docker inspect 换正式容器名（宿主侧 docker CLI；失败返回 None）。"""
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.Name}}", ref],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    name = proc.stdout.decode("utf-8", errors="replace").strip()
    return name.lstrip("/") if name else None


def _resolve_workdir(env: Any) -> str:
    """task_env_config.workdir → 缺省 /workspace（收养面保证存在）。"""
    cfg = getattr(env, "task_env_config", None)
    workdir = getattr(cfg, "workdir", None) if cfg is not None else None
    if isinstance(workdir, str) and workdir.strip():
        return workdir.strip()
    return "/workspace"


def resolve_container(env: Any) -> ContainerRef:
    """同步入口（测试/脚本用；真 Harbor 异步环境用 resolve_container_async）。"""
    rc, stdout, stderr = _env_exec(env, "cat /etc/hostname")
    if rc != 0 or not stdout.strip():
        raise ContainerProbeError(
            f"容器自证失败（cat /etc/hostname rc={rc}）：{stderr[:200]}"
        )
    short_id = stdout.strip()
    name = _docker_inspect_name(short_id) or short_id
    return ContainerRef(name=name, workdir=_resolve_workdir(env))


async def resolve_container_async(env: Any) -> ContainerRef:
    """异步入口（BaseAgent.setup/run 内用；environment.exec 是协程）。"""
    rc, stdout, stderr = await _env_exec_async(env, "cat /etc/hostname")
    if rc != 0 or not stdout.strip():
        raise ContainerProbeError(
            f"容器自证失败（cat /etc/hostname rc={rc}）：{stderr[:200]}"
        )
    short_id = stdout.strip()
    name = _docker_inspect_name(short_id) or short_id
    return ContainerRef(name=name, workdir=_resolve_workdir(env))


def ref_to_dict(ref: ContainerRef) -> dict[str, str]:
    """ContainerRef → dict（日志/审计）。"""
    return {"name": ref.name, "workdir": ref.workdir}

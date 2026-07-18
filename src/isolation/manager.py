"""
隔离环境管理器

暴露接口：
- async get_isolation_manager(config_path: str | None) -> IsolationManager：获取全局隔离管理器（线程安全）
- get_stats(self) -> dict[str, Any]：get_stats功能
- IsolationManager：IsolationManager类
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path, PurePath  # noqa: F401
from typing import Any

from isolation.decider import IsolationDecider, IsolationUnrecoverableError
from isolation.providers.base import IsolationProvider
from isolation.providers.docker_provider import DockerProvider
from isolation.providers.host_provider import HostProvider
from isolation.types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
    OperationType,
    TaskType,
)

logger = logging.getLogger(__name__)


def _load_provider_config() -> dict[str, Any]:
    """从配置文件加载提供者配置（通过 ConfigCenter 统一缓存）。"""
    try:
        from config.config_center import get_config_center  # noqa: PLC0415

        config = get_config_center().get("isolation/isolation_config.yaml") or {}
        providers_config = config.get("providers", {})
        logger.debug("[IsolationManager] 从 ConfigCenter 加载提供者配置")
        return providers_config
    except Exception as e:
        logger.warning(f"[IsolationManager] 加载配置文件失败: {e}，使用默认配置")
        return {}


def _create_providers_from_config(
    providers_config: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[IsolationLevel, IsolationProvider]:
    """根据配置创建提供者实例。

    Args:
        providers_config: providers 配置字典（来自 isolation_config.yaml）
        profile: 硬件自适应资源配额（来自 hardware_profile.compute_resource_profile）。
                 非 None 时，其值优先于 providers_config 的 limits（自适应覆盖配置）。
    """
    if providers_config is None:
        providers_config = _load_provider_config()

    providers = {}

    # 宿主机提供者
    host_config = providers_config.get("host", {})
    if host_config.get("enabled", True):
        providers[IsolationLevel.HOST] = HostProvider()

    # Docker 提供者
    docker_config = providers_config.get("docker", providers_config.get("cua", {}))
    if docker_config.get("enabled", True):
        # 自适应 profile 优先于配置文件的 limits（profile 非 None 时覆盖）
        cfg_limits = docker_config.get("limits", {})
        if profile:
            memory_limit = profile.get("container_memory", cfg_limits.get("memory", "512m"))
            cpu_limit = profile.get("container_cpus", cfg_limits.get("cpus", "1.0"))
            memory_swap = profile.get("memory_swap", memory_limit)
            pids_limit = profile.get("pids_limit", 100)
        else:
            memory_limit = cfg_limits.get("memory", "512m")
            cpu_limit = cfg_limits.get("cpus", "1.0")
            memory_swap = cfg_limits.get("memory_swap", memory_limit)
            pids_limit = cfg_limits.get("pids_limit", 100)

        providers[IsolationLevel.CONTAINER] = DockerProvider(
            config={
                "image": docker_config.get("image", "agentos:latest"),
                "memory_limit": memory_limit,
                "cpu_limit": cpu_limit,
                "memory_swap": memory_swap,
                "pids_limit": pids_limit,
                "network_mode": docker_config.get("network_mode", "bridge"),
                # system-issue #3 escape hatch：容器端口映射到宿主，默认空。
                "publish_ports": docker_config.get("publish_ports", []),
                # 首次无镜像时自动构建：本地有 Dockerfile 则 docker build
                # （BuildKit 缓存复用本机已下载的包），无 Dockerfile 则回退 pull。
                # 默认 dockerfile_path/build_context 相对仓库根；超时默认 600s
                # （镜像含 apt+pip+playwright 下载，分钟级）。
                "auto_build": docker_config.get("auto_build", True),
                "dockerfile_path": docker_config.get("dockerfile_path", "docker/agentos/Dockerfile"),
                "build_context": docker_config.get("build_context", "."),
                "build_timeout": docker_config.get("build_timeout", 1800),
            },
        )
        logger.debug(
            "[IsolationManager] 创建 DockerProvider: image=%s memory=%s cpu=%s swap=%s pids=%s (source=%s)",
            docker_config.get("image", "agentos:latest"),
            memory_limit,
            cpu_limit,
            memory_swap,
            pids_limit,
            "hardware-adaptive" if profile else "config-file",
        )

    return providers


class IsolationManager:
    """隔离环境管理器

    负责：
    - 创建和销毁隔离环境
    - 环境生命周期管理（由任务状态驱动）
    - 环境复用

    容器生命周期策略：
    - 子任务共享根任务容器
    - 任务进入终态（COMPLETED/FAILED/CANCELLED/TIMEOUT）时自动销毁容器
    - 项目关闭时停止容器（不删除）
    - 项目启动时恢复已停止容器
    """

    CONTAINER_NAME_PREFIX = "cua-"

    # 连续建环境失败达此阈值即熔断（跨 core-retry 累计，ws 维度计数）。
    # docker create 自身已有「重建1次」+ engine core retry 3 次，这里的计数是
    # 在更高一层的「每轮 tool_execute」维度累计：连续 N 轮都建不起来即判定
    # 隔离环境不可恢复，抛 IsolationUnrecoverableError 让 engine_chain 挂引擎，
    # 不再每轮空转 docker create 30s。
    _MAX_ENV_FAILURES = 3

    def __init__(
        self,
        providers: dict[IsolationLevel, IsolationProvider] | None = None,
        decider: IsolationDecider | None = None,
        config_path: str | None = None,
        task_repository=None,
    ):
        """初始化管理器"""
        # 硬件自适应：启动时检测硬件，计算资源配额
        # profile 决定容器内存/CPU/数量上限，保护低配机不被压垮
        try:
            from isolation.hardware_profile import get_resource_profile  # noqa: PLC0415

            self._resource_profile = get_resource_profile()
        except Exception as e:
            logger.warning(f"[IsolationManager] 硬件检测失败，使用保守默认值: {e}")
            self._resource_profile = {
                "max_environments": 3,
                "container_memory": "256m",
                "container_cpus": "0.25",
                "memory_swap": "256m",
                "pids_limit": 64,
                "max_concurrent_tasks": 3,
                "tier": "low(fallback)",
            }

        if providers is not None:
            self._providers = providers
        else:
            providers_config = None
            if config_path:
                try:
                    from config.config_center import get_config_center  # noqa: PLC0415

                    # config_path 可能是绝对路径或相对路径，ConfigCenter 统一解析
                    rel = str(config_path).replace("\\", "/")
                    if "config/" in rel:
                        rel = rel[rel.index("config/") + len("config/") :]
                    config = get_config_center().get(rel) or {}
                    providers_config = config.get("providers", {})
                except Exception as e:
                    logger.warning(f"[IsolationManager] 加载配置文件失败: {e}")
            self._providers = _create_providers_from_config(providers_config, profile=self._resource_profile)

        self._decider = decider or IsolationDecider()  # 内部自动创建 IsolationPolicyLoader
        self._task_repository = task_repository

        self._environments: dict[str, IsolationEnvironment] = {}
        self._reuse_map: dict[str, str] = {}
        # workspace 标识 → env_id，同 workspace 复用同一容器
        self._workspace_env_map: dict[str, str] = {}
        # workspace 标识 → 专属锁，串行化同一 workspace 的「查找→创建→写缓存」，
        # 防止并发任务重复创建同名容器（docker create 报 Conflict）。
        self._ws_locks: dict[str, asyncio.Lock] = {}
        # workspace 标识 → 连续建环境失败次数。成功建/复用即清零；
        # 达 _MAX_ENV_FAILURES 抛 IsolationUnrecoverableError 熔断。
        self._ws_env_fail_counts: dict[str, int] = {}
        self._running = False

    async def start(self):
        """启动管理器"""
        if self._running:
            return

        self._running = True

        await self._resume_containers()

        # 清理 Docker 镜像/构建缓存（异步不阻塞启动）。
        # 限频：距上次清理不足 24h 则跳过，避免频繁启动管理器时反复对 daemon
        # 施加全局持锁重操作（image/builder prune 会遍历所有镜像层），
        # 加剧 WSL2 后端的 ext4.vhdx 锁死与 daemon 假死风险。
        if self._should_prune():
            asyncio.ensure_future(self._prune_docker_images())
        else:
            logger.debug("[IsolationManager] 跳过镜像清理（距上次不足 24h）")

        logger.debug("隔离环境管理器已启动")

    _PRUNE_INTERVAL_SEC = 24 * 3600
    _PRUNE_MARK_FILE = ".docker_prune_last"

    def _should_prune(self) -> bool:
        """是否应该执行镜像清理（距上次清理超 24h）。

        用项目根的持久化标记文件记录上次清理时间，跨进程重启有效，
        避免每次启动管理器就触发一次全局 prune。
        """
        import time  # noqa: PLC0415

        mark = Path(self._PRUNE_MARK_FILE)
        try:
            last = mark.read_text(encoding="utf-8").strip()
            if last and (time.time() - float(last)) < self._PRUNE_INTERVAL_SEC:
                return False
        except (FileNotFoundError, ValueError):
            pass
        except Exception:
            logger.warning("[IsolationManager] 读取 prune 标记失败，执行清理", exc_info=True)
        return True

    def _mark_prune_done(self) -> None:
        """记录本次清理完成的时间戳。"""
        import time  # noqa: PLC0415

        try:
            with open(self._PRUNE_MARK_FILE, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except Exception:
            logger.warning("[IsolationManager] 写入 prune 标记失败", exc_info=True)

    async def _run_docker_sync(
        self,
        sync_fn: Any,
        *,
        timeout: float,
        op_name: str,
    ) -> Any:
        """在线程池执行同步 Docker SDK 调用，硬超时兜底防 daemon 假死。

        docker Python SDK 的 HTTP 调用默认 socket 无超时，daemon 假死
        （挂起不响应、不抛错）时会永久阻塞；这些调用若直接跑在事件循环
        线程，会冻死整个 asyncio 事件循环（所有任务停摆，只能强杀进程）。
        此处把同步调用丢进线程池 + asyncio.wait_for 硬超时兜底：
        超时返回 None；异常透传由调用方按语义处理。

        Args:
            sync_fn: 同步可调用对象（封装一次完整的 docker SDK 交互）
            timeout: 硬超时秒数
            op_name: 操作名（日志标识）

        Returns:
            sync_fn 的返回值；超时返回 None；异常透传。
        """
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, sync_fn),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning(f"[IsolationManager] {op_name} 超时({timeout}s)，daemon 可能假死，放弃操作")
            return None

    async def _resume_containers(self):  # noqa: PLR0912
        """恢复未完成任务的容器，清理已完成/不存在任务的容器

        项目启动时：
        1. 查找所有 cua-* 容器
        2. 从容器名提取 workspace 标识，验证该 workspace 是否还有活跃任务
        3. 仅恢复有活跃任务的容器
        4. 无活跃任务的容器直接销毁
        """
        try:
            from docker.errors import DockerException, NotFound  # noqa: PLC0415

            import docker  # noqa: PLC0415

            # timeout=10：daemon 假死时 HTTP 调用 10s 后超时抛异常，
            # 避免永久阻塞冻死事件循环（本方法结构含循环+await，
            # 不做完整线程池拆分，靠 socket 超时兜底）
            client = docker.from_env(timeout=10)
            containers = client.containers.list(all=True)

            active_ws_keys = await self._load_active_workspace_keys()
            if active_ws_keys is None:
                # fail-closed：加载失败时跳过销毁逻辑，仅尝试恢复 exited 容器
                logger.warning("[IsolationManager] 活跃 workspace 加载失败，跳过容器销毁逻辑，仅尝试恢复 exited 容器")

            resumed_count = 0
            destroyed_count = 0
            for container in containers:
                if not container.name.startswith(self.CONTAINER_NAME_PREFIX):
                    continue

                ws_key = container.name[len(self.CONTAINER_NAME_PREFIX) :]

                # 仅当成功加载活跃 workspace 时才执行销毁逻辑，避免 fail-open 误销毁在用容器
                if active_ws_keys is not None and ws_key not in active_ws_keys:
                    logger.debug(f"[IsolationManager] workspace {ws_key} 无活跃任务，销毁容器: {container.name}")
                    try:
                        container.stop(timeout=5)
                        container.remove()
                        destroyed_count += 1
                    except (DockerException, NotFound) as e:
                        logger.warning(f"[IsolationManager] 销毁容器失败: {container.name}, 错误: {e}")
                    continue

                workspace_path = None
                mounts = container.attrs.get("Mounts", [])
                for mount in mounts:
                    if mount.get("Destination") == "/workspace" and mount.get("Type") == "bind":
                        workspace_path = mount.get("Source")
                        break

                if workspace_path:
                    from pathlib import Path  # noqa: PLC0415

                    workspace_dir = Path(workspace_path)
                    if not workspace_dir.exists():
                        logger.debug(f"[IsolationManager] 工作空间目录不存在，正在创建: {workspace_path}")
                        workspace_dir.mkdir(parents=True, exist_ok=True)

                if container.status == "exited":
                    try:
                        container.start()
                        logger.debug(f"[IsolationManager] 已恢复容器: {container.name}")
                        resumed_count += 1
                    except DockerException as e:
                        logger.warning(f"[IsolationManager] 恢复容器失败: {container.name}, 错误: {e}")

            if resumed_count > 0:
                logger.debug(f"[IsolationManager] 共恢复 {resumed_count} 个容器")
            if destroyed_count > 0:
                logger.debug(f"[IsolationManager] 共销毁 {destroyed_count} 个无效容器")

            client.close()

        except Exception as e:
            logger.warning(f"[IsolationManager] 恢复容器失败: {e}")

    async def _prune_docker_images(self) -> None:
        """清理 Docker 悬挂镜像和过期构建缓存。

        每次构建/拉取镜像后，旧版本标签被新镜像抢占，旧镜像变为
        <none>:<none> 悬挂镜像。同时 Docker 构建缓存不断累积中间层。
        此方法在启动时清理这些垃圾，防止 VHDX 无限膨胀。

        策略：
        - 删除所有悬挂镜像（<none>:<none>）
        - 保留近 72 小时的构建缓存（加速频繁重建）
        - 静默失败不阻塞启动
        """
        await asyncio.sleep(5)  # 延迟 5s，等 Docker daemon 完全就绪
        import subprocess as _sp  # noqa: PLC0415

        try:
            loop = asyncio.get_event_loop()

            # 1. 清理悬挂镜像
            proc = await loop.run_in_executor(
                None,
                lambda: _sp.run(  # noqa: PLW1510
                    ["docker", "image", "prune", "-f"],
                    capture_output=True,
                    timeout=60,
                ),
            )
            if proc.returncode == 0:
                logger.debug("[IsolationManager] Docker 悬挂镜像已清理")
            else:
                err = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
                logger.debug(
                    "[IsolationManager] image prune 跳过（Docker 不可用或无需清理）: %s",
                    err.strip()[:200],
                )

            # 2. 清理过期构建缓存（保留近 72 小时）
            rc2, _, stderr2 = await loop.run_in_executor(
                None,
                lambda: _sp.run(  # noqa: PLW1510
                    ["docker", "builder", "prune", "-f", "--filter", "until=72h"],
                    capture_output=True,
                    timeout=60,
                ),
            )
            if rc2 == 0:
                logger.debug("[IsolationManager] Docker 过期构建缓存已清理")
            else:
                err2 = stderr2.decode("utf-8", errors="replace") if stderr2 else ""
                logger.debug(
                    "[IsolationManager] builder prune 跳过: %s",
                    err2.strip()[:200],
                )

            # 记录本次清理时间戳（即使部分跳过也算尝试过，限频防反复触发）
            self._mark_prune_done()

        except FileNotFoundError:
            logger.debug("[IsolationManager] Docker CLI 不可用，跳过镜像清理")
        except TimeoutError:
            logger.debug("[IsolationManager] 镜像清理超时（Docker 可能未就绪）")
        except Exception as e:
            logger.debug("[IsolationManager] 镜像清理失败（非致命）: %s", e)

    async def _load_active_workspace_keys(self) -> set[str] | None:
        """从 YAML 任务存储加载所有非终态任务的 workspace 标识集合

        Returns:
            活跃 workspace 标识集合；加载失败时返回 None（fail-closed），
            调用方需检查 None 以避免误销毁在用容器。
        """
        if self._task_repository is None:
            logger.warning("[IsolationManager] task_repository 未注入，无法加载活跃 workspace")
            return None
        try:
            from tasks.types import TaskStatus  # noqa: PLC0415

            terminal_statuses = {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.STOPPED,
                TaskStatus.TIMEOUT,
            }
            # task_repository 可能是 TaskStorage（有 _tasks）或 TaskService（有 _storage）
            storage = getattr(self._task_repository, "_storage", None) or self._task_repository
            tasks_dict = getattr(storage, "_tasks", None)
            if tasks_dict is None:
                logger.warning("[IsolationManager] task_repository 无 _tasks 属性，无法加载活跃 workspace")
                return None
            active_ws_keys: set[str] = set()
            for t in tasks_dict.values():
                if t.status in terminal_statuses:
                    continue
                ws_meta = (t.metadata or {}).get("ws_meta") or {}
                workspace = ws_meta.get("path")
                if workspace:
                    key = PurePath(workspace).name
                    if key:
                        active_ws_keys.add(key)
            return active_ws_keys
        except Exception as e:
            # fail-closed：返回 None 让调用方跳过销毁逻辑，避免空集合导致在用容器被误销毁
            logger.warning(f"[IsolationManager] 加载活跃 workspace 失败: {e}")
            return None

    async def stop(self):
        """停止管理器"""
        if not self._running:
            return

        self._running = False

        await self._stop_containers()

        logger.debug("隔离环境管理器已停止")

    async def _stop_containers(self):
        """停止所有活跃任务的容器（不删除）

        项目关闭时，查找所有 cua-* 容器，
        仅停止仍有活跃任务的 workspace 对应的容器
        """
        try:
            from docker.errors import DockerException  # noqa: PLC0415

            import docker  # noqa: PLC0415

            # timeout=10：daemon 假死时 HTTP 调用 10s 后超时抛异常，避免永久阻塞
            client = docker.from_env(timeout=10)
            containers = client.containers.list(all=True)

            active_ws_keys = await self._load_active_workspace_keys()
            if active_ws_keys is None:
                active_ws_keys = set()

            stopped_count = 0
            for container in containers:
                if not container.name.startswith(self.CONTAINER_NAME_PREFIX):
                    continue

                ws_key = container.name[len(self.CONTAINER_NAME_PREFIX) :]
                if ws_key not in active_ws_keys:
                    continue

                if container.status == "running":
                    try:
                        container.stop(timeout=5)
                        logger.debug(f"[IsolationManager] 已停止容器: {container.name}")
                        stopped_count += 1
                    except DockerException as e:
                        logger.warning(f"[IsolationManager] 停止容器失败: {container.name}, 错误: {e}")

            if stopped_count > 0:
                logger.debug(f"[IsolationManager] 共停止 {stopped_count} 个容器")

            client.close()

        except Exception as e:
            logger.warning(f"[IsolationManager] 停止容器失败: {e}")

    async def get_or_create_environment(  # noqa: PLR0912,PLR0915
        self,
        task_id: str,
        task_type: TaskType,
        operation_type: OperationType | None = None,
        parent_env_id: str | None = None,
        workspace: str | None = None,
        parent_workspace: str | None = None,
        is_root_task: bool = True,
        isolation_level: IsolationLevel | None = None,
        metadata: dict | None = None,
        parent_task_id: str | None = None,
        tool_name: str | None = None,
    ) -> IsolationEnvironment:
        """获取或创建隔离环境"""
        # 1. 按 workspace 生成容器名（同 workspace 共享一个容器）
        ws_key = PurePath(workspace).name if workspace else task_id
        container_name = self._workspace_to_container_name(workspace, task_id)

        logger.debug(f"[IsolationManager] 任务 {task_id} 的 workspace={workspace}，容器名称: {container_name}")

        async with self._get_ws_lock(ws_key):
            # 2. 检查是否已有该 workspace 的容器（优先复用）
            if ws_key in self._workspace_env_map:
                env_id = self._workspace_env_map[ws_key]
                existing = self._environments.get(env_id)
                if existing and existing.status == EnvironmentStatus.READY.value:
                    existing.last_used_at = datetime.now(UTC).isoformat()
                    logger.debug(f"[IsolationManager] 复用 workspace 容器: {container_name} (env_id={env_id})")
                    self._ws_env_fail_counts.pop(ws_key, None)
                    return existing

            # 3. 尝试从 Docker 查找已有容器
            existing_env = await self._find_existing_container(container_name)
            if existing_env:
                self._environments[existing_env.env_id] = existing_env
                self._workspace_env_map[ws_key] = existing_env.env_id
                # 同步注册到 provider，确保 execute_in_environment 能在 provider 侧命中。
                # _find_existing_container 绕过 provider 直接构造了 env，
                # 若不注册则 provider._environments 中无此记录。
                provider = self._providers.get(existing_env.level)
                if provider is not None:
                    provider._environments[existing_env.env_id] = existing_env
                logger.debug(f"[IsolationManager] 恢复已有容器: {container_name}")
                self._ws_env_fail_counts.pop(ws_key, None)
                return existing_env

            # 4. 检查是否可以复用父级环境
            if parent_env_id:
                existing = self._environments.get(parent_env_id)
                if existing and existing.status == EnvironmentStatus.READY.value:
                    existing.last_used_at = datetime.now(UTC).isoformat()
                    logger.debug(f"复用父级环境: {parent_env_id}")
                    self._ws_env_fail_counts.pop(ws_key, None)
                    return existing

            # 5. 决策隔离级别
            available = await self._check_providers_availability()
            if isolation_level:
                level = isolation_level
                requires_approval = level == IsolationLevel.HOST
                logger.debug(f"使用指定的隔离级别: {level.value} (requires_approval={requires_approval})")
            else:
                # 使用决策器根据工具名决策隔离策略
                # 注意：decider 需要的是 tool_name（如 bash_execute）而非 task_type（atomic），
                # 否则 policy 匹配不到工具级规则会落到 default=host。
                tool_category = operation_type.value if operation_type else None
                policy = await self._decider.decide(
                    tool_name=tool_name or task_type.value,
                    tool_category=tool_category,
                    available_providers=available,
                )
                level = policy.isolation
                requires_approval = policy.approval
                logger.debug(
                    f"为任务 {task_id} 选择隔离级别: {level.value} (requires_approval={requires_approval})"
                    f"(tool_name={tool_name}, task_type={task_type.value}, operation_type={operation_type.value if operation_type else None})"
                )

            # 6. 创建新环境
            provider = self._providers.get(level)
            if not provider:
                raise RuntimeError(f"找不到 {level.value} 对应的提供者")

            # 资源保护：容器隔离时检查环境数量上限（防止低配机被压垮）
            # 自适应 profile 决定上限，超限时拒绝新建，保护系统稳定性
            if level == IsolationLevel.CONTAINER:
                max_env = self._resource_profile.get("max_environments", 8)
                # 统计活跃的容器环境（READY/BUSY/CREATING），排除已停止和出错的
                inactive_statuses = {EnvironmentStatus.STOPPED.value, EnvironmentStatus.ERROR.value}
                current_count = sum(
                    1
                    for env in self._environments.values()
                    if env.level == IsolationLevel.CONTAINER and env.status not in inactive_statuses
                )
                if current_count >= max_env:
                    logger.error(
                        f"[IsolationManager] 隔离环境数量已达上限 {current_count}/{max_env} "
                        f"(tier={self._resource_profile.get('tier', '?')}), "
                        f"拒绝为 workspace {ws_key} 创建新环境。"
                        f"可通过环境变量 AO_MAX_ENVIRONMENTS 调整上限。"
                    )
                    raise RuntimeError(
                        f"隔离环境数量已达上限 ({current_count}/{max_env})，"
                        f"无法为任务创建新环境。请等待现有任务完成，"
                        f"或通过环境变量 AO_MAX_ENVIRONMENTS 调整上限。"
                    )

            context = IsolationContext(
                task_id=task_id,
                task_type=task_type,
                operation_type=operation_type,
                parent_env_id=parent_env_id,
                workspace=workspace,
                parent_workspace=parent_workspace,
                is_root_task=True,
                isolation_level=level,
                requires_approval=requires_approval,
                metadata=metadata or {},
            )

            if level == IsolationLevel.CONTAINER:
                env = await provider.create_environment(context, container_name=container_name)
            else:
                env = await provider.create_environment(context)

            self._environments[env.env_id] = env
            self._workspace_env_map[ws_key] = env.env_id

            logger.debug(f"创建新隔离环境: {env.env_id} (level={level.value}, container_name={container_name})")
            self._ws_env_fail_counts.pop(ws_key, None)
            return env

    def set_task_repository(self, repo: Any) -> None:
        """注入任务仓储，启用按 workspace 销毁容器。

        注入后，destroy_by_task_id 会查询 repo 获取任务的 workspace，
        再按 workspace 标识销毁对应容器。repo 需实现同步 get(task_id)。

        Args:
            repo: 任务仓储实例（如 TaskService._storage，TaskStorage）
        """
        self._task_repository = repo
        logger.debug("[IsolationManager] task_repository 已注入，按 workspace 管理容器")

    def _get_ws_lock(self, ws_key: str) -> asyncio.Lock:
        """获取 workspace 专属锁（同 ws_key 恒返回同一把锁）。

        用于串行化同一 workspace 的 get_or_create_environment，防止并发任务
        同时通过「容器不存在」检查后重复 docker create 同名容器（Conflict）。
        不同 ws_key 互不阻塞。
        """
        return self._ws_locks.setdefault(ws_key, asyncio.Lock())

    @staticmethod
    def _workspace_to_container_name(workspace: str | None, task_id: str) -> str:
        """根据 workspace 路径生成容器名。

        同一 workspace 共享一个容器，避免每个任务创建独立容器。
        容器名取 workspace 路径的最后一段（如 container_036fa__wt_726def4a），
        兜底用 task_id。

        Args:
            workspace: workspace 绝对路径
            task_id: 任务 ID（workspace 为空时兜底）

        Returns:
            容器名（含 cua- 前缀）
        """
        if workspace:
            from pathlib import PurePath  # noqa: PLC0415

            name = PurePath(workspace).name or task_id
        else:
            name = task_id
        return f"{IsolationManager.CONTAINER_NAME_PREFIX}{name}"

    async def _find_existing_container(self, container_name: str) -> IsolationEnvironment | None:
        """查找已存在的容器（异步外壳：线程池+硬超时防 daemon 假死阻塞）。

        同步实现见 _find_existing_container_sync；超时/异常均返回 None，
        不冻死事件循环。
        """
        try:
            return await self._run_docker_sync(
                lambda: self._find_existing_container_sync(container_name),
                timeout=15,
                op_name="find_existing_container",
            )
        except Exception as e:
            logger.warning(f"[IsolationManager] 查找容器失败: {e}")
            return None

    def _find_existing_container_sync(self, container_name: str) -> IsolationEnvironment | None:
        """查找已存在容器的同步实现（由 _run_docker_sync 在线程池执行）。

        - running → exec 探针验活性后返回 READY 环境（setns 脱节的坏容器探针会失败）
        - created/exited → 尝试 start；失败（如挂载脏路径）删容器返回 None
        - NotFound → None

        第三层根因修复：setns 命名空间脱节时 docker inspect 仍报 running，
        旧实现直接信任 status 复用坏容器 → 后续 exec 全部 setns 失败。
        修复：running 容器加 exec_run(["true"]) 探针，runc 卡死的容器探针会抛
        APIError（setns failure），此时删容器返回 None，让上层走 create_environment
        真正新建。探针开销极小（true 是 no-op），且只在复用路径触发。
        """
        from docker.errors import DockerException, NotFound  # noqa: PLC0415

        import docker  # noqa: PLC0415

        client = docker.from_env(timeout=10)
        try:
            container = client.containers.get(container_name)

            # 仅 running 可直接复用；created/exited 需启动。
            # 启动失败（如 created 卡死在挂载脏路径）→ 删除容器返回 None，
            # 让上层走新建路径（create_environment 内含 start 失败重建重试）。
            if container.status != "running":
                try:
                    container.start()
                    container.reload()
                    logger.debug(f"[IsolationManager] 已恢复停止的容器: {container_name}")
                except DockerException as e:
                    logger.warning(f"[IsolationManager] 恢复容器失败，将重建: {container_name}, 错误: {e}")
                    with contextlib.suppress(DockerException):
                        container.remove(force=True)
                    return None

            # 活性探针：running 容器也可能是 runc 命名空间脱节的"僵尸"
            # （inspect 报 running 但 setns 已坏）。exec_run(["true"]) 正是
            # 会触发 setns 的操作——坏容器探针抛 APIError，健康容器返回 (0, ...)。
            # 用 no-op 命令把开销压到最小；探针失败则当坏容器处理：尝试删，
            # 删不掉也无妨（docker rm -f 对卡死容器会失败），返回 None 让上层新建。
            try:
                exit_code, _ = container.exec_run(["true"])
                if exit_code != 0:
                    logger.warning(
                        f"[IsolationManager] 容器活性探针失败（exec 非零），视为坏容器重建: "
                        f"{container_name}, exit_code={exit_code}"
                    )
                    with contextlib.suppress(DockerException):
                        container.remove(force=True)
                    return None
            except DockerException as e:
                logger.warning(
                    f"[IsolationManager] 容器活性探针异常（疑似 runc 命名空间脱节），视为坏容器重建: "
                    f"{container_name}, 错误: {e}"
                )
                with contextlib.suppress(DockerException):
                    container.remove(force=True)
                return None

            workspace_path = None
            mounts = container.attrs.get("Mounts", [])
            for mount in mounts:
                if mount.get("Destination") == "/workspace" and mount.get("Type") == "bind":
                    workspace_path = mount.get("Source")
                    break

            if workspace_path:
                from pathlib import Path  # noqa: PLC0415

                workspace_dir = Path(workspace_path)
                if not workspace_dir.exists():
                    logger.debug(f"[IsolationManager] 工作空间目录不存在，正在创建: {workspace_path}")
                    workspace_dir.mkdir(parents=True, exist_ok=True)
                    logger.debug(f"[IsolationManager] 已创建工作空间目录: {workspace_path}")

            now = datetime.now(UTC)

            # 统一使用 container_name 作为 env_id（与 create_environment 一致），
            # 同一 workspace 对应同一 container_name，env_id 稳定不变。
            env = IsolationEnvironment(
                env_id=container_name,
                level=IsolationLevel.CONTAINER,
                provider_type="cua",
                status=EnvironmentStatus.READY.value,
                context=IsolationContext(
                    task_id=container_name.replace(self.CONTAINER_NAME_PREFIX, ""),
                    task_type=TaskType.ATOMIC,
                    is_root_task=True,
                    isolation_level=IsolationLevel.CONTAINER,
                ),
                provider_info={
                    "container_id": container.id,
                    "container_name": container_name,
                    "workspace_root": workspace_path,
                },
                created_at=now.isoformat(),
                last_used_at=now.isoformat(),
            )
            return env

        except NotFound:
            return None
        finally:
            client.close()

    async def destroy_by_task_id(self, task_id: str, success: bool = True) -> None:
        """根据任务 ID 销毁关联的隔离容器（强制销毁）

        从任务元数据读取 workspace，按 workspace 标识销毁对应容器。
        同一 workspace 的所有任务共享容器，销毁时一次清理。
        用于任务显式删除场景——无论 workspace 是否还有其他任务都销毁。
        """
        ws_key = self._resolve_workspace_key(task_id)
        if ws_key is None:
            logger.warning(f"[IsolationManager] 任务 {task_id} 无 workspace 信息，跳过容器销毁")
            return

        env_id = self._workspace_env_map.get(ws_key)
        if env_id:
            logger.debug(f"[IsolationManager] 任务 {task_id} (workspace {ws_key}) 通过内存映射销毁容器 {env_id}")
            await self.destroy_environment(env_id, success=success)
            return

        await self._destroy_container_by_name(ws_key)

    async def destroy_if_workspace_idle(self, task_id: str) -> None:
        """任务进入终态时尝试销毁容器（仅当 workspace 无其他活跃任务时）

        同一 workspace 的任务共享容器。单个任务终态时不能贸然销毁——
        需检查该 workspace 是否还有其他非终态任务：
        - 有活跃任务 → 保留容器（兄弟任务还在用）
        - 无活跃任务 → 销毁容器（整个 workspace 已结束）

        失败任务重启时，get_or_create_environment 会自动重建容器。
        """
        ws_key = self._resolve_workspace_key(task_id)
        if ws_key is None:
            return

        active_ws_keys = await self._load_active_workspace_keys()
        if active_ws_keys is None:
            # 加载失败 fail-closed，不销毁避免误删在用容器
            logger.debug(f"[IsolationManager] 任务 {task_id} 终态，但活跃 workspace 加载失败，跳过销毁")
            return

        if ws_key in active_ws_keys:
            logger.debug(f"[IsolationManager] 任务 {task_id} 终态，但 workspace {ws_key} 仍有活跃任务，保留容器")
            return

        logger.debug(f"[IsolationManager] 任务 {task_id} 终态且 workspace {ws_key} 无活跃任务，销毁容器")
        env_id = self._workspace_env_map.get(ws_key)
        if env_id:
            await self.destroy_environment(env_id, success=True)
        else:
            await self._destroy_container_by_name(ws_key)

    def _resolve_workspace_key(self, task_id: str) -> str | None:
        """从任务仓储解析 task 的 workspace 标识（路径最后一段）。

        子任务可能未加载到内存，沿 parent_task_id 链向上查找，
        直到找到带 ws_meta 的祖先任务（子任务共享父工作空间）。

        Args:
            task_id: 任务 ID

        Returns:
            workspace 标识（如 container_036fa__wt_726def4a），无信息返回 None
        """
        if not self._task_repository:
            return None
        try:
            # TaskStorage.get 是同步方法，不能 await
            current_id = task_id
            visited: set[str] = set()
            while current_id and current_id not in visited:
                visited.add(current_id)
                task = self._task_repository.get(current_id)
                if task is None:
                    break
                ws_meta = (task.metadata or {}).get("ws_meta") or {}
                workspace = ws_meta.get("path")
                if workspace:
                    return PurePath(workspace).name or None
                # 子任务未加载或无 ws_meta，向上找父任务
                current_id = task.parent_task_id
        except Exception as e:
            logger.warning(f"[IsolationManager] 解析任务 {task_id} 的 workspace 失败: {e}")
        return None

    async def _destroy_container_by_name(self, ws_key: str) -> None:
        """通过 Docker API 直接查找并删除容器（异步外壳：线程池+硬超时防阻塞）。"""
        container_name = f"{self.CONTAINER_NAME_PREFIX}{ws_key}"
        try:
            await self._run_docker_sync(
                lambda: self._destroy_container_by_name_sync(container_name),
                timeout=15,
                op_name="destroy_container",
            )
        except Exception as e:
            logger.warning(f"[IsolationManager] 通过 Docker API 删除容器失败: {container_name}, error={e}")

    def _destroy_container_by_name_sync(self, container_name: str) -> None:
        """删除容器的同步实现（由 _run_docker_sync 在线程池执行）。"""
        from docker.errors import NotFound  # noqa: PLC0415

        import docker  # noqa: PLC0415

        client = docker.from_env(timeout=10)
        try:
            try:
                container = client.containers.get(container_name)
                container.stop(timeout=5)
                container.remove()
                logger.debug(f"[IsolationManager] 已通过 Docker API 删除容器: {container_name}")
            except NotFound:
                logger.debug(f"[IsolationManager] 容器不存在，无需删除: {container_name}")
        finally:
            client.close()

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        """销毁隔离环境。

        返回 provider 层是否真正删除成功（provider.destroy_environment 现返回 bool）。
        provider 删除失败（如 runc 卡死删不掉）时返回 False 且不清理内存映射——
        docker 里容器仍在，记录不能丢，供上层重建逻辑判断是否可重建。
        """
        env = self._environments.get(env_id)
        if not env:
            logger.warning(f"尝试销毁不存在的环境: {env_id}")
            return True  # 无可销毁记录，视为已成功（幂等）

        logger.debug(f"销毁隔离环境: {env_id}")

        provider = self._providers.get(env.level)
        destroyed_ok = True
        if provider:
            try:
                destroyed_ok = await provider.destroy_environment(env_id, success=success)
            except Exception as e:
                logger.error(f"提供者销毁环境失败: {e}")
                destroyed_ok = False

        # 只有 provider 真删成功，才清理内存映射；删失败时保留记录
        # （docker 里容器仍在，内存记录不能脱节，否则上层误判）。
        if destroyed_ok:
            self._environments.pop(env_id, None)

            keys_to_remove = [k for k, v in self._reuse_map.items() if v == env_id]
            for key in keys_to_remove:
                del self._reuse_map[key]

            ws_keys_to_remove = [k for k, v in self._workspace_env_map.items() if v == env_id]
            for key in ws_keys_to_remove:
                del self._workspace_env_map[key]

        return destroyed_ok

    async def execute_in_isolation(
        self,
        task_id: str,
        task_type: TaskType,
        operation: dict,
        operation_type: OperationType | None = None,
        parent_env_id: str | None = None,
        workspace: str | None = None,
        parent_workspace: str | None = None,
        is_root_task: bool = True,
        isolation_level: IsolationLevel | None = None,
        parent_task_id: str | None = None,
        tool_name: str | None = None,
    ) -> ExecutionResult:
        """在隔离环境中执行操作"""
        # 复用/重建环境所需入参（执行前自愈重建时复用）
        rebuild_kwargs: dict[str, Any] = {
            "task_id": task_id,
            "task_type": task_type,
            "operation_type": operation_type,
            "parent_env_id": parent_env_id,
            "workspace": workspace,
            "parent_workspace": parent_workspace,
            "is_root_task": is_root_task,
            "isolation_level": isolation_level,
            "parent_task_id": parent_task_id,
            "tool_name": tool_name,
        }

        # 获取或创建环境。
        # docker create/pull 超时（subprocess.TimeoutExpired）历史上在此冒泡成
        # Core 异常 → engine_chain 当 transient 不熔断 → 无限 next_llm 空转（每轮
        # 白等 docker create 30s × core retry 3 次 ≈ 4min）。这里把建环境整体 try：
        # 未达阈值返回失败 result（走正常路径回 LLM，LLM 可自行调整）；
        # 达阈值抛 IsolationUnrecoverableError 让 engine_chain 直接 ENDED 挂引擎。
        try:
            env = await self.get_or_create_environment(**rebuild_kwargs)

            # 执行前健康检查：容器非就绪(created/exited/dead)时透明自愈重建一次，
            # 避免 docker start 失败被吞后，用卡死的容器执行导致 "is not running"。
            env = await self._ensure_env_healthy_or_rebuild(
                env,
                rebuild_kwargs=rebuild_kwargs,
            )
        except IsolationUnrecoverableError:
            raise  # 熔断信号原样上抛，engine_chain 收到后 ENDED
        except Exception as build_exc:
            ws_key = PurePath(workspace).name if workspace else (task_id or "unknown")
            count = self._ws_env_fail_counts.get(ws_key, 0) + 1

            if count >= self._MAX_ENV_FAILURES:
                # 隔离环境连续多次建不起来：自愈已无意义（docker/WSL 长时间不可用），
                # 继续重试/next_llm 只会空转烧钱。清零计数后抛熔断信号挂引擎。
                self._ws_env_fail_counts.pop(ws_key, None)
                logger.error(
                    "[IsolationManager] 隔离环境连续 %d/%d 次不可用，触发熔断 | ws_key=%s task=%s | last_error=%s",
                    count,
                    self._MAX_ENV_FAILURES,
                    ws_key,
                    task_id,
                    build_exc,
                )
                raise IsolationUnrecoverableError(
                    f"隔离环境连续 {count} 次不可用，自愈失败，已熔断。"
                    f"请检查 Docker/WSL 是否正常运行"
                    f"（docker ps 是否秒回、agentos:latest 镜像是否存在）。"
                    f"最近错误: {build_exc}"
                ) from build_exc

            self._ws_env_fail_counts[ws_key] = count
            logger.warning(
                "[IsolationManager] 建隔离环境失败 (%d/%d)，返回失败结果 | ws_key=%s | error=%s",
                count,
                self._MAX_ENV_FAILURES,
                ws_key,
                build_exc,
            )
            return ExecutionResult(
                success=False,
                output=None,
                error=f"隔离环境暂不可用({count}/{self._MAX_ENV_FAILURES}): {build_exc}",
                metadata={"isolation_unavailable": True, "fail_count": count},
            )

        logger.debug(f"在环境 {env.env_id} 中执行操作: {operation.get('type', 'unknown')}")

        # 在环境中执行
        provider = self._providers.get(env.level)
        if not provider:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"找不到 {env.level.value} 对应的提供者",
            )

        try:
            result = await provider.execute_in_environment(env.env_id, operation)

            # post-exec 自愈：runc 命名空间脱节（setns failure）。
            # 这类故障下 docker inspect 仍报 running，pre-exec 健康检查（上面的
            # _ensure_env_healthy_or_rebuild）无法发现，错误只在 exec 时以 runc
            # 特征串冒泡。命中则 destroy + recreate + 单次重试——重建容器通常即恢复，
            # 与 _create_and_start / _ensure_image 的「自愈重试一次」范式一致。
            # 不计熔断：setns 是容器级瞬时故障，与 build 失败的熔断语义不同。
            # 仅 command 操作纳入：file_operation 走不同 exec 组装路径，暂不覆盖。
            # 标记识别（runc 特征串）与 provider 实例无关，直接用静态方法判定，
            # 故不绑 isinstance——只对 CONTAINER 层（env.level）的 docker 容器路径有意义。
            if (
                not result.success
                and operation.get("type") == "command"
                and self._is_namespace_desync_result(result)
            ):
                result = await self._rebuild_and_retry_exec(
                    env, rebuild_kwargs, operation, original_error=result.error
                )

            # 更新最后使用时间
            env.last_used_at = datetime.now(UTC).isoformat()

            return result

        except Exception as e:
            logger.error(f"执行操作失败: {e}", exc_info=True)
            return ExecutionResult(
                success=False,
                output=None,
                error=f"执行操作失败: {str(e)}",
            )

    @staticmethod
    def _is_namespace_desync_result(result: ExecutionResult) -> bool:
        """判断 exec 失败结果是否为 runc 命名空间脱节（同时看 error 与 output.stderr）。"""
        if DockerProvider._is_namespace_desync_error(result.error):
            return True
        # output 可能是 dict（command exec 的标准结构）也可能为 None
        if isinstance(result.output, dict):
            return DockerProvider._is_namespace_desync_error(result.output.get("stderr"))
        return False

    async def _rebuild_and_retry_exec(
        self,
        env: IsolationEnvironment,
        rebuild_kwargs: dict[str, Any],
        operation: dict[str, Any],
        *,
        original_error: str | None,
    ) -> ExecutionResult:
        """命中 setns 命名空间脱节后：destroy + recreate + 单次重试。

        重建/重试过程中的任何异常都只记 warning 并返回原失败 result——自愈逻辑
        本身不应把引擎搞挂（重建失败说明 docker 长时间不可用，会由后续轮次的
        建环境熔断器接管）。重试成功则在 metadata 标记 namespace_desync_recovered。

        第三层根因闭环：runc 卡死的容器 docker rm -f 会失败（删不掉），且同名新
        容器 create 必冲突（容器名唯一）。destroy 返回 False（没删干净）时绝不进
        重建——否则重建要么被 _find_existing_container 捡回坏容器，要么 create
        同名冲突，必然失败并空转。此时直接返回明确错误（坏容器删不掉，需重启
        docker），让熔断器/LLM 知道该 workspace 已不可恢复，避免空转烧资源。
        """
        logger.warning(
            "[IsolationManager] exec 命中 runc 命名空间脱节，触发自愈重建 | env=%s | err_tail=%s",
            env.env_id,
            (original_error or "")[:200],
        )

        # 1. 销毁脱节的旧容器。destroy 现在返回是否真从 docker 删除成功。
        # rm -f 对 runc 卡死的容器会失败（could not kill ... no exit event），
        # 此时 docker 里坏容器仍在，同名 create 必冲突——重建注定失败，
        # 不空转，直接报错提示需重启 docker 清理。
        try:
            destroyed_ok = await self.destroy_environment(env.env_id, success=False)
        except Exception as e:
            logger.warning(
                "[IsolationManager] setns 自愈：销毁旧环境异常 | env=%s | error=%s",
                env.env_id, e,
            )
            destroyed_ok = False

        if not destroyed_ok:
            logger.error(
                "[IsolationManager] setns 自愈：坏容器删不掉（runc 卡死），重建将同名冲突，"
                "放弃重建避免空转 | env=%s | 提示：需重启 docker 清理僵尸容器",
                env.env_id,
            )
            return ExecutionResult(
                success=False,
                output=None,
                error=(
                    f"容器命名空间脱节且无法删除（runc 卡死），已无法自愈。"
                    f"原错误: {original_error}。"
                    f"请在宿主执行 systemctl restart docker（或 service docker restart）"
                    f"清理僵尸容器后重试。"
                ),
                metadata={"namespace_desync_unremovable": True},
            )

        # 2. 重建（复用 get_or_create_environment，与 pre-exec 自愈同路径）
        try:
            new_env = await self.get_or_create_environment(**rebuild_kwargs)
        except Exception as e:
            logger.warning(
                "[IsolationManager] setns 自愈：重建环境失败，返回原失败结果 | error=%s", e,
            )
            return ExecutionResult(
                success=False,
                output=None,
                error=original_error,
                metadata={"namespace_desync_rebuild_failed": True},
            )

        # 3. 在新环境上单次重试
        provider = self._providers.get(new_env.level)
        if not provider:
            logger.warning(
                "[IsolationManager] setns 自愈：新环境无对应 provider | level=%s",
                new_env.level,
            )
            return ExecutionResult(
                success=False, output=None, error=original_error,
            )

        try:
            retry_result = await provider.execute_in_environment(new_env.env_id, operation)
        except Exception as e:
            logger.warning(
                "[IsolationManager] setns 自愈：重试执行抛异常，返回原失败结果 | error=%s", e,
            )
            return ExecutionResult(
                success=False,
                output=None,
                error=original_error,
                metadata={"namespace_desync_retry_failed": True},
            )

        if retry_result.success:
            logger.info(
                "[IsolationManager] setns 自愈重建成功 | old=%s | new=%s",
                env.env_id, new_env.env_id,
            )
            retry_result.metadata["namespace_desync_recovered"] = True
        else:
            logger.warning(
                "[IsolationManager] setns 自愈：重试仍失败 | new=%s | err_tail=%s",
                new_env.env_id,
                (retry_result.error or "")[:200],
            )
        return retry_result

    async def _ensure_env_healthy_or_rebuild(
        self,
        env: IsolationEnvironment,
        *,
        rebuild_kwargs: dict[str, Any],
    ) -> IsolationEnvironment:
        """执行前确保容器健康；异常态(created/exited/dead)时透明重建。

        DockerProvider.create_environment 曾丢弃 docker start 返回值，start 失败
        （WSL2 挂载脏路径）后容器卡在 created，却被标记 READY，导致后续 docker
        exec 报 "container is not running"。此处用 docker inspect 复核真实状态，
        非 READY 即销毁重建，对调用方透明。重建走 get_or_create_environment →
        create_environment（已含 start 失败删除+重试）。

        单次调用最多重建一次：重建返回的新 env 不再二次检查，直接交付执行；
        重建失败返回原 env，执行将以错误告终，不形成循环。

        Args:
            env: 当前环境
            rebuild_kwargs: get_or_create_environment 的入参（用于重建）

        Returns:
            READY 的环境；重建失败时返回原 env。
        """
        provider = self._providers.get(env.level)
        if provider is None:
            return env
        try:
            status = await provider.get_environment_status(env.env_id)
        except Exception as e:
            logger.warning(f"[IsolationManager] 健康检查失败，按原环境执行: {e}")
            return env
        if status == EnvironmentStatus.READY:
            return env

        logger.info(f"[IsolationManager] 容器非就绪，触发自愈重建 | env={env.env_id} | status={status.value}")
        try:
            await self.destroy_environment(env.env_id, success=False)
        except Exception as e:
            logger.warning(f"[IsolationManager] 自愈前销毁旧环境失败: {e}")

        try:
            new_env = await self.get_or_create_environment(**rebuild_kwargs)
            logger.info(f"[IsolationManager] 自愈重建完成 | old={env.env_id} | new={new_env.env_id}")
            return new_env
        except Exception as e:
            logger.error(f"[IsolationManager] 自愈重建失败，返回原环境: {e}")
            return env

    async def get_environment(self, env_id: str) -> IsolationEnvironment | None:
        """获取环境"""
        return self._environments.get(env_id)

    async def list_environments(
        self,
        task_id: str | None = None,
        level: IsolationLevel | None = None,
    ) -> list[IsolationEnvironment]:
        """列出环境"""
        envs = list(self._environments.values())

        if task_id:
            envs = [e for e in envs if e.context.task_id == task_id]

        if level:
            envs = [e for e in envs if e.level == level]

        return envs

    async def _check_providers_availability(
        self,
    ) -> dict[IsolationLevel, bool]:
        """检查所有提供者的可用性"""
        available = {}
        for level, provider in self._providers.items():
            is_avail, _ = await provider.is_available()
            available[level] = is_avail
            logger.debug(f"提供者 {level.value} 可用性: {is_avail}")

        return available

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        level_counts = {}
        for env in self._environments.values():
            level = env.level.value
            level_counts[level] = level_counts.get(level, 0) + 1

        return {
            "total_environments": len(self._environments),
            "level_counts": level_counts,
            "running": self._running,
        }


# 全局单例（带 asyncio.Lock 保护，防止并发创建）
_manager_lock = asyncio.Lock()
_global_manager: IsolationManager | None = None


async def get_isolation_manager(config_path: str | None = None) -> IsolationManager:
    """获取全局隔离管理器（线程安全，通过 asyncio.Lock 防止并发重复创建）"""
    global _global_manager  # noqa: PLW0603
    async with _manager_lock:
        if _global_manager is None:
            _global_manager = IsolationManager(config_path=config_path)
        return _global_manager


def get_isolation_manager_sync(config_path: str | None = None) -> IsolationManager:
    """同步获取全局隔离管理器单例。

    供 build_services 等同步初始化上下文使用（asyncio.get_event_loop()
    在 Python 3.12+ 同步上下文里会抛 RuntimeError）。
    与 async 版本共享同一个 _global_manager 单例。
    """
    global _global_manager  # noqa: PLW0603
    if _global_manager is None:
        _global_manager = IsolationManager(config_path=config_path)
    return _global_manager


async def start_isolation_manager():
    """启动全局隔离管理器"""
    manager = await get_isolation_manager()
    await manager.start()


async def stop_isolation_manager():
    """停止全局隔离管理器"""
    global _global_manager  # noqa: PLW0603
    async with _manager_lock:
        if _global_manager:
            await _global_manager.stop()
            _global_manager = None

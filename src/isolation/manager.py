"""
隔离环境管理器

暴露接口：
- async get_isolation_manager(config_path: str | None) -> IsolationManager：获取全局隔离管理器（线程安全）
- get_stats(self) -> dict[str, Any]：get_stats功能
- IsolationManager：IsolationManager类
"""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.isolation.decider import IsolationDecider
from src.isolation.providers.base import IsolationProvider
from src.isolation.providers.docker_provider import DockerProvider
from src.isolation.providers.host_provider import HostProvider
from src.isolation.types import (
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
    """从配置文件加载提供者配置"""
    config_path = Path("config/isolation/isolation_config.yaml")
    if not config_path.exists():
        logger.info(f"[IsolationManager] 配置文件不存在: {config_path}，使用默认配置")
        return {}

    try:
        import yaml

        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
            providers_config = config.get("providers", {})
            logger.info(f"[IsolationManager] 从配置文件加载提供者配置: {config_path}")
            return providers_config
    except Exception as e:
        logger.warning(f"[IsolationManager] 加载配置文件失败: {e}，使用默认配置")
        return {}


def _create_providers_from_config(
    providers_config: dict[str, Any] | None = None,
) -> dict[IsolationLevel, IsolationProvider]:
    """根据配置创建提供者实例"""
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
        providers[IsolationLevel.CONTAINER] = DockerProvider(
            config={
                "image": docker_config.get("image", "python:3.12-slim"),
                "memory_limit": docker_config.get("limits", {}).get("memory", "512m"),
                "cpu_limit": docker_config.get("limits", {}).get("cpus", "1.0"),
                "network_mode": docker_config.get("network_mode", "bridge"),
            },
        )
        logger.info(
            "[IsolationManager] 创建 DockerProvider: image=%s",
            docker_config.get("image", "python:3.12-slim"),
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

    def __init__(
        self,
        providers: dict[IsolationLevel, IsolationProvider] | None = None,
        decider: IsolationDecider | None = None,
        config_path: str | None = None,
        task_repository=None,
    ):
        """初始化管理器"""
        if providers is not None:
            self._providers = providers
        else:
            providers_config = None
            if config_path:
                try:
                    import yaml

                    with open(config_path, encoding="utf-8") as f:
                        config = yaml.safe_load(f) or {}
                        providers_config = config.get("providers", {})
                except Exception as e:
                    logger.warning(f"[IsolationManager] 加载配置文件失败: {e}")
            self._providers = _create_providers_from_config(providers_config)

        self._decider = decider or IsolationDecider()  # 内部自动创建 IsolationPolicyLoader
        self._task_repository = task_repository

        self._environments: dict[str, IsolationEnvironment] = {}
        self._reuse_map: dict[str, str] = {}
        self._root_task_env_map: dict[str, str] = {}
        self._running = False

    async def start(self):
        """启动管理器"""
        if self._running:
            return

        self._running = True

        await self._resume_containers()

        logger.info("隔离环境管理器已启动")

    async def _resume_containers(self):
        """恢复未完成任务的容器，清理已完成/不存在任务的容器

        项目启动时：
        1. 查找所有 cua-* 容器
        2. 从容器名提取 task_id，查询数据库验证任务状态
        3. 仅恢复数据库中存在且未完成的任务容器
        4. 已完成/不存在/被删除的任务容器直接销毁
        """
        try:
            from docker.errors import DockerException, NotFound

            import docker

            client = docker.from_env()
            containers = client.containers.list(all=True)

            active_task_ids = await self._load_active_task_ids()

            resumed_count = 0
            destroyed_count = 0
            for container in containers:
                if not container.name.startswith(self.CONTAINER_NAME_PREFIX):
                    continue

                task_id = container.name[len(self.CONTAINER_NAME_PREFIX) :]

                if task_id not in active_task_ids:
                    logger.info(
                        f"[IsolationManager] 任务 {task_id} 非活跃状态或不存在，"
                        f"销毁容器: {container.name}"
                    )
                    try:
                        container.stop(timeout=5)
                        container.remove()
                        destroyed_count += 1
                    except (DockerException, NotFound) as e:
                        logger.warning(
                            f"[IsolationManager] 销毁容器失败: {container.name}, 错误: {e}"
                        )
                    continue

                workspace_path = None
                mounts = container.attrs.get("Mounts", [])
                for mount in mounts:
                    if (
                        mount.get("Destination") == "/workspace"
                        and mount.get("Type") == "bind"
                    ):
                        workspace_path = mount.get("Source")
                        break

                if workspace_path:
                    from pathlib import Path

                    workspace_dir = Path(workspace_path)
                    if not workspace_dir.exists():
                        logger.info(
                            f"[IsolationManager] 工作空间目录不存在，正在创建: {workspace_path}"
                        )
                        workspace_dir.mkdir(parents=True, exist_ok=True)

                if container.status == "exited":
                    try:
                        container.start()
                        logger.info(f"[IsolationManager] 已恢复容器: {container.name}")
                        resumed_count += 1
                    except DockerException as e:
                        logger.warning(
                            f"[IsolationManager] 恢复容器失败: {container.name}, 错误: {e}"
                        )

            if resumed_count > 0:
                logger.info(f"[IsolationManager] 共恢复 {resumed_count} 个容器")
            if destroyed_count > 0:
                logger.info(f"[IsolationManager] 共销毁 {destroyed_count} 个无效容器")

            client.close()

        except Exception as e:
            logger.warning(f"[IsolationManager] 恢复容器失败: {e}")

    async def _load_active_task_ids(self) -> set[str]:
        """从数据库加载所有非终态任务的 ID 集合"""
        try:
            from sqlalchemy import select

            from src.core.states.execution import ExecutionStatus
            from infrastructure.db import get_session_context
            from src.db.models import Task

            terminal_statuses = {s.value for s in ExecutionStatus if s.is_terminal}
            async with get_session_context() as session:
                result = await session.execute(
                    select(Task.id).where(Task.status.notin_(terminal_statuses))
                )
                return {row[0] for row in result.all()}
        except Exception as e:
            logger.warning(f"[IsolationManager] 加载活跃任务ID失败: {e}")
            return set()

    async def stop(self):
        """停止管理器"""
        if not self._running:
            return

        self._running = False

        await self._stop_containers()

        logger.info("隔离环境管理器已停止")

    async def _stop_containers(self):
        """停止所有活跃任务的容器（不删除）

        项目关闭时，查找所有 cua-* 容器，
        仅停止数据库中非终态任务对应的容器
        """
        try:
            from docker.errors import DockerException

            import docker

            client = docker.from_env()
            containers = client.containers.list(all=True)

            active_task_ids = await self._load_active_task_ids()

            stopped_count = 0
            for container in containers:
                if not container.name.startswith(self.CONTAINER_NAME_PREFIX):
                    continue

                task_id = container.name[len(self.CONTAINER_NAME_PREFIX) :]
                if task_id not in active_task_ids:
                    continue

                if container.status == "running":
                    try:
                        container.stop(timeout=5)
                        logger.info(f"[IsolationManager] 已停止容器: {container.name}")
                        stopped_count += 1
                    except DockerException as e:
                        logger.warning(
                            f"[IsolationManager] 停止容器失败: {container.name}, 错误: {e}"
                        )

            if stopped_count > 0:
                logger.info(f"[IsolationManager] 共停止 {stopped_count} 个容器")

            client.close()

        except Exception as e:
            logger.warning(f"[IsolationManager] 停止容器失败: {e}")

    async def get_or_create_environment(
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
    ) -> IsolationEnvironment:
        """获取或创建隔离环境"""
        # 1. 查找根任务 ID（容器归属者）
        root_task_id = await self._find_root_task_id(task_id, parent_task_id)
        container_name = f"{self.CONTAINER_NAME_PREFIX}{root_task_id}"

        logger.info(
            f"[IsolationManager] 任务 {task_id} 的根任务为 {root_task_id}，容器名称: {container_name}"
        )

        # 2. 检查是否已有该根任务的容器（优先复用）
        if root_task_id in self._root_task_env_map:
            env_id = self._root_task_env_map[root_task_id]
            existing = self._environments.get(env_id)
            if existing and existing.status == EnvironmentStatus.READY.value:
                existing.last_used_at = datetime.now(UTC).isoformat()
                logger.info(
                    f"[IsolationManager] 复用根任务容器: {container_name} (env_id={env_id})"
                )
                return existing

        # 3. 尝试从 Docker 查找已有容器
        existing_env = await self._find_existing_container(container_name)
        if existing_env:
            self._environments[existing_env.env_id] = existing_env
            self._root_task_env_map[root_task_id] = existing_env.env_id
            logger.info(f"[IsolationManager] 恢复已有容器: {container_name}")
            return existing_env

        # 4. 检查是否可以复用父级环境
        if parent_env_id:
            existing = self._environments.get(parent_env_id)
            if existing and existing.status == EnvironmentStatus.READY.value:
                existing.last_used_at = datetime.now(UTC).isoformat()
                logger.debug(f"复用父级环境: {parent_env_id}")
                return existing

        # 5. 决策隔离级别
        available = await self._check_providers_availability()
        if isolation_level:
            level = isolation_level
            requires_approval = level == IsolationLevel.HOST
            logger.info(
                f"使用指定的隔离级别: {level.value} (requires_approval={requires_approval})"
            )
        else:
            # 使用决策器根据操作类型决策隔离策略
            tool_category = operation_type.value if operation_type else None
            policy = await self._decider.decide(
                tool_name=task_type.value,
                tool_category=tool_category,
                available_providers=available,
            )
            level = policy.isolation
            requires_approval = policy.approval
            logger.info(
                f"为任务 {task_id} 选择隔离级别: {level.value} (requires_approval={requires_approval})"
                f"(task_type={task_type.value}, operation_type={operation_type.value if operation_type else None})"
            )

        # 6. 创建新环境
        provider = self._providers.get(level)
        if not provider:
            raise RuntimeError(f"找不到 {level.value} 对应的提供者")

        context = IsolationContext(
            task_id=root_task_id,
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
            env = await provider.create_environment(
                context, container_name=container_name
            )
        else:
            env = await provider.create_environment(context)

        self._environments[env.env_id] = env
        self._root_task_env_map[root_task_id] = env.env_id

        logger.info(
            f"创建新隔离环境: {env.env_id} (level={level.value}, container_name={container_name})"
        )
        return env

    async def _find_root_task_id(
        self, task_id: str, parent_task_id: str | None = None
    ) -> str:
        """向上查找根任务 ID"""
        if not parent_task_id:
            return task_id

        if not self._task_repository:
            logger.warning(
                f"[IsolationManager] 未设置 task_repository，无法查找根任务，使用当前任务 ID: {task_id}"
            )
            return task_id

        try:
            current_task_id = parent_task_id
            max_depth = 10
            depth = 0

            while depth < max_depth:
                task = await self._task_repository.get_by_id(current_task_id)
                if not task:
                    logger.warning(
                        f"[IsolationManager] 任务不存在: {current_task_id}，返回当前任务 ID: {task_id}"
                    )
                    return task_id

                if not task.parent_task_id:
                    logger.info(
                        f"[IsolationManager] 找到根任务: {task.id} (depth={depth})"
                    )
                    return task.id

                current_task_id = task.parent_task_id
                depth += 1

            logger.warning(
                f"[IsolationManager] 查找根任务超过最大深度 {max_depth}，返回当前任务 ID: {task_id}"
            )
            return task_id

        except Exception as e:
            logger.error(f"[IsolationManager] 查找根任务失败: {e}")
            return task_id

    async def _find_existing_container(
        self, container_name: str
    ) -> IsolationEnvironment | None:
        """查找已存在的容器"""
        try:
            from docker.errors import NotFound

            import docker

            client = docker.from_env()

            try:
                container = client.containers.get(container_name)

                if container.status == "exited":
                    container.start()
                    logger.info(
                        f"[IsolationManager] 已恢复停止的容器: {container_name}"
                    )

                workspace_path = None
                mounts = container.attrs.get("Mounts", [])
                for mount in mounts:
                    if (
                        mount.get("Destination") == "/workspace"
                        and mount.get("Type") == "bind"
                    ):
                        workspace_path = mount.get("Source")
                        break

                if workspace_path:
                    from pathlib import Path

                    workspace_dir = Path(workspace_path)
                    if not workspace_dir.exists():
                        logger.info(
                            f"[IsolationManager] 工作空间目录不存在，正在创建: {workspace_path}"
                        )
                        workspace_dir.mkdir(parents=True, exist_ok=True)
                        logger.info(
                            f"[IsolationManager] 已创建工作空间目录: {workspace_path}"
                        )

                now = datetime.now(UTC)

                env = IsolationEnvironment(
                    env_id=container.id,
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

                client.close()
                return env

            except NotFound:
                client.close()
                return None

        except Exception as e:
            logger.warning(f"[IsolationManager] 查找容器失败: {e}")
            return None

    async def destroy_by_task_id(self, task_id: str, success: bool = True) -> None:
        """根据任务 ID 销毁关联的隔离容器

        优先通过内存映射查找，找不到则直接通过 Docker API 查找容器名删除。
        """
        root_task_id = task_id

        if self._task_repository:
            try:
                task = await self._task_repository.get_by_id(task_id)
                if task:
                    current_id = task_id
                    while task and task.parent_task_id:
                        current_id = task.parent_task_id
                        task = await self._task_repository.get_by_id(current_id)
                    root_task_id = current_id
            except Exception as e:
                logger.warning(
                    f"[IsolationManager] 查找根任务失败: {e}，使用当前任务 ID: {task_id}"
                )

        env_id = self._root_task_env_map.get(root_task_id)
        if env_id:
            logger.info(
                f"[IsolationManager] 任务 {task_id} (根任务 {root_task_id}) "
                f"通过内存映射销毁容器 {env_id}"
            )
            await self.destroy_environment(env_id, success=success)
            return

        await self._destroy_container_by_name(root_task_id)

    async def _destroy_container_by_name(self, root_task_id: str) -> None:
        """通过 Docker API 直接查找并删除容器"""
        container_name = f"{self.CONTAINER_NAME_PREFIX}{root_task_id}"
        try:
            from docker.errors import NotFound

            import docker

            client = docker.from_env()
            try:
                container = client.containers.get(container_name)
                container.stop(timeout=5)
                container.remove()
                logger.info(
                    f"[IsolationManager] 已通过 Docker API 删除容器: {container_name}"
                )
            except NotFound:
                logger.debug(
                    f"[IsolationManager] 容器不存在，无需删除: {container_name}"
                )
            finally:
                client.close()
        except Exception as e:
            logger.warning(
                f"[IsolationManager] 通过 Docker API 删除容器失败: "
                f"{container_name}, error={e}"
            )

    async def destroy_environment(self, env_id: str, success: bool = True) -> None:
        """销毁隔离环境"""
        env = self._environments.get(env_id)
        if not env:
            logger.warning(f"尝试销毁不存在的环境: {env_id}")
            return

        logger.info(f"销毁隔离环境: {env_id}")

        provider = self._providers.get(env.level)
        if provider:
            try:
                await provider.destroy_environment(env_id, success=success)
            except Exception as e:
                logger.error(f"提供者销毁环境失败: {e}")

        self._environments.pop(env_id, None)

        keys_to_remove = [k for k, v in self._reuse_map.items() if v == env_id]
        for key in keys_to_remove:
            del self._reuse_map[key]

        root_keys_to_remove = [
            k for k, v in self._root_task_env_map.items() if v == env_id
        ]
        for key in root_keys_to_remove:
            del self._root_task_env_map[key]

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
    ) -> ExecutionResult:
        """在隔离环境中执行操作"""
        # 获取或创建环境
        env = await self.get_or_create_environment(
            task_id=task_id,
            task_type=task_type,
            operation_type=operation_type,
            parent_env_id=parent_env_id,
            workspace=workspace,
            parent_workspace=parent_workspace,
            is_root_task=is_root_task,
            isolation_level=isolation_level,
        )

        logger.debug(
            f"在环境 {env.env_id} 中执行操作: {operation.get('type', 'unknown')}"
        )

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
    global _global_manager
    async with _manager_lock:
        if _global_manager is None:
            _global_manager = IsolationManager(config_path=config_path)
        return _global_manager


async def start_isolation_manager():
    """启动全局隔离管理器"""
    manager = await get_isolation_manager()
    await manager.start()


async def stop_isolation_manager():
    """停止全局隔离管理器"""
    global _global_manager
    async with _manager_lock:
        if _global_manager:
            await _global_manager.stop()
            _global_manager = None

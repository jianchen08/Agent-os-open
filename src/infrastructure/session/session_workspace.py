"""会话工作空间服务：会话级工作空间与隔离模式的管理。

职责边界（与任务级隔离体系解耦）：
- 只管理「会话」自己的工作空间与容器生命周期（创建/校验/归一化/销毁）
- 工作空间路径校验复用 isolation.workspace.validate_workspace_path（共享 util）
- 容器创建/销毁经由 IsolationManager 基础设施（按 workspace 幂等复用、`-v {目录}:/workspace` 挂载已有）
- 不感知任务系统：任务级隔离由 isolation_guard / task_executor 独立管理

会话隔离模式取值（与任务级 IsolationLevel 语义对齐）：
- non_isolated：宿主直接执行，危险操作走 security_check 审批链（默认）
- isolated：bash 进 Docker 容器执行，工作空间直挂 /workspace
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_VALID_LEVELS = ("isolated", "non_isolated")


def normalize_isolation_mode(isolation_mode: str | None) -> str:
    """归一化会话隔离模式：None/空 → 配置 session_default_level（默认 non_isolated）。

    会话级默认与任务级 coordinator.default_level 相互独立：
    任务默认保持 isolated（现有语义不变），会话默认 non_isolated（读取放行+危险阻拦）。
    """
    if isolation_mode in _VALID_LEVELS:
        return isolation_mode
    try:
        from config.config_center import get_config_center  # noqa: PLC0415

        cfg = get_config_center().get("isolation/isolation_config.yaml") or {}
        level = (cfg.get("coordinator") or {}).get("session_default_level")
        if level in _VALID_LEVELS:
            return level
    except Exception as e:
        logger.warning("[SessionWorkspace] 读取会话默认隔离级别失败，回退 non_isolated | error=%s", e)
    return "non_isolated"


def validate_workspace(workspace: str) -> str | None:
    """校验会话工作空间路径安全性。

    Returns:
        校验通过返回 None；不通过返回错误信息字符串
    """
    if not workspace or not workspace.strip():
        return "工作空间路径不能为空。"
    try:
        from isolation.workspace import validate_workspace_path  # noqa: PLC0415

        return validate_workspace_path(workspace)
    except Exception as e:
        logger.warning("[SessionWorkspace] 工作空间校验异常 | error=%s", e)
        return None


class SessionWorkspaceService:
    """会话工作空间服务（全局单例语义：纯静态方法，无内部状态）。

    容器生命周期（惰性创建 + 会话删除销毁）：
    - 容器在会话内第一次 bash_execute 时才创建（session_isolation 插件调用本服务），
      避免创建会话即拉起容器的启动开销
    - 会话删除时 destroy_session_container 幂等销毁（同 workspace 容器共享语义
      与 IsolationManager 一致：容器名由 workspace 决定）
    """

    @staticmethod
    def normalize_isolation_mode(isolation_mode: str | None) -> str:
        """归一化会话隔离模式（见模块级函数）。"""
        return normalize_isolation_mode(isolation_mode)

    @staticmethod
    def validate_workspace(workspace: str) -> str | None:
        """校验会话工作空间路径安全性（见模块级函数）。"""
        return validate_workspace(workspace)

    @staticmethod
    async def get_or_create_session_container(workspace: str) -> str | None:
        """为会话工作空间获取/创建容器 env_id（幂等，同 workspace 复用）。

        Args:
            workspace: 会话工作空间绝对路径

        Returns:
            容器 env_id（= 容器名）；失败返回 None，调用方降级到宿主执行
        """
        if not workspace:
            return None
        try:
            from isolation.manager import get_isolation_manager  # noqa: PLC0415
            from isolation.types import TaskType  # noqa: PLC0415

            manager = await get_isolation_manager()
            env = await manager.get_or_create_environment(
                task_id="session_workspace",
                task_type=TaskType.ATOMIC,
                operation_type=None,
                workspace=workspace,
                tool_name="bash_execute",
            )
            if env and env.env_id:
                logger.info(
                    "[SessionWorkspace] 会话容器就绪 | ws=%s env_id=%s",
                    workspace,
                    env.env_id,
                )
                return env.env_id
            logger.warning("[SessionWorkspace] 会话容器返回空 env_id | ws=%s", workspace)
        except Exception as e:
            logger.warning("[SessionWorkspace] 获取会话容器失败 | ws=%s | error=%s", workspace, e)
        return None

    @staticmethod
    async def destroy_session_container(workspace: str) -> None:
        """销毁会话工作空间对应的容器（幂等，不存在视为成功）。

        容器名 = IsolationManager 按 workspace 生成的 `cua-{目录名}`，
        与 get_or_create_environment 的命名规则一致。
        """
        if not workspace:
            return
        try:
            from isolation.manager import get_isolation_manager  # noqa: PLC0415

            manager = await get_isolation_manager()
            env_id = manager._workspace_to_container_name(workspace, "session")
            destroyed = await manager.destroy_environment(env_id)
            logger.info("[SessionWorkspace] 会话容器销毁 | ws=%s env_id=%s destroyed=%s", workspace, env_id, destroyed)
        except Exception as e:
            logger.warning("[SessionWorkspace] 销毁会话容器失败 | ws=%s | error=%s", workspace, e)


def get_session_workspace_service() -> SessionWorkspaceService:
    """获取会话工作空间服务（无状态，直接返回类实例）。"""
    return SessionWorkspaceService()

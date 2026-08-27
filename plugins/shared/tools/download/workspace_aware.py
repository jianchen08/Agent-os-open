"""Download 插件的 workspace 消费 Mixin——SDK 公共基座 + 路径权限校验扩展层。

路径解析、项目根推断、工作目录获取等通用能力单一真值源在
``agentos_plugin_sdk.workspace_aware``；本文件只承载 download 特有的
路径权限校验面：check_path_allowed 依赖插件自带的 permission_policy /
permission_checker 平铺策略模块（与 server.py 同目录）做读/写决策。

暴露接口：
- WorkspaceAwareMixin（SDK 基类的 download 扩展）：在公共能力之上提供
  统一路径权限校验入口 check_path_allowed，按 permission_policies 声明
  （root_task / subtask / default）决策。
"""

import logging

from agentos_plugin_sdk.workspace_aware import (
    WorkspaceAwareMixin as _BaseWorkspaceAwareMixin,
)

_logger = logging.getLogger(__name__)

# 策略层不可用（自带策略模块缺失）的一次性告警开关：fail-closed 拒绝所有路径操作，
# 但告警只发一次，避免每个请求重复刷屏。
_policy_unavailable_warned = False


def _warn_policy_unavailable(detail: str) -> None:
    """策略层不可用时一次性 warning 留痕（拒绝本身每次照常执行）。"""
    global _policy_unavailable_warned
    if _policy_unavailable_warned:
        return
    _policy_unavailable_warned = True
    _logger.warning(
        "[workspace_aware] 路径权限校验层不可用，已按 fail-closed 拒绝后续"
        "路径操作（仅提示一次）| detail=%s",
        detail,
    )


class WorkspaceAwareMixin(_BaseWorkspaceAwareMixin):
    """工作空间感知 Mixin（download 扩展版）。

    继承 SDK 公共基座的 workspace 消费逻辑（_init_workspace / resolve_path /
    _format_output_path / get_working_dir / _infer_project_root），并新增
    统一的路径权限校验入口 check_path_allowed：所有工具无需各自实现
    workspace 范围检查，调用此方法即可按 permission_policies 声明
    （root_task / subtask / default）决策。
    """

    # ── 权限策略管理器（类级缓存，避免每次调用重新解析配置）──
    _policy_manager = None

    @classmethod
    def _get_policy_manager(cls):
        """获取缓存的 PermissionPolicyManager 单例（从配置文件加载策略）。

        策略模块为插件自带的平铺镜像（permission_policy.py，与 server.py 同目录，
        sys.path 已含插件目录）。加载失败返回 None，调用方按 fail-closed 拒绝。
        """
        if cls._policy_manager is None:
            try:
                from permission_policy import PermissionPolicyManager  # noqa: PLC0415

                cls._policy_manager = PermissionPolicyManager()
            except Exception as e:
                _warn_policy_unavailable(f"permission_policy 加载失败: {e!r}")
                return None
        return cls._policy_manager

    def check_path_allowed(
        self,
        path: str,
        operation: str = "read",
        agent_level: int | str | None = None,
    ) -> tuple[bool, str]:
        """统一的路径权限校验入口。

        根据 agent 层级选取对应策略（L1/缺省→root_task, L2+→subtask），
        再按操作类型（read/write）调用 PermissionChecker 决策。
        通过返回 (True, "")，拒绝返回 (False, 错误原因)。

        fail-closed 契约：策略层不可用（模块缺失/加载失败）时拒绝路径操作
        并给出可传播给用户的错误原因——安全控制的失效模式是拒绝而不是放行。

        Args:
            path: 待校验的文件路径（绝对路径或相对于 project_root 的相对路径）
            operation: "read" 或 "write"
            agent_level: 调用方 agent 层级（1=主agent, 2+=子任务, None=按L1处理）

        Returns:
            (通过与否, 错误描述)
        """
        # 确保 workspace/project_root 已初始化（由基座 _init_workspace 赋值）
        workspace = getattr(self, "_workspace", None)
        project_root = getattr(self, "_project_root", None)
        if workspace is None or project_root is None:
            return False, "workspace 未初始化，无法校验路径权限"

        policy_manager = self._get_policy_manager()
        if policy_manager is None:
            return False, "路径权限校验层不可用（权限策略模块加载失败），已按安全策略拒绝该操作"

        policy_name = policy_manager.get_policy_name_for_agent_level(agent_level)
        policy = policy_manager.get_policy(policy_name)

        try:
            from permission_checker import PermissionChecker  # noqa: PLC0415

            checker = PermissionChecker(str(project_root))

            if operation == "write":
                ok, err = checker.check_write_permission(
                    path,
                    str(workspace),
                    policy,
                )
            else:
                ok, err = checker.check_read_permission(
                    path,
                    str(workspace),
                    policy,
                )
            return ok, err
        except Exception as e:
            _warn_policy_unavailable(f"permission_checker 执行失败: {e!r}")
            return False, f"路径权限校验执行失败，已按安全策略拒绝该操作: {e}"

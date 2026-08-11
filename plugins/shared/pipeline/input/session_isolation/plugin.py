"""会话级隔离守卫 Input 插件。

主会话（无任务上下文）的容器执行决策——与任务级 isolation_guard 解耦：

- isolation_guard：任务管道（有 task_id）的工具级隔离决策（task metadata 驱动，
  写 execution_contexts，tool_core 消费）
- 本插件：主会话管道（无 task_id）的会话级隔离决策（会话 isolation_level 驱动，
  直接注入 bash 工具的 _container_id，走 bash 已有容器 backend 通路）

两者互不感知、互不修改；容器执行统一落在 bash 工具的 _container_id 通路上
（tool.py 判定 is_isolated = bool(container_id) 后走 docker exec + 轮询流程）。

生效条件（全部满足才干预）：
1. core_type == tool_execute
2. state 无 task_id（主会话；子任务管道由 isolation_guard 管理）
3. state.workspace 非空（会话绑定了工作空间）
4. state.isolation_level == "isolated"（会话显式选择容器隔离）

non_isolated / 未绑定工作空间 / 任务管道：完全不干预。

容器内固定挂载 workspace → /workspace，因此：
- 未显式指定 working_dir 时补 /workspace（宿主路径在容器内不存在）
- 显式指定宿主路径时保留原样（容器内报错由 LLM 自行调整）
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class SessionIsolationPlugin(IInputPlugin):
    """会话级隔离守卫 Input 插件。"""

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化会话级隔离守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用（默认 True）
                - priority: 插件优先级（默认 25，在 param_inject 之后执行）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "session_isolation"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 25)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行会话级隔离决策：为主会话的 bash_execute 注入容器执行上下文。"""
        if not self._enabled:
            return PluginResult()

        state = ctx.state

        if state.get(StateKeys.CORE_TYPE) != "tool_execute":
            return PluginResult()

        # 只处理主会话（无任务上下文）；任务管道由 isolation_guard 独立管理
        if state.get(StateKeys.TASK_ID):
            return PluginResult()

        workspace = state.get("workspace", "") or ""
        isolation_level = state.get("isolation_level", "") or ""
        if not workspace or isolation_level != "isolated":
            return PluginResult()

        tool_calls = state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return PluginResult()

        # 经 SessionWorkspaceService 幂等获取/创建会话容器（同 workspace 复用，
        # `-v {workspace}:/workspace` 挂载由 IsolationManager/DockerProvider 完成）
        # infrastructure.session.session_workspace 是 0.1 模块，已归档为 reference/0.1_src/
        # （参考文件）。0.2 环境下不可 import → 走 fallback：不做会话级容器隔离，
        # 工具调用按原样透传（由后续 isolation_guard 等插件做宿主/容器决策）。
        try:
            from infrastructure.session.session_workspace import (  # noqa: PLC0415
                SessionWorkspaceService,
            )
        except ImportError:
            logger.debug(
                "[%s] infrastructure.session.session_workspace 不可用（0.1 已归档），"
                "跳过会话级容器隔离 | ws=%s",
                self.name,
                workspace,
            )
            return PluginResult()

        container_id = await SessionWorkspaceService.get_or_create_session_container(workspace)
        if not container_id:
            logger.warning(
                "[%s] 会话容器不可用，降级宿主执行 | ws=%s",
                self.name,
                workspace,
            )
            return PluginResult()

        injected_calls = []
        injected_count = 0
        for tc in tool_calls:
            injected_tc = dict(tc)
            if injected_tc.get("name") != "bash_execute":
                injected_calls.append(injected_tc)
                continue

            args = injected_tc.get("args", injected_tc.get("arguments", {}))
            if isinstance(args, str):
                try:
                    import json  # noqa: PLC0415

                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {}
            if not isinstance(args, dict):
                args = {}

            args["_container_id"] = container_id
            # 容器内固定挂载 /workspace：未显式指定 working_dir 时补容器路径
            if not args.get("working_dir"):
                args["working_dir"] = "/workspace"
            injected_tc["args"] = args
            injected_calls.append(injected_tc)
            injected_count += 1

        if not injected_count:
            return PluginResult()

        logger.info(
            "[%s] 主会话 bash_execute 路由到容器 | ws=%s env_id=%s tools=%d",
            self.name,
            workspace,
            container_id,
            injected_count,
        )
        return PluginResult(state_updates={StateKeys.RAW_TOOL_CALLS: injected_calls})

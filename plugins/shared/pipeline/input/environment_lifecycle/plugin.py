"""执行环境生命周期 Input 插件。

挂载在管道的 init / exit 循环体（多循环体模型），按 `state["current_phase"]`
分发两个阶段。**插件自己持有环境服务**（IsolationManager），自己解析/
创建、给 agent 使用、自己清理——不依赖跨进程 capability：

- **init（resolver）**：消费 `state.execution_context.isolation`
  （`{level: isolated|non_isolated}`）解析环境基线写入 `state.environment_basis`
  ——循环内的 isolation_guard（薄选择器）据此查表决策，不再运行期查
  task_service。容器创建按需交给 isolation_guard / 工具执行路径（tool_core
  经 isolation.* 工具调环境服务），此处只做基线与可达性检查。
- **exit（release）**：有任务与环境基线时调 `destroy_by_task_id` 销毁容器
  （幂等，失败留痕不阻断）。

与工作空间解耦：本插件只管执行环境（容器/宿主），"在哪个目录执行"由
workspace_lifecycle 负责。

State 命名空间：
    - environment_basis：init 阶段写入（isolation_guard 消费）
    - environment_released：exit 阶段写入
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


def _ensure_isolation_path() -> None:
    """把 isolation 插件目录加入 sys.path（IsolationManager 所在）。"""
    _here = Path(__file__).resolve().parent
    _system_dir = _here.parents[2] / "system"
    _iso_dir = _system_dir / "isolation"
    for _p in (str(_system_dir), str(_iso_dir)):
        if _p not in sys.path:
            sys.path.insert(0, _p)


class EnvironmentLifecyclePlugin(IInputPlugin):
    """执行环境生命周期插件：init 解析环境基线，exit 销毁环境（自持服务）。"""

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化。

        Args:
            config: 插件配置（环境定义注册表 / config_path 等）
        """
        self._config = config or {}
        self._manager: Any = None

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "environment_lifecycle"

    @property
    def priority(self) -> int:
        """插件执行优先级（init/exit 体单次执行，置于链首）。"""
        return self._config.get("priority", 6)

    # ── 服务对象（懒加载，插件进程内自持）──────────────────────

    def _get_manager(self) -> Any | None:
        """懒加载 IsolationManager（服务不可用时返回 None，降级只写基线）。"""
        if self._manager is not None:
            return self._manager
        try:
            _ensure_isolation_path()
            from isolation.manager import IsolationManager  # noqa: PLC0415

            self._manager = IsolationManager(
                config_path=self._config.get("config_path"),
            )
            logger.info("[EnvironmentLifecycle] 环境服务已实例化")
        except Exception as exc:
            logger.warning(
                "[EnvironmentLifecycle] 环境服务实例化失败，降级只写基线 | error=%s",
                exc,
            )
            self._manager = None
        return self._manager

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """按 current_phase 分发 init/exit 阶段。"""
        phase = ctx.state.get("current_phase", "")
        if phase == "init":
            return self._resolve(ctx)
        if phase == "exit":
            return await self._release(ctx)
        # 其它循环体（main）：生命周期插件不参与，零产出
        return PluginResult()

    # ── init：解析环境基线 ──────────────────────────────────────

    def _resolve(self, ctx: PluginContext) -> PluginResult:
        """从 execution_context.isolation 解析环境基线写入 state。

        幂等：state 已存在 environment_basis 时跳过。容器创建按需延迟到
        工具执行路径（isolation_guard / isolation.create_env），此处只解析。
        """
        state = ctx.state
        if state.get("environment_basis"):
            logger.debug(
                "[EnvironmentLifecycle] environment_basis 已就位，跳过解析 | basis=%s",
                state["environment_basis"],
            )
            return PluginResult()

        ec = state.get("execution_context")
        if not isinstance(ec, dict):
            logger.debug("[EnvironmentLifecycle] 无 execution_context，跳过环境解析")
            return PluginResult()
        iso_spec = ec.get("isolation")
        if not isinstance(iso_spec, dict):
            logger.debug("[EnvironmentLifecycle] execution_context 无 isolation 声明，跳过")
            return PluginResult()

        level = iso_spec.get("level") or ""
        if level not in ("isolated", "non_isolated"):
            logger.debug("[EnvironmentLifecycle] isolation level 非法或缺失: %r，跳过", level)
            return PluginResult()

        # 服务可达性探测（仅记录，不创建——创建按需延迟）
        service_ready = self._get_manager() is not None
        updates: dict[str, Any] = {
            "environment_basis": {
                "level": level,
                "resolved": True,
                "service_ready": service_ready,
            }
        }
        logger.info(
            "[EnvironmentLifecycle] init 解析环境基线 | level=%s | service_ready=%s",
            level,
            service_ready,
        )
        return PluginResult(state_updates=updates)

    # ── exit：销毁环境（服务自持，真实执行）────────────────────

    async def _release(self, ctx: PluginContext) -> PluginResult:
        """环境释放：销毁任务容器（幂等，失败留痕不阻断）。"""
        state = ctx.state
        if not state.get("environment_basis"):
            return PluginResult()
        # 0.2 统一：任务身份 = pipeline_id，引擎注入 state 的扁平键是 task.id
        task_id = state.get("task_id") or state.get("task.id") or ""
        if not task_id:
            # 主会话（无任务）：容器由会话生命周期管理，此处不销毁
            return PluginResult(state_updates={"environment_released": True})
        manager = self._get_manager()
        if manager is None:
            return PluginResult(state_updates={"environment_released": False})
        try:
            await ctx_await(manager.destroy_by_task_id, task_id)
            logger.info("[EnvironmentLifecycle] exit 销毁环境 | task=%s", task_id)
            return PluginResult(state_updates={"environment_released": True})
        except Exception as exc:
            logger.warning(
                "[EnvironmentLifecycle] exit 销毁环境失败（留痕不阻断）| task=%s | error=%s",
                task_id,
                exc,
            )
            return PluginResult(state_updates={"environment_released": False})


async def ctx_await(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """同步/异步统一调用（服务方法是同步的，跑在线程池避免阻塞事件循环）。"""
    import asyncio  # noqa: PLC0415

    return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kwargs))

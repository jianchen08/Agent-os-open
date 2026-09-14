"""执行环境生命周期 Input 插件。

挂载在管道的 init / exit 循环体（多循环体模型），按 `state["current_phase"]`
分发两个阶段：

- **init（resolver）**：消费 `state.execution_context.isolation`
  （`{level: isolated|non_isolated}`）解析环境基线写入 `state.environment_basis`
  ——循环内的 isolation_guard（薄选择器）据此查表决策，不再运行期查
  task_service。容器创建按需交给 isolation_guard / 工具执行路径（tool_core
  经 isolation.* 工具调环境服务），此处只做基线与可达性检查。
- **exit（release）**：有任务与环境基线时经 tool-executor 正门调
  isolation_service 的 `isolation.destroy_env` 销毁容器（幂等，失败留痕不阻断）。

与工作空间解耦：本插件只管执行环境（容器/宿主），"在哪个目录执行"由
workspace_lifecycle 负责。

State 命名空间：
    - environment_basis：init 阶段写入（isolation_guard 消费）
    - environment_released：exit 阶段写入
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult

logger = logging.getLogger(__name__)

# 销毁调用方（server.py on_load 注入）：async (tool_name, args) -> Any，
# 经 tool-executor.invoke 以显式 plugin_id 直达 isolation_service——系统插件
# 工具不在 LLM 工具注册表，反查必失败，须带 plugin_id（同 hindsight.recall 惯例）。
DestroyCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
_destroy_caller: DestroyCaller | None = None


def set_destroy_caller(caller: DestroyCaller | None) -> None:
    """注入/清除销毁调用方（server.py on_load 构造；None = 能力未注入，降级）。"""
    global _destroy_caller
    _destroy_caller = caller


class EnvironmentLifecyclePlugin(IInputPlugin):
    """执行环境生命周期插件：init 解析环境基线，exit 经 capability 销毁环境。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化。

        Args:
            config: 插件配置（priority 等）
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "environment_lifecycle"

    @property
    def priority(self) -> int:
        """插件执行优先级（init/exit 体单次执行，置于链首）。"""
        return self._config.get("priority", 6)

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

        # 服务可达性 = 销毁调用方已注入（不做网络/进程探测，创建按需延迟）
        service_ready = _destroy_caller is not None
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

    # ── exit：销毁环境（capability 正门，幂等失败留痕不阻断）────

    async def _release(self, ctx: PluginContext) -> PluginResult:
        """环境释放：销毁任务容器（幂等，失败留痕不阻断）。"""
        state = ctx.state
        if not state.get("environment_basis"):
            return PluginResult()
        # 0.2 统一：任务身份 = pipeline_id，引擎注入 state 的扁平键是 task.id
        task_id = state.get("task.id") or ""
        if not task_id:
            # 主会话（无任务）：容器由会话生命周期管理，此处不销毁
            return PluginResult(state_updates={"environment_released": True})
        caller = _destroy_caller
        if caller is None:
            return PluginResult(state_updates={"environment_released": False})
        # state 真值通道：isolation.container_name 是落地时写入的容器名，
        # 服务重启后内存登记为空也能按名删除（D6：杜绝登记丢失=泄漏）。
        container_name = state.get("isolation.container_name") or ""
        try:
            raw = await caller(
                "isolation.destroy_env",
                {"task_id": task_id, "container_name": container_name},
            )
        except Exception as exc:
            logger.warning(
                "[EnvironmentLifecycle] exit 销毁环境失败（留痕不阻断）| task=%s | error=%s",
                task_id,
                exc,
            )
            return PluginResult(state_updates={"environment_released": False})
        # isolation.destroy_env 约定业务失败以 {"error": ...} 返回而非抛异常
        if isinstance(raw, dict) and raw.get("error"):
            logger.warning(
                "[EnvironmentLifecycle] exit 销毁环境被拒绝（留痕不阻断）| task=%s | error=%s",
                task_id,
                raw["error"],
            )
            return PluginResult(state_updates={"environment_released": False})
        logger.info("[EnvironmentLifecycle] exit 销毁环境 | task=%s", task_id)
        return PluginResult(state_updates={"environment_released": True})

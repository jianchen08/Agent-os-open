"""管道引擎 — 核心循环和生命周期管理。"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time as _time
import uuid as _uuid
from typing import TYPE_CHECKING, Any

from pipeline.engine_chain import (  # noqa: F401
    execute_core_plugin,
    execute_input_chain,
    execute_output_chain,
    handle_no_route_signals,
    run_post_end_output_chain,
)
from pipeline.engine_iteration import IterationAction, run_iteration  # noqa: F401
from pipeline.engine_route import (  # noqa: F401
    apply_route,
    resolve_output_plugins,
)
from pipeline.plugin_resolver import apply_agent_model_override
from pipeline.registry import PluginRegistry, get_engine_registry
from pipeline.route import InputRouteTable, OutputRouteTable
from pipeline.types import StateKeys

if TYPE_CHECKING:
    from infrastructure.checkpoint.pipeline_checkpoint import PipelineCheckpointManager


logger = logging.getLogger(__name__)


class PipelineEngine:
    """管道引擎。"""

    def __init__(
        self,
        input_route_table: InputRouteTable,
        output_route_table: OutputRouteTable,
        plugin_registry: PluginRegistry,
        services: dict[str, Any] | None = None,
        max_iterations: int = 500,
        agent_registry: Any | None = None,
        checkpoint_manager: PipelineCheckpointManager | None = None,
    ) -> None:
        self.input_route_table = input_route_table

        self.output_route_table = output_route_table

        self.plugin_registry = plugin_registry.fork()

        self._services = services or {}

        self.max_iterations = max_iterations

        # agent_registry 默认使用全局单例，确保热重载对所有 engine 生效

        if agent_registry is not None:
            self._agent_registry = agent_registry

        else:
            from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415

            self._agent_registry = get_global_agent_registry_sync()

        self._suspended_state: dict[str, Any] | None = None

        self._checkpoint_manager = checkpoint_manager

        self._pipeline_id: str = _uuid.uuid4().hex[:12]

        self._wake_event: asyncio.Event | None = None

        self._engine_loop: asyncio.AbstractEventLoop | None = None

        self._watching_task_ids: list[str] = []

        self._consecutive_core_errors: int = 0

        self._max_consecutive_core_errors: int = 3

        self._last_state: dict[str, Any] | None = None

        self._current_state: dict[str, Any] | None = None

        self._agent_config: Any | None = None

        self._running: bool = False

        self._run_started: bool = False

        # 流式输出口（output port）：bridge、chunk 队列、消费者/keepalive 协程、
        # 流式上下文全部委托给 StreamingOutput，引擎核心不再持有这些传输层状态。
        # stop_check 回调注入协作式停止判定，保持单向依赖（streaming 不反向依赖引擎）。
        from pipeline.engine_streaming import StreamingOutput  # noqa: PLC0415

        self._streaming: StreamingOutput = StreamingOutput(
            self._pipeline_id,
            self._is_stop_signal_active,
        )

        # per-pipeline 日志管理（横切基础设施）：FileHandler 创建/关闭、contextvar 绑定、
        # 防重复守卫全部委托给 PipelineLogger，引擎核心不再持有日志层细节。
        from pipeline.engine_logging import PipelineLogger  # noqa: PLC0415

        self._pipeline_logger: PipelineLogger = PipelineLogger()

        self._preserved_bridge: Any = None

        self._preserved_drain_task: Any = None

        # 在 unregister/register 循环中保留 engine_task 引用

        self._preserved_engine_task: Any = None

        # 保存旧 entry 的 msg_sequence，避免 sequence 重置

        self._preserved_msg_sequence: int = 0

        # 统一注入队列，所有 external 通知通过 inject_message 写入。
        # 元素为 (message, source) —— source 必须随消息入队，
        # 供 consume_pending_notifications 区分 user 注入与 system 通知。

        self._inject_queue: list[tuple[str, str]] = []

        # 前端乐观消息 ID，供 track 插件持久化时写入 user_record

        self._pending_client_message_id: str = ""

    async def run(
        self,
        user_input: str | None = None,
        agent_config: Any | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        initial_state: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        **extra_state: Any,
    ) -> dict[str, Any]:
        """执行管道。"""

        # 每次新 run() 调用重置挂起状态，防止引擎复用时旧状态泄漏

        self._suspended_state = None

        self._wake_event = None

        self._streaming.reset_for_run()

        # 保留当前 entry 中的 bridge 和 drain_task 引用，

        # 避免下面 _run_loop register 时丢失 idle 阶段绑定的流式桥接。

        # 保留 engine_task 引用，确保 suspended 路径下次 ensure_bridge 可用

        _preserved_bridge = None

        _preserved_drain_task = None

        _preserved_engine_task: Any = None

        _preserved_msg_sequence: int = 0

        if self._pipeline_id:
            _old_entry = get_engine_registry().get(self._pipeline_id)

            if _old_entry is not None:
                _preserved_bridge = _old_entry.bridge

                _preserved_drain_task = _old_entry.drain_task

                _preserved_engine_task = _old_entry.engine_task

                _preserved_msg_sequence = _old_entry.msg_sequence

        self._preserved_bridge = _preserved_bridge

        self._preserved_drain_task = _preserved_drain_task

        self._preserved_engine_task = _preserved_engine_task

        self._preserved_msg_sequence = _preserved_msg_sequence

        # pipeline_id 由引擎构造时确定，外部不可覆盖。

        extra_state["pipeline_id"] = self._pipeline_id

        raw_state = None

        if initial_state is not None and user_input is None:
            raw_state = initial_state

        elif isinstance(user_input, dict) and initial_state is None:
            raw_state = user_input

        if raw_state is not None:
            state: dict[str, Any] = {
                **raw_state,
                StateKeys.ITERATION: 0,
                StateKeys.ENDED: False,
            }

            if StateKeys.PIPELINE_ID not in state:
                state[StateKeys.PIPELINE_ID] = self._pipeline_id

            return await self._run_loop(state, resumed=False)

        if agent_config is None:
            agent_config = self._load_config_from_tags()
        if agent_config is None:
            raise ValueError(
                "PipelineEngine.run() 无法确定 Agent 配置：既未显式传入 "
                "agent_config，也无法从注册表 tags 解析 agent_id。"
                "禁止静默回退到默认 Agent。"
            )

        from pipeline.state_builder import build_initial_state  # noqa: PLC0415

        state = build_initial_state(
            user_input=user_input or "",
            agent_config=agent_config,
            conversation_history=conversation_history,
            pipeline_id=self._pipeline_id,
            services=self._services,
            extra_state=extra_state,
            attachments=attachments,
        )

        if agent_config and hasattr(agent_config, "max_iterations") and agent_config.max_iterations:
            self.max_iterations = agent_config.max_iterations

        self._agent_config = agent_config

        from pipeline.plugin_resolver import apply_agent_plugin_configs  # noqa: PLC0415

        apply_agent_plugin_configs(self.plugin_registry, agent_config)

        apply_agent_model_override(self.plugin_registry, agent_config, self._services)

        return await self._run_loop(state, resumed=False)

    def _load_config_from_tags(self) -> Any | None:
        """从注册表 entry.tags 加载 agent_config（I5：一切上下文皆 tags）。"""
        try:
            from pipeline.engine_registry import get_engine_registry  # noqa: PLC0415

            entry = get_engine_registry().get(self._pipeline_id)
            if entry is None:
                return None
            agent_id = entry.tags.get("agent_id", "")
            if not agent_id or self._agent_registry is None:
                return None
            return self._agent_registry.get(agent_id)
        except Exception as exc:
            logger.debug(
                "[Engine] 从 tags 加载 agent_config 失败 (pipeline=%s): %s",
                self._pipeline_id[:12],
                exc,
            )
            return None

    async def resume(self) -> dict[str, Any]:
        """从暂停状态恢复管道执行。"""

        if self._suspended_state is None:
            raise RuntimeError("No suspended state to resume from")

        saved_state = self._suspended_state

        self._suspended_state = None

        logger.debug(
            "Pipeline resuming from suspended state (iteration=%d)",
            saved_state.get(StateKeys.ITERATION, 0),
        )

        return await self._run_loop(saved_state, resumed=True)

    async def _run_loop(self, state: dict[str, Any], *, resumed: bool = False) -> dict[str, Any]:  # noqa: PLR0912,PLR0915
        """管道核心循环。"""

        # per-run contextvar token，供 finally 重置（日志 contextvar 由 PipelineLogger 管理）
        _pipeline_id_token: contextvars.Token | None = None

        self._running = True

        self._run_start_time = _time.monotonic()

        # Phase 1: set_trace_id 接入现有日志追踪机制

        try:
            from monitoring.logging_config import set_trace_id  # noqa: PLC0415

            set_trace_id(self._pipeline_id)

        except Exception:
            pass

        logger.info(
            "[Engine] 引擎启动: pipeline=%s task_id=%s",
            self._pipeline_id[:12],
            state.get("task_id", "?"),
        )

        pipeline_run_id = state.get(StateKeys.PIPELINE_ID, self._pipeline_id)

        self._pipeline_id = pipeline_run_id

        _pipeline_id_token = self._pipeline_logger.bind_context(pipeline_run_id)

        # 重置连续错误计数器

        self._consecutive_core_errors = 0

        if not resumed:
            self._streaming.save_context(state)

        else:
            self._streaming.restore_context(state)

        # 引擎不自我注册——注册是创建者的职责。只恢复 preserved 属性到已有 entry。

        _reg_entry = get_engine_registry().get(pipeline_run_id)

        if _reg_entry is not None:
            if self._preserved_bridge is not None and _reg_entry.bridge is None:
                _reg_entry.bridge = self._preserved_bridge

            if self._preserved_drain_task is not None and _reg_entry.drain_task is None:
                _reg_entry.drain_task = self._preserved_drain_task

            if self._preserved_engine_task is not None and _reg_entry.engine_task is None:
                _reg_entry.engine_task = self._preserved_engine_task

            if self._preserved_engine_task is not None:
                logger.debug(
                    "[Engine] 恢复 preserved engine_task: pipeline=%s has_task=%s",
                    pipeline_run_id[:12],
                    not self._preserved_engine_task.done(),
                )

        self._preserved_bridge = None

        self._preserved_drain_task = None

        self._preserved_engine_task = None

        # 恢复旧 entry 的 msg_sequence，避免 sequence 从 0 重新开始

        if self._preserved_msg_sequence > 0:
            _reg_entry.init_sequence(self._preserved_msg_sequence)

        self._preserved_msg_sequence = 0

        self._run_started = True

        # Phase 1: register 块完成后获取 bridge，发送 emit_start + 安装 on_chunk 适配器

        # resume 时也需 emit_start：emit_suspend 已发 stream_end 关闭上一轮，

        # resume 是新的一轮流式输出，需要新的 stream_start + 新的 message_id。

        self._streaming.attach_bridge(self._get_bridge())

        if self._streaming.bridge is not None:
            try:
                await self._streaming.start(state)

            except Exception as _emit_start_exc:
                logger.warning(
                    "[Engine] emit_start 失败（非致命，继续执行）: %s",
                    _emit_start_exc,
                )

        try:
            self._pipeline_logger.setup(pipeline_run_id, resumed)

            # context_window 需在首次迭代前注入 state

            if not state.get("context_window"):
                _llm_core = self.plugin_registry.get_core("llm_call")

                if _llm_core and hasattr(_llm_core, "_context_window") and _llm_core._context_window:
                    state["context_window"] = _llm_core._context_window

            while not state.get(StateKeys.ENDED, False):
                # 1. 递增迭代计数器

                state[StateKeys.ITERATION] = state.get(StateKeys.ITERATION, 0) + 1

                iteration = state[StateKeys.ITERATION]

                # 暴露当前状态供外部读取（如 TaskNotifierMixin 读取上下文使用率）

                self._current_state = state

                # 安全阀（-1 表示无限制）

                if self.max_iterations > 0 and iteration > self.max_iterations:
                    logger.warning("Pipeline exceeded %d iterations, forcing end", self.max_iterations)

                    state[StateKeys.ENDED] = True

                    break

                if resumed:
                    logger.debug("=== Pipeline iteration %d (resumed) ===", iteration)

                else:
                    logger.debug("=== Pipeline iteration %d ===", iteration)

                # 显示当前使用的模型信息

                self._pipeline_logger.model_info(self.plugin_registry)

                # 每次迭代刷新模型配置，确保运行中修改 YAML 后新配置生效

                if self._agent_config is not None:
                    from pipeline.plugin_resolver import _tier_cache, apply_agent_model_override  # noqa: PLC0415

                    _tier_cache.clear()

                    apply_agent_model_override(self.plugin_registry, self._agent_config, self._services)

                # 发射 iteration 事件

                self._emit_iteration_event(state, iteration)  # type: ignore[arg-type]

                # 自动保存检查点

                if self._checkpoint_manager is not None:
                    try:
                        _cp_pid = state.get(StateKeys.PIPELINE_ID, "default")

                        await self._checkpoint_manager.save(_cp_pid, state, phase="auto")

                    except Exception as exc:
                        # 检查点对崩溃恢复至关重要，失败时必须可见（warning 而非 debug）

                        logger.warning("Checkpoint auto-save failed: %s", exc)

                # 主动重置 idle timer：每轮迭代开始时重置，

                # 表示上一轮迭代已完成（含 Agent thinking），防止被误判为 idle

                _task_worker = self._services.get("task_worker")

                _task_id_for_reset = state.get("task_id")

                if _task_worker and _task_id_for_reset:
                    try:
                        await _task_worker.reset_idle_timer(_task_id_for_reset)

                    except Exception as _reset_exc:
                        logger.debug("idle timer reset failed (non-critical): %s", _reset_exc)

                # 单轮迭代调度：通知消费 → Input 链 → target 分发 →

                # Core 执行 → Output 链 → 路由仲裁（engine_iteration.py）

                _iter_action = await run_iteration(self, state, iteration)

                if _iter_action == IterationAction.BREAK:
                    break

            # 管道结束后，再执行一次 Output 链

            if state.get(StateKeys.ENDED, False):
                state[StateKeys.ENDED] = True

                await run_post_end_output_chain(self, state)

            # Phase 1: 正常完成时推送 emit_finish

            if self._streaming.bridge is not None:
                try:
                    await self._streaming.emit_finish(state)

                except Exception as _emit_exc:
                    logger.warning(
                        "[Engine] emit_finish 失败（非致命）: %s",
                        _emit_exc,
                    )

        except asyncio.CancelledError:
            _task = asyncio.current_task()

            _must_cancel = getattr(_task, "_must_cancel", None) if _task else None

            # 尝试获取取消来源信息

            _cancel_source = "unknown"

            if _task and hasattr(_task, "get_name"):
                _cancel_source = f"task_name={_task.get_name()}"

            if _must_cancel is True:
                _cancel_source = "explicit_cancel(_must_cancel=True)"

            logger.warning(
                "Pipeline cancelled | iteration=%d | _must_cancel=%s | cancel_source=%s",
                state.get(StateKeys.ITERATION, 0),
                _must_cancel,
                _cancel_source,
            )

            state[StateKeys.ENDED] = True

            state[StateKeys.RAW_ERROR] = f"Pipeline engine cancelled (source={_cancel_source})"

            # Phase 1: 推送 emit_error

            if self._streaming.bridge is not None:
                with contextlib.suppress(Exception):
                    await self._streaming.emit_error(RuntimeError(f"Pipeline cancelled ({_cancel_source})"))

            await self._mark_task_failed_on_engine_exit(state, f"Pipeline engine cancelled (source={_cancel_source})")

        except Exception as exc:
            _iter = state.get(StateKeys.ITERATION, 0)

            _core_type = state.get(StateKeys.CORE_TYPE, "?")

            _elapsed = _time.monotonic() - getattr(self, "_run_start_time", _time.monotonic())

            _msg_count = len(state.get("messages", []))

            logger.error(
                "[Engine] 管道异常退出 | pipeline=%s iteration=%d core_type=%s messages=%d elapsed=%.0fs error=%s",
                self._pipeline_id[:12],
                _iter,
                _core_type,
                _msg_count,
                _elapsed,
                exc,
            )

            # 强制刷新日志以确保错误不被缓冲丢失

            for _h in logging.getLogger().handlers + logging.getLogger("pipeline").handlers:
                with contextlib.suppress(Exception):
                    _h.flush()

            state[StateKeys.ENDED] = True

            state[StateKeys.RAW_ERROR] = str(exc)

            # Phase 1: 推送 emit_error

            if self._streaming.bridge is not None:
                with contextlib.suppress(Exception):
                    await self._streaming.emit_error(exc)

            # 构造含上下文的错误信息，写入任务 error 字段

            _err_detail = (
                f"管道异常退出: {exc}，错误分析: {{"
                f"'retryable': True, "
                f"'reason': '{exc}', "
                f"'category': 'core_error', "
                f"'iteration': {_iter}, "
                f"'core_type': '{_core_type}', "
                f"'messages': {_msg_count}, "
                f"'elapsed_seconds': {_elapsed:.0f}"
                f"}}"
            )

            await self._mark_task_failed_on_engine_exit(state, _err_detail)

        finally:
            self._running = False

            # I3：复位 _run_started，使 is_idle 返回 True。
            # 引擎正常结束后 entry 保留（不再 unregister），下次 send 命中 entry
            # 时 _find_engine 需返回 idle 才能走 _start_idle_engine 重启。
            # 若不复位，is_idle=False 且非 running/suspended，_find_engine 返回 None，
            # send 误判为未注册而拒绝。
            self._run_started = False

            self._last_state = state

            logger.debug(
                "[Engine] 引擎停止: pipeline=%s iteration=%d ended=%s raw_error=%s",
                self._pipeline_id[:12],
                state.get(StateKeys.ITERATION, 0),
                state.get(StateKeys.ENDED, False),
                (state.get(StateKeys.RAW_ERROR) or "(none)")[:100],
            )

            await self._cleanup_run_loop(
                state,
                _pipeline_id_token,
            )

        return state

    # _run_loop 辅助方法

    def _emit_iteration_event(self, state: dict[str, Any], iteration: int) -> None:
        """发射 iteration 事件供 CLI 状态栏实时更新。"""

        on_chunk_cb = state.get("on_chunk")

        if on_chunk_cb:
            try:
                on_chunk_cb(
                    {
                        "type": "iteration",
                        "iteration": iteration,
                        "max_iterations": self.max_iterations,
                    }
                )

            except Exception as exc:
                logger.debug("on_chunk iteration emit failed: %s", exc)

    async def _mark_task_failed_on_engine_exit(
        self,
        state: dict[str, Any],
        reason: str,
    ) -> None:
        """引擎异常退出时，将关联的 running 任务标记为 failed。"""

        pipeline_run_id = state.get(StateKeys.PIPELINE_ID, "")

        if not pipeline_run_id:
            return

        task_service = self._services.get("task_service")

        if task_service is None:
            logger.debug(
                "[Engine] 引擎异常退出但无 task_service，跳过任务状态清理: pipeline=%s",
                pipeline_run_id[:12],
            )

            return

        try:
            for task in task_service.list_by_status("running"):
                if getattr(task, "pipeline_run_id", None) == pipeline_run_id:
                    await task_service.fail_task(task.id, reason=reason)

                    logger.info(
                        "[Engine] 已将关联任务标记为 failed: task=%s pipeline=%s reason=%s",
                        task.id[:12],
                        pipeline_run_id[:12],
                        reason,
                    )

        except Exception as exc:
            logger.warning(
                "[Engine] 标记关联任务 failed 失败（非致命）: pipeline=%s err=%s",
                pipeline_run_id[:12],
                exc,
            )

    async def _cleanup_run_loop(  # noqa: PLR0912
        self,
        state: dict[str, Any],
        pipeline_id_token: contextvars.Token | None,
    ) -> None:
        """清理 _run_loop 的资源和注册。"""

        # Phase 1: 优雅关闭流式消费者 + keepalive 协程（防泄漏），委托给流式输出口
        await self._streaming.shutdown()

        # 关闭 per-pipeline 日志 FileHandler + 重置防重复守卫（防 handler 泄漏 +
        # 引擎复用重启时日志不写文件，见 PipelineLogger.teardown 的 BUG 注释）。
        self._pipeline_logger.teardown()

        # 重置 contextvar（_current_pipeline_id），必须用 setup 时 bind_context 返回的 token
        if pipeline_id_token is not None:
            self._pipeline_logger.reset_context(pipeline_id_token)

        # 清理 EngineRegistry 注册

        _cp_pipeline_id = state.get(StateKeys.PIPELINE_ID, "")

        if _cp_pipeline_id:
            _cl_entry = get_engine_registry().get(_cp_pipeline_id)

            if _cl_entry:
                _cl_entry.engine_task = None

            # 引擎生命由注册表/持有者管理：正常结束保留 entry（下次 send 走 idle 重启），不主动注销。

            # 释放 chunk_service 内存缓存

            try:
                from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

                _cs = get_service_provider().get_service("chunk_service")

                if _cs:
                    await _cs.evict_pipeline(_cp_pipeline_id)

            except Exception as exc:
                logger.debug("chunk_service.evict_pipeline 失败（非致命）: %s", exc)

        if self._checkpoint_manager is not None:
            try:
                _cp_pipeline_id = state.get(StateKeys.PIPELINE_ID, "default")

                await self._checkpoint_manager.cleanup_old(_cp_pipeline_id, keep_count=2)

            except Exception as _cp_exc:
                logger.debug("Checkpoint cleanup failed (non-critical): %s", _cp_exc)

    # 属性

    @property
    def pipeline_id(self) -> str:
        """管道唯一标识。"""

        return self._pipeline_id

    @pipeline_id.setter
    def pipeline_id(self, value: str) -> None:
        """设置管道 ID（供 registry 和会话恢复使用）。"""

        self._pipeline_id = value

        # 同步到流式输出口（日志/事件信封要用 pipeline_id）
        self._streaming._pipeline_id = value

    @property
    def services(self) -> dict[str, Any]:
        """服务实例字典，传递给 PluginContext。"""

        return self._services

    def update_services(self, services: dict[str, Any]) -> None:
        """更新服务实例字典。"""

        self._services = services

    @property
    def consecutive_core_errors(self) -> int:
        """连续 Core 执行错误计数。"""

        return self._consecutive_core_errors

    @consecutive_core_errors.setter
    def consecutive_core_errors(self, value: int) -> None:
        """设置连续 Core 错误计数。"""

        self._consecutive_core_errors = value

    @property
    def max_consecutive_core_errors(self) -> int:
        """连续 Core 错误上限阈值（只读）。"""

        return self._max_consecutive_core_errors

    @property
    def is_running(self) -> bool:
        """管道是否正在运行（非挂起、非完成）。"""

        return self._running

    @property
    def is_suspended(self) -> bool:
        """管道是否处于暂停状态。"""

        return self._suspended_state is not None

    @property
    def last_state(self) -> dict[str, Any] | None:
        """管道最近一次运行结束后的状态快照。"""

        return self._last_state

    # 挂起/恢复

    async def _suspend_and_wait(self, state: dict[str, Any]) -> bool:  # noqa: PLR0912,PLR0915
        """挂起管道，等待外部通过 wake() 或 message_bus 唤醒。"""

        pipeline_id = state.get(StateKeys.PIPELINE_ID, "")

        _on_chunk_cb = state.get("on_chunk")

        if _on_chunk_cb:
            try:
                _on_chunk_cb(
                    {
                        "type": "pipeline_suspended",
                        "pipeline_id": pipeline_id,
                    }
                )

            except Exception:
                logger.debug("on_chunk pipeline_suspended 回调失败（非致命）")

        await self._streaming.drain(timeout=2.0)

        # Phase 1: 推送 emit_suspend（本轮流式完成）

        if self._streaming.bridge is not None:
            try:
                await self._streaming.emit_suspend(state)

            except Exception as _emit_exc:
                logger.warning(
                    "[Engine] emit_suspend 失败（非致命）: %s",
                    _emit_exc,
                )

        self._watching_task_ids = list(state.get("submitted_task_ids", []))

        self._running = False

        # 挂起/运行统一消息处理：消息一律留在 _inject_queue，不在此 drain。
        # 唤醒后由主循环 run_iteration → consume_pending_notifications 统一消费
        # （注入 state + 推送 system 通知），挂起与运行路径无差别。
        # 这里只负责「等待唤醒」与「判定要不要 resume」。

        logger.debug(
            "[Engine] 管道挂起，等待唤醒: pipeline=%s, watching_tasks=%s",
            pipeline_id,
            self._watching_task_ids,
        )

        # watching_tasks 语义切分：避免 50 轮 × 600s ≈ 8.3h 的静默死挂。
        # - 空：无子任务可等，1 轮 600s 无注入即 return False（管道结束 → fail）
        # - 非空：6 轮（约 60min）周期 _check_children_terminal，覆盖正常等子任务终态
        max_wait_rounds = 6 if self._watching_task_ids else 1

        self._engine_loop = asyncio.get_running_loop()

        if self._wake_event is None:
            self._wake_event = asyncio.Event()

        _wake_reason = ""

        for wait_round in range(max_wait_rounds):
            _registry = get_engine_registry()

            _entry = _registry.get(pipeline_id)

            if _entry is not None:
                _entry.engine = self

            else:
                _registry.register(pipeline_id, self)

            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=600)
                # 正常唤醒 = wake_event 被 set = inject_message 入队后唤醒
                _wake_reason = "injected"
                break

            except asyncio.TimeoutError:
                # 超时边界检查：Event 可能在 timeout 和异常处理之间被 set
                if self._wake_event.is_set():
                    _wake_reason = "injected"
                    break

                if self._check_children_terminal(state):
                    logger.debug(
                        "[Engine] 管道超时后发现子任务已终态，唤醒: pipeline=%s",
                        pipeline_id,
                    )
                    _wake_reason = "children_terminal"
                    break

                logger.debug(
                    "[Engine] 管道等待超时(600s)无新通知，重新挂起 (round=%d/%d): pipeline=%s",
                    wait_round + 1,
                    max_wait_rounds,
                    pipeline_id,
                )

                self._wake_event.clear()

        else:
            logger.warning(
                "[Engine] 管道等待超过 %d 轮，强制唤醒: pipeline=%s",
                max_wait_rounds,
                pipeline_id,
            )
            _wake_reason = "forced"

        self._wake_event = None

        self._engine_loop = None

        self._watching_task_ids = []

        self._running = True

        # 判定要不要 resume（消息留队列，不 drain）：
        # - 队列有消息（inject_message 入队）→ resume
        # - 子任务终态（children_terminal）→ resume（子任务终态本身是有意义的事件，
        #   即使通知还没到队列，也该醒来看 LLM 怎么决策）
        # - 其余（裸 wake()、跑满轮数 forced 但无消息）→ 丢弃，没有新内容喂 LLM
        # 队列里的消息由 consume_pending_notifications 统一处理，这里只读大小。
        _has_message = self.inject_queue_size > 0
        if not _has_message and _wake_reason != "children_terminal":
            logger.debug(
                "[Engine] 管道唤醒但无新内容（%s），丢弃唤醒: pipeline=%s",
                _wake_reason or "bare_wake",
                pipeline_id,
            )
            self._suspended_state = None
            return False

        # 恢复流式上下文（on_chunk/streaming）。挂起期间不再往 _suspended_state
        # 注入消息，所以 user_input/messages 不需要从快照合并 —— 消息在队列里，
        # 由 consume 处理。
        if self._suspended_state is not None:
            for _key in ("on_chunk", "streaming"):
                if _key in self._suspended_state:
                    state[_key] = self._suspended_state[_key]

            self._suspended_state = None

            self._streaming.save_context(state)

            logger.debug("[Engine] 管道被唤醒并恢复 state: pipeline=%s", pipeline_id)

            # resume 后不在此 emit_start —— 让 run_iteration 的 1.5 续接来负责。
            # 原因：此处的 emit_start 在 consume 之前，导致 system 通知排在 AI 流之后。
            # 改为 consume（run_iteration ①）→ emit_start（run_iteration 1.5），
            # 通知排在 emit_start 之前 = 通知排在新 AI 气泡之前。
            # emit_suspend 已发 stream_end（_stream_started=False），续接会检测到并 emit_start。

            return True

        logger.debug("[Engine] 管道被唤醒但无 suspended_state: pipeline=%s", pipeline_id)

        return False

    def drain_inject_queue(self) -> list[tuple[str, str]]:
        """从 _inject_queue 取出所有 (message, source) 并清空。"""

        if not self._inject_queue:
            return []

        msgs = self._inject_queue[:]

        self._inject_queue.clear()

        return msgs

    @property
    def inject_queue_size(self) -> int:
        """当前注入队列中的待处理消息数量（只读公共接口）。"""

        return len(self._inject_queue)

    @property
    def is_idle(self) -> bool:
        """引擎是否处于 idle 状态（尚未启动 run）。"""
        return not self._run_started

    def deliver_signal(self, signal_tags: dict[str, Any]) -> None:
        """I6：投递控制信号到管道 state（开放式 tags，插件自治处理）。"""
        if not signal_tags:
            return
        signal_type = signal_tags.get("signal_type", "")

        # stop_generation 无条件中断 engine_task（不依赖 last_state，是否真有 await 由 _interrupt_engine_task 内部判定）。
        if signal_type == "stop_generation":
            self._interrupt_engine_task()

        # 信号留痕到 state（供插件下一轮从 pending_signals 读取并自治处理）。
        # 运行中用实时 _current_state（last_state 此时还是 None）；引擎从未 run 时
        # 两者皆无，跳过留痕（中断已在上面按需执行，与 state 无关）。
        state = self._current_state or self.last_state
        if state is None:
            logger.debug(
                "[Engine] 信号留痕跳过（state 尚未初始化）: pipeline=%s signal_type=%s",
                self._pipeline_id[:12],
                signal_type or "?",
            )
            return
        bucket = state.setdefault("pending_signals", {})
        bucket[signal_type or "_default"] = signal_tags
        logger.debug(
            "[Engine] 信号已写入 state: pipeline=%s signal_type=%s",
            self._pipeline_id[:12],
            signal_type or "?",
        )

    def _interrupt_engine_task(self) -> None:
        """立即中断当前 engine_task（打断进行中的 LLM await）。"""
        try:
            from pipeline.engine_registry import get_engine_registry  # noqa: PLC0415

            entry = get_engine_registry().get(self._pipeline_id)
            if entry is None:
                logger.info(
                    "[Engine] interrupt: entry 不存在（未注册）: pipeline=%s",
                    self._pipeline_id[:12],
                )
                return
            if entry.engine_task is None:
                logger.info(
                    "[Engine] interrupt: engine_task 为 None（未启动 run 或已复位）"
                    ": pipeline=%s engine.is_running=%s engine.is_idle=%s",
                    self._pipeline_id[:12],
                    self.is_running,
                    self.is_idle,
                )
                return
            if entry.engine_task.done():
                logger.info(
                    "[Engine] interrupt: engine_task 已 done（run 已结束）: pipeline=%s",
                    self._pipeline_id[:12],
                )
                return
            entry.engine_task.cancel()
            logger.info(
                "[Engine] 已中断 engine_task（停止生成）: pipeline=%s",
                self._pipeline_id[:12],
            )
        except Exception as exc:
            logger.warning(
                "[Engine] interrupt engine_task 失败（非致命）: pipeline=%s err=%s",
                self._pipeline_id[:12],
                exc,
            )

    async def cleanup(self) -> None:
        """公开清理接口（供 message_bus.stop 调用）。"""
        # 关闭流式消费者 + keepalive 协程（防泄漏），委托给流式输出口
        await self._streaming.shutdown()

        # 释放 chunk_service 内存缓存
        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            _cs = get_service_provider().get_service("chunk_service")
            if _cs:
                await _cs.evict_pipeline(self._pipeline_id)
        except Exception as exc:
            logger.debug("[Engine] cleanup: chunk_service.evict_pipeline 失败（非致命）: %s", exc)

    @property
    def agent_config(self) -> Any:
        """当前绑定的 Agent 配置（只读公共接口）。"""
        return self._agent_config

    def _check_children_terminal(self, state: dict[str, Any]) -> bool:
        """检查 submitted_task_ids 中的子任务是否全部已到达终态。"""

        task_ids = state.get("submitted_task_ids", [])

        if not task_ids:
            return False

        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            provider = get_service_provider()

            task_service = provider.get("task_service")

            if task_service is None:
                return False

        except Exception as exc:
            logger.debug("获取 task_service 失败: %s", exc)

            return False

        # TaskStatus 枚举仅有 STOPPED/COMPLETED/FAILED（无 cancelled）。
        # cancel_task 产生的子任务状态为 stopped，必须纳入终态判定，
        # 否则父管道在 child_task_guard 反复"挂起→超时唤醒→查子任务非终态→再挂起"死循环。
        terminal_statuses = {"completed", "failed", "stopped"}

        for tid in task_ids:
            try:
                task = task_service.get_task(tid)

                if task is None:
                    continue

                status = task.status.value if hasattr(task.status, "value") else str(task.status)

                if status not in terminal_statuses:
                    logger.debug(
                        "[Engine] 子任务未终态，继续等待: pipeline=%s task_id=%s status=%s",
                        state.get(StateKeys.PIPELINE_ID, ""),
                        tid,
                        status,
                    )

                    return False

            except Exception as exc:
                logger.debug("查询子任务状态失败 (task_id=%s): %s", tid, exc)

                return False

        logger.info(
            "[Engine] 所有子任务已终态: pipeline=%s task_ids=%s",
            state.get(StateKeys.PIPELINE_ID, ""),
            task_ids,
        )

        state["submitted_task_ids"] = []

        return True

    def wake(self) -> None:
        """唤醒挂起的管道（不注入消息）。"""

        # engine 在主循环运行，直接 set Event。

        if self._wake_event is not None:
            self._wake_event.set()

    def _suspend_copy_state(self, state: dict) -> dict:
        """轻量级挂起状态拷贝，仅深拷贝 messages（唯一会被修改的嵌套结构）。"""

        import copy  # noqa: PLC0415

        new_state = dict(state)

        new_state["messages"] = copy.deepcopy(state.get("messages", []))

        return new_state

    async def suspend_and_wait(self, state: dict[str, Any]) -> bool:
        """保存状态快照并挂起管道，等待外部唤醒（公开入口）。"""

        self._suspended_state = self._suspend_copy_state(state)

        return await self._suspend_and_wait(state)

    async def resume_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """从外部提供的状态快照恢复管道执行（检查点恢复专用）。"""

        self._suspended_state = state

        return await self.resume()

    # Phase 1: bridge 获取 + 同步→异步适配

    def _get_bridge(self) -> Any:
        """从 registry 获取当前管道的 bridge 引用。"""

        try:
            _entry = get_engine_registry().get(self._pipeline_id)

            if _entry is not None and _entry.bridge is not None:
                return _entry.bridge

        except Exception:
            pass

        return None

    def _is_stop_signal_active(self) -> bool:
        """协作式停止检查：state 中是否存在未消费的 stop_generation 信号。"""
        state = self._current_state or self.last_state
        if not state:
            return False
        pending = state.get("pending_signals") or {}
        return "stop_generation" in pending

    def inject_message(self, message: str, *, source: str = "user", client_message_id: str = "") -> None:
        """消息注入入口。"""

        if not message or not message.strip():
            return

        self._inject_queue.append((message, source))

        if client_message_id:
            self._pending_client_message_id = client_message_id

        logger.info(
            "[Engine] inject_message: 消息入队 | pipeline=%s source=%s queue_size=%d preview=%.60s",
            self._pipeline_id[:12],
            source,
            len(self._inject_queue),
            message,
        )

        # 唤醒引擎

        if self._wake_event is not None:
            if self._engine_loop is not None and self._engine_loop.is_running():
                self._engine_loop.call_soon_threadsafe(self._wake_event.set)

            else:
                self._wake_event.set()

    def _try_cancel_pending_interaction(self) -> None:
        """尝试取消当前管道关联的 pending human_interaction 请求。"""

        try:
            from human_interaction import get_human_interaction_service  # noqa: PLC0415

            svc = get_human_interaction_service()

            if svc is not None:
                try:
                    loop = asyncio.get_running_loop()

                    loop.create_task(svc.cancel_pending_for_thread(self._pipeline_id))

                except RuntimeError:
                    pass

        except ImportError:
            pass

    async def save_checkpoint(self, phase: str = "manual") -> str | None:
        """保存管道检查点（委托到 pipeline.checkpoint）。"""

        from pipeline.checkpoint import save_checkpoint as _save  # noqa: PLC0415

        return await _save(
            self._checkpoint_manager,
            self._suspended_state,
            self._pipeline_id,
            phase,
        )

    async def restore_from_checkpoint(self, checkpoint_id: str) -> bool:
        """从检查点恢复管道状态（委托到 pipeline.checkpoint）。"""

        from pipeline.checkpoint import restore_from_checkpoint as _restore  # noqa: PLC0415

        success, state = await _restore(self._checkpoint_manager, checkpoint_id)

        if success and state is not None:
            self._suspended_state = state

        return success

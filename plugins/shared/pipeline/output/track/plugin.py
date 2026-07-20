"""追踪统计 Output 插件 — 从旧代码 monitoring/ 迁移。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from infrastructure.execution_record_storage import (
    ExecutionRecordData,
    ExecutionRecordStorage,
    PipelineRunSummary,
)
from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class TrackPlugin(IOutputPlugin):
    """追踪统计 Output 插件。"""

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化追踪统计插件。"""
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._track_tokens = self._config.get("track_token_usage", True)
        self._track_time = self._config.get("track_execution_time", True)
        self._start_time = time.monotonic()
        self._initialized_pipeline_ids: set[str] = set()
        self._last_saved_user_input: str = ""
        # 本地 sequence 计数器，registry entry 不可用时作为 fallback
        self._local_sequences: dict[str, int] = {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "track"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 15)

    @property
    def route_signals(self) -> list[str]:
        """本插件不产出路由信号。"""
        return []

    def _get_current_sequence(self, pipeline_run_id: str) -> int:
        """获取当前消息的 sequence 值（只读，用于 summary 统计）。"""
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            entry = get_engine_registry().get(pipeline_run_id)
            if entry is not None:
                return entry.msg_sequence
        except Exception:
            pass
        return 0

    def _next_sequence(self, pipeline_run_id: str) -> int:
        """获取下一条记录的 sequence。"""
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            registry = get_engine_registry()
            entry = registry.get(pipeline_run_id)
            if entry is not None:
                seq = entry.next_sequence()
                # 同步到本地计数器
                self._local_sequences[pipeline_run_id] = seq
                logger.debug(
                    "TrackPlugin._next_sequence: pipeline=%s entry_found=True seq=%d",
                    pipeline_run_id[:12],
                    seq,
                )
                return seq
            logger.warning(
                "TrackPlugin._next_sequence: pipeline=%s entry_found=False total_engines=%d "
                "ids=%s — using local fallback",
                pipeline_run_id[:12],
                len(registry._engines) if hasattr(registry, "_engines") else -1,
                [pid[:12] for pid in (list(registry._engines.keys())[:5] if hasattr(registry, "_engines") else [])],
            )
        except Exception as exc:
            logger.warning(
                "TrackPlugin._next_sequence: pipeline=%s exception=%s — using local fallback",
                pipeline_run_id[:12],
                exc,
            )
        # fallback: 使用本地计数器，确保即使 registry 不可用也能递增
        current = self._local_sequences.get(pipeline_run_id, 0)
        current += 1
        self._local_sequences[pipeline_run_id] = current
        return current

    def _resolve_ai_record_id(
        self,
        pipeline_run_id: str,
        preset_record_id: str,
    ) -> str:
        """解析 AI 记录的 record_id，保证与前端 stream_start 下发的 message_id 一致。"""
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            entry = get_engine_registry().get(pipeline_run_id)
            if entry is not None and entry.bridge is not None:
                bridge_id = getattr(entry.bridge, "message_id", "") or ""
                if bridge_id:
                    return bridge_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TrackPlugin._resolve_ai_record_id: bridge 不可用 pipeline=%s err=%s — 使用 preset fallback",
                pipeline_run_id[:12],
                exc,
            )
        # bridge 不可用 → 用调用方传入的 preset（state.preset_ai_record_id）兜底
        return preset_record_id

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """收集追踪统计信息。"""
        result = await self._do_work(ctx)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行追踪统计逻辑。"""
        if not self._enabled:
            return {}

        updates: dict[str, Any] = {}
        now = time.monotonic()

        # 1. Token 用量追踪
        if self._track_tokens:
            usage = self._collect_token_usage(ctx)
            updates["track.llm_usage"] = usage
            # 写入标准累计 token 值，供 cost_control 插件读取
            updates["track.total_tokens"] = usage.get("total_tokens", 0)
            # 推送本轮单轮 token 用量到前端（输入框进度条实时显示）
            await self._try_notify_cost_update(ctx)

        # 2. 执行耗时追踪
        elapsed = now - self._start_time
        if self._track_time:
            iteration = ctx.state.get(StateKeys.ITERATION, 0)
            stats = {
                "iteration": iteration,
                "elapsed_total": round(elapsed, 3),
                "elapsed_per_iteration": round(elapsed / max(iteration, 1), 3),
                "core_type": ctx.state.get(StateKeys.CORE_TYPE, ""),
                "execution_status": ctx.state.get(StateKeys.EXECUTION_STATUS, ""),
            }
            updates["track.execution_stats"] = stats

        # 3. 逐动作执行记录持久化
        # storage 落盘是同步阻塞 IO（yaml.safe_dump + 文件追加），
        # 在事件循环里裸调用会冻结所有协程（含 messages 接口）。
        # 改用 to_thread 卸载到线程池，避免长 LLM 调用期间阻塞 API。
        await self._try_persist_record(ctx, elapsed)

        # 4. 管道结束时保存运行摘要
        if ctx.state.get(StateKeys.ENDED, False):
            await self.save_pipeline_summary(ctx, elapsed)

        return updates

    async def _try_notify_cost_update(self, ctx: PluginContext) -> None:
        """推送本轮 LLM 调用的 token 用量（单轮值），供输入框进度条实时显示。

        llm_usage 直接取自 state（llm_core 插件写入），是单轮 API 返回的用量，
        天然对应当前 pipeline。pipeline_id 一并带出，供前端按 pipeline 分桶。
        tool_execute 轮 llm_usage 为上一轮残留，跳过推送避免覆盖。

        会话标识取 session_id（state 标准字段）；thread_id 未必存在时由
        TargetedSink 按 pipeline_id 从 registry 自解析，不在此硬守卫。
        """
        if ctx.state.get(StateKeys.CORE_TYPE) != "llm_call":
            return
        try:
            from channels.websocket.ws_handler import ws_interaction_notifier as _notifier  # noqa: PLC0415

            if _notifier:
                _llm_usage = ctx.state.get("llm_usage") or {}
                _pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
                _session_id = ctx.state.get(StateKeys.SESSION_ID, "")
                from pipeline.stream_bridge import create_targeted_sink  # noqa: PLC0415

                _sink = create_targeted_sink(
                    _notifier,
                    _session_id,
                    pipeline_id=_pipeline_id,
                )
                if _sink:
                    await _sink.send_event(
                        {
                            "type": "cost_update",
                            "data": {
                                "pipeline_id": _pipeline_id,
                                "total_tokens": _llm_usage.get("total_tokens", 0),
                                "input_tokens": _llm_usage.get("input_tokens", 0),
                                "output_tokens": _llm_usage.get("output_tokens", 0),
                            },
                        }
                    )
        except Exception:
            logger.debug("cost_update 推送失败", exc_info=True)

    def _collect_token_usage(self, ctx: PluginContext) -> dict[str, Any]:
        """收集 token 用量统计。"""
        core_type = ctx.state.get(StateKeys.CORE_TYPE, "")
        current_usage = ctx.state.get("llm_usage", {})

        # tool_execute 轮不累加 token（llm_usage 是上一轮残留）
        if core_type != "llm_call" or not current_usage:
            prev_total = ctx.state.get("track.llm_usage", {})
            return {
                "total_input_tokens": prev_total.get("total_input_tokens", 0),
                "total_output_tokens": prev_total.get("total_output_tokens", 0),
                "total_tokens": prev_total.get("total_tokens", 0),
                "total_cached_tokens": prev_total.get("total_cached_tokens", 0),
                "last_input_tokens": 0,
                "last_output_tokens": 0,
                "last_cached_tokens": 0,
            }

        prev_total = ctx.state.get("track.llm_usage", {})
        total_input = prev_total.get("total_input_tokens", 0) + current_usage.get("input_tokens", 0)
        total_output = prev_total.get("total_output_tokens", 0) + current_usage.get("output_tokens", 0)
        total_cached = prev_total.get("total_cached_tokens", 0) + current_usage.get("cached_tokens", 0)

        return {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "total_cached_tokens": total_cached,
            "last_input_tokens": current_usage.get("input_tokens", 0),
            "last_output_tokens": current_usage.get("output_tokens", 0),
            "last_cached_tokens": current_usage.get("cached_tokens", 0),
        }

    async def _try_persist_record(self, ctx: PluginContext, elapsed: float) -> None:  # noqa: PLR0912,PLR0915
        """将逐动作执行记录持久化到存储后端。"""
        try:
            storage = ctx.get_service("execution_record_storage")
        except KeyError:
            return

        if not isinstance(storage, ExecutionRecordStorage):
            logger.warning("execution_record_storage 服务类型不匹配，跳过持久化")
            return

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return

        iteration = ctx.state.get(StateKeys.ITERATION, 0)
        core_type = ctx.state.get(StateKeys.CORE_TYPE, "")

        # 从 pipeline 上下文获取 container_task_id
        container_task_id = ctx.state.get("task_id") or ""

        # CLI 重启后续接已有记录的 sequence，同时更新本地计数器
        if pipeline_run_id not in self._initialized_pipeline_ids:
            self._initialized_pipeline_ids.add(pipeline_run_id)
            existing = (await asyncio.to_thread(storage.list_by_pipeline, pipeline_run_id))[0]

            if existing:
                max_seq = max(r.sequence for r in existing)
                # 更新本地计数器（关键：确保 fallback 路径从正确值开始）
                if max_seq > self._local_sequences.get(pipeline_run_id, 0):
                    self._local_sequences[pipeline_run_id] = max_seq
                try:
                    from pipeline.registry import get_engine_registry  # noqa: PLC0415

                    entry = get_engine_registry().get(pipeline_run_id)
                    if entry is not None:
                        entry.init_sequence(max_seq)
                        logger.debug(
                            "TrackPlugin: resumed shared sequence to %d for pipeline %s",
                            max_seq,
                            pipeline_run_id,
                        )
                except Exception:
                    pass

        # 管道结束后的 Output 链仅用于保存摘要，跳过记录创建避免重复
        if ctx.state.get(StateKeys.ENDED, False):
            return

        # 本轮注入的 source 标记（consume 写入，区分 user vs system，供 track 区分 type）
        _inject_sources: list[str] = ctx.state.pop("_last_inject_sources", [])

        # -- 0. 用户消息记录 --
        user_input = ctx.state.get("user_input", "")
        if user_input and user_input != self._last_saved_user_input:
            if iteration == 1:
                self._last_saved_user_input = user_input
                # 序列化附件信息
                attachments_json = None
                attachments = ctx.state.get(StateKeys.ATTACHMENTS)
                if attachments and isinstance(attachments, list):
                    try:
                        attachments_json = json.dumps(attachments, ensure_ascii=False)
                    except (TypeError, ValueError):
                        logger.warning("附件序列化失败", exc_info=True)

                user_record = ExecutionRecordData(
                    pipeline_run_id=pipeline_run_id,
                    type="user",
                    sequence=self._next_sequence(pipeline_run_id),
                    iteration=0,
                    role="user",
                    content=str(user_input),
                    container_task_id=container_task_id or None,
                    client_message_id=ctx.state.get("client_message_id") or None,
                    attachments_json=attachments_json,
                )
                try:
                    await asyncio.to_thread(storage.save, user_record)
                except Exception:
                    logger.exception("用户消息记录持久化失败")
            elif self._last_saved_user_input:
                new_content = self._extract_injected_content(user_input, self._last_saved_user_input)
                if new_content:
                    self._last_saved_user_input = user_input
                    # 增量段可能是 user 注入或 system 通知（consume 统一写 user_input）。
                    # 通过 _inject_sources 区分 type：含非 user source 的标 system，否则 user。
                    _has_system = any(s != "user" for s in _inject_sources)
                    _record_type = "system" if _has_system else "user"
                    _record_role = "system" if _has_system else "user"
                    injected_record = ExecutionRecordData(
                        pipeline_run_id=pipeline_run_id,
                        type=_record_type,
                        sequence=self._next_sequence(pipeline_run_id),
                        iteration=iteration,
                        role=_record_role,
                        content=new_content,
                        container_task_id=container_task_id or None,
                        client_message_id=ctx.state.get("client_message_id") or None,
                    )
                    try:
                        await asyncio.to_thread(storage.save, injected_record)
                        logger.debug(
                            "Injected content saved at iteration %d type=%s (%d chars)",
                            iteration,
                            _record_type,
                            len(new_content),
                        )
                    except Exception:
                        logger.exception("注入消息记录持久化失败")

        # -- 1. AI 回复记录（LLM Core 后写入） --
        raw_result = ctx.state.get(StateKeys.RAW_RESULT)
        raw_thinking = ctx.state.get(StateKeys.RAW_THINKING)
        raw_tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        has_llm_output = raw_result or raw_tool_calls
        if has_llm_output and core_type != "tool_execute":
            # 保存 tool_calls JSON，供 task_worker 恢复对话历史时使用
            _tool_calls_json = None
            if raw_tool_calls:
                with contextlib.suppress(TypeError, ValueError):
                    _tool_calls_json = json.dumps(raw_tool_calls, ensure_ascii=False, default=str)
            # 解析 AI 记录 record_id：始终与前端 stream_start 的 message_id 对齐（id 契约硬约束）。
            # state.preset_ai_record_id 由 bridge.emit_start 写入，作为 bridge 不可用时的 fallback。
            # 见 _resolve_ai_record_id 的 说明（修复多轮/resume 场景 id 断裂导致的重复渲染）。
            preset_record_id = ctx.state.get("preset_ai_record_id") or ""
            ai_record_id = self._resolve_ai_record_id(pipeline_run_id, preset_record_id)
            # record_id 始终用裸 message_id（与前端 id 契约一致）；
            # 同 record_id 多轮记录的覆盖由 ExecutionRecordStorage 组合 key（record_id::sequence）解决。
            ai_record = ExecutionRecordData(
                record_id=ai_record_id,
                pipeline_run_id=pipeline_run_id,
                type="ai",
                sequence=self._next_sequence(pipeline_run_id),
                iteration=iteration,
                role="assistant",
                content=str(raw_result) if raw_result else "",
                thinking_content=str(raw_thinking) if raw_thinking else None,
                tool_calls_json=_tool_calls_json,
                container_task_id=container_task_id or None,
            )
            try:
                await asyncio.to_thread(storage.save, ai_record)
                ctx.state["track.last_ai_sequence"] = ai_record.sequence
                try:
                    from pipeline.registry import get_engine_registry  # noqa: PLC0415

                    _entry = get_engine_registry().get(pipeline_run_id)
                    if _entry and _entry.bridge:
                        _entry.bridge._last_ai_sequence = ai_record.sequence
                except Exception:
                    pass
            except Exception:
                logger.exception("AI 执行记录持久化失败")

        # -- 2. 工具执行记录（Tool Core 后写入，此时工具已执行完毕） --
        if core_type == "tool_execute":
            tool_results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
            raw_tool_calls = ctx.state.get("_executed_tool_calls") or ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
            if tool_results and isinstance(tool_results, list):
                for idx, tr in enumerate(tool_results):
                    if not isinstance(tr, dict):
                        continue
                    tool_name = tr.get("tool_name", "unknown")

                    tool_output = ""
                    if tr.get("success"):
                        tool_output = str(tr.get("data", ""))
                    else:
                        tool_output = f"Error: {tr.get('error', 'unknown')}"

                    # 从 RAW_TOOL_CALLS 中获取对应的工具输入参数
                    # 过滤掉以下划线开头的注入参数（服务对象，不可序列化）
                    tool_input = None
                    if raw_tool_calls and isinstance(raw_tool_calls, list) and idx < len(raw_tool_calls):
                        raw_call = raw_tool_calls[idx]
                        if isinstance(raw_call, dict):
                            raw_args = raw_call.get("args", {})
                            if isinstance(raw_args, dict):
                                filtered_args = {k: v for k, v in raw_args.items() if not k.startswith("_")}
                            else:
                                filtered_args = raw_args
                            tool_input = {
                                "name": raw_call.get("name", tool_name),
                                "args": filtered_args,
                            }

                    # 从 RAW_TOOL_CALLS 获取对应的 tool_call_id
                    _tc_id = None
                    if raw_tool_calls and isinstance(raw_tool_calls, list) and idx < len(raw_tool_calls):
                        _raw_call = raw_tool_calls[idx]
                        if isinstance(_raw_call, dict):
                            _tc_id = _raw_call.get("id")

                    tool_record = ExecutionRecordData(
                        pipeline_run_id=pipeline_run_id,
                        type="tool",
                        name=tool_name,
                        sequence=self._next_sequence(pipeline_run_id),
                        iteration=iteration,
                        role="tool",
                        content=tool_output,
                        tool_input=tool_input,
                        tool_call_id=_tc_id,
                        container_task_id=container_task_id or None,
                    )
                    try:
                        await asyncio.to_thread(storage.save, tool_record)
                    except Exception:
                        logger.exception("工具执行记录持久化失败")

    @staticmethod
    def _extract_injected_content(current: str, previous: str) -> str:
        """从变更的 user_input 中提取新注入的内容。"""
        if not current or not previous:
            return current or ""

        stripped_prev = previous.strip()
        stripped_curr = current.strip()

        if stripped_prev and stripped_curr.endswith(stripped_prev):
            prefix = stripped_curr[: -len(stripped_prev)].strip()
            return prefix

        if stripped_prev and stripped_curr.startswith(stripped_prev):
            suffix = stripped_curr[len(stripped_prev) :].strip()
            return suffix

        return stripped_curr

    def _check_cache_anomaly(self, llm_usage: dict[str, Any], pipeline_id: str) -> None:
        """检测缓存命中异常并输出警告。"""
        total_input = llm_usage.get("total_input_tokens", 0)
        total_cached = llm_usage.get("total_cached_tokens", 0)
        last_input = llm_usage.get("last_input_tokens", 0)

        if total_input <= 0:
            return

        total_uncached = total_input - total_cached
        gap = abs(total_uncached - last_input)
        ratio = gap / total_input

        if ratio > 0.05:
            logger.warning(
                "[%s] 缓存命中异常: 总未命中=%d, 末轮input=%d, 偏差=%d (%.1f%% > 5%%阈值), "
                "总input=%d, 总cached=%d, 命中率=%.1f%%",
                pipeline_id,
                total_uncached,
                last_input,
                gap,
                ratio * 100,
                total_input,
                total_cached,
                total_cached / total_input * 100 if total_input else 0,
            )

    async def save_pipeline_summary(self, ctx: PluginContext, elapsed_total: float) -> None:
        """保存管道运行摘要。"""
        try:
            storage = ctx.get_service("execution_record_storage")
        except KeyError:
            return

        if not isinstance(storage, ExecutionRecordStorage):
            return

        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return

        llm_usage = ctx.state.get("track.llm_usage", {})

        # 缓存命中异常检测
        self._check_cache_anomaly(llm_usage, pipeline_run_id)

        # 从管道 state 中读取 thread_id 写入 summary
        thread_id = ctx.state.get("thread_id", "") or ctx.state.get("session_id", "")

        summary = PipelineRunSummary(
            run_id=pipeline_run_id,
            thread_id=thread_id,
            total_iterations=ctx.state.get(StateKeys.ITERATION, 0),
            total_tokens={
                "input_tokens": llm_usage.get("total_input_tokens", 0),
                "output_tokens": llm_usage.get("total_output_tokens", 0),
                "total_tokens": llm_usage.get("total_tokens", 0),
                "cached_tokens": llm_usage.get("total_cached_tokens", 0),
            },
            total_seconds=round(elapsed_total, 3),
            total_records=self._get_current_sequence(pipeline_run_id),
            status=ctx.state.get(StateKeys.EXECUTION_STATUS, "completed"),
            final_output=str(ctx.state.get(StateKeys.RAW_RESULT, ""))[:500],
            error=ctx.state.get(StateKeys.RAW_ERROR),
        )

        try:
            await asyncio.to_thread(storage.save_summary, summary)
            _total_recs = self._get_current_sequence(pipeline_run_id)
            logger.info("PipelineRunSummary saved: %s (%d records)", pipeline_run_id, _total_recs)
        except Exception:
            logger.exception("PipelineRunSummary 持久化失败")

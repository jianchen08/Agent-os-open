"""追踪统计 Output 插件 — 从旧代码 monitoring/ 迁移。

负责在管道循环的输出阶段收集执行追踪和统计信息，
包括 LLM token 用量、执行耗时、迭代计数等。

M6c 阶段：从 monitoring/execution_monitor.py 和
monitoring/usage_monitor.py 的核心逻辑迁移。

M12c 阶段：增加执行记录持久化写入，通过
ctx.get_service("execution_record_storage") 获取存储后端。

M12d 阶段：改造为逐动作写入模式，每条 AI 回复和每个工具调用
分别生成独立的 ExecutionRecordData 记录；管道结束时保存
PipelineRunSummary 摘要。

State 命名空间：
    - track.llm_usage : 本插件写入的 LLM 用量统计
    - track.execution_stats : 本插件写入的执行统计
"""

from __future__ import annotations

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
    """追踪统计 Output 插件。

    从旧代码 monitoring/ 模块迁移而来。收集管道执行的
   追踪和统计信息，写入 state 供外部系统（如 WebSocket 进度推送）消费。

    追踪信息包括：
    1. LLM token 用量（输入/输出 token 数）
    2. 执行耗时（每轮迭代耗时）
    3. 迭代计数和状态统计

    M12d 改造：
    - 逐动作写入：每次 Output 插件链执行时，将 AI 回复和每个工具调用
      分别持久化为独立的 ExecutionRecordData 记录。
    - 管道摘要：管道循环结束后，保存 PipelineRunSummary 汇总信息。

    优先级：15（副作用型，在 persist 之后）
    错误策略：SKIP（追踪失败不影响当轮结果）

    Attributes:
        _config: 插件配置字典
        _start_time: 插件创建时间，用于计算总耗时
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化追踪统计插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用追踪（默认 True）
                - track_token_usage: 是否追踪 token 用量（默认 True）
                - track_execution_time: 是否追踪执行时间（默认 True）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._track_tokens = self._config.get("track_token_usage", True)
        self._track_time = self._config.get("track_execution_time", True)
        self._start_time = time.monotonic()
        self._initialized_pipeline_ids: set[str] = set()
        self._last_saved_user_input: str = ""

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

    # BUG-FIX-fix_20260529_msg_order: 从共享计数器读取当前 sequence（不递增）
    def _get_current_sequence(self, pipeline_run_id: str) -> int:
        """从 PipelineEntry 共享计数器获取当前值（不递增）。

        TrackPlugin 仅读取 WS 事件推送时已递增过的 sequence 值，
        用于持久化记录，不再独立自增计数器。

        Args:
            pipeline_run_id: 管道 ID

        Returns:
            当前共享计数器的值；Registry 不可用时返回 0
        """
        try:
            from pipeline.registry import get_engine_registry
            entry = get_engine_registry().get(pipeline_run_id)
            if entry is not None:
                return entry.msg_sequence
        except Exception:
            pass
        return 0

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """收集追踪统计信息。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含追踪信息状态更新的输出结果
        """
        result = await self._do_work(ctx)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行追踪统计逻辑。

        按以下顺序执行：
        1. Token 用量追踪
        2. 执行耗时追踪
        3. 逐动作执行记录持久化
        4. 管道结束时保存运行摘要

        Args:
            ctx: 插件执行上下文

        Returns:
            追踪信息字典
        """
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
            # 推送 Token 用量变更事件到前端
            await self._try_notify_cost_update(ctx, usage)

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
        self._try_persist_record(ctx, elapsed)

        # 4. 管道结束时保存运行摘要
        if ctx.state.get(StateKeys.ENDED, False):
            self.save_pipeline_summary(ctx, elapsed)

        return updates

    async def _try_notify_cost_update(self, ctx: PluginContext, usage: dict[str, Any]) -> None:
        """通过 WebSocket 推送 Token 用量变更事件。

        推送失败不影响主流程。

        Args:
            ctx: 插件执行上下文
            usage: Token 用量字典
        """
        try:
            from ws_handler import ws_interaction_notifier as _notifier
            if _notifier:
                _thread_id = ctx.state.get("thread_id", "")
                if _thread_id:
                    from pipeline.stream_bridge import TargetedSink
                    _sink = TargetedSink(_notifier, _thread_id)
                    await _sink.send_event({
                        "type": "cost_update",
                        "data": {
                            "total_tokens": usage.get("total_tokens", 0),
                            "total_input_tokens": usage.get("total_input_tokens", 0),
                            "total_output_tokens": usage.get("total_output_tokens", 0),
                        },
                    })
        except Exception:
            pass

    def _collect_token_usage(self, ctx: PluginContext) -> dict[str, Any]:
        """收集 token 用量统计。

        从 state 中读取 LLM 返回的 token 用量信息，
        累加到跨迭代的总用量中。

        Args:
            ctx: 插件执行上下文

        Returns:
            Token 用量字典
        """
        # 从当前轮次读取
        current_usage = ctx.state.get("llm_usage", {})

        # 累加到跨迭代总量
        prev_total = ctx.state.get("track.llm_usage", {})
        total_input = prev_total.get("total_input_tokens", 0) + current_usage.get("input_tokens", 0)
        total_output = prev_total.get("total_output_tokens", 0) + current_usage.get("output_tokens", 0)

        return {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "last_input_tokens": current_usage.get("input_tokens", 0),
            "last_output_tokens": current_usage.get("output_tokens", 0),
        }

    def _try_persist_record(self, ctx: PluginContext, elapsed: float) -> None:
        """将逐动作执行记录持久化到存储后端。

        根据当前 core_type 分阶段写入：
        - LLM Core 后：写 AI 回复记录（raw_result）
        - Tool Core 后：写工具执行记录（tool_results，含实际执行结果）

        工具记录只在 Tool Core 执行后才写入，避免 LLM Core 后工具未执行
        导致 content/tool_input 为空的问题。

        Args:
            ctx: 插件执行上下文
            elapsed: 本轮耗时（秒）
        """
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

        # BUG-FIX-20260427: CLI 重启后续接已有记录的 sequence 到共享计数器
        if pipeline_run_id not in self._initialized_pipeline_ids:
            self._initialized_pipeline_ids.add(pipeline_run_id)
            existing = storage.list_by_pipeline(pipeline_run_id)
            if existing:
                max_seq = max(r.sequence for r in existing)
                try:
                    from pipeline.registry import get_engine_registry
                    entry = get_engine_registry().get(pipeline_run_id)
                    if entry is not None:
                        entry.init_sequence(max_seq)
                        logger.info(
                            "TrackPlugin: resumed shared sequence to %d for pipeline %s",
                            max_seq, pipeline_run_id,
                        )
                except Exception:
                    pass

        # BUG-FIX-20260418: 管道结束后的 Output 链仅用于保存摘要，
        # 跳过记录创建，避免与循环内已保存的记录重复
        if ctx.state.get(StateKeys.ENDED, False):
            return

        # -- 0. 用户消息记录 --
        user_input = ctx.state.get("user_input", "")
        if user_input and user_input != self._last_saved_user_input:
            if iteration == 1:
                self._last_saved_user_input = user_input
                user_record = ExecutionRecordData(
                    pipeline_run_id=pipeline_run_id,
                    type="user",
                    sequence=self._get_current_sequence(pipeline_run_id),
                    iteration=0,
                    role="user",
                    content=str(user_input),
                )
                try:
                    storage.save(user_record)
                except Exception:
                    logger.exception("用户消息记录持久化失败")
            elif self._last_saved_user_input:
                new_content = self._extract_injected_content(
                    user_input, self._last_saved_user_input
                )
                if new_content:
                    self._last_saved_user_input = user_input
                    notification_record = ExecutionRecordData(
                        pipeline_run_id=pipeline_run_id,
                        type="user",
                        sequence=self._get_current_sequence(pipeline_run_id),
                        iteration=iteration,
                        role="user",
                        content=new_content,
                    )
                    try:
                        storage.save(notification_record)
                        logger.info(
                            "Injected content saved at iteration %d (%d chars)",
                            iteration, len(new_content),
                        )
                    except Exception:
                        logger.exception("注入内容记录持久化失败")

        # -- 1. AI 回复记录（LLM Core 后写入） --
        raw_result = ctx.state.get(StateKeys.RAW_RESULT)
        raw_thinking = ctx.state.get(StateKeys.RAW_THINKING)
        raw_tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        has_llm_output = raw_result or raw_tool_calls
        if has_llm_output and core_type != "tool_execute":
            # 保存 tool_calls JSON，供 task_worker 恢复对话历史时使用
            _tool_calls_json = None
            if raw_tool_calls:
                try:
                    _tool_calls_json = json.dumps(raw_tool_calls, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    pass
            ai_record = ExecutionRecordData(
                pipeline_run_id=pipeline_run_id,
                type="ai",
                sequence=self._get_current_sequence(pipeline_run_id),
                iteration=iteration,
                role="assistant",
                content=str(raw_result) if raw_result else "",
                thinking_content=str(raw_thinking) if raw_thinking else None,
                tool_calls_json=_tool_calls_json,
            )
            try:
                storage.save(ai_record)
            except Exception:
                logger.exception("AI 执行记录持久化失败")

        # -- 2. 工具执行记录（Tool Core 后写入，此时工具已执行完毕） --
        if core_type == "tool_execute":
            tool_results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
            raw_tool_calls = (
                ctx.state.get("_executed_tool_calls")
                or ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
            )
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
                                filtered_args = {
                                    k: v for k, v in raw_args.items()
                                    if not k.startswith("_")
                                }
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
                        sequence=self._get_current_sequence(pipeline_run_id),
                        iteration=iteration,
                        role="tool",
                        content=tool_output,
                        tool_input=tool_input,
                        tool_call_id=_tc_id,
                    )
                    try:
                        storage.save(tool_record)
                    except Exception:
                        logger.exception("工具执行记录持久化失败")

    @staticmethod
    def _extract_injected_content(current: str, previous: str) -> str:
        """从变更的 user_input 中提取新注入的内容。

        常见注入模式：
        - _notify_suspended_pipelines 通过 send_pipeline_message 注入子任务通知

        提取策略：
        1. 若 previous 是 current 的后缀 → 返回前缀部分
        2. 若 previous 是 current 的前缀 → 返回后缀部分
        3. 否则返回完整 current（兜底）

        Args:
            current: 当前迭代的 user_input
            previous: 上一次保存的 user_input

        Returns:
            提取出的新注入内容，无新内容时返回空字符串
        """
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

    def save_pipeline_summary(self, ctx: PluginContext, elapsed_total: float) -> None:
        """保存管道运行摘要。

        在管道循环结束后调用，汇总本次管道运行的统计信息。

        Args:
            ctx: 插件执行上下文
            elapsed_total: 总耗时（秒）
        """
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

        # BUG-FIX-fix_pipeline_thread_id_missing:
        # 问题根因: thread_id 从未被写入 PipelineRunSummary，导致服务器重启后
        #           _try_recover_pipeline_ids 无法通过 summary.thread_id 找到管道记录。
        # 修复方案: 从管道 state 中读取 thread_id（由启动方注入），写入 summary。
        # 影响范围: list_messages、get_thread_detail 等消息查询接口的管道关联逻辑。
        # 修复日期: 2026-05-07
        thread_id = (
            ctx.state.get("thread_id", "")
            or ctx.state.get("session_id", "")
        )

        summary = PipelineRunSummary(
            run_id=pipeline_run_id,
            thread_id=thread_id,
            total_iterations=ctx.state.get(StateKeys.ITERATION, 0),
            total_tokens={
                "input_tokens": llm_usage.get("total_input_tokens", 0),
                "output_tokens": llm_usage.get("total_output_tokens", 0),
                "total_tokens": llm_usage.get("total_tokens", 0),
            },
            total_seconds=round(elapsed_total, 3),
            total_records=self._get_current_sequence(pipeline_run_id),
            status=ctx.state.get(StateKeys.EXECUTION_STATUS, "completed"),
            final_output=str(ctx.state.get(StateKeys.RAW_RESULT, ""))[:500],
            error=ctx.state.get(StateKeys.RAW_ERROR),
        )

        try:
            storage.save_summary(summary)
            _total_recs = self._get_current_sequence(pipeline_run_id)
            logger.info("PipelineRunSummary saved: %s (%d records)", pipeline_run_id, _total_recs)
        except Exception:
            logger.exception("PipelineRunSummary 持久化失败")

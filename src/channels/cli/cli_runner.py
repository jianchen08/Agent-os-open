"""CLI 管道运行支持模块。

提供 CLIApplication 的管道执行辅助方法混入类，包含：
- 流式输出回调构建
- 工具调用信息显示
- 管道结果处理
- 事件等待（输入/交互/管道完成）
- 斜杠命令处理
- 状态查询辅助

由 CLIApplication 通过多重继承混入使用，不单独实例化。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time as _time
from typing import Any

from rich.console import Console

from channels.cli.output_adapter import sanitize_for_terminal

logger = logging.getLogger(__name__)


class CLIRunnerMixin:
    """CLIApplication 的管道运行辅助方法混入类。"""

    # ------------------------------------------------------------------
    # 流式输出回调
    # ------------------------------------------------------------------

    def _build_on_chunk_callback(self, console: Console) -> Any:  # noqa: PLR0915
        """构建流式输出的 on_chunk 回调。

        处理五种 chunk 类型：
        - type='text': 正常回复内容，逐 token 输出
        - type='thinking': 思考过程内容，根据 show_thinking 决定是否显示
        - type='tool_call': LLM 返回工具调用，实时显示工具名称
        - type='tool_result': 工具执行完成，显示执行结果
        - type='tool_start': 工具开始执行，显示执行中指示
        - type='iteration': 管道迭代进度，更新状态栏

        Args:
            console: rich Console 实例

        Returns:
            on_chunk 回调函数
        """
        _displayed_tool_indices: set[int] = set()
        self._last_was_text = False
        self._text_output_received = False
        self._last_chunk_time = 0

        def on_chunk(chunk: dict[str, Any]) -> None:  # noqa: PLR0911,PLR0912,PLR0915
            """流式回调：将管道事件实时输出到终端。"""
            # 子对话期间抑制管道输出，缓冲到 _streaming_buffer
            if self._suppress_streaming:
                chunk_type = chunk.get("type", "text")
                _content = chunk.get("content", "")
                if chunk_type == "text" and _content:
                    self._streaming_buffer.append(_content)
                return

            chunk_type = chunk.get("type", "text")
            content = chunk.get("content", "")
            self._last_chunk_time = _time.monotonic()

            if chunk_type == "thinking":
                if self._show_thinking and content:
                    safe = sanitize_for_terminal(content)
                    console.print(safe, end="", highlight=False)
                    self._last_was_text = True
                return

            if chunk_type == "text":
                if content:
                    safe = sanitize_for_terminal(content)
                    console.print(safe, end="", highlight=False)
                    self._last_was_text = True
                    self._text_output_received = True
                return

            # 非文本 chunk：先结束之前的文本行
            if self._last_was_text:
                print()
                self._last_was_text = False

            if chunk_type == "tool_call":
                tool_calls_data = chunk.get("tool_calls", [])
                for tc in tool_calls_data:
                    tc_idx = getattr(tc, "index", 0)
                    if tc_idx in _displayed_tool_indices:
                        continue
                    func = getattr(tc, "function", None)
                    if func:
                        name = getattr(func, "name", "")
                        if name:
                            _displayed_tool_indices.add(tc_idx)
                            args_str = getattr(func, "arguments", "")
                            try:
                                import json as _json  # noqa: PLC0415

                                args = _json.loads(args_str) if args_str else {}
                            except Exception:
                                args = {}
                            self._output_adapter.show_tool_call(name, args)
                return

            if chunk_type == "tool_start":
                tool_name = chunk.get("tool_name", "unknown")
                console.print(f"  [dim yellow]>> 执行 {tool_name}...[/dim yellow]")
                return

            if chunk_type == "tool_result":
                tool_name = chunk.get("tool_name", "unknown")
                result = chunk.get("result", "")
                success = chunk.get("success", True)
                duration_ms = chunk.get("duration_ms", 0)
                self._output_adapter.show_tool_result(
                    tool_name,
                    result,
                    success=success,
                    duration_ms=duration_ms,
                )
                return

            if chunk_type == "iteration":
                iteration = chunk.get("iteration", 0)
                max_iterations = chunk.get("max_iterations", 0)
                self._output_adapter.update_status_bar(
                    pipeline_iteration=iteration,
                    pipeline_max_iterations=max_iterations,
                    pipeline_running=True,
                )
                self._output_adapter.render_status_bar()
                return

        return on_chunk

    # ------------------------------------------------------------------
    # 工具调用显示（非流式兜底）
    # ------------------------------------------------------------------

    def _display_tool_calls_from_state(self, state: dict[str, Any]) -> None:
        """从管道最终 state 中显示工具调用信息（非流式模式的兜底显示）。

        流式模式下工具调用已通过 on_chunk 实时显示，此方法仅显示
        迭代信息等补充内容，避免重复显示。

        Args:
            state: 管道引擎的最终 state 字典
        """
        if not self._streaming:
            tool_results = state.get("tool_results")
            if tool_results and isinstance(tool_results, list):
                for tr in tool_results:
                    if isinstance(tr, dict):
                        tool_name = tr.get("tool_name", "unknown")
                        data = tr.get("data", tr.get("error", ""))
                        success = tr.get("success", True)
                        duration_ms = tr.get("duration_ms", 0)
                        self._output_adapter.show_tool_call(tool_name)
                        self._output_adapter.show_tool_result(
                            tool_name,
                            str(data),
                            success=success,
                            duration_ms=duration_ms,
                        )

            raw_tool_calls = state.get("raw_tool_calls")
            if raw_tool_calls and isinstance(raw_tool_calls, list):
                for tc in raw_tool_calls:
                    if isinstance(tc, dict):
                        func = tc.get("function", {})
                        name = func.get("name", tc.get("name", "unknown"))
                        args_str = func.get("arguments", tc.get("args", ""))
                        try:
                            import json  # noqa: PLC0415

                            args = json.loads(args_str) if isinstance(args_str, str) else args_str
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        self._output_adapter.show_tool_call(name, args)

        iteration = state.get("iteration", 0)
        max_iterations = state.get("max_iterations", 0)
        if iteration and max_iterations and iteration > 1:
            self._output_adapter.show_iteration(iteration, max_iterations)

    # ------------------------------------------------------------------
    # 管道结果处理
    # ------------------------------------------------------------------

    async def _handle_pipeline_result(
        self,
        final_state: dict[str, Any],
        initial_state: dict[str, Any],
        conversation_history: list[dict[str, Any]],
        console: Console,
    ) -> list[dict[str, Any]]:
        """处理管道执行结果：更新历史、绑定任务、刷新状态栏。

        Args:
            final_state: 管道引擎的最终 state 字典
            initial_state: 用户输入的初始 state 字典
            conversation_history: 当前对话历史
            console: rich Console 实例

        Returns:
            更新后的对话历史
        """
        # 错误结果直接显示
        if "error" in final_state and final_state.get("error"):
            await self._output_adapter.send({"error": final_state["error"]})
            self._update_status_bar_idle()
            return conversation_history

        # 回填 pipeline_run_id 到关联的任务
        pipeline_run_id = final_state.get("pipeline_id", "")
        if pipeline_run_id:
            submitted_task_id = final_state.get("submitted_task_id")
            if submitted_task_id:
                task_service = self._services.get("task_service")
                if task_service and hasattr(task_service, "bind_pipeline_run"):
                    try:
                        await task_service.bind_pipeline_run(submitted_task_id, pipeline_run_id)
                        logger.info(
                            "Bound task %s to pipeline_run %s",
                            submitted_task_id,
                            pipeline_run_id,
                        )
                        exec_storage = self._services.get("execution_record_storage")
                        if exec_storage:
                            root_id = task_service.get_root_task_id(submitted_task_id)
                            if root_id:
                                exec_storage.register_pipeline(pipeline_run_id, root_id)
                    except Exception as exc:
                        logger.warning("Failed to bind pipeline_run_id: %s", exc)
            logger.info("Pipeline run completed: pipeline_id=%s", pipeline_run_id)

        await self._output_adapter.send(final_state, streamed=self._streaming)

        # 显示管道产生的工具调用信息
        self._display_tool_calls_from_state(final_state)

        # 更新对话轮次
        self._turn_count += 1

        # 更新对话历史
        final_messages = final_state.get("messages", [])
        if final_messages:
            conversation_history = list(final_messages)
        else:
            user_input = initial_state.get("user_input", "")
            raw_result = final_state.get("raw_result", "")
            if user_input:
                conversation_history.append({"role": "user", "content": user_input})
            if raw_result:
                conversation_history.append({"role": "assistant", "content": raw_result})

        # 更新状态栏
        ctx_pct = self._estimate_context_pct(conversation_history)
        task_stats = self._get_task_stats()
        iteration = final_state.get("iteration", 0)
        max_iterations = final_state.get("max_iterations", 0)
        self._output_adapter.update_status_bar(
            turn_count=self._turn_count,
            context_pct=ctx_pct,
            is_processing=False,
            pipeline_running=False,
            pipeline_iteration=iteration,
            pipeline_max_iterations=max_iterations,
            running_task_count=task_stats["running"],
            pending_task_count=task_stats["pending"],
            completed_task_count=task_stats["completed"],
            failed_task_count=task_stats["failed"],
        )

        return conversation_history

    def _update_status_bar_idle(self) -> None:
        """将状态栏更新为空闲状态。"""
        task_stats = self._get_task_stats()
        self._output_adapter.update_status_bar(
            is_processing=False,
            pipeline_running=False,
            running_task_count=task_stats["running"],
            pending_task_count=task_stats["pending"],
            completed_task_count=task_stats["completed"],
            failed_task_count=task_stats["failed"],
        )

    # ------------------------------------------------------------------
    # 事件等待（输入/交互/管道完成）
    # ------------------------------------------------------------------

    async def _wait_input_or_interaction(
        self,
        cli_notifier: Any,
        console: Console,
    ) -> dict[str, Any] | None:
        """等待用户输入或交互请求，先到先处理。

        Args:
            cli_notifier: CLI 交互通知器
            console: rich Console 实例

        Returns:
            用户输入的 state 字典，或 None 表示交互已处理。
        """
        receive_task = asyncio.create_task(self._input_adapter.receive())

        if cli_notifier is None:
            try:
                return await receive_task, False
            except (EOFError, KeyboardInterrupt):
                return {"should_stop": True}, False
            except Exception as _recv_exc:
                logger.warning(
                    "[_wait_for_next_event] receive error (no cli_notifier): %s",
                    _recv_exc,
                    exc_info=True,
                )
                return None, False

        async def _poll_interaction() -> None:
            while not cli_notifier.has_pending():
                await asyncio.sleep(0.3)

        interaction_task = asyncio.create_task(_poll_interaction())

        done, pending = await asyncio.wait(
            {receive_task, interaction_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()

        if receive_task in done:
            return receive_task.result()

        # 交互请求到达 → 中断 stdin 读取
        self._input_adapter.interrupt_stdin()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await receive_task
        self._input_adapter.drain_stdin()

        # 子对话期间抑制管道流式输出
        self._suppress_streaming = True
        try:
            human_svc = self._services.get("human_interaction_service")
            from channels.cli.cli_interaction import run_sub_conversation  # noqa: PLC0415

            await run_sub_conversation(
                console=console,
                input_adapter=self._input_adapter,
                notifier=cli_notifier,
                interaction_service=human_svc,
                idle_timeout=60,
            )
        finally:
            self._suppress_streaming = False
            # 回放缓冲的管道输出
            if self._streaming_buffer:
                safe = sanitize_for_terminal("".join(self._streaming_buffer))
                console.print(safe, end="", highlight=False)
                self._last_was_text = True
                self._streaming_buffer.clear()

        self._input_adapter.drain_stdin()
        return None

    async def _wait_for_next_event(  # noqa: PLR0912,PLR0915
        self,
        cli_notifier: Any,
        console: Console,
    ) -> tuple[dict[str, Any] | None, bool]:
        """等待下一个事件：用户输入、交互请求或管道完成。

        提示符已显示，在此等待任一事件发生。

        Args:
            cli_notifier: CLI 交互通知器
            console: rich Console 实例

        Returns:
            (initial_state, pipeline_done)
            - initial_state: 用户输入的 state，或 None（交互已处理）
            - pipeline_done: 管道是否已完成（需要回到循环顶部处理）
        """
        receive_task = asyncio.create_task(self._input_adapter.receive())

        tasks: dict[asyncio.Task, str] = {receive_task: "input"}

        if cli_notifier is not None:

            async def _poll_interaction() -> None:
                while not cli_notifier.has_pending():
                    await asyncio.sleep(0.3)

            interaction_task = asyncio.create_task(_poll_interaction())
            tasks[interaction_task] = "interaction"

        if self._pipeline_task is not None and not self._pipeline_task.done():
            tasks[self._pipeline_task] = "pipeline"

        done, pending = await asyncio.wait(
            set(tasks.keys()),
            return_when=asyncio.FIRST_COMPLETED,
        )

        done_tags = [tasks.get(t) for t in done]
        pending_tags = [tasks.get(t) for t in pending]
        logger.info(
            "[_wait_for_next_event] done=%s pending=%s",
            done_tags,
            pending_tags,
        )

        # 取消非管道的 pending 任务
        for t in pending:
            if t != self._pipeline_task:
                t.cancel()

        # 判断哪个事件先完成（优先级：input > pipeline > interaction）
        for t in done:
            tag = tasks.get(t)
            if tag == "input":
                try:
                    result = t.result()
                    logger.info(
                        "[_wait_for_next_event] input result: stop=%s empty=%s interrupted=%s",
                        result.get("should_stop"),
                        result.get("_is_empty"),
                        result.get("_interrupted"),
                    )
                    return result, False
                except (EOFError, KeyboardInterrupt):
                    logger.warning("[_wait_for_next_event] input EOFError")
                    return {"should_stop": True}, False
                except Exception as _input_exc:
                    # 输入适配器异常不应导致 CLI 退出。
                    # 记录日志并返回 None 让主循环继续。
                    logger.warning(
                        "[_wait_for_next_event] Input adapter error: %s",
                        _input_exc,
                        exc_info=True,
                    )
                    return None, False
            elif tag == "pipeline":
                # 管道完成 → 中断 stdin，回到循环顶部处理
                if receive_task not in done:
                    self._input_adapter.interrupt_stdin()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await receive_task
                    self._input_adapter.drain_stdin()
                return None, True
            elif tag == "interaction":
                # 交互请求 → 中断 stdin，处理子对话
                self._input_adapter.interrupt_stdin()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await receive_task
                # 清除残留的 interrupt 信号，防止
                # run_sub_conversation 中的 stdin 读取
                # 立即返回 None（假 EOF）
                self._input_adapter.drain_stdin()

                self._suppress_streaming = True
                try:
                    human_svc = self._services.get("human_interaction_service")
                    from channels.cli.cli_interaction import (  # noqa: PLC0415
                        run_sub_conversation,
                    )

                    await run_sub_conversation(
                        console=console,
                        input_adapter=self._input_adapter,
                        notifier=cli_notifier,
                        interaction_service=human_svc,
                        idle_timeout=60,
                    )
                except Exception as _sub_conv_exc:
                    logger.warning(
                        "[_wait_for_next_event] run_sub_conversation error: %s",
                        _sub_conv_exc,
                        exc_info=True,
                    )
                finally:
                    self._suppress_streaming = False
                    if self._streaming_buffer:
                        safe = sanitize_for_terminal("".join(self._streaming_buffer))
                        console.print(safe, end="", highlight=False)
                        self._last_was_text = True
                        self._streaming_buffer.clear()
                self._input_adapter.drain_stdin()
                return None, False

        # 兜底（不应到达）
        return None, False

    # ------------------------------------------------------------------
    # 斜杠命令处理
    # ------------------------------------------------------------------

    async def _handle_slash_command(self, state: dict[str, Any]) -> Any | None:
        """处理斜杠命令。

        Args:
            state: 包含 _is_slash_command 标记的 state

        Returns:
            命令执行结果，None 表示跳过
        """
        user_input = state.get("user_input", "")

        # 构建命令执行上下文
        cmd_context = self._build_command_context()

        # 执行命令
        result = await self._command_registry.execute(user_input, cmd_context)
        return result

    def _build_command_context(self) -> dict[str, Any]:
        """构建斜杠命令执行上下文。

        Returns:
            包含 services/config/state 等引用的上下文字典
        """
        return {
            "services": self._services,
            "agent_config": self._agent_config,
            "mode": self._interaction_mode,
            "show_thinking": self._show_thinking,
            "turn_count": self._turn_count,
            "conversation_history": [],
            "last_state": {},
        }

    def _apply_command_updates(self, updates: dict[str, Any]) -> None:
        """应用斜杠命令产生的状态更新。

        Args:
            updates: 命令返回的 state_updates 字典
        """
        # 交互模式切换
        if "interaction_mode" in updates:
            new_mode = updates["interaction_mode"]
            if new_mode in ("normal", "auto", "plan"):
                self._interaction_mode = new_mode
                self._output_adapter.update_status_bar(mode=new_mode)
                # 更新输入提示符
                mode_label = new_mode.upper()
                agent_name = self._agent_config.display_name if self._agent_config else "Agent OS"
                self._input_adapter._prompt_str = f"[{mode_label}] {agent_name} > "

        # 思考过程显示切换
        if "show_thinking" in updates:
            self._show_thinking = updates["show_thinking"]
            self._output_adapter.show_thinking = self._show_thinking

        # 模型切换
        if "model_override" in updates:
            model_name = updates["model_override"]
            self._output_adapter.update_status_bar(model_name=model_name)

    # ------------------------------------------------------------------
    # 状态查询辅助
    # ------------------------------------------------------------------

    def _get_model_name(self) -> str:
        """获取当前模型名称。

        Returns:
            模型名称字符串
        """
        if self._agent_config:
            if hasattr(self._agent_config, "model"):
                return self._agent_config.model
            if hasattr(self._agent_config, "config_id"):
                return self._agent_config.config_id
        return "unknown"

    def _get_task_stats(self) -> dict[str, int]:
        """收集任务状态统计。

        从 TaskService 中获取各状态的任务数量。

        Returns:
            包含 running/pending/completed/failed 计数的字典
        """
        stats = {"running": 0, "pending": 0, "completed": 0, "failed": 0}
        task_service = self._services.get("task_service")
        if task_service is None:
            return stats
        try:
            from tasks.types import TaskStatus  # noqa: PLC0415

            stats["running"] = len(task_service.list_by_status(TaskStatus.RUNNING))
            stats["pending"] = len(task_service.list_by_status(TaskStatus.PENDING))
            stats["completed"] = len(task_service.list_by_status(TaskStatus.COMPLETED))
            stats["failed"] = len(task_service.list_by_status(TaskStatus.FAILED))
        except Exception as exc:
            logger.debug("Failed to collect task stats: %s", exc)
        return stats

    def _estimate_context_pct(self, history: list[dict[str, Any]]) -> float:
        """估算上下文占用百分比。

        Args:
            history: 对话历史消息列表

        Returns:
            占用百分比 (0-100)
        """
        if not history:
            return 0.0

        char_count = sum(len(m.get("content", "")) if isinstance(m, dict) else len(str(m)) for m in history)
        estimated_tokens = char_count // 3
        max_context = 128000
        return min(100.0, estimated_tokens / max_context * 100)

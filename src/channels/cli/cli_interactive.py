"""CLI 交互模式模块。"""

from __future__ import annotations

import asyncio
import logging
import sys as _sys
import time as _time
from pathlib import Path
from typing import Any

from channels.cli.output_adapter import sanitize_for_terminal
from infrastructure.execution_record_storage import record_role_for_llm

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_SESSION_DIR = _PROJECT_ROOT / "data" / "session"


class CLIInteractiveMixin:
    """CLIApplication 的交互 REPL 循环方法混入类。"""

    async def run(self) -> None:  # noqa: PLR0912,PLR0915
        """运行 Claude Code 风格 CLI 交互主循环。"""

        _run_t0 = _time.monotonic()

        console = self._output_adapter.console

        agent_name = self._agent_config.display_name if self._agent_config else "Agent OS"

        model_name = self._get_model_name()

        self._output_adapter.show_startup_banner(agent_name, self._interaction_mode)

        await self._init_tag_network_retriever()

        _run_t2 = _time.monotonic()

        if hasattr(self, "_task_worker") and self._task_worker and hasattr(self._task_worker, "start"):
            await self._task_worker.start()

            logger.info("Task worker started")

        try:
            import asyncio as _asyncio  # noqa: PLC0415

            from triggers.manager import get_trigger_manager  # noqa: PLC0415

            get_trigger_manager().set_main_loop(_asyncio.get_running_loop())

        except Exception:
            pass

        logger.info(
            "[STARTUP] TaskWorker start: %.2fs",
            _time.monotonic() - _run_t2,
        )

        self._output_adapter.update_status_bar(
            agent_name=agent_name,
            model_name=model_name,
            turn_count=0,
            context_pct=0.0,
            mode=self._interaction_mode,
            is_processing=False,
        )

        session, session_svc, conversation_history = await self._repl_init_session(_run_t0)

        _repl_iteration = 0

        _exit_reason = ""

        while True:
            _repl_iteration += 1

            # --- 后台管道完成检查 ---

            if self._pipeline_task is not None and self._pipeline_task.done():
                final_state = await self._repl_collect_pipeline_result()

                initial_state = self._pipeline_initial_state or {}

                self._pipeline_task = None

                self._pipeline_initial_state = None

                if getattr(self, "_last_was_text", False):
                    _sys.stdout.write("\n")

                    _sys.stdout.flush()

                    self._last_was_text = False

                try:
                    conversation_history = await self._handle_pipeline_result(
                        final_state,
                        initial_state,
                        conversation_history,
                        console,
                    )

                except Exception:
                    logger.exception("Error handling pipeline result, continuing REPL loop")

            # 渲染状态栏提示符

            status_text = self._output_adapter.status_bar.render_simple()

            self._input_adapter._prompt_str = f"{status_text} > "

            # --- 子 Agent 交互请求处理 ---

            cli_notifier = self._services.get("cli_notifier")

            if cli_notifier and cli_notifier.has_pending():
                human_svc = self._services.get("human_interaction_service")

                await self._run_sub_conversation_safe(console, cli_notifier, human_svc, "top")

                self._input_adapter.drain_stdin()

            # === 等待输出结束 ===

            _pipeline_was_running = self._pipeline_task is not None and not self._pipeline_task.done()

            if _pipeline_was_running and await self._repl_wait_for_output(cli_notifier):
                continue

            # === 显示提示符 ===

            if getattr(self, "_last_was_text", False):
                _sys.stdout.write("\n")

                _sys.stdout.flush()

                self._last_was_text = False

            _sys.stdout.write(self._input_adapter.prompt_text())

            _sys.stdout.flush()

            # === 等待事件：用户输入 / 交互请求 / 管道完成 ===

            try:
                initial_state, pipeline_done = await self._wait_for_next_event(cli_notifier, console)

            except (EOFError, KeyboardInterrupt):
                logger.warning(
                    "[REPL] EOF/KeyboardInterrupt at iter=%d, pipeline_running=%s — exiting REPL",
                    _repl_iteration,
                    self._pipeline_task is not None and not self._pipeline_task.done(),
                )

                self._output_adapter.show_system_message("感谢使用 Agent OS，再见！", "bold blue")

                _exit_reason = "EOF/KeyboardInterrupt"

                break

            except asyncio.CancelledError:
                logger.warning(
                    "[REPL] CancelledError at iter=%d — suppressing",
                    _repl_iteration,
                )

                continue

            except Exception as _wait_exc:
                logger.warning(
                    "[REPL] _wait_for_next_event unexpected error (iter=%d): %s",
                    _repl_iteration,
                    _wait_exc,
                    exc_info=True,
                )

                continue

            if pipeline_done:
                continue

            if initial_state is None:
                continue

            # 多行粘贴反馈

            if self._input_adapter.was_paste():
                total = self._input_adapter.paste_line_count() + 1

                console.print(f"\n[dim green]  ▲ 已接收 {total} 行粘贴内容，正在处理...[/dim green]")

            # --- 处理退出信号 ---

            if initial_state.get("should_stop"):
                _exit_reason = await self._repl_handle_should_stop(console, _repl_iteration)

                if _exit_reason:
                    break

                continue

            # --- 空输入 — 检查待处理的交互请求 ---

            if initial_state.get("_is_empty"):
                if cli_notifier and cli_notifier.has_pending():
                    human_svc = self._services.get("human_interaction_service")

                    await self._run_sub_conversation_safe(console, cli_notifier, human_svc, "empty")

                    self._input_adapter.drain_stdin()

                continue

            # --- 斜杠命令处理 ---

            cmd_handled, _exit_reason = await self._repl_handle_slash_commands(
                initial_state,
                console,
                conversation_history,
                session,
                session_svc,
            )

            if _exit_reason:
                break

            if cmd_handled:
                continue

            # --- Plan 模式：只显示规划 ---

            if self._interaction_mode == "plan":
                self._output_adapter.show_system_message(
                    "[PLAN 模式] 不会执行任何操作，仅显示规划。使用 /mode normal 切换回正常模式。",
                    "yellow",
                )

                user_input = initial_state.get("user_input", "")

                console.print(f"\n[dim][规划模式] 收到输入: {user_input}[/dim]")

                console.print("[dim]使用 /mode normal 或 /mode auto 切换模式后执行[/dim]\n")

                continue

            # --- 管道已在运行 ---

            if (
                self._pipeline_task is not None and not self._pipeline_task.done()
            ) and await self._repl_handle_pipeline_busy(initial_state, console, cli_notifier):
                continue

            # === 启动管道（后台运行，不阻塞提示符） ===

            self._repl_start_pipeline(
                initial_state,
                console,
                conversation_history,
                session,
                session_svc,
            )

            continue

        await self._repl_cleanup(_exit_reason, _repl_iteration)

    # run() 的 helper 方法

    async def _init_tag_network_retriever(self) -> None:
        """异步初始化 TagNetworkRetriever。"""

        _run_t1 = _time.monotonic()

        tag_retriever = self._services.get("tag_network_retriever")

        vector_retriever = self._services.get("vector_retriever")

        if tag_retriever is not None and vector_retriever is not None:
            try:
                await tag_retriever.init_from_pg(vector_retriever)

            except Exception as exc:
                logger.warning("TagNetworkRetriever init failed: %s", exc)

        logger.info(
            "[STARTUP] TagNetworkRetriever init: %.2fs",
            _time.monotonic() - _run_t1,
        )

    async def _repl_init_session(  # noqa: PLR0912
        self, _run_t0: float
    ) -> tuple[Any, Any, list[dict[str, Any]]]:
        """初始化会话并恢复对话历史。"""

        console = self._output_adapter.console

        _run_t3 = _time.monotonic()

        session_svc = self._services.get("session_service")

        if session_svc is None:
            from infrastructure.session import SessionService  # noqa: PLC0415

            session_svc = SessionService(session_dir=_SESSION_DIR)

        session = await session_svc.create_or_restore(channel_type="cli")

        logger.info(
            "[STARTUP] session restore: %.2fs",
            _time.monotonic() - _run_t3,
        )

        # CLI 重启时恢复 pipeline_id

        if session.active_pipeline_id and self._engine is not None:
            logger.info(
                "Restoring engine pipeline_id: %s → %s",
                self._engine.pipeline_id,
                session.active_pipeline_id,
            )

            self._engine.pipeline_id = session.active_pipeline_id

        elif self._engine is not None:
            session.register_pipeline(self._engine.pipeline_id)

            session_svc._persist_session_state(session)

        # 跨轮次对话历史恢复

        _run_t4 = _time.monotonic()

        conversation_history: list[dict[str, Any]] = []

        restored = False

        if session.active_pipeline_id:
            exec_storage = self._services.get("execution_record_storage")

            if exec_storage:
                try:
                    prev_records = exec_storage.list_by_pipeline(session.active_pipeline_id)[0]

                    if prev_records:
                        # 基于 record.type 映射 role（type==system 的注入通知降级为 user）

                        for r in prev_records:
                            role = record_role_for_llm(r)

                            msg: dict[str, Any] = {
                                "role": role,
                                "content": r.content,
                            }

                            if r.name:
                                msg["name"] = r.name

                            if r.tool_call_id:
                                msg["tool_call_id"] = r.tool_call_id

                            if r.tool_input:
                                msg["tool_input"] = r.tool_input

                            if r.tool_calls_json:
                                try:
                                    import json as _json  # noqa: PLC0415

                                    msg["tool_calls"] = _json.loads(r.tool_calls_json)

                                except (_json.JSONDecodeError, TypeError):
                                    pass

                            conversation_history.append(msg)

                        from infrastructure.task_worker import (  # noqa: PLC0415
                            _reconstruct_tool_calls,
                        )

                        _reconstruct_tool_calls(conversation_history)

                        restored = True

                        logger.info(
                            "Restored %d messages from pipeline records (pipeline=%s)",
                            len(conversation_history),
                            session.active_pipeline_id,
                        )

                except Exception as exc:
                    logger.debug("Failed to restore from pipeline records: %s", exc)

        logger.info(
            "[STARTUP] conversation restore (%d msgs): %.2fs",
            len(conversation_history),
            _time.monotonic() - _run_t4,
        )

        logger.info(
            "[STARTUP] === run() total: %.2fs ===",
            _time.monotonic() - _run_t0,
        )

        if restored:
            console.print(f"[dim]已恢复上次会话 ({len(conversation_history)} 条消息)，使用 /clear 开启新会话[/dim]")

        return session, session_svc, conversation_history

    async def _repl_collect_pipeline_result(self) -> dict[str, Any]:
        """收集后台管道任务的执行结果。"""

        try:
            final_state = self._pipeline_task.result()  # type: ignore[union-attr]

        except asyncio.CancelledError:
            logger.info("Pipeline task cancelled")

            final_state = {"error": "Pipeline cancelled"}

        except Exception as exc:
            logger.warning("Pipeline task failed: %s", exc)

            final_state = {"error": str(exc)}

        return final_state

    async def _repl_wait_for_output(self, cli_notifier: Any) -> bool:
        """等待管道输出结束。"""

        _output_wait_start = _time.monotonic()

        while True:
            if self._pipeline_task is None or self._pipeline_task.done():
                break

            if self._engine is not None and self._engine.is_suspended:
                break

            _last_t = getattr(self, "_last_chunk_time", 0)

            if _last_t > 0 and (_time.monotonic() - _last_t) >= 0.3:
                break

            if (_time.monotonic() - _output_wait_start) >= 2.0:
                break

            await asyncio.wait(
                {self._pipeline_task},  # type: ignore[arg-type]
                return_when=asyncio.FIRST_COMPLETED,
                timeout=0.3,
            )

            if cli_notifier and cli_notifier.has_pending():
                break

        return bool(self._pipeline_task is not None and self._pipeline_task.done())

    async def _repl_handle_should_stop(
        self,
        console: Any,
        _repl_iteration: int,
    ) -> str:
        """处理退出信号。管道运行中则阻止退出。"""

        _pipeline_still_running = self._pipeline_task is not None and not self._pipeline_task.done()

        if _pipeline_still_running:
            logger.warning(
                "[REPL] should_stop while pipeline running (iter=%d) — ignoring",
                _repl_iteration,
            )

            return ""

        if hasattr(self, "_task_worker") and self._task_worker and hasattr(self._task_worker, "stop"):
            await self._task_worker.stop()

        self._print_task_summary(console)

        self._output_adapter.show_system_message("感谢使用 Agent OS，再见！", "bold blue")

        return "should_stop (no pipeline)"

    def _print_task_summary(self, console: Any) -> None:
        """打印所有任务的状态汇总。"""

        ts = self._services.get("task_service")

        if not ts or not hasattr(ts, "list_by_status"):
            return

        try:
            from tasks.types import TaskStatus  # noqa: PLC0415

            all_tasks = []

            for st in TaskStatus:
                all_tasks.extend(ts.list_by_status(st))

            if all_tasks:
                console.print("\n[bold]任务状态汇总:[/bold]")

                for t in all_tasks:
                    tid = t.id if hasattr(t, "id") else str(t.get("id", "?"))

                    tstatus = t.status if hasattr(t, "status") else t.get("status", "?")

                    tstatus_str = tstatus.value if hasattr(tstatus, "value") else str(tstatus)

                    ttitle = t.title if hasattr(t, "title") else t.get("title", "")

                    icon = "✅" if tstatus_str == "completed" else "❌" if tstatus_str == "failed" else "🔄"

                    console.print(f"  {icon} {tid[:12]} | {tstatus_str} | {ttitle}")

        except Exception as exc:
            logger.debug("任务状态汇总失败: %s", exc)

    async def _repl_handle_slash_commands(
        self,
        initial_state: dict[str, Any],
        console: Any,
        conversation_history: list[dict[str, Any]],
        session: Any,
        session_svc: Any,
    ) -> tuple[bool, str]:
        """处理斜杠命令。"""

        # 新版斜杠命令结果

        slash_result = initial_state.get("slash_command")

        if slash_result and hasattr(slash_result, "output"):
            if slash_result.output:
                console.print(slash_result.output)

            if slash_result.should_exit:
                console.print("[bold blue]Goodbye![/bold blue]")

                return True, "slash_result.should_exit"

            return True, ""

        # 旧版斜杠命令兼容

        if hasattr(self._input_adapter, "is_slash_command") and self._input_adapter.is_slash_command(initial_state):
            cmd_result = await self._handle_slash_command(initial_state)

            if cmd_result is None:
                return True, ""

            if cmd_result.should_stop:
                self._output_adapter.show_system_message("感谢使用 Agent OS，再见！", "bold blue")

                return True, "cmd_result.should_stop"

            if cmd_result.should_clear_history:
                conversation_history.clear()

                session_svc.clear(session)

                self._turn_count = 0

                self._output_adapter.update_status_bar(turn_count=0, context_pct=0.0)

            if cmd_result.should_clear_session:
                _cp_mgr = self._services.get("checkpoint_manager")

                if _cp_mgr and self._engine is not None:
                    try:
                        await _cp_mgr.cleanup_old(self._engine.pipeline_id, keep_count=0)

                    except Exception as _cp_exc:
                        logger.debug("Session checkpoint cleanup failed: %s", _cp_exc)

            if cmd_result.state_updates:
                self._apply_command_updates(cmd_result.state_updates)

            return True, ""

        return False, ""

    async def _repl_handle_pipeline_busy(
        self,
        initial_state: dict[str, Any],
        console: Any,
        cli_notifier: Any,
    ) -> bool:
        """处理管道运行中的用户输入。返回 True 表示已处理。"""

        if cli_notifier and cli_notifier.has_pending():
            human_svc = self._services.get("human_interaction_service")

            await self._run_sub_conversation_safe(console, cli_notifier, human_svc, "busy")

            return True

        # 通过统一入口注入消息

        user_input = initial_state.get("user_input", "")

        if user_input.strip():
            from pipeline.message_bus import send_pipeline_message  # noqa: PLC0415
            from pipeline.message_types import MessageType, PipelineMessage  # noqa: PLC0415

            _pid = self._engine.pipeline_id if self._engine else ""

            if _pid:
                _msg = PipelineMessage(type=MessageType.CHAT, content=user_input, pipeline_id=_pid)

                result = await send_pipeline_message(_msg)

                if result.method == "wake":
                    console.print("[dim cyan]→ 已将输入注入挂起的管道并唤醒[/dim cyan]")

                elif result.method == "notification":
                    console.print("[dim cyan]→ 消息已发送给运行中的管道[/dim cyan]")

                elif not result.success:
                    console.print(f"[dim red]→ 消息注入失败: {result.error}[/dim red]")

        elif self._engine and self._engine.is_suspended:
            self._engine.wake()

        self._input_adapter.drain_stdin()

        return True

    def _repl_start_pipeline(
        self,
        initial_state: dict[str, Any],
        console: Any,
        conversation_history: list[dict[str, Any]],
        session: Any,
        session_svc: Any,
    ) -> None:
        """启动后台管道任务。"""

        user_input = initial_state.get("user_input", "")

        on_chunk = None

        if self._streaming:
            on_chunk = self._build_on_chunk_callback(console)

        task_stats = self._get_task_stats()

        self._output_adapter.update_status_bar(
            is_processing=True,
            pipeline_running=True,
            pipeline_iteration=0,
            pipeline_max_iterations=(self._engine.max_iterations if self._engine else 0),
            running_task_count=task_stats["running"],
            pending_task_count=task_stats["pending"],
            completed_task_count=task_stats["completed"],
            failed_task_count=task_stats["failed"],
        )

        pipeline_id = self._engine.pipeline_id

        session.register_pipeline(pipeline_id)

        session_svc._persist_session_state(session)

        # session_id 作为 thread_id 注入管道 state

        _thread_id = session.session_id if session else ""

        self._pipeline_task = asyncio.create_task(
            self._engine.run(
                user_input=user_input,
                agent_config=self._agent_config,
                conversation_history=(conversation_history if conversation_history else None),
                streaming=self._streaming,
                on_chunk=on_chunk,
                auto_approve=(self._interaction_mode == "auto"),
                interaction_mode=self._interaction_mode,
                thread_id=_thread_id,
            )
        )

        self._pipeline_initial_state = initial_state

    async def _run_sub_conversation_safe(
        self,
        console: Any,
        cli_notifier: Any,
        human_svc: Any,
        context: str,
    ) -> None:
        """安全执行子对话，处理异常和流式输出抑制。"""

        from channels.cli.cli_interaction import run_sub_conversation  # noqa: PLC0415

        self._suppress_streaming = True

        try:
            await run_sub_conversation(
                console=console,
                input_adapter=self._input_adapter,
                notifier=cli_notifier,
                interaction_service=human_svc,
                idle_timeout=60,
            )

        except Exception as exc:
            logger.warning(
                "[REPL] run_sub_conversation (%s) error: %s",
                context,
                exc,
                exc_info=True,
            )

        finally:
            self._suppress_streaming = False

            if self._streaming_buffer:
                safe = sanitize_for_terminal("".join(self._streaming_buffer))

                console.print(safe, end="", highlight=False)

                self._last_was_text = True

                self._streaming_buffer.clear()

    async def _repl_cleanup(
        self,
        _exit_reason: str,
        _repl_iteration: int,
    ) -> None:
        """REPL 循环退出后的清理工作。"""

        logger.warning(
            "[REPL] Loop exited! reason=%s | pipeline_running=%s | _repl_iteration=%d",
            _exit_reason or "UNKNOWN (exception?)",
            self._pipeline_task is not None and not self._pipeline_task.done(),
            _repl_iteration,
        )

        for _t in list(self._bg_tasks):
            if not _t.done():
                _t.cancel()

        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)

            self._bg_tasks.clear()

        try:
            from llm.adapter import cleanup_litellm_resources  # noqa: PLC0415

            await cleanup_litellm_resources()

        except Exception:
            pass

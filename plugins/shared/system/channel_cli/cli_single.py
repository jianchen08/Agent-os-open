"""CLI 单次运行模式模块。

提供 CLIApplication 的单次运行方法混入类，包含：
- run_single: 非交互模式，发送单条消息并等待后台任务闭环后退出

由 CLIApplication 通过多重继承混入使用，不单独实例化。
"""

from __future__ import annotations

import logging
import time as _time
from typing import Any

logger = logging.getLogger(__name__)


class CLISingleMixin:
    """CLIApplication 的单次运行方法混入类。"""

    async def run_single(self, message: str) -> None:  # noqa: PLR0912,PLR0915
        """非交互模式：发送单条消息，等待后台任务闭环后退出。

        流程：
        1. 启动 TaskWorker（如果可用）
        2. 通过管道引擎执行用户消息
        3. 等待关联的后台任务到达终态
        4. 如有子任务完成，触发 Agent 汇报
        5. 清理 TaskWorker 和 LLM 资源

        Args:
            message: 用户输入的单条消息文本
        """
        t0 = _time.time()
        console = self._output_adapter.console

        console.print(f"\n[bold green]User:[/bold green] {message}\n")

        tw = getattr(self, "_task_worker", None)
        if tw and hasattr(tw, "start"):
            await tw.start()
            logger.info("TaskWorker started (single-message mode)")

        try:
            try:
                result = await self._engine.run(
                    user_input=message,
                    agent_config=self._agent_config,
                    conversation_history=None,
                    streaming=False,
                    auto_approve=True,
                    interaction_mode="auto",
                )
            except Exception as exc:
                console.print(f"\n[red]Engine error: {exc}[/red]")
                return

            elapsed_l1 = _time.time() - t0
            iters = result.get("iteration", 0)
            pipeline_id = result.get("pipeline_id", "")
            raw = result.get("raw_result", "")

            ts = self._services.get("task_service")
            task_ids = []
            if ts:
                try:
                    storage = getattr(ts, "_storage", None)
                    if storage is not None:
                        all_tasks = getattr(storage, "_tasks", {})
                        running_tasks = [
                            (tid, t)
                            for tid, t in all_tasks.items()
                            if hasattr(t, "status") and t.status.value in ("running", "pending")
                        ]
                        if running_tasks:
                            running_tasks.sort(
                                key=lambda x: getattr(x[1], "created_at", ""),
                                reverse=True,
                            )
                            task_ids = [tid for tid, _ in running_tasks]
                except Exception:
                    pass

            console.print(f"\n[dim]L1 done: {elapsed_l1:.1f}s, {iters} iterations, pipeline={pipeline_id}[/dim]")
            if task_ids:
                console.print(f"[dim]Tasks submitted: {task_ids}, waiting for completion...[/dim]\n")

                final_statuses = await self._wait_for_tasks_completion(ts, task_ids)

                if final_statuses and self._engine:
                    await self._report_task_results(ts, task_ids, final_statuses, console)

                elapsed_total = _time.time() - t0
                for tid in task_ids:
                    status = final_statuses.get(tid, "timeout")
                    console.print(f"\n[bold]Task {tid}: {status}[/bold]")
                console.print(f"[dim]Total: {elapsed_total:.1f}s[/dim]")
            else:
                console.print(f"\n[dim]No task submitted. Response: {str(raw)[:300]}[/dim]")

        finally:
            # 确保 TaskWorker 和 LiteLLM 资源始终被清理
            if tw and hasattr(tw, "stop"):
                await tw.stop()
            try:
                from llm.adapter import cleanup_litellm_resources  # noqa: PLC0415

                await cleanup_litellm_resources()
            except Exception:
                pass

    async def _wait_for_tasks_completion(
        self,
        ts: Any,
        task_ids: list[str],
        timeout_per_check: int = 5,
        max_checks: int = 120,
    ) -> dict[str, str]:
        """等待任务到达终态。

        Args:
            ts: TaskService 实例
            task_ids: 待等待的任务 ID 列表
            timeout_per_check: 每次检查的等待秒数
            max_checks: 最大检查次数

        Returns:
            任务 ID 到终态的映射
        """
        import asyncio  # noqa: PLC0415

        final_statuses: dict[str, str] = {}
        for _ in range(max_checks):
            await asyncio.sleep(timeout_per_check)
            if ts:
                try:
                    remaining: list[str] = []
                    for tid in task_ids:
                        task = ts.get_task(tid)
                        if task:
                            status = task.status if hasattr(task, "status") else task.get("status", "?")
                            status_val = status.value if hasattr(status, "value") else str(status)
                            if status_val in (
                                "completed",
                                "failed",
                                "cancelled",
                            ):
                                final_statuses[tid] = status_val
                            else:
                                remaining.append(tid)
                    if not remaining:
                        break
                except Exception:
                    pass
        return final_statuses

    async def _report_task_results(
        self,
        ts: Any,
        task_ids: list[str],
        final_statuses: dict[str, str],
        console: Any,
    ) -> None:
        """让 Agent 汇报任务执行结果。

        Args:
            ts: TaskService 实例
            task_ids: 任务 ID 列表
            final_statuses: 任务终态映射
            console: rich Console 实例
        """
        try:
            summary_lines = []
            for tid, st in final_statuses.items():
                task_obj = ts.get_task(tid) if ts else None
                title = getattr(task_obj, "title", tid) if task_obj else tid
                summary_lines.append(f"- 任务 [{title}](id={tid}): {st}")
            summary_text = "\n".join(summary_lines)
            followup = (
                f"[系统通知] 以下子任务已到达终态，"
                f"请向用户汇报最终结果：\n{summary_text}\n\n"
                "请用简洁的方式向用户汇报任务执行结果。"
            )
            followup_result = await self._engine.run(
                user_input=followup,
                agent_config=self._agent_config,
                conversation_history=None,
                streaming=False,
                auto_approve=True,
                interaction_mode="auto",
            )
            followup_raw = followup_result.get("raw_result", "")
            if followup_raw:
                console.print(f"\n[bold green]Agent:[/bold green] {followup_raw}")
        except Exception as exc:
            logger.warning("run_single followup failed: %s", exc)

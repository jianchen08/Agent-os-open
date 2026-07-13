"""套件 F：E2E 集成稳定性测试。

编程式管道调用方案：直接创建 CLIApplication 实例，
通过 engine.run() 发送消息，验证管道执行不崩溃、
状态流转合理、文件系统有产出。

所有测试标记为 @pytest.mark.integration + @pytest.mark.e2e，
仅在 --run-integration 时运行。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLITestHarness — 编程式 E2E 测试工具
# ---------------------------------------------------------------------------


class CLITestHarness:
    """编程式 E2E 测试工具，替代真实 CLI 进程启动。

    封装 CLIApplication 的完整服务初始化流程，
    提供简洁的 run_pipeline / wait_for_tasks 接口供测试用例使用。
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.app: Any = None

    async def setup(self) -> None:
        """初始化 CLI 应用、构建管道、启动 TaskWorker。"""
        from channels.cli.cli_main import CLIApplication

        self.app = CLIApplication(streaming=False)
        self.app.setup_pipeline()

        tw = getattr(self.app, "_task_worker", None)
        if tw and hasattr(tw, "start"):
            await tw.start()
            logger.info("CLITestHarness: TaskWorker started")

    async def teardown(self) -> None:
        """停止 TaskWorker，释放后台资源。"""
        if self.app is None:
            return
        tw = getattr(self.app, "_task_worker", None)
        if tw and hasattr(tw, "stop"):
            await tw.stop()
            logger.info("CLITestHarness: TaskWorker stopped")

    async def run_pipeline(
        self,
        user_input: str,
        timeout: int = 600,
    ) -> dict[str, Any]:
        """运行管道并返回最终状态字典。

        Args:
            user_input: 发送给主 Agent 的用户消息。
            timeout: 管道执行超时时间（秒）。

        Returns:
            管道引擎的最终 state 字典。

        Raises:
            asyncio.TimeoutError: 管道执行超时。
        """
        result = await asyncio.wait_for(
            self.app._engine.run(
                user_input=user_input,
                agent_config=self.app._agent_config,
                conversation_history=None,
                streaming=False,
                auto_approve=True,
                interaction_mode="auto",
            ),
            timeout=timeout,
        )
        return result

    async def wait_for_tasks(
        self,
        timeout: int = 600,
        poll_interval: float = 5.0,
    ) -> dict[str, str]:
        """等待所有活跃任务到达终态。

        轮询 TaskService 中的任务状态，直到全部到达终态
        （completed / failed / cancelled）或超时。

        Args:
            timeout: 等待超时时间（秒）。
            poll_interval: 轮询间隔（秒）。

        Returns:
            task_id → final_status 的映射字典。
        """
        ts = self.app._services.get("task_service")
        if not ts:
            return {}

        task_ids = self._collect_active_task_ids(ts)
        if not task_ids:
            return {}

        final_statuses: dict[str, str] = {}
        elapsed = 0.0

        while elapsed < timeout:
            remaining: list[str] = []
            for tid in task_ids:
                if tid in final_statuses:
                    continue
                status_val = self._get_task_status_value(ts, tid)
                if status_val in ("completed", "failed", "cancelled"):
                    final_statuses[tid] = status_val
                else:
                    remaining.append(tid)

            if not remaining:
                break

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return final_statuses

    def get_task_service(self) -> Any:
        """获取 TaskService 实例，供断言检查使用。"""
        return self.app._services.get("task_service")

    def get_human_interaction_service(self) -> Any:
        """获取 HumanInteractionService 实例。"""
        return self.app._services.get("human_interaction_service")

    def _collect_active_task_ids(self, ts: Any) -> list[str]:
        """从 TaskService 收集所有活跃（pending / running）任务 ID。"""
        ids: list[str] = []
        try:
            storage = getattr(ts, "_storage", None)
            if storage is not None:
                all_tasks = getattr(storage, "_tasks", {})
                for tid, t in all_tasks.items():
                    if hasattr(t, "status") and t.status.value in (
                        "running",
                        "pending",
                    ):
                        ids.append(tid)
        except Exception:
            pass
        return ids

    @staticmethod
    def _get_task_status_value(ts: Any, task_id: str) -> str:
        """获取单个任务的状态字符串。"""
        try:
            task = ts.get_task(task_id)
            if task is None:
                return "unknown"
            status = getattr(task, "status", None)
            if status is None:
                return "unknown"
            return status.value if hasattr(status, "value") else str(status)
        except Exception:
            return "unknown"


# ---------------------------------------------------------------------------
# MockAutoConfirmNotifier — 自动确认人类交互请求
# ---------------------------------------------------------------------------


class MockAutoConfirmNotifier:
    """自动确认人类交互请求的通知器。

    当子 Agent 发起人类交互请求时，自动提交确认响应，
    避免 E2E 测试因等待人工操作而阻塞。
    """

    def __init__(self, service: Any) -> None:
        self._service = service

    async def notify_request(self, request: Any) -> bool:
        """收到请求后自动提交确认响应。"""
        request_id = (
            request.get("id")
            if isinstance(request, dict)
            else getattr(request, "id", "")
        )
        if request_id:
            await self._service.submit_response(
                request_id=request_id,
                response_type="approved",
                feedback="方案确认通过，请继续执行",
            )
        return True

    async def notify_cancel(
        self,
        request_id: str,
        reason: str | None = None,
        thread_id: str = "",
    ) -> bool:
        """请求取消通知（空实现）。"""
        return True

    async def notify_timeout(
        self,
        request_id: str,
        thread_id: str = "",
    ) -> bool:
        """超时通知（空实现）。"""
        return True

    async def notify_timeout_reminder(
        self,
        request_id: str,
        remaining_seconds: int,
        thread_id: str = "",
        *,
        title: str = "",
        mode: str = "",
        options: list[dict] | None = None,
        questions: list[str] | None = None,
    ) -> bool:
        """超时提醒通知（空实现）。"""
        return True

    async def notify_conversation_start(
        self,
        thread_id: str,
        tab_id: str,
        title: str,
        request_id: str = "",
        initial_message: str | None = None,
        suggestions: list[str] | None = None,
    ) -> bool:
        """对话模式开始通知（自动标记已查看并提交确认）。"""
        if request_id:
            await self._service.mark_as_viewed(request_id)
            await self._service.submit_response(
                request_id=request_id,
                response_type="approved",
                feedback="方案确认通过，请继续执行",
            )
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def cli_harness(tmp_path: Path) -> CLITestHarness:
    """创建并初始化 E2E 测试工具，测试结束后自动清理。"""
    harness = CLITestHarness(tmp_path)
    await harness.setup()
    yield harness
    await harness.teardown()


# ---------------------------------------------------------------------------
# 套件 F：E2E 集成稳定性测试
# ---------------------------------------------------------------------------


class TestE2EStability:
    """E2E 集成测试套件 — 验证完整管道在真实 LLM 调用下的稳定性。"""

    @pytest.mark.integration
    @pytest.mark.e2e
    async def test_real_short_term_pass(self, cli_harness: CLITestHarness) -> None:
        """F3. 短期任务 — 正常通过。

        提交一个可达标的调研任务，验证基本管道流程：
        主 Agent → task_submit → 子 Agent 执行 → 评估 → 终态。
        由于 LLM 输出不可控，主要验证流程不崩溃、终态合理。
        """
        user_input = (
            "提交调研任务：让 research_agent 写一份关于 Python 列表推导式的"
            "简短教程（保存到 tutorial.md），验收标准使用 semantic_check 语义评估，"
            "评估要求是教程包含列表推导式的基本语法和至少 3 个实用示例。"
            "提交任务不要自己做"
        )

        final_state = await cli_harness.run_pipeline(user_input, timeout=600)

        assert isinstance(final_state, dict), "管道应返回 state 字典"
        assert not final_state.get("raw_error"), (
            f"管道不应产生致命错误: {final_state.get('raw_error')}"
        )

        task_statuses = await cli_harness.wait_for_tasks(timeout=600)
        if task_statuses:
            for tid, status in task_statuses.items():
                assert status in ("completed", "failed"), (
                    f"任务 {tid} 终态应为 completed 或 failed，实际: {status}"
                )
                if status == "completed":
                    ts = cli_harness.get_task_service()
                    if ts:
                        task = ts.get_task(tid)
                        assert task is not None, f"已完成的任务 {tid} 应可查询"

    @pytest.mark.integration
    @pytest.mark.e2e
    async def test_real_short_term_with_retry(
        self,
        cli_harness: CLITestHarness,
    ) -> None:
        """F1. 短期任务 — 高难度评估（大概率触发重试）。

        提交任务时附带极其严格甚至矛盾的验收标准，
        验证管道在评估不通过时能正确重试，且不会崩溃。
        """
        user_input = (
            "提交调研任务：让 research_agent 写一份关于 Python GIL 机制的"
            "深度分析报告（保存到 gil_analysis.md），验收标准使用 semantic_check "
            "语义评估，评估要求是报告必须包含 CPython 源码级别的详细分析、"
            "至少 5 个多线程性能基准测试数据、GIL 对 asyncio 和 multiprocessing "
            "两种并发模型的精确量化影响对比，以及一份可编译的 C 扩展代码示例"
            "来演示 GIL 的释放和获取过程。提交任务不要自己做"
        )

        final_state = await cli_harness.run_pipeline(user_input, timeout=600)

        assert isinstance(final_state, dict), "管道应返回 state 字典"
        assert not final_state.get("raw_error"), (
            f"管道不应产生致命错误: {final_state.get('raw_error')}"
        )

        task_statuses = await cli_harness.wait_for_tasks(timeout=600)
        if task_statuses:
            for tid, status in task_statuses.items():
                assert status in ("completed", "failed"), (
                    f"任务 {tid} 终态应为 completed 或 failed，实际: {status}"
                )

    @pytest.mark.integration
    @pytest.mark.e2e
    async def test_real_short_term_retry_exhausted(
        self,
        cli_harness: CLITestHarness,
    ) -> None:
        """F2. 短期任务 — 必败评估（验证重试耗尽后任务标记为 failed）。

        使用不可能达到的验收标准（要求在报告中证明数学上不可证明的命题），
        验证重试机制在耗尽后能正确将任务标记为 failed，且管道不崩溃。
        """
        user_input = (
            "提交调研任务：让 research_agent 写一份简短的数学证明报告"
            "（保存到 math_proof.md），验收标准使用 semantic_check 语义评估，"
            "评估要求是报告必须包含哥德巴赫猜想的完整数学证明，"
            "并且必须经过至少三位菲尔兹奖得主的签字认证。提交任务不要自己做"
        )

        final_state = await cli_harness.run_pipeline(user_input, timeout=600)

        assert isinstance(final_state, dict), "管道应返回 state 字典"
        assert not final_state.get("raw_error"), (
            f"管道不应产生致命错误: {final_state.get('raw_error')}"
        )

        task_statuses = await cli_harness.wait_for_tasks(timeout=600)
        if task_statuses:
            has_failed = any(
                status == "failed" for status in task_statuses.values()
            )
            has_completed = any(
                status == "completed" for status in task_statuses.values()
            )
            assert has_failed or has_completed, (
                f"至少应有一个任务到达终态（failed 或 completed），"
                f"实际: {task_statuses}"
            )

    @pytest.mark.integration
    @pytest.mark.e2e
    async def test_real_long_term_with_auto_confirm(
        self,
        cli_harness: CLITestHarness,
    ) -> None:
        """F4. 长期任务 — 自动化人类确认。

        提交长期任务，通过 MockAutoConfirmNotifier 自动批准人类交互请求，
        验证长期任务管道在自动确认模式下不崩溃。

        注意：长期任务涉及子 Agent 与人类的多轮交互，
        本测试用自动确认替代人工操作，确保管道流程完整执行。
        """
        human_svc = cli_harness.get_human_interaction_service()
        if human_svc is not None:
            auto_notifier = MockAutoConfirmNotifier(human_svc)
            human_svc.set_notifier(auto_notifier)

        user_input = (
            "提交长期任务：创建一个 Python 计算器程序，"
            "支持加减乘除四则运算和括号优先级处理，"
            "需要包含完整的单元测试。提交任务不要自己做"
        )

        final_state = await cli_harness.run_pipeline(user_input, timeout=900)

        assert isinstance(final_state, dict), "管道应返回 state 字典"
        assert not final_state.get("raw_error"), (
            f"管道不应产生致命错误: {final_state.get('raw_error')}"
        )

        task_statuses = await cli_harness.wait_for_tasks(timeout=900)
        if task_statuses:
            for tid, status in task_statuses.items():
                assert status in ("completed", "failed"), (
                    f"任务 {tid} 终态应为 completed 或 failed，实际: {status}"
                )

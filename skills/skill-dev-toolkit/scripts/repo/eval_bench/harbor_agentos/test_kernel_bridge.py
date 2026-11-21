# @feature: FP-0.2.TB评测适配 | @ci: python-coverage
"""kernel_bridge.py 单元测试（内核桥，harbor-free）。

覆盖：
1. 构造：缺口令（参数与环境都无）显式报错；环境变量供给成功；
2. 建会话：带 agent_id 的 POST 体断言；
3. 外部绑定：成功信封 / 4xx 转显式 KernelBridgeError（fail-loud）/ 信封
   success=False 转错；
4. run_task 两路（≥2 组有区分度输入）：
   - completed 路：流终态 stream_end + run_status 轮询到 completed → ok=True；
   - failed 路：流终态 stream_error → run_status=failed，不再轮询；
   - 轮询兜底路：流超时 deadline + run_status 轮询到 failed → ok=False；
5. poll_run_status：终态即返 / pipeline_id 缺失返回空串。
6. R19 轮询进度日志：间隔归零逐轮记进度 / 快收敛零噪音 / 预算耗尽 warning
   带快照（时序打桩=模块常量置 0，不碰全局时钟）。

隔离策略：KernelClient 是外部依赖（HTTP），经 client_factory 注入记录型
桩；kernel_client.dispatch_and_collect 经 monkeypatch 替换（网络面）。
不 mock 被测对象（KernelBridge 本体真实）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG_DIR = Path(__file__).resolve().parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

import kernel_bridge  # noqa: E402
from kernel_bridge import (  # noqa: E402
    CONTAINER_INSTRUCTION_PATH,
    INLINE_DESCRIPTION_MAX_CHARS,
    TASK_DESCRIPTION_MAX_CHARS,
    KernelBridge,
    KernelBridgeError,
    RunOutcome,
    compose_dispatch_message,
    compose_task_description,
    harness_time_budget_note,
    outcome_to_context_metadata,
)
from kernel_client import KernelClientError  # noqa: E402


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeKernelClient:
    """KernelClient 记录型桩：REST 面按脚本应答，HTTP 调用全部记录。"""

    def __init__(self, base_url: str = "http://kernel.test") -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, str, Any]] = []
        self.login_calls: list[tuple[str, str]] = []
        self.session_response: dict[str, Any] = {"thread_id": "th-123"}
        # path → 抛出的 KernelClientError（模拟 4xx）
        self.raise_on: dict[str, str] = {}
        # R17 任务接口面：create 响应 + task.status 轮询脚本（FIFO，耗尽后
        # 复读末项；条目为 dict=响应体、KernelClientError=读失败）
        self.task_create_response: dict[str, Any] = {"id": "t-abcd1234"}
        self.task_status_script: list[Any] = []
        self.task_status_script_last: Any = None

    def login(self, username: str, password: str) -> str:
        self.login_calls.append((username, password))
        return "token-x"

    def ws_chat_url(self) -> str:
        return "ws://kernel.test/ws/chat?token=x"

    def _authed_json(self, method: str, url: str, body: Any = None, timeout: float = 30) -> Any:
        self.calls.append((method, url, body))
        for path, message in self.raise_on.items():
            if url.endswith(path):
                raise KernelClientError(message)
        if url.endswith("/api/v1/sessions"):
            assert isinstance(body, dict), "建会话请求体必须是 dict"
            assert body.get("agent_id"), "建会话必须带 agent_id"
            return self.session_response
        if url.endswith("/ext/isolation_service/external/bind"):
            return {"success": True, "container_name": body["container"]}
        if url.endswith("/ext/isolation_service/external/unbind"):
            return {"success": True, "removed": 1}
        if url.endswith("/api/v1/pipelines/state"):
            return {"items": [{"pipeline_id": "p-1", "state": {"run_status": "completed"}}]}
        if url.endswith("/ext/task_service/tasks/root"):
            assert isinstance(body, dict), "任务创建请求体必须是 dict"
            return self.task_create_response
        if "/ext/task_service/tasks/" in url:
            if self.task_status_script:
                item = self.task_status_script.pop(0)
            elif self.task_status_script_last is not None:
                item = self.task_status_script_last
            else:
                raise AssertionError("task_status_script 未配置")
            self.task_status_script_last = item
            if isinstance(item, KernelClientError):
                raise item
            return item
        raise AssertionError(f"未预期的调用: {method} {url}")


def _make_bridge(client: FakeKernelClient, monkeypatch: pytest.MonkeyPatch) -> KernelBridge:
    monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "pw-from-env")
    return KernelBridge(
        base_url="http://kernel.test",
        client_factory=lambda _base_url: client,
    )


# ── 构造 ────────────────────────────────────────────────────────


class TestInit:
    def test_missing_password_explicit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTOS_ADMIN_PASSWORD", raising=False)
        with pytest.raises(KernelBridgeError, match="AGENTOS_ADMIN_PASSWORD"):
            KernelBridge(base_url="http://kernel.test", client_factory=FakeKernelClient)

    def test_password_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bridge = _make_bridge(FakeKernelClient(), monkeypatch)
        assert bridge._client.base_url == "http://kernel.test"


# ── 建会话 / 绑定 ───────────────────────────────────────────────


class TestSessionAndBind:
    def test_create_session_sends_agent_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeKernelClient()
        bridge = _make_bridge(client, monkeypatch)
        thread = bridge.create_session("TB trial x", "tb_eval_agent")
        assert thread == "th-123"
        method, url, body = client.calls[-1]
        assert method == "POST"
        assert url.endswith("/api/v1/sessions")
        assert body == {"title": "TB trial x", "agent_id": "tb_eval_agent"}

    def test_bind_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeKernelClient()
        bridge = _make_bridge(client, monkeypatch)
        result = bridge.bind_external_container("t-1", "D:\\ws", "tb-box", workdir="/app")
        assert result["success"] is True
        _, url, body = client.calls[-1]
        assert url.endswith("/ext/isolation_service/external/bind")
        assert body == {
            "task_id": "t-1",
            "workspace": "D:\\ws",
            "container": "tb-box",
            "workdir": "/app",
        }

    def test_bind_4xx_translated_to_bridge_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeKernelClient()
        client.raise_on["/ext/isolation_service/external/bind"] = (
            "POST ... -> 422: 外部容器不可达: no such object"
        )
        bridge = _make_bridge(client, monkeypatch)
        with pytest.raises(KernelBridgeError, match="fail-loud"):
            bridge.bind_external_container("t-1", "D:\\ws", "ghost")
        # 登录发生过（fail-loud 不是鉴权问题）
        assert client.login_calls

    def test_unbind_best_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeKernelClient()
        bridge = _make_bridge(client, monkeypatch)
        bridge.unbind_external_container(workspace="D:\\ws")
        _, url, body = client.calls[-1]
        assert url.endswith("/ext/isolation_service/external/unbind")


# ── R12 时间预算注记 ────────────────────────────────────────────


class TestHarnessTimeBudgetNote:
    def test_minutes_conversion_and_required_sentences(self) -> None:
        """分钟换算（秒/60）+ 三句关键语义齐备（预算/verifier 只读/早写交付）。"""
        note = harness_time_budget_note(1800.0)
        assert "Time budget: you have about 30 minutes in total for this task." in note
        assert "the verifier reads your deliverable as-is" in note
        assert "write the final deliverable EARLY" in note
        assert harness_time_budget_note(900.0).find("about 15 minutes") != -1

    def test_non_round_budget_rounds_to_minute(self) -> None:
        """非整分钟预算就近取整（1850s ≈ 30.8min → 31min）；「about」兜住误差。"""
        note = harness_time_budget_note(1850.0)
        assert "about 31 minutes" in note

    def test_tiny_budget_floors_at_one_minute(self) -> None:
        """极小预算下限 1 分钟（不给「0 minutes」退化文案）。"""
        assert "about 1 minute" in harness_time_budget_note(30.0)
        assert "about 1 minute" in harness_time_budget_note(5.0)

    def test_compose_keeps_instruction_verbatim_prefix(self) -> None:
        """原题逐字在前、注记在后；空行分隔；原题不被改动。"""
        original = "[TB-X] 请完成验收任务：写 config.json。\n第二行原题。"
        composed = compose_dispatch_message(original, 1800.0)
        assert composed.startswith(original), "原题必须逐字作为前缀"
        note = harness_time_budget_note(1800.0)
        assert composed == original + "\n\n" + note, "原题与注记间空行分隔"
        assert original in composed
        assert "about 30 minutes" in composed
        # 注记在原题之后（顺序契约：R12 原题在前注记在后）
        assert composed.index(note) > composed.index("[TB-X]")


# ── run_task 三路 ───────────────────────────────────────────────


class TestRunTask:
    def test_stream_end_running_polls_to_completed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R8 判据①：stream_end 早到（管道仍 running）→ 轮询到 completed 才收敛。

        真实形态（宿主 30 分钟真题实证）：长 bash 命令期间 WS 流空闲超时把流
        掐了（stream_end 提前），内核管道仍在跑——适配器不立即判负，轮询
        run_status 直到终态。桩面：第 1 次读 running、第 2 次读 completed。
        """
        client = FakeKernelClient()
        bridge = _make_bridge(client, monkeypatch)
        bridge._poll_interval_s = 0.01

        reads: list[dict[str, Any]] = [
            {"items": [{"pipeline_id": "p-1", "state": {"run_status": "running"}}]},
            {"items": [{"pipeline_id": "p-1", "state": {"run_status": "completed"}}]},
        ]

        def _stateful_read(
            self_client: Any, method: str, url: str, body: Any = None, timeout: float = 30
        ) -> Any:
            if url.endswith("/api/v1/pipelines/state"):
                self_client.calls.append((method, url, body))
                return reads.pop(0) if reads else {"items": [{"pipeline_id": "p-1", "state": {"run_status": "running"}}]}
            return FakeKernelClient._authed_json(self_client, method, url, body, timeout)

        client._authed_json = _stateful_read.__get__(client)  # type: ignore[method-assign]
        self._patch_dispatch(monkeypatch, "stream_end")
        outcome = _run(
            bridge.run_task(
                "x", workspace=r"D:\ws", container_name="tb-box",
                agent_id="tb_eval_agent", title="t", poll_timeout_s=5.0,
            )
        )
        assert outcome.stream_terminal == "stream_end"
        assert outcome.run_status == "completed"
        assert outcome.ok is True
        state_reads = [1 for _, u, _ in client.calls if u.endswith("/api/v1/pipelines/state")]
        assert len(state_reads) >= 2, f"应轮询多次直到终态，实际 {len(state_reads)} 次"

    def test_poll_timeout_still_running_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """R8 判据②：轮询预算耗尽仍 running → ok=False（适配器据此 raise）。"""
        client = FakeKernelClient()
        bridge = _make_bridge(client, monkeypatch)
        bridge._poll_interval_s = 0.01

        def _running_read(
            self_client: Any, method: str, url: str, body: Any = None, timeout: float = 30
        ) -> Any:
            if url.endswith("/api/v1/pipelines/state"):
                return {"items": [{"pipeline_id": "p-1", "state": {"run_status": "running"}}]}
            return FakeKernelClient._authed_json(self_client, method, url, body, timeout)

        client._authed_json = _running_read.__get__(client)  # type: ignore[method-assign]
        self._patch_dispatch(monkeypatch, "stream_end")
        outcome = _run(
            bridge.run_task(
                "x", workspace=r"D:\ws", container_name="tb-box",
                agent_id="tb_eval_agent", title="t", poll_timeout_s=0.3,
            )
        )
        assert outcome.stream_terminal == "stream_end"
        assert outcome.run_status == "running"
        assert outcome.ok is False

    def _patch_dispatch(self, monkeypatch: pytest.MonkeyPatch, terminal: str) -> dict[str, Any]:
        """替换 kernel_client.dispatch_and_collect（网络面外部依赖）。"""
        captured: dict[str, Any] = {}

        async def _fake_dispatch(ws_url, thread_id, content, client_message_id, **kwargs):  # type: ignore[no-untyped-def]
            captured["thread_id"] = thread_id
            captured["content"] = content
            captured["execution_context"] = kwargs.get("execution_context")
            result = RunOutcome()
            result.terminal = terminal
            result.pipeline_id = "p-1" if terminal != "deadline" else "p-1"
            result.events = [{"type": terminal}]
            return result

        import kernel_bridge as _kb

        monkeypatch.setattr(_kb.kernel_client, "dispatch_and_collect", _fake_dispatch)
        return captured

    def test_completed_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeKernelClient()
        bridge = _make_bridge(client, monkeypatch)
        bridge._poll_interval_s = 0.01
        captured = self._patch_dispatch(monkeypatch, "stream_end")
        outcome = _run(
            bridge.run_task(
                "do the task",
                workspace="D:\\ws",
                container_name="tb-box",
                agent_id="tb_eval_agent",
                title="TB trial t",
            )
        )
        assert outcome.ok is True
        assert outcome.run_status == "completed"
        assert outcome.session_id == "th-123"
        # R12：派发消息 = 原题逐字前缀 + 时间预算注记（缺省预算 1800s → 30min）
        assert captured["content"].startswith("do the task\n\n")
        assert "about 30 minutes" in captured["content"]
        assert captured["thread_id"] == "th-123"
        ec = captured["execution_context"]
        assert ec["workspace"]["source_path"] == "D:\\ws"
        assert ec["isolation"]["level"] == "isolated"

    def test_dispatch_note_follows_poll_budget_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R12：注记分钟数与 poll_timeout_s 覆盖值同源（option/env 链一致）。"""
        client = FakeKernelClient()
        bridge = _make_bridge(client, monkeypatch)
        captured = self._patch_dispatch(monkeypatch, "stream_end")
        _run(
            bridge.run_task(
                "solve it", workspace="D:\\ws", container_name="tb-box",
                agent_id="tb_eval_agent", title="t", poll_timeout_s=900.0,
            )
        )
        assert captured["content"].startswith("solve it\n\n")
        assert "about 15 minutes" in captured["content"]

    def test_stream_error_short_circuits_to_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeKernelClient()
        bridge = _make_bridge(client, monkeypatch)
        self._patch_dispatch(monkeypatch, "stream_error")
        outcome = _run(
            bridge.run_task(
                "x", workspace="D:\\ws", container_name="tb-box",
                agent_id="tb_eval_agent", title="t",
            )
        )
        assert outcome.ok is False
        assert outcome.run_status == "failed"
        # 失败路不进 REST 轮询面（无 pipelines/state 调用）
        assert not any(u.endswith("/api/v1/pipelines/state") for _, u, _ in client.calls)

    def test_deadline_polls_run_status_to_truth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """WS 流超时（deadline）只是收流窗没等到：run_status 轮询是权威终态。

        - run_status=completed → 任务真完成，ok=True；
        - run_status=failed → 任务失败，ok=False（第二组区分输入）。
        """
        client = FakeKernelClient()
        bridge = _make_bridge(client, monkeypatch)
        bridge._poll_interval_s = 0.01
        self._patch_dispatch(monkeypatch, "deadline")
        outcome = _run(
            bridge.run_task(
                "x", workspace="D:\\ws", container_name="tb-box",
                agent_id="tb_eval_agent", title="t", poll_timeout_s=1.0,
            )
        )
        assert outcome.stream_terminal == "deadline"
        assert outcome.run_status == "completed"
        assert outcome.ok is True
        # 轮询确实走了 REST 面
        assert any(u.endswith("/api/v1/pipelines/state") for _, u, _ in client.calls)

        # 第二组：轮询应答失败 → ok=False
        client2 = FakeKernelClient()

        def _fail_state(self_client: Any, method: str, url: str, body: Any = None, timeout: float = 30) -> Any:
            if url.endswith("/api/v1/pipelines/state"):
                return {"items": [{"pipeline_id": "p-1", "state": {"run_status": "failed"}}]}
            return client._authed_json(method, url, body, timeout)

        client2._authed_json = _fail_state.__get__(client2)  # type: ignore[method-assign]
        bridge2 = _make_bridge(client2, monkeypatch)
        bridge2._poll_interval_s = 0.01
        outcome2 = _run(
            bridge2.run_task(
                "x", workspace="D:\\ws", container_name="tb-box",
                agent_id="tb_eval_agent", title="t", poll_timeout_s=1.0,
            )
        )
        assert outcome2.run_status == "failed"
        assert outcome2.ok is False


# ── poll_run_status ─────────────────────────────────────────────


class TestPollRunStatus:
    def test_missing_pipeline_id_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bridge = _make_bridge(FakeKernelClient(), monkeypatch)
        assert _run(bridge.poll_run_status(None, timeout_s=0.1)) == ""

    def test_outcome_metadata_shape(self) -> None:
        outcome = RunOutcome(
            session_id="th-1", pipeline_id="p-1", stream_terminal="stream_end",
            run_status="completed", container_name="tb-box", workspace="D:\\ws",
            interactions=[{"request_id": "r1", "action": "approved"}],
        )
        meta = outcome_to_context_metadata(outcome)
        assert meta["agentos"]["run_status"] == "completed"
        assert meta["agentos"]["container_name"] == "tb-box"
        assert meta["agentos"]["interaction_count"] == 1
        assert meta["agentos"]["dispatch_mode"] == "session"


# ── R17 任务接口通道 ────────────────────────────────────────────


class TestTaskDescription:
    """任务接口描述组合：内联原题 or 容器内指令文件指针（2000 字闸补偿）。"""

    def test_short_instruction_inlines_verbatim_with_budget_note(self) -> None:
        desc, needs_file = compose_task_description("write config.json", 1800.0)
        assert needs_file is False
        assert desc.startswith("write config.json\n\n"), "原题逐字前缀"
        assert "about 30 minutes" in desc, "预算注记同源"
        assert len(desc) <= INLINE_DESCRIPTION_MAX_CHARS

    def test_long_instruction_switches_to_pointer(self) -> None:
        instruction = "x" * (INLINE_DESCRIPTION_MAX_CHARS + 500)
        desc, needs_file = compose_task_description(instruction, 1800.0)
        assert needs_file is True
        assert CONTAINER_INSTRUCTION_PATH in desc, "指针指向容器内指令文件"
        assert "Read that file first" in desc
        assert "about 30 minutes" in desc, "指针模式同样带预算注记"
        assert instruction not in desc, "超限原题不内联（2000 字闸必过）"
        assert len(desc) <= TASK_DESCRIPTION_MAX_CHARS, "描述必须过 task_submit 硬闸"

    def test_inline_boundary_exactly_at_limit(self) -> None:
        """组合长度恰好压线 → 内联；再长一字 → 指针（边界行为钉死）。"""
        note = harness_time_budget_note(1800.0)
        exact_instruction = "y" * (INLINE_DESCRIPTION_MAX_CHARS - len(note) - 2)
        desc, needs_file = compose_task_description(exact_instruction, 1800.0)
        assert needs_file is False
        assert len(desc) == INLINE_DESCRIPTION_MAX_CHARS
        _, needs_over = compose_task_description(exact_instruction + "!", 1800.0)
        assert needs_over is True


class TestCreateTask:
    def test_create_task_posts_root_with_explicit_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeKernelClient()
        bridge = _make_bridge(client, monkeypatch)
        task_id = bridge.create_task(
            title="TB trial x",
            description="do the task",
            agent_id="general_agent",
            workspace="D:\\ws",
        )
        assert task_id == "t-abcd1234"
        method, url, body = client.calls[-1]
        assert method == "POST"
        assert url.endswith("/ext/task_service/tasks/root")
        assert body == {
            "title": "TB trial x",
            "description": "do the task",
            "target_id": "general_agent",
            "workspace": "D:\\ws",
            "workspace_mode": "plain",
            "isolation_level": "isolated",
            "thread_id": "",
        }

    def test_create_task_rejection_translated_to_bridge_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeKernelClient()
        client.raise_on["/ext/task_service/tasks/root"] = (
            "POST ... -> 400: 任务描述过长（2500字符，上限2000字符）"
        )
        bridge = _make_bridge(client, monkeypatch)
        with pytest.raises(KernelBridgeError, match="任务创建被拒"):
            bridge.create_task(
                title="t", description="d", agent_id="a", workspace="w"
            )

    def test_create_task_missing_id_explicit_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeKernelClient()
        client.task_create_response = {"unexpected_shape": True}
        bridge = _make_bridge(client, monkeypatch)
        with pytest.raises(KernelBridgeError, match="缺少 id"):
            bridge.create_task(
                title="t", description="d", agent_id="a", workspace="w"
            )


class TestTaskRunTask:
    """任务接口通道全链（桩任务面：create → 轮询 task.status 终态）。"""

    def _make_task_bridge(
        self, monkeypatch: pytest.MonkeyPatch, status_script: list[Any]
    ) -> tuple[KernelBridge, FakeKernelClient]:
        client = FakeKernelClient()
        client.task_status_script = list(status_script)
        bridge = _make_bridge(client, monkeypatch)
        bridge._poll_interval_s = 0.01
        return bridge, client

    def test_task_completed_full_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """create → running → completed 全链；任务即管道；元数据分形正确。"""
        bridge, client = self._make_task_bridge(
            monkeypatch,
            [
                {"status": "running"},
                {"status": "completed", "result": {"artifacts": ["report.md"]}},
            ],
        )
        outcome = _run(
            bridge.run_task(
                "write config.json",
                workspace="D:\\ws",
                container_name="tb-box",
                agent_id="general_agent",
                title="t",
                poll_timeout_s=30.0,
                dispatch_mode="task",
            )
        )
        assert outcome.dispatch_mode == "task"
        assert outcome.task_id == "t-abcd1234"
        assert outcome.pipeline_id == "t-abcd1234", "任务即管道（pipeline_id=task_id）"
        assert outcome.task_status == "completed"
        assert outcome.task_result == {"artifacts": ["report.md"]}
        assert outcome.task_error is None
        assert outcome.ok is True
        # 不触会话面（无 WS/建会话调用），轮询走任务面（≥2 次读）
        assert not any(u.endswith("/api/v1/sessions") for _, u, _ in client.calls)
        status_reads = [
            1
            for _, u, _ in client.calls
            if "/ext/task_service/tasks/" in u and not u.endswith("/root")
        ]
        assert len(status_reads) >= 2
        # 元数据分形：task 键在、session 键不出现
        meta = outcome_to_context_metadata(outcome)["agentos"]
        assert meta["dispatch_mode"] == "task"
        assert meta["task_id"] == "t-abcd1234"
        assert meta["task_status"] == "completed"
        assert meta["task_result"] == {"artifacts": ["report.md"]}
        assert "session_id" not in meta
        assert "run_status" not in meta

    def test_task_failed_verdict_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """failed 是产品裁决（评估闸门/熔断署名），如实透传判失败不翻案。"""
        bridge, _ = self._make_task_bridge(
            monkeypatch, [{"status": "failed", "error": "提醒耗尽未评估"}]
        )
        outcome = _run(
            bridge.run_task(
                "x",
                workspace="w",
                container_name="c",
                agent_id="a",
                title="t",
                poll_timeout_s=30.0,
                dispatch_mode="task",
            )
        )
        assert outcome.task_status == "failed"
        assert outcome.task_error == "提醒耗尽未评估"
        assert outcome.ok is False

    def test_task_budget_exhausted_reports_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """预算耗尽仍未终态 → 快照如实回报（running），ok=False。"""
        bridge, _ = self._make_task_bridge(monkeypatch, [{"status": "running"}])
        outcome = _run(
            bridge.run_task(
                "x",
                workspace="w",
                container_name="c",
                agent_id="a",
                title="t",
                poll_timeout_s=0.2,
                dispatch_mode="task",
            )
        )
        assert outcome.task_status == "running"
        assert outcome.ok is False

    def test_pointer_mode_description_uses_container_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """指针模式：描述 = 指针文案 + 预算注记，原题不内联；路径入 outcome。"""
        bridge, client = self._make_task_bridge(monkeypatch, [{"status": "completed"}])
        outcome = _run(
            bridge.run_task(
                "x",
                workspace="w",
                container_name="c",
                agent_id="a",
                title="t",
                poll_timeout_s=30.0,
                dispatch_mode="task",
                instruction_file_path=CONTAINER_INSTRUCTION_PATH,
            )
        )
        assert outcome.instruction_file_path == CONTAINER_INSTRUCTION_PATH
        posts = [
            b
            for m, u, b in client.calls
            if m == "POST" and u.endswith("/ext/task_service/tasks/root")
        ]
        assert posts, "任务创建 POST 必须发生"
        description = str(posts[0]["description"])
        assert CONTAINER_INSTRUCTION_PATH in description
        assert "Read that file first" in description
        assert "about 1 minute" in description, "指针模式预算注记同源（30s → 1min 下限）"


class TestPollProgressLogging:
    """R19：超长预算下轮询循环的进度日志节奏（每 60s 一条，避免静默）。

    时序打桩不碰全局 time.monotonic——进度间隔读模块常量
    POLL_PROGRESS_LOG_INTERVAL_S，测试直接置 0 强制每轮都记（fake clock
    同语义：间隔判定逻辑被验证，真实 60s 节奏不在单测里等待）。
    """

    def test_task_poll_logs_progress_each_interval(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """task 通道：两次非终态读 → 间隔归零下两条进度日志（elapsed/budget 在场）。"""
        client = FakeKernelClient()
        client.task_status_script = [
            {"status": "running"},
            {"status": "running"},
            {"status": "completed"},
        ]
        bridge = _make_bridge(client, monkeypatch)
        bridge._poll_interval_s = 0.01
        monkeypatch.setattr(kernel_bridge, "POLL_PROGRESS_LOG_INTERVAL_S", 0.0)
        with caplog.at_level(logging.INFO, logger="kernel_bridge"):
            status, _ = _run(bridge.poll_task_status("t-1", timeout_s=30.0))
        assert status == "completed"
        progress = [r for r in caplog.records if "task 轮询进行中" in r.message]
        assert len(progress) == 2, (
            f"两次非终态读应各记一条进度（实得 {[r.getMessage() for r in progress]}）"
        )
        assert all(r.levelno == logging.INFO for r in progress)

    def test_quick_poll_emits_no_progress_noise(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """短预算快收敛（缺省 60s 间隔）→ 零进度日志（不刷屏）。"""
        client = FakeKernelClient()
        client.task_status_script = [{"status": "completed"}]
        bridge = _make_bridge(client, monkeypatch)
        bridge._poll_interval_s = 0.01
        with caplog.at_level(logging.INFO, logger="kernel_bridge"):
            status, _ = _run(bridge.poll_task_status("t-2", timeout_s=5.0))
        assert status == "completed"
        assert [r for r in caplog.records if "轮询进行中" in r.message] == []

    def test_budget_exhaustion_logs_warning_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """预算耗尽 → warning 一条（task 坐标 + 预算 + 末态快照）。"""
        client = FakeKernelClient()
        client.task_status_script = [{"status": "running"}]
        bridge = _make_bridge(client, monkeypatch)
        bridge._poll_interval_s = 0.01
        with caplog.at_level(logging.WARNING, logger="kernel_bridge"):
            status, _ = _run(bridge.poll_task_status("t-3", timeout_s=0.2))
        assert status == "running"
        warnings = [r for r in caplog.records if "轮询预算耗尽" in r.message]
        assert len(warnings) == 1
        assert "t-3" in warnings[0].getMessage()
        assert "running" in warnings[0].getMessage()

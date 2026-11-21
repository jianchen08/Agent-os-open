# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""human 插件 run 停止看门狗——注入送达的插件侧前提（负样本回归）。

场景锚（本文件的意图）：`human_interaction` 是 agent 侧唯一「等一个人类答案」
的阻塞工具，缺省等待 86400s。run 卡在这个等待里时，引擎的待输入队列消费点
（只在 LLM 迭代开轮前）永远走不到，注入消息无限期滞留——外部表现就是
「注入了没反应 / 永久排队」。本文件的断言全部围绕「等待期检出停止信号 →
立刻收口 → run 回边界 → 排队的注入消息才有机会被 drain 消费」。

判定只认 `run_status == suspended`（显式停止信号），不按耗时猜卡死——
正常等用户作答几小时也不得被打断。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_TOOLS_DIR = _PLUGIN_DIR.parent


def _load_module(module_name: str, filename: str) -> Any:
    """importlib 显式路径加载（与 test_human_server._load_server 同款约定）。"""
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    sys.modules.pop("human", None)
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None, f"cannot load {filename}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def watch() -> Any:
    """看门狗模块本体（判定逻辑与 asyncio 编排都在这里，独立于 server 装配）。"""
    return _load_module("human_run_stop_watch_under_test", "run_stop_watch.py")


@pytest.fixture
def server() -> Any:
    return _load_module("human_server_run_stop_under_test", "server.py")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ════════════════════════════════════════════════════════════
# 旋钮解析：缺省/关闭/非法值三态（误杀防护的第一道闸）
# ════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, 2.0),  # 缺省 2s
        ({"AGENTOS_RUN_ABORT_POLL_SECS": ""}, 2.0),  # 空串 = 未配置
        ({"AGENTOS_RUN_ABORT_POLL_SECS": "0"}, 0.0),  # 显式关闭
        ({"AGENTOS_RUN_ABORT_POLL_SECS": "-3"}, 0.0),  # 负值 = 关闭
        ({"AGENTOS_RUN_ABORT_POLL_SECS": "abc"}, 0.0),  # 非法值 = 关闭（不静默变缺省）
        ({"AGENTOS_RUN_ABORT_POLL_SECS": "0.05"}, 0.05),
    ],
)
def test_resolve_poll_secs(watch: Any, environ: dict[str, str], expected: float) -> None:
    assert watch.resolve_poll_secs(environ) == pytest.approx(expected)


# ════════════════════════════════════════════════════════════
# 行状态解析：本管道精确匹配（别的管道 suspended 不得当本 run 的停止理由）
# ════════════════════════════════════════════════════════════


def test_pick_row_status_matches_only_own_pipeline(watch: Any) -> None:
    other_suspended = [{"pipeline_id": "other", "run_status": "suspended"}]
    assert watch.pick_row_status(other_suspended, "mine") is None

    mine_running = [
        {"pipeline_id": "other", "run_status": "suspended"},
        {"pipeline_id": "mine", "run_status": "running"},
    ]
    assert watch.pick_row_status(mine_running, "mine") == "running"

    mine_suspended = [{"pipeline_id": "mine", "status": "Suspended"}]
    assert watch.pick_row_status(mine_suspended, "mine") == "suspended"


def test_pick_row_status_non_list_and_empty(watch: Any) -> None:
    assert watch.pick_row_status(None, "mine") is None
    assert watch.pick_row_status([], "mine") is None
    assert watch.pick_row_status([{"pipeline_id": "mine"}], "mine") is None


# ════════════════════════════════════════════════════════════
# 轮询主循环：只在 suspended 收口；读异常 = 不可判定（不打断用户）
# ════════════════════════════════════════════════════════════


def test_watch_returns_true_on_suspended_after_running_polls(watch: Any) -> None:
    """running 若干窗后出现 suspended → 收口（模拟用户停止落标）。"""
    statuses = ["running", "running", "suspended"]

    async def _read() -> str | None:
        return statuses.pop(0) if statuses else "suspended"

    watcher = watch.RunStopWatcher(_read, 0.01, "pipe-A")
    assert _run(watcher.wait()) is True
    assert watcher.polls >= 2


def test_watch_ignores_terminal_statuses_and_read_errors(watch: Any) -> None:
    """completed 之类终态与读失败都不是停止理由——只有 suspended 才收口。"""
    calls = {"n": 0}

    async def _read() -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("读面通道故障")
        if calls["n"] == 2:
            return "completed"
        return "suspended"

    watcher = watch.RunStopWatcher(_read, 0.01, "pipe-B")
    assert _run(watcher.wait()) is True
    assert calls["n"] == 3, "读失败与终态都必须继续等待"


def test_watch_stop_bit_exits_false(watch: Any) -> None:
    """stop() 后退出且返回 False——工具正常结束时不伪造停止结果。"""
    watcher = watch.RunStopWatcher(lambda: asyncio.sleep(0, result="running"), 0.01, "pipe-C")

    async def _scenario() -> bool:
        task = asyncio.ensure_future(watcher.wait())
        await asyncio.sleep(0.02)
        watcher.stop()
        return await task

    assert _run(_scenario()) is False


# ════════════════════════════════════════════════════════════
# 赛跑编排：停止先到 → 取消工作；否则直通/等工作
# ════════════════════════════════════════════════════════════


def test_race_without_watcher_passes_through(watch: Any) -> None:
    async def _work() -> str:
        return "answered"

    stopped, value = _run(watch.race_with_stop(_work(), None))
    assert stopped is False
    assert value == "answered"


def test_race_stop_wins_cancels_slow_work(watch: Any) -> None:
    """负样本：工作项永不返回（模拟 24h 等待），停止信号到达即收口。"""
    cancelled = {"flag": False}

    async def _never() -> str:
        try:
            await asyncio.sleep(30)
            return "unreachable"
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise

    async def _read() -> str | None:
        return "suspended"

    watcher = watch.RunStopWatcher(_read, 0.01, "pipe-D")

    async def _scenario() -> tuple[bool, Any]:
        return await asyncio.wait_for(watch.race_with_stop(_never(), watcher), timeout=5)

    stopped, value = _run(_scenario())
    assert stopped is True
    assert value is None
    assert cancelled["flag"] is True, "停止收口必须取消在等的工作项（不留孤儿等待）"


def test_race_work_finishes_first_keeps_result(watch: Any) -> None:
    async def _fast() -> str:
        return "answered"

    async def _read() -> str | None:
        return "running"

    watcher = watch.RunStopWatcher(_read, 0.01, "pipe-E")
    stopped, value = _run(watch.race_with_stop(_fast(), watcher))
    assert stopped is False
    assert value == "answered"


# ════════════════════════════════════════════════════════════
# 能力装配：句柄缺失/旋钮关闭 → 看门狗不启用（工具本体行为不变）
# ════════════════════════════════════════════════════════════


class _FakeHandle:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, Any, Any]] = []

    async def call(self, method: str, params: Any, timeout: Any = None) -> Any:
        self.calls.append((method, params, timeout))
        return self._rows


class _FakePlugin:
    def __init__(self, capabilities: dict[str, Any]) -> None:
        self._caps = capabilities

    def get_capability(self, name: str) -> Any:
        if name not in self._caps:
            raise KeyError(name)
        return self._caps[name]


def test_start_watcher_disabled_without_pipeline_or_capability(watch: Any) -> None:
    handle = _FakeHandle([])
    assert watch.start_watcher(_FakePlugin({"service-registry": handle}), "") is None
    assert watch.start_watcher(_FakePlugin({}), "pipe-F") is None


def test_start_watcher_uses_primary_then_fallback(watch: Any) -> None:
    primary = _FakeHandle([])
    watcher = watch.start_watcher(_FakePlugin({"service-registry": primary}), "pipe-G")
    assert watcher is not None
    _run(watcher._read_status())
    assert primary.calls[0][0] == "pipeline-runs.list_by_pipeline"

    fallback = _FakeHandle([])
    watcher2 = watch.start_watcher(_FakePlugin({"pipeline-state": fallback}), "pipe-H")
    assert watcher2 is not None
    _run(watcher2._read_status())
    assert fallback.calls[0][0] == "list"


def test_start_watcher_respects_disabled_knob(watch: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_RUN_ABORT_POLL_SECS", "0")
    assert watch.start_watcher(_FakePlugin({"service-registry": _FakeHandle([])}), "pipe-I") is None


# ════════════════════════════════════════════════════════════
# server 装配：停止 → 取消交互 + RUN_STOPPED 如实返回（不是假成功）
# ════════════════════════════════════════════════════════════


class _StubService:
    """最小 service 替身：等待可控、取消可观测。"""

    def __init__(self, wait_impl: Any) -> None:
        self._wait_impl = wait_impl
        self.cancelled: list[tuple[str, str | None]] = []
        self.conversations: list[dict[str, Any]] = []

    async def create_conversation_request(self, **kwargs: Any) -> str:
        self.conversations.append(kwargs)
        return "rid-stop"

    async def wait_for_choice(self, rid: str, timeout: float | None = None) -> dict[str, Any]:
        return await self._wait_impl(rid, timeout)

    async def cancel_request(self, rid: str, reason: str | None = None) -> bool:
        self.cancelled.append((rid, reason))
        return True


def test_await_choice_cancels_interaction_on_run_stop(
    watch: Any, server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止先到：取消交互请求（卡片收口）+ 抛 _RunStoppedDuringWait。"""

    async def _never(_rid: str, _timeout: float | None) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {"response_type": "answered"}

    service = _StubService(_never)
    monkeypatch.setattr(server, "_service", service)
    monkeypatch.setattr(
        server,
        "_start_run_stop_watch",
        lambda _plugin, _pid: watch.RunStopWatcher(
            lambda: asyncio.sleep(0, result="suspended"), 0.01, _pid
        ),
    )
    monkeypatch.setattr(server, "_race_with_stop", watch.race_with_stop)

    with pytest.raises(server._RunStoppedDuringWait) as exc:
        _run(server._await_choice_or_run_stop("rid-stop", 86400, "pipe-J"))

    assert exc.value.request_id == "rid-stop"
    assert service.cancelled == [("rid-stop", "run_stopped")], "停止时必须取消交互请求"


def test_await_choice_passes_through_when_no_stop(
    watch: Any, server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常作答：看门狗不误杀，结果原样返回且不取消请求。"""

    async def _answer(_rid: str, _timeout: float | None) -> dict[str, Any]:
        return {"response_type": "answered", "selected_option": "ok"}

    service = _StubService(_answer)
    monkeypatch.setattr(server, "_service", service)
    monkeypatch.setattr(
        server,
        "_start_run_stop_watch",
        lambda _plugin, _pid: watch.RunStopWatcher(
            lambda: asyncio.sleep(0, result="running"), 0.01, _pid
        ),
    )
    monkeypatch.setattr(server, "_race_with_stop", watch.race_with_stop)

    result = _run(server._await_choice_or_run_stop("rid-ok", 5, "pipe-K"))
    assert result["selected_option"] == "ok"
    assert service.cancelled == []


def test_human_interaction_returns_run_stopped_envelope(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具入口：停止语义翻译为 RUN_STOPPED 显式结果（不得谎报 answered）。"""

    async def _stopped(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise server._RunStoppedDuringWait("rid-stop", "pipe-L")

    monkeypatch.setattr(server, "_service", _StubService(lambda *_: asyncio.sleep(0)))
    monkeypatch.setattr(server, "_do_conversation", _stopped)

    result = _run(server.human_interaction(title="要不要继续？", options=["要", "不要"], pipeline_id="pipe-L"))
    assert result["success"] is False
    assert result["error_code"] == "RUN_STOPPED"
    assert result["run_stopped"] is True
    assert result["request_id"] == "rid-stop"

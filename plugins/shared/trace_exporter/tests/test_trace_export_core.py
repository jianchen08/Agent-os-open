# @feature: FP-0.2.可观测性 E-1 trace_exporter | @ci: python-plugins-test
"""trace_export_core 测试：outbox 持久化/压实/有界、双阈值攒批、退避时序不变量、事件过滤。

时序纪律（AGENTS.md）：时序断言用 fake clock 注入记录**请求的延迟序列**断言
时序不变量（1→2→4→… 封顶 300），不靠真实等待；后台循环行为另用真实小间隔
用例（flush_interval=0.05s 真实 asyncio + 真实时钟）锁"时间阈值到点自动出批"。
断行为不断实现：断言公共入口（enqueue/flush_when_ready/load_pending）的
可观察输出，不窥内部队列。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from trace_export_core import (
    PROTOCOL_VERSION,
    EventFilter,
    OutboxStore,
    TraceExporter,
)
from ulid_keys import is_valid_ulid

URL = "http://cloud.example/ext/trace_ingestion/events"
TOKEN = "tok"
HOST = "dev-box"
KIND = "dev"
T0 = 1000.0


class FakeClock:
    """可调单调钟：手动推进（fake clock 注入，配合记录型 sleep 断延迟序列）。"""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordedSleep:
    """记录型 sleep（fake clock 语义）：零真实等待，记录请求延迟供时序断言。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class AdvancingSleep(RecordedSleep):
    """记录型 sleep + fake clock 推进：记录请求延迟并令假时钟真实流逝。

    比 RecordedSleep 更强一档：退避期间假时钟按请求延迟推进，时间阈值类
    判定（_batch_ready）看到的是退避后的时刻，时序不变量在假时间轴上成立。
    """

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self._clock.advance(seconds)


class FlakyCommitOutbox(OutboxStore):
    """磁盘 IO 故障注入面：装填次数内 commit 抛 OSError，其余委托真实实现。

    只替换 commit 落盘一步（重写发生前抛出，内存队列不动、事件不丢）；
    append/peek/load/__len__ 全部走真实 OutboxStore——关键路径真实依赖。
    """

    def __init__(self, path: Path, *, failures: int = 0) -> None:
        super().__init__(path)
        self._armed_failures = failures

    def arm(self, failures: int) -> None:
        """装填接下来连续失败的 commit 次数（公开测试面）。"""
        self._armed_failures = failures

    def commit(self, count: int) -> None:
        if self._armed_failures > 0:
            self._armed_failures -= 1
            raise OSError(28, "No space left on device")
        super().commit(count)


class PostSpy:
    """出站 POST 记录/编排：按脚本逐次决定成败，捕获信封供断言。"""

    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self.outcomes = list(outcomes)
        self.envelopes: list[dict] = []

    async def __call__(self, url: str, token: str, payload: dict) -> None:
        self.envelopes.append(json.loads(json.dumps(payload)))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


def make_exporter(
    tmp_path: Path,
    *,
    clock: FakeClock | None = None,
    sleep: RecordedSleep | None = None,
    post: PostSpy | None = None,
    batch_max: int = 100,
    flush_interval: float = 30.0,
    patterns: list[str] | None = None,
    max_bytes: int = 64 * 1024 * 1024,
    real_time: bool = False,
    outbox: OutboxStore | None = None,
) -> TraceExporter:
    clk = time.monotonic if real_time else (clock or FakeClock())
    slp = sleep or RecordedSleep()
    spy = post or PostSpy([])
    if outbox is None:  # 不用 `or`：OutboxStore 带 __len__，空队列 falsy 会顶掉注入件
        outbox = OutboxStore(tmp_path / "outbox.jsonl", max_bytes=max_bytes)
    return TraceExporter(
        ingestion_url=URL,
        token=TOKEN,
        origin_host=HOST,
        origin_kind=KIND,
        outbox=outbox,
        event_filter=EventFilter(patterns if patterns is not None else ["run.*"]),
        batch_max_events=batch_max,
        flush_interval_secs=flush_interval,
        post_json=spy,
        sleep_fn=slp,
        clock=clk,
    )


def enqueue(exporter: TraceExporter, n: int, event: str = "run.completed") -> None:
    for i in range(n):
        assert exporter.enqueue(event, {"i": i}, "2026-10-12T08:00:00+00:00")


# ── OutboxStore：持久化 / 压实 / 有界 ────────────────────────────


class TestOutboxStore:
    def test_append_peek_commit_roundtrip(self, tmp_path: Path) -> None:
        outbox = OutboxStore(tmp_path / "outbox.jsonl")
        for i in range(5):
            outbox.append({"ulid": f"U{i}", "event": "run.completed"})
        assert len(outbox) == 5
        batch = outbox.peek(3)
        assert [e["ulid"] for e in batch] == ["U0", "U1", "U2"]
        outbox.commit(3)
        assert len(outbox) == 2
        assert [e["ulid"] for e in outbox.peek(10)] == ["U3", "U4"]
        # 文件 = 待投递队列的序列化（游标语义：已投递前缀已物理移除）。
        lines = (tmp_path / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["ulid"] for line in lines] == ["U3", "U4"]

    def test_commit_zero_is_noop(self, tmp_path: Path) -> None:
        outbox = OutboxStore(tmp_path / "outbox.jsonl")
        outbox.append({"ulid": "U0"})
        outbox.commit(0)
        assert len(outbox) == 1

    def test_load_resumes_pending_after_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "outbox.jsonl"
        first = OutboxStore(path)
        first.append({"ulid": "U0", "event": "run.started"})
        first.append({"ulid": "U1", "event": "run.completed"})
        # 模拟重启：全新实例从文件恢复。
        second = OutboxStore(path)
        assert second.load() == 2
        assert [e["ulid"] for e in second.peek(10)] == ["U0", "U1"]

    def test_load_skips_corrupt_tail_line(self, tmp_path: Path) -> None:
        path = tmp_path / "outbox.jsonl"
        good = json.dumps({"ulid": "U0", "event": "run.started"})
        path.write_text(good + "\n" + good[:10] + "\n", encoding="utf-8")
        outbox = OutboxStore(path)
        assert outbox.load() == 1
        assert outbox.peek(10)[0]["ulid"] == "U0"

    def test_load_missing_file_is_empty(self, tmp_path: Path) -> None:
        outbox = OutboxStore(tmp_path / "absent.jsonl")
        assert outbox.load() == 0

    def test_bounded_drops_oldest_when_over_cap(self, tmp_path: Path) -> None:
        exporter = make_exporter(tmp_path, max_bytes=500, batch_max=1000)
        big_tags = {"blob": "x" * 60}
        for _ in range(10):
            exporter.enqueue("run.completed", big_tags, "2026-10-12T08:00:00+00:00")
        # 有界不变量（性质断言）：文件不超上限 + 单行余量；容量语义丢最旧且留痕计数。
        assert exporter.dropped_total > 0
        assert 1 <= exporter.pending_count <= 10 - exporter.dropped_total
        assert (tmp_path / "outbox.jsonl").stat().st_size <= 500 + 200


# ── EventFilter ──────────────────────────────────────────────────


class TestEventFilter:
    @pytest.mark.parametrize(
        ("patterns", "event", "expected"),
        [
            pytest.param(["run.*"], "run.completed", True, id="族通配命中"),
            pytest.param(["run.*"], "session.created", False, id="族外不命中"),
            pytest.param(["run.completed"], "run.completed", True, id="精确名命中"),
            pytest.param(["run.completed"], "run.failed", False, id="精确名不命中"),
            pytest.param(["run.*", "approval.*"], "approval.created", True, id="多模式任一"),
            pytest.param(["RUN.*"], "run.completed", False, id="大小写敏感"),
            pytest.param([], "run.completed", False, id="空配置全不导出"),
        ],
    )
    def test_matches(self, patterns: list[str], event: str, expected: bool) -> None:
        assert EventFilter(patterns).matches(event) is expected


# ── 入队与信封形状 ───────────────────────────────────────────────


class TestEnqueue:
    def test_envelope_shape_and_ulid_keys(self, tmp_path: Path) -> None:
        spy = PostSpy([])
        exporter = make_exporter(tmp_path, batch_max=2, post=spy)
        exporter.enqueue("run.completed", {"pipeline_id": "p-1"}, "2026-10-12T08:00:00+00:00")
        exporter.enqueue("run.failed", {}, "2026-10-12T08:00:01+00:00")
        asyncio.run(exporter.flush_when_ready())
        assert len(spy.envelopes) == 1
        envelope = spy.envelopes[0]
        assert envelope["origin_host"] == HOST
        assert envelope["origin_kind"] == KIND
        assert envelope["protocol_version"] == PROTOCOL_VERSION == 1
        assert [e["event"] for e in envelope["events"]] == ["run.completed", "run.failed"]
        assert all(is_valid_ulid(e["ulid"]) for e in envelope["events"])
        assert envelope["events"][0]["tags"] == {"pipeline_id": "p-1"}

    def test_filtered_event_not_enqueued(self, tmp_path: Path) -> None:
        spy = PostSpy([])
        exporter = make_exporter(tmp_path, post=spy)
        assert exporter.enqueue("session.created", {}, "t") is False
        assert exporter.pending_count == 0
        assert exporter.enqueued_total == 0
        asyncio.run(exporter.flush_when_ready())
        assert spy.envelopes == []


# ── 双阈值攒批 ───────────────────────────────────────────────────


class TestBatchThresholds:
    def test_size_threshold_triggers_flush(self, tmp_path: Path) -> None:
        spy = PostSpy([])
        exporter = make_exporter(tmp_path, batch_max=3, post=spy)
        enqueue(exporter, 2)
        assert asyncio.run(exporter.flush_when_ready()) is False  # 未达大小阈值、未到时间
        enqueue(exporter, 1)  # 第 3 条 → 大小阈值命中
        assert asyncio.run(exporter.flush_when_ready()) is True
        assert len(spy.envelopes[0]["events"]) == 3
        assert exporter.pending_count == 0
        assert exporter.flushed_total == 3

    def test_time_threshold_triggers_flush(self, tmp_path: Path) -> None:
        spy = PostSpy([])
        clock = FakeClock()
        exporter = make_exporter(
            tmp_path, batch_max=100, flush_interval=30.0, post=spy, clock=clock
        )
        enqueue(exporter, 2)
        clock.advance(29.9)
        assert asyncio.run(exporter.flush_when_ready()) is False
        clock.advance(0.2)  # 越过 30s
        assert asyncio.run(exporter.flush_when_ready()) is True
        assert len(spy.envelopes[0]["events"]) == 2

    def test_large_backlog_flushes_in_batches(self, tmp_path: Path) -> None:
        spy = PostSpy([])
        clock = FakeClock()
        exporter = make_exporter(tmp_path, batch_max=4, post=spy, clock=clock)
        enqueue(exporter, 10)
        asyncio.run(exporter.flush_when_ready())
        # 满批先行；余 2 条未到时间阈值不出批（双阈值纪律在排空段同样生效）。
        assert [len(e["events"]) for e in spy.envelopes] == [4, 4]
        assert exporter.pending_count == 2
        clock.advance(30.1)
        asyncio.run(exporter.flush_when_ready())
        assert [len(e["events"]) for e in spy.envelopes] == [4, 4, 2]
        assert exporter.pending_count == 0

    def test_empty_outbox_never_ready(self, tmp_path: Path) -> None:
        clock = FakeClock()
        exporter = make_exporter(tmp_path, clock=clock)
        clock.advance(10_000)
        assert asyncio.run(exporter.flush_when_ready()) is False

    def test_restored_pending_is_flush_ready_immediately(self, tmp_path: Path) -> None:
        # 断网恢复语义：重启恢复的待投递事件按已到期处理，首个循环拍即出批。
        spy = PostSpy([])
        outbox = OutboxStore(tmp_path / "outbox.jsonl")
        outbox.append({"ulid": "U0", "event": "run.completed"})
        exporter = make_exporter(tmp_path, post=spy)
        assert exporter.load_pending() == 1
        assert asyncio.run(exporter.flush_when_ready()) is True
        assert spy.envelopes[0]["events"][0]["ulid"] == "U0"


# ── 失败退避：指数序列 + 封顶 + 成功归零 + 事件不丢 ───────────────


class TestBackoff:
    def test_exponential_backoff_schedule_and_no_loss(self, tmp_path: Path) -> None:
        slp = RecordedSleep()
        spy = PostSpy([ConnectionError("refused"), ConnectionError("refused"), None])
        exporter = make_exporter(tmp_path, batch_max=2, sleep=slp, post=spy)
        enqueue(exporter, 2)

        async def drive() -> bool:
            delivered = False
            for _ in range(10):
                delivered = await exporter.flush_when_ready()
                if delivered:
                    break
            return delivered

        assert asyncio.run(drive()) is True
        # 时序不变量：请求的延迟按指数退避 1 → 2（fake clock 记录，非零延迟 mock）。
        assert slp.delays == [1.0, 2.0]
        # 失败期间事件留 outbox，成功后整批投出（断网不丢）。
        assert exporter.pending_count == 0
        assert exporter.failed_flushes == 2
        assert exporter.flushed_total == 2
        assert exporter.last_error == ""

    def test_backoff_capped_at_max(self, tmp_path: Path) -> None:
        slp = RecordedSleep()
        spy = PostSpy([ConnectionError("down")] * 10)
        exporter = make_exporter(tmp_path, batch_max=2, sleep=slp, post=spy)
        enqueue(exporter, 2)

        async def drive() -> None:
            for _ in range(20):
                await exporter.flush_when_ready()
                if len(slp.delays) >= 10:
                    break

        asyncio.run(drive())
        # 指数序列 1→2→4→…→256→300：第 10 拍触顶（封顶不变量）。
        assert slp.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 300.0]

    def test_success_resets_failure_streak(self, tmp_path: Path) -> None:
        slp = RecordedSleep()
        spy = PostSpy([ConnectionError("down"), None, ConnectionError("down"), None])
        exporter = make_exporter(tmp_path, batch_max=2, sleep=slp, post=spy)
        enqueue(exporter, 2)
        asyncio.run(exporter.flush_when_ready())  # 失败一次后成功
        enqueue(exporter, 2)
        asyncio.run(exporter.flush_when_ready())  # 再次失败一次后成功
        # 若未归零，第二轮首个延迟会是 4.0（1→2 之后 4）；归零则重新从 1 起。
        assert slp.delays == [1.0, 1.0]

    def test_failure_keeps_events_and_order(self, tmp_path: Path) -> None:
        calls = {"n": 0}

        async def always_fail(url: str, token: str, payload: dict) -> None:
            calls["n"] += 1
            raise ConnectionError("down")

        # 真实 asyncio.sleep 作退避睡眠：让出点使任务可取消（记录型 sleep 无
        # 让出点，配合恒败 post 会同步自旋）。batch_max=3 使 3 条事件立即达到
        # 大小阈值（失败路径只关心投递行为，不依赖时钟推进）。
        exporter = make_exporter(tmp_path, batch_max=3, post=always_fail, sleep=asyncio.sleep)
        enqueue(exporter, 3, event="run.started")

        async def drive() -> None:
            task = asyncio.create_task(exporter.flush_when_ready())
            await asyncio.sleep(0.05)  # 真实让出：让首个失败与退避睡眠发生
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(drive())
        assert calls["n"] >= 1
        assert exporter.pending_count == 3  # 事件全数留存（失败不推进消费）
        assert "down" in exporter.last_error
        assert exporter.failed_flushes >= 1

    def test_http_error_status_treated_as_failure(self, tmp_path: Path) -> None:
        slp = RecordedSleep()
        spy = PostSpy([RuntimeError("ingestion 401: invalid_token"), None])
        exporter = make_exporter(tmp_path, batch_max=1, post=spy, sleep=slp)
        assert exporter.enqueue("run.completed", {}, "t") is True
        asyncio.run(exporter.flush_when_ready())
        # 401 同样走退避重试（下一拍成功）——失败语义与网络错误一致，事件不丢。
        assert slp.delays == [1.0]
        assert exporter.pending_count == 0


# ── 后台循环（真实小间隔时序 + 真实时钟，非 mock） ────────────────


class TestBackgroundLoop:
    def test_loop_flushes_on_time_threshold(self, tmp_path: Path) -> None:
        spy = PostSpy([])
        exporter = make_exporter(
            tmp_path, batch_max=100, flush_interval=0.05, post=spy, real_time=True
        )

        async def drive() -> None:
            exporter.start()  # start 需在事件循环内（创建任务）
            exporter.enqueue("run.completed", {}, "t")
            for _ in range(100):
                await asyncio.sleep(0.02)
                if exporter.flushed_total == 1:
                    break
            await exporter.stop()

        asyncio.run(drive())
        assert exporter.flushed_total == 1
        assert exporter.pending_count == 0

    def test_stop_is_idempotent_and_start_twice_single_loop(self, tmp_path: Path) -> None:
        exporter = make_exporter(tmp_path, real_time=True)

        async def drive() -> None:
            exporter.start()
            exporter.start()  # 幂等：不叠加任务
            await exporter.stop()
            await exporter.stop()

        asyncio.run(drive())


# ── 磁盘 IO 兜底（P1-17/X-5）：outbox 落盘失败不终结循环、告警可见、退避统一 ──


class TestDiskIoFailureBackstop:
    """outbox 落盘失败（commit/_rewrite，OSError 族）与网络失败同款兜底。

    缺陷：commit 抛穿 `_run` 直接终结后台 task 且 last_error 不写（仅网络分支
    写）→ 导出静默停摆。契约：落盘失败写 last_error + WARNING 告警 + 指数退避
    续跑不终结；成功归零恢复投递。

    注入面两条（关键路径真实依赖，不 mock 内部）：
    - 真实磁盘故障：outbox 文件替换为同名目录 → 真实 OutboxStore 重写必抛
      OSError（os.replace 目标为目录，Win PermissionError / POSIX
      IsADirectoryError，同属 OSError 族）；
    - 故障装填：OutboxStore 子类只替换 commit 一步（重写发生前抛出），其余委托
      真实实现。
    时序断言：FakeClock + AdvancingSleep 记录请求延迟并推进假时钟，断言指数
    序列（1→2→…封顶 300），零真实等待、无零延迟 mock。
    """

    @staticmethod
    async def _wait_until(
        predicate: Callable[[], bool], timeout: float, message: str
    ) -> None:
        """轮询等待至多 timeout 秒（真实让出；超时即失败——防死等掩蔽循环停摆）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError(message)

    @staticmethod
    async def _drive_until_delivered(exporter: TraceExporter, rounds: int = 10) -> bool:
        """逐拍调用 flush_when_ready 直到投递成功或轮次耗尽。"""
        for _ in range(rounds):
            if await exporter.flush_when_ready():
                return True
        return False

    def test_real_disk_io_error_loop_survives_alerts_and_recovers(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """真实磁盘故障：循环存活不终结 + last_error/WARNING 可见 + 恢复清零续投。"""
        outbox_path = tmp_path / "outbox.jsonl"
        exporter = make_exporter(
            tmp_path, batch_max=2, real_time=True, sleep=asyncio.sleep
        )

        async def drive() -> None:
            enqueue(exporter, 2)  # 大小阈值即满，文件已落盘两行
            outbox_path.unlink()
            outbox_path.mkdir()  # 真实磁盘故障注入：重写目标成目录
            with caplog.at_level(logging.WARNING):
                exporter.start()
                # 修复前：commit OSError 抛穿终结 task → last_error 永不置位，此处超时红。
                await self._wait_until(
                    lambda: exporter.last_error != "",
                    timeout=5.0,
                    message="落盘失败未置 last_error（循环已静默终结？）",
                )
            assert exporter.flushed_total == 0, "故障期间不得推进投递计数"
            assert exporter.failed_flushes == 1
            outbox_path.rmdir()  # 解除故障：磁盘恢复
            enqueue(exporter, 2)  # 新事件进队（唤醒循环）
            await self._wait_until(
                lambda: exporter.flushed_total >= 2,
                timeout=5.0,
                message="落盘失败后循环未续跑投递（后台 task 已终结）",
            )
            assert exporter.last_error == "", "恢复成功后 last_error 须清零"
            assert exporter.pending_count == 0
            await exporter.stop()

        asyncio.run(drive())
        # 告警面：WARNING 在案且指明落盘失败（插件侧告警 = 日志面 + last_error 读面）。
        warnings = [
            r.getMessage()
            for r in caplog.records
            if "outbox 落盘失败" in r.getMessage()
        ]
        assert len(warnings) == 1, "落盘失败须留 WARNING 告警（与网络分支同一告警面）"
        assert "第 1 次" in warnings[0]

    def test_io_error_backoff_sequence_on_fake_clock(self, tmp_path: Path) -> None:
        """IO 失败退避序列与网络分支同一：1→2 指数、成功归零后重起于基础窗。"""
        clock = FakeClock()
        slp = AdvancingSleep(clock)
        outbox = FlakyCommitOutbox(tmp_path / "outbox.jsonl", failures=2)
        exporter = make_exporter(
            tmp_path, batch_max=2, clock=clock, sleep=slp, outbox=outbox
        )
        enqueue(exporter, 2)

        # 修复前：首个 commit OSError 抛穿 flush_when_ready，本断言前即红。
        assert asyncio.run(self._drive_until_delivered(exporter)) is True
        assert slp.delays == [1.0, 2.0], "IO 失败按指数退避请求延迟（与网络分支同一序列）"
        # 性质断言：逐项 min(前窗×2, 封顶 300)，且记录的假时钟真实流逝。
        assert all(
            slp.delays[i + 1] == min(slp.delays[i] * 2, 300.0)
            for i in range(len(slp.delays) - 1)
        )
        assert clock.now == T0 + sum(slp.delays), "退避期间假时钟按请求延迟推进"
        # 失败不丢事件：恢复后整批投出、账面清零。
        assert exporter.pending_count == 0
        assert exporter.flushed_total == 2
        assert exporter.failed_flushes == 2
        assert exporter.last_error == ""

        # 恢复正常后的新失败从基础窗重起（成功归零性质跨 IO 分支成立）。
        outbox.arm(1)
        enqueue(exporter, 2)
        assert asyncio.run(self._drive_until_delivered(exporter)) is True
        assert slp.delays == [1.0, 2.0, 1.0], "成功归零后新失败退避重起于 1s（非 4s 续涨）"
        assert exporter.flushed_total == 4

    def test_network_and_io_failures_share_one_backoff_streak(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """区分度：网络异常与磁盘 IO 异常同批交替——共用连续计数、各留阶段告警。"""
        clock = FakeClock()
        slp = AdvancingSleep(clock)
        outbox = FlakyCommitOutbox(tmp_path / "outbox.jsonl", failures=1)
        spy = PostSpy([ConnectionError("down"), None, None])
        exporter = make_exporter(
            tmp_path, batch_max=2, clock=clock, sleep=slp, post=spy, outbox=outbox
        )
        enqueue(exporter, 2)

        with caplog.at_level(logging.WARNING):
            assert asyncio.run(self._drive_until_delivered(exporter)) is True

        # 统一记账：网络失败（窗 1）→ IO 失败（窗 2）同一连续计数继续指数上涨。
        assert slp.delays == [1.0, 2.0]
        assert exporter.failed_flushes == 2
        assert exporter.flushed_total == 2
        assert exporter.last_error == ""
        # 两种失败阶段各自留痕（同一 WARNING 告警面、阶段可辨）。
        messages = [r.getMessage() for r in caplog.records]
        assert any("批量投递失败" in m for m in messages), "网络失败留原阶段告警"
        assert any("outbox 落盘失败" in m for m in messages), "IO 失败留落盘阶段告警"

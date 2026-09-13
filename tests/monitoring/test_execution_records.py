# @feature: FP-0.2.可观测性 可观测性基座 | @ci: python-coverage
"""monitoring execution/records + sessions token-usage 域 handler 缺口补测。

覆盖面（按行为分组）：
- recent_pipelines：同管道多 run 去重取最新、无 pipeline_id 跳过、limit 截断；
- _message_to_record：tool_calls_json / tool_result_json 非法 JSON 降级 None
  （合法解析对照）；
- get_execution_record：同管道多 run 去重（只取一次消息快照）、未命中缺省形态；
- list_orphan_runs：running + 时长阈值过滤、非法 started_at 跳过、按时长倒序、
  limit 截断与 total 保真；
- _state_total_tokens：非数值 token 降级 0（数值/数值字符串对照）。

内核数据入口走 kernel_reads.set_provider 公共注入口（provider 由 on_load 注入的
既有测试契约），不做内核进程间调用。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import execution_records as er
import kernel_reads
import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试


@pytest.fixture(autouse=True)
def _clean_providers():
    kernel_reads.reset_providers()
    yield
    kernel_reads.reset_providers()


def _set_provider(name: str, rows):
    async def _fn(*args, **kwargs):
        return rows

    kernel_reads.set_provider(name, _fn)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# ═══════════════════════════════════════════════════════════
# recent_pipelines：最近 N 个去重管道
# ═══════════════════════════════════════════════════════════


class TestRecentPipelines:
    async def test_dedup_keeps_first_run_and_skips_missing_pid(self):
        """runs 倒序入参：同管道多 run 只保留首个（最新）；无 pipeline_id 跳过。"""
        runs = [
            {"run_id": "r1", "pipeline_id": "p1", "started_at": "2026-09-13T02:00:00Z"},
            {"run_id": "r2", "pipeline_id": "p1", "started_at": "2026-09-13T01:00:00Z"},
            {"run_id": "r3"},  # 无 pipeline_id
            {"run_id": "r4", "pipeline_id": None},
            {"run_id": "r5", "pipeline_id": "p2", "started_at": "2026-09-13T00:00:00Z"},
        ]
        seen_calls: list[dict] = []

        async def _fn(*args, **kwargs):
            seen_calls.append(kwargs)
            return runs

        kernel_reads.set_provider("pipeline-runs", _fn)

        out = await er.recent_pipelines()

        assert seen_calls == [{"status": None, "limit": 500}]  # search 域共用扫描边界
        assert [r["run_id"] for r in out] == ["r1", "r5"]  # 去重取最新 + 跳过无 pid

    async def test_limit_stops_after_n_distinct_pipelines(self):
        """limit 按"去重后管道数"截断（同管道重复不占名额）。"""
        runs = [
            {"run_id": f"r{i}", "pipeline_id": f"p{i // 2}"} for i in range(6)  # p0,p0,p1,p1,p2,p2
        ]
        _set_provider("pipeline-runs", runs)

        out = await er.recent_pipelines(limit=2)

        assert [r["pipeline_id"] for r in out] == ["p0", "p1"]


# ═══════════════════════════════════════════════════════════
# _message_to_record：JSON 信封解析失败降级
# ═══════════════════════════════════════════════════════════


class TestMessageToRecordJsonFallback:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("not-json", None),  # 非法 JSON → None
            (42, None),  # 非字符串（json.loads TypeError）→ None
            ('[{"id": "c1", "name": "f"}]', [{"id": "c1", "name": "f"}]),  # 合法 → 结构化对象
        ],
        ids=["invalid-json", "non-string", "valid-json"],
    )
    def test_tool_calls_json_parsing(self, raw, expected):
        msg = {"message_id": "m1", "role": "assistant", "tool_calls_json": raw}
        record = er._message_to_record(msg, "p1")
        assert record["message_data"]["tool_calls"] == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("{bad", None),  # 非法 JSON → None
            (3.5, None),  # 非字符串 → None
            ('{"ok": true}', {"ok": True}),  # 合法 → 结构化对象
        ],
        ids=["invalid-json", "non-string", "valid-json"],
    )
    def test_tool_result_json_parsing(self, raw, expected):
        msg = {"message_id": "m1", "role": "tool", "tool_result_json": raw}
        record = er._message_to_record(msg, "p1")
        assert record["message_data"]["tool_result"] == expected


# ═══════════════════════════════════════════════════════════
# get_execution_record：线性查找 + 去重 + 未命中缺省
# ═══════════════════════════════════════════════════════════


class TestGetExecutionRecord:
    async def test_found_after_scanning_multiple_pipelines(self):
        """目标不在首个管道快照 → 继续扫描后续管道；命中返回完整 ExecutionRecord。"""
        _set_provider("pipeline-runs", [
            {"run_id": "r1", "pipeline_id": "p1", "started_at": "2026-09-13T01:00:00Z"},
            {"run_id": "r2", "pipeline_id": "p2", "started_at": "2026-09-13T02:00:00Z"},
        ])

        async def _messages(*args, pipeline_id=None, limit=None):
            if pipeline_id == "p1":
                return [{"message_id": "m-other", "role": "user", "content_preview": "hi"}]
            return [{"message_id": "m-target", "role": "assistant", "content_preview": "答",
                     "seq_in_branch": 1, "created_at": "2026-09-13T02:00:05Z", "run_id": "r2"}]

        kernel_reads.set_provider("messages", _messages)

        record = await er.get_execution_record("m-target")

        assert record["id"] == "m-target"
        assert record["session_id"] == "p2"
        assert record["message_data"]["content"] == "答"
        assert record["sequence"] == 1

    async def test_duplicate_pipeline_scanned_once_and_default_on_miss(self):
        """同管道多 run：去重后只取一次消息快照（重复 run 走 continue）；
        未命中 → 缺省形态（空 message_data）。"""
        runs = [
            {"run_id": "r2", "pipeline_id": "p1", "started_at": "2026-09-13T02:00:00Z"},
            {"run_id": "r1", "pipeline_id": "p1", "started_at": "2026-09-13T01:00:00Z"},
        ]
        calls: list[tuple] = []

        async def _fn(*args, pipeline_id=None, limit=None):
            calls.append((pipeline_id, limit))
            return [{"message_id": "m-other", "role": "user", "content_preview": "hi"}]

        async def _runs(*args, **kwargs):
            return runs

        kernel_reads.set_provider("pipeline-runs", _runs)
        kernel_reads.set_provider("messages", _fn)

        record = await er.get_execution_record("m-target")

        assert calls == [("p1", er._PER_SESSION_MSG_LIMIT)]  # 去重：同管道只查一次
        assert record == {"id": "m-target", "session_id": "", "message_data": {}, "created_at": ""}


# ═══════════════════════════════════════════════════════════
# list_orphan_runs：孤儿管道观测
# ═══════════════════════════════════════════════════════════


class TestListOrphanRuns:
    def _seed(self):
        now = datetime.now(UTC)
        runs = [
            {"run_id": "r-older", "pipeline_id": "p-old2", "thread_id": "t2",
             "status": "running", "started_at": _iso(now - timedelta(minutes=180))},
            {"run_id": "r-old", "pipeline_id": "p-old1", "thread_id": "t1",
             "status": "running", "started_at": _iso(now - timedelta(minutes=120))},
            # 以下三条被过滤：时长不足 / 非法时间 / 缺 started_at
            {"run_id": "r-recent", "pipeline_id": "p-new", "status": "running",
             "started_at": _iso(now - timedelta(minutes=1))},
            {"run_id": "r-bad", "pipeline_id": "p-bad", "status": "running",
             "started_at": "not-a-date"},
            {"run_id": "r-nostart", "pipeline_id": "p-nostart", "status": "running"},
        ]
        states = [
            {"pipeline_id": "p-old1", "display_name": "会话一", "message_count": 7,
             "task.status": "running", "current_phase": "llm", "suspended": False},
            {"pipeline_id": "p-old2", "task.goal": "写贪吃蛇"},
        ]
        return runs, states

    async def test_filters_and_sorts_by_age_desc(self):
        """孤儿判据 = running 且时长超阈值；按 age 倒序；state 摘要补标题/阶段。"""
        runs, states = self._seed()
        calls: list[dict] = []

        async def _runs(*args, **kwargs):
            calls.append(kwargs)
            return runs

        kernel_reads.set_provider("pipeline-runs", _runs)
        _set_provider("pipeline-state", states)

        body = await er.list_orphan_runs(min_minutes=10)

        assert calls == [{"status": "running", "limit": 200}]
        assert [it["pipeline_id"] for it in body["items"]] == ["p-old2", "p-old1"]  # 时长倒序
        # 性质断言：age 单调递减且与阈值口径一致
        ages = [it["age_minutes"] for it in body["items"]]
        assert ages == sorted(ages, reverse=True)
        assert all(a >= 10 for a in ages)
        assert 115 <= ages[1] <= 125  # ~120 分钟 ± 容差
        older, old = body["items"]
        assert older["title"] == "写贪吃蛇"  # state.task.goal 兜底标题
        assert old["title"] == "会话一"  # display_name 优先
        assert old["message_count"] == 7
        assert old["task_status"] == "running"
        assert body["total"] == 2
        assert body["min_minutes"] == 10

    async def test_limit_slices_items_but_keeps_total(self):
        """limit 截断呈现条数，total 保真全量候选数（前端分页依据）。"""
        runs, states = self._seed()
        _set_provider("pipeline-runs", runs)
        _set_provider("pipeline-state", states)

        body = await er.list_orphan_runs(min_minutes=10, limit=1)

        assert len(body["items"]) == 1
        assert body["items"][0]["pipeline_id"] == "p-old2"  # 最久的先呈现
        assert body["total"] == 2
        assert body["min_minutes"] == 10


# ═══════════════════════════════════════════════════════════
# sessions token-usage：_state_total_tokens 降级
# ═══════════════════════════════════════════════════════════


class TestStateTotalTokens:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("abc", 0),  # 非数值字符串 → 0
            (["x"], 0),  # 不可转换类型 → 0
            (3400, 3400),  # 数值直取
            ("5500", 5500),  # 数值字符串解析
        ],
        ids=["non-numeric-str", "non-convertible-type", "int", "numeric-str"],
    )
    async def test_session_total_token_usage(self, value, expected):
        """token 总量取 state 摘要行累计位；非法值降级 0 不阻断，请求数照常统计。"""
        _set_provider(
            "pipeline-state",
            [{"pipeline_id": "p1", "track.total_tokens": value}],
        )
        _set_provider(
            "pipeline-runs",
            [
                {"run_id": "r1", "pipeline_id": "p1"},
                {"run_id": "r2", "pipeline_id": "p1"},
                {"run_id": "r3", "pipeline_id": "p-other"},
            ],
        )

        body = await er.get_session_total_token_usage("p1")

        assert body["total_tokens"] == expected
        assert body["request_count"] == 2  # 只计本管道 run 次数
        assert body["session_id"] == "p1"

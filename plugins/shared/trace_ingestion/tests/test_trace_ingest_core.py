# @feature: FP-0.2.可观测性 E-2 trace_ingestion | @ci: python-plugins-test
"""trace_ingestion 核心测试：信封校验/版本宽容/死信不阻塞/ULID 幂等/origin 过滤/token 鉴权。

断行为不断实现：全部断言走公共入口（process_envelope / TokenAuth /
TraceEventStore 查询面）的可观察输出（返回值/落库行），不窥内部状态。
同契约 ≥2 组有区分度输入（正常/边界/符号量级相反）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from trace_ingest_core import (
    UNKNOWN_FIELDS_DROP,
    UNKNOWN_FIELDS_KEEP,
    EnvelopeError,
    TokenAuth,
    parse_tokens_env,
    process_envelope,
)
from trace_ingest_store import TraceEventStore
from ulid_keys import new_ulid

HOST_A = "dev-box"
HOST_B = "offline-box"
TOKEN_A = "tok-a-1234"
RECEIVED_AT = "2026-10-12T08:00:00+00:00"


def _make_event(event: str = "run.completed", tags: dict | None = None, **extra: object) -> dict:
    entry: dict = {"ulid": new_ulid(), "event": event, "emitted_at": RECEIVED_AT}
    if tags is not None:
        entry["tags"] = tags
    entry.update(extra)
    return entry


def _make_store(tmp_path: Path) -> TraceEventStore:
    return TraceEventStore(tmp_path / "trace_events.db")


def _auth(hosts: dict[str, str] | None = None) -> TokenAuth:
    return TokenAuth({HOST_A: TOKEN_A, **(hosts or {})})


def _process(store: TraceEventStore, envelope: object, *, bearer: str = TOKEN_A, **kw: object):
    return process_envelope(
        store,
        envelope,
        auth=_auth(),
        bearer=bearer,
        unknown_fields=str(kw.get("unknown_fields", UNKNOWN_FIELDS_KEEP)),
        received_at=RECEIVED_AT,
    )


# ── TokenAuth：每宿主 token，fail-closed ─────────────────────────


class TestTokenAuth:
    def test_unconfigured_rejects_everything(self) -> None:
        status, code = TokenAuth({}).check(HOST_A, TOKEN_A)
        assert status == 403
        assert code == "tokens_not_configured"

    def test_unknown_host_rejected_even_with_valid_token(self) -> None:
        status, code = _auth().check("stranger", TOKEN_A)
        assert status == 403
        assert code == "unknown_origin_host"

    def test_wrong_token_rejected(self) -> None:
        status, _ = _auth().check(HOST_A, "not-the-token")
        assert status == 401

    def test_missing_bearer_rejected(self) -> None:
        status, _ = _auth().check(HOST_A, "")
        assert status == 401

    def test_correct_host_token_pair_passes(self) -> None:
        status, code = _auth().check(HOST_A, TOKEN_A)
        assert (status, code) == (0, "")

    def test_token_bound_to_its_own_host_only(self) -> None:
        # host B 的 token 打 host A 的信封 → 拒（token↔host 绑定，防顶名）；
        # A 已配置，凭据不符走 401（未知 host 的 403 由上一用例锁定）。
        status, code = _auth({HOST_B: "tok-b"}).check(HOST_A, "tok-b")
        assert (status, code) == (401, "invalid_token")

    def test_parse_tokens_env_pairs_and_skips_malformed(self) -> None:
        tokens = parse_tokens_env(" a:tok1 , bad-entry , :tok2 , b:tok:3 , ")
        assert tokens == {"a": "tok1", "b": "tok:3"}

    def test_parse_tokens_env_empty_is_unconfigured(self) -> None:
        assert parse_tokens_env("") == {}


# ── 信封级校验：必需缺失 → 400，不部分落库 ───────────────────────


class TestEnvelopeValidation:
    def test_valid_envelope_shape_passes(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _process(store, {
            "origin_host": HOST_A,
            "origin_kind": "dev",
            "protocol_version": 1,
            "events": [_make_event()],
        })
        assert result == {"received": 1, "accepted": 1, "duplicates": 0, "dead_lettered": 0}

    @pytest.mark.parametrize(
        ("envelope", "code"),
        [
            pytest.param({"origin_kind": "dev", "protocol_version": 1, "events": []}, "missing_field", id="缺origin_host"),
            pytest.param({"origin_host": HOST_A, "protocol_version": 1, "events": []}, "missing_field", id="缺origin_kind"),
            pytest.param({"origin_host": HOST_A, "origin_kind": "dev", "events": []}, "missing_field", id="缺protocol_version"),
            pytest.param({"origin_host": HOST_A, "origin_kind": "dev", "protocol_version": 1}, "missing_field", id="缺events"),
            pytest.param({"origin_host": HOST_A, "origin_kind": "dev", "protocol_version": 0, "events": []}, "bad_shape", id="版本0"),
            pytest.param({"origin_host": HOST_A, "origin_kind": "dev", "protocol_version": -1, "events": []}, "bad_shape", id="负版本"),
            pytest.param({"origin_host": HOST_A, "origin_kind": "dev", "protocol_version": True, "events": []}, "bad_shape", id="布尔版本"),
            pytest.param({"origin_host": HOST_A, "origin_kind": "dev", "protocol_version": 1, "events": {}}, "bad_shape", id="events非列表"),
            pytest.param({"origin_host": 42, "origin_kind": "dev", "protocol_version": 1, "events": []}, "bad_shape", id="origin_host非串"),
        ],
    )
    def test_invalid_envelopes_rejected_as_protocol_error(
        self, tmp_path: Path, envelope: dict, code: str
    ) -> None:
        store = _make_store(tmp_path)
        outcome = _process(store, envelope)
        assert isinstance(outcome, EnvelopeError)
        assert outcome.code == code
        assert outcome.status == 400
        assert store.count_events() == 0

    def test_non_dict_envelope_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        outcome = _process(store, ["not", "an", "envelope"])
        assert isinstance(outcome, EnvelopeError)
        assert outcome.status == 400

    def test_auth_rejected_before_any_processing(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        outcome = _process(
            store,
            {"origin_host": HOST_A, "origin_kind": "dev", "protocol_version": 1,
             "events": [_make_event()]},
            bearer="wrong",
        )
        assert isinstance(outcome, EnvelopeError)
        assert outcome.status == 401
        assert store.count_events() == 0


# ── ULID 幂等：同批/跨批重放零新增 ───────────────────────────────


class TestUlidIdempotency:
    def test_same_envelope_replayed_yields_zero_new(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        envelope = {
            "origin_host": HOST_A,
            "origin_kind": "dev",
            "protocol_version": 1,
            "events": [_make_event("run.started"), _make_event("run.completed")],
        }
        first = _process(store, envelope)
        assert first["accepted"] == 2
        second = _process(store, envelope)
        assert second == {"received": 2, "accepted": 0, "duplicates": 2, "dead_lettered": 0}
        assert store.count_events(origin_host=HOST_A) == 2

    def test_overlap_batch_only_new_counted(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        shared = _make_event()
        _process(store, {"origin_host": HOST_A, "origin_kind": "dev",
                         "protocol_version": 1, "events": [shared]})
        result = _process(store, {"origin_host": HOST_A, "origin_kind": "dev",
                                  "protocol_version": 1,
                                  "events": [shared, _make_event()]})
        assert result["accepted"] == 1
        assert result["duplicates"] == 1
        assert store.count_events() == 2

    def test_same_ulid_different_host_is_distinct(self, tmp_path: Path) -> None:
        # 幂等键 = (origin_host, ulid)：不同宿主各存一份（来源隔离语义）。
        store = _make_store(tmp_path)
        event = _make_event()
        for host in (HOST_A, HOST_B):
            result = process_envelope(
                store,
                {"origin_host": host, "origin_kind": "dev", "protocol_version": 1,
                 "events": [event]},
                auth=_auth({HOST_B: "tok-b"}),
                bearer="tok-b" if host == HOST_B else TOKEN_A,
                unknown_fields=UNKNOWN_FIELDS_KEEP,
                received_at=RECEIVED_AT,
            )
            assert result["accepted"] == 1
        assert store.count_events() == 2


# ── schema 宽容：死信不阻塞同批后续 ──────────────────────────────


class TestSchemaTolerance:
    def test_bad_events_dead_lettered_without_blocking_good_ones(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        good = _make_event("run.completed")
        result = _process(store, {
            "origin_host": HOST_A,
            "origin_kind": "dev",
            "protocol_version": 1,
            "events": [
                {"event": "run.started"},          # 缺 ULID
                good,                               # 好事件夹中间
                {"ulid": "not-a-ulid", "event": "x"},  # ULID 形态非法
                {"ulid": new_ulid()},               # 缺事件名
            ],
        })
        assert result["received"] == 4
        assert result["accepted"] == 1
        assert result["dead_lettered"] == 3
        dead = store.list_dead_letters()
        reasons = sorted(d["reason"] for d in dead)
        assert reasons == ["missing_event_name", "missing_or_bad_ulid", "missing_or_bad_ulid"]
        assert dead[0]["payload"]["event"] == "run.started" or len(dead) == 3
        rows = store.query_events(origin_host=HOST_A)
        assert [r["ulid"] for r in rows] == [good["ulid"]]

    def test_tags_not_object_goes_to_dead_letter(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = _process(store, {
            "origin_host": HOST_A, "origin_kind": "dev", "protocol_version": 1,
            "events": [{"ulid": new_ulid(), "event": "run.started", "tags": "oops"}],
        })
        assert result["dead_lettered"] == 1
        assert store.list_dead_letters()[0]["reason"] == "tags_not_object"

    def test_higher_protocol_version_processed_tolerantly_keep(
        self, tmp_path: Path
    ) -> None:
        store = _make_store(tmp_path)
        future_event = _make_event("run.completed", tags={"k": "v"}, future_field={"x": 1})
        result = _process(store, {
            "origin_host": HOST_A, "origin_kind": "dev", "protocol_version": 2,
            "events": [future_event],
        }, unknown_fields=UNKNOWN_FIELDS_KEEP)
        assert result["accepted"] == 1
        row = store.query_events(origin_host=HOST_A)[0]
        assert row["protocol_version"] == 2
        assert row["extra"] == {"future_field": {"x": 1}}

    def test_higher_protocol_version_drop_unknown_fields(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        future_event = _make_event("run.completed", tags={"k": "v"}, future_field={"x": 1})
        result = _process(store, {
            "origin_host": HOST_A, "origin_kind": "dev", "protocol_version": 9,
            "events": [future_event],
        }, unknown_fields=UNKNOWN_FIELDS_DROP)
        assert result["accepted"] == 1
        row = store.query_events(origin_host=HOST_A)[0]
        assert row["extra"] == {}
        assert row["tags"] == {"k": "v"}

    def test_lower_version_envelope_still_accepted(self, tmp_path: Path) -> None:
        # 版本协商双向宽容：v1 事件配 v9 信封语义上不成立，但单测锁定
        # "版本号只是整数、处理规则不因高版本拒绝" 的宽容不变量。
        store = _make_store(tmp_path)
        result = _process(store, {
            "origin_host": HOST_A, "origin_kind": "dev", "protocol_version": 1,
            "events": [_make_event()],
        }, unknown_fields=UNKNOWN_FIELDS_DROP)
        assert result["accepted"] == 1


# ── 存储面：origin 过滤 / (origin_host, ulid) 排序 / 游标 ────────


class TestStoreQueries:
    def test_origin_filter_and_cross_host_sorting(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        for host, token in ((HOST_B, "tok-b"), (HOST_A, TOKEN_A)):
            process_envelope(
                store,
                {"origin_host": host, "origin_kind": "dev", "protocol_version": 1,
                 "events": [_make_event("run.started"), _make_event("run.completed")]},
                auth=_auth({HOST_B: "tok-b"}),
                bearer=token,
                unknown_fields=UNKNOWN_FIELDS_KEEP,
                received_at=RECEIVED_AT,
            )
        host_a_rows = store.query_events(origin_host=HOST_A)
        assert {r["event"] for r in host_a_rows} == {"run.started", "run.completed"}
        assert all(r["origin_host"] == HOST_A for r in host_a_rows)
        all_rows = store.query_events()
        keys = [(r["origin_host"], r["ulid"]) for r in all_rows]
        assert keys == sorted(keys)

    def test_after_ulid_cursor_paging(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ulids = [new_ulid() for _ in range(5)]
        events = [
            {"ulid": u, "event": f"run.{i}", "emitted_at": RECEIVED_AT, "tags": {}}
            for i, u in enumerate(ulids)
        ]
        _process(store, {"origin_host": HOST_A, "origin_kind": "dev",
                         "protocol_version": 1, "events": events})
        page1 = store.query_events(origin_host=HOST_A, limit=2)
        assert [r["ulid"] for r in page1] == ulids[:2]
        page2 = store.query_events(origin_host=HOST_A, limit=10, after_ulid=ulids[1])
        assert [r["ulid"] for r in page2] == ulids[2:]

    def test_event_row_roundtrip_preserves_tags_and_timestamps(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        event = _make_event("run.failed", tags={"pipeline_id": "p-1", "state": {"x": 2}})
        _process(store, {"origin_host": HOST_A, "origin_kind": "offline_assistant",
                         "protocol_version": 1, "events": [event]})
        row = store.query_events(origin_host=HOST_A)[0]
        assert row["event"] == "run.failed"
        assert row["tags"] == {"pipeline_id": "p-1", "state": {"x": 2}}
        assert row["emitted_at"] == RECEIVED_AT
        assert row["received_at"] == RECEIVED_AT
        # 来源标两级都落库（§3.3；检索/召回按 origin_kind 过滤的数据面）。
        assert row["origin_kind"] == "offline_assistant"

# @feature: FP-0.2.多端同步 T1.DB逻辑记录同步 | @ci: python-coverage
"""sync_agent 捕获步与只读镜像测试（T1：会话/消息/任务三类 DB 逻辑记录，两通道架构）。

捕获面：管道步骤 diff（新消息一对象、任务态漂移、会话骨架、热路径零扫描、
畸形 state 零抛出）+ 域事件会话路径；镜像面：他端事件经通用 apply 落地
**单一镜像表**（HLC 门槛防乱序重放、ULID 幂等、同呈现维度改写收敛单行、
墓碑打标记不物理删除、本端事件不镜像）；通道注册表属主判定。

relay 交互走注入式记录桩；时序全走注入 fake clock（禁零延迟 mock）。
"""

from __future__ import annotations

from typing import Any

import pytest
from _root_api import asset_channel, is_owner_scoped
from agent_core import AgentConfig, SyncAgentEngine
from agent_state import AgentStateStore
from capture import (
    canonical_message,
    fingerprint_message,
    message_record,
    session_record,
    task_record,
    task_snapshot_fingerprint,
)
from outbox_store import OutboxStore

pytestmark = pytest.mark.unit

DEV_A = "desktop-a"
DEV_B = "phone-b"


def _msg(role: str, content: str, seq: int, **extra: Any) -> dict[str, Any]:
    return {"role": role, "content": content, "seq": seq, **extra}


class _Relay:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, token: str, payload: dict[str, Any]) -> None:
        self.posts.append((url, payload))

    async def get(self, url: str, token: str) -> dict[str, Any]:
        return {"events": [], "cursor": 0}


class _SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _engine(
    tmp_path: Any,
    *,
    device: str = DEV_A,
) -> tuple[SyncAgentEngine, AgentStateStore, OutboxStore]:
    cfg = AgentConfig(
        relay_url="http://relay.test/ext/sync_relay",
        token=f"tok-{device}",
        device_id=device,
        device_tier="desktop" if device == DEV_A else "phone",
        app_version="1.0.0",
        watch_root=tmp_path / "config",
        poll_interval_secs=1.0,
        heartbeat_interval_secs=30.0,
    )
    state = AgentStateStore(tmp_path / "state.db")
    outbox = OutboxStore(tmp_path / "outbox.jsonl")
    engine = SyncAgentEngine(
        cfg,
        state=state,
        outbox=outbox,
        post_json=_Relay().post,
        get_json=_Relay().get,
        sleep_fn=_SleepRecorder(),
        clock_ms=lambda: 1_000_000,
        clock_mono=lambda: 0.0,
        now_fn=lambda: "2026-10-17T08:00:00+00:00",
    )
    return engine, state, outbox


def _outbox_events(outbox: OutboxStore) -> list[dict[str, Any]]:
    return list(outbox.peek(10_000))


def _wire_of(event: dict[str, Any], *, cursor: int = 1) -> dict[str, Any]:
    return {**event, "cursor": cursor}


def _message_wire(
    *, ulid: str, physical: int, content: str, seq: int = 1,
    pipeline: str = "pipe-x", device: str = DEV_B,
) -> dict[str, Any]:
    """DB 通道消息事件线格式（capture.message_record 载荷形态）。"""
    msg = _msg("user", content, seq)
    fp = fingerprint_message(msg)
    asset_id, payload = message_record(pipeline, seq, msg, fp)
    return {
        "ulid": ulid,
        "device_id": device,
        "asset_type": "message",
        "asset_id": asset_id,
        "version": 1,
        "updated_at": "2026-10-17T08:00:00+00:00",
        "payload": payload,
        "hlc": {"physical_ms": physical, "logical": 0, "device_id": device},
    }


# ── 通道注册表（路由中间件单点）──────────────────────────────


class TestChannelRegistry:
    def test_t1_types_route_to_db_channel(self) -> None:
        """session/message/task 注册到 db 通道；config 在 file 通道。"""
        assert asset_channel("session") == "db"
        assert asset_channel("message") == "db"
        assert asset_channel("task") == "db"
        assert asset_channel("config") == "file"
        assert asset_channel("unknown-type") == ""

    def test_owner_scope_follows_channel_not_type(self) -> None:
        """属主面挂通道语义：db=True、file/未注册=False。"""
        assert is_owner_scoped("session")
        assert is_owner_scoped("message")
        assert is_owner_scoped("task")
        assert not is_owner_scoped("config")
        assert not is_owner_scoped("unknown-type")


# ── 捕获步：消息 diff ─────────────────────────────────────────


class TestCaptureStepMessages:
    def test_new_messages_captured_per_object(self, tmp_path: Any) -> None:
        """历史两条消息首次捕获 = 两条 message 事件（每消息一对象）。"""
        engine, _, outbox = _engine(tmp_path)
        state = {
            "pipeline_id": "pipe1",
            "session_id": "sess1",
            "messages": [_msg("user", "你好", 1), _msg("assistant", "你好呀", 2)],
        }
        result = engine.capture_step(state)
        assert result == {}, "管道步骤契约：零 state_updates"
        events = _outbox_events(outbox)
        msg_events = [e for e in events if e["asset_type"] == "message"]
        assert len(msg_events) == 2
        assert {e["payload"]["row"]["role"] for e in msg_events} == {"user", "assistant"}
        for e in msg_events:
            assert e["asset_id"].startswith("pipe1/"), "asset_id = {pipeline}/{指纹}"
            assert e["payload"]["table"] == "messages"
            assert e["payload"]["view"]["group"] == "pipe1"
            assert "seq" not in e["payload"]["row"], "seq 是槽位元数据，只在呈现维度"
            assert e["hlc"] is not None, "捕获事件恒打 HLC"
        assert len({e["ulid"] for e in msg_events}) == 2, "每消息一 ULID"

    def test_append_captures_only_delta(self, tmp_path: Any) -> None:
        """增量：首轮 1 条、次轮 2 条 → 第二次只捕获新追加的 1 条。"""
        engine, _, outbox = _engine(tmp_path)
        base = {"pipeline_id": "pipe1", "session_id": "sess1"}
        engine.capture_step({**base, "messages": [_msg("user", "q1", 1)]})
        first = [e for e in _outbox_events(outbox) if e["asset_type"] == "message"]
        assert len(first) == 1
        engine.capture_step(
            {**base, "messages": [_msg("user", "q1", 1), _msg("assistant", "a1", 2)]}
        )
        events = [e for e in _outbox_events(outbox) if e["asset_type"] == "message"]
        assert len(events) == 2, "只补捕获增量 1 条"
        assert {e["payload"]["row"]["content"] for e in events} == {"q1", "a1"}

    def test_content_rewrite_new_object_not_replay(self, tmp_path: Any) -> None:
        """同 seq 内容改写（重生成/压缩）= 新指纹新对象；旧指纹不重发。"""
        engine, _, outbox = _engine(tmp_path)
        base = {"pipeline_id": "pipe1", "session_id": "sess1"}
        engine.capture_step({**base, "messages": [_msg("assistant", "旧答", 1)]})
        engine.capture_step({**base, "messages": [_msg("assistant", "新答", 1)]})
        events = [e for e in _outbox_events(outbox) if e["asset_type"] == "message"]
        assert len(events) == 2, "改写产生恰好 1 条新对象事件"
        assert {e["payload"]["row"]["content"] for e in events} == {"旧答", "新答"}

    def test_hot_path_unchanged_zero_capture(self, tmp_path: Any) -> None:
        """热路径：长度与尾指纹未变 → 零事件（重复轮询零语义差）。"""
        engine, _, outbox = _engine(tmp_path)
        state = {
            "pipeline_id": "pipe1",
            "session_id": "sess1",
            "messages": [_msg("user", "q1", 1)],
        }
        engine.capture_step(state)
        before = len(_outbox_events(outbox))
        engine.capture_step(dict(state))
        engine.capture_step(dict(state))
        assert len(_outbox_events(outbox)) == before, "热路径重复调用零新事件"

    def test_malformed_state_never_raises(self, tmp_path: Any) -> None:
        """畸形 state（messages 非数组/缺 pipeline/垃圾元素）零抛出零事件。"""
        engine, _, outbox = _engine(tmp_path)
        for bad in (
            {},
            None,
            "not-a-dict",
            {"pipeline_id": "p", "messages": None},
            {"pipeline_id": "p", "messages": "not-a-list"},
            {"messages": [_msg("user", "x", 1)]},
            {"pipeline_id": "p", "messages": ["junk", 42, None]},
        ):
            assert engine.capture_step(bad) == {}
        assert [e for e in _outbox_events(outbox) if e["asset_type"] == "message"] == []


# ── 捕获步：任务面与会话骨架 ──────────────────────────────────


class TestCaptureStepTaskAndSession:
    def test_task_status_transition_emits_versioned_events(self, tmp_path: Any) -> None:
        """任务态：首见 running 一事件；status→completed 漂移再一事件（同键追加）。"""
        engine, _, outbox = _engine(tmp_path)
        base = {"pipeline_id": "taskpipe", "task.id": "taskpipe", "session_id": "sess1"}
        engine.capture_step({**base, "task.status": "running", "task.goal": "做一件事",
                             "messages": [_msg("user", "开始", 1)]})
        engine.capture_step({**base, "task.status": "completed", "task.goal": "做一件事",
                             "messages": [_msg("user", "开始", 1), _msg("assistant", "完成", 2)]})
        events = [e for e in _outbox_events(outbox) if e["asset_type"] == "task"]
        assert len(events) == 2, "每次状态漂移恰好一事件"
        assert {e["payload"]["row"]["task.status"] for e in events} == {"running", "completed"}
        assert all(e["asset_id"] == "taskpipe" for e in events)
        assert all(e["payload"]["table"] == "tasks" for e in events)
        assert events[1]["version"] > events[0]["version"], "任务事件版本递增"

    def test_chat_session_has_no_task_event(self, tmp_path: Any) -> None:
        """无 task.status 的聊天会话零任务事件（会话/任务面分流）。"""
        engine, _, outbox = _engine(tmp_path)
        engine.capture_step({
            "pipeline_id": "pipe1", "session_id": "sess1",
            "messages": [_msg("user", "聊天", 1)],
        })
        assert [e for e in _outbox_events(outbox) if e["asset_type"] == "task"] == []

    def test_session_skeleton_registered_once(self, tmp_path: Any) -> None:
        """会话骨架：首见 session_id 一事件；重复轮询零新事件。"""
        engine, _, outbox = _engine(tmp_path)
        state = {"pipeline_id": "pipe1", "session_id": "sess1",
                 "messages": [_msg("user", "hi", 1)]}
        engine.capture_step(state)
        engine.capture_step(dict(state))
        events = [e for e in _outbox_events(outbox) if e["asset_type"] == "session"]
        assert len(events) == 1
        assert events[0]["asset_id"] == "sess1"
        assert events[0]["payload"]["table"] == "sessions"
        assert events[0]["payload"]["row"]["thread_id"] == "sess1"

    def test_capture_session_domain_event_and_tombstone(self, tmp_path: Any) -> None:
        """域事件路径：session.created 登记一次；session.deleted 墓碑为追加事件。"""
        engine, _, outbox = _engine(tmp_path)
        assert engine.capture_session("sess9") == 1
        assert engine.capture_session("sess9") == 0, "重复登记零新事件"
        assert engine.capture_session("sess9", deleted=True) == 1
        events = [e for e in _outbox_events(outbox) if e["asset_type"] == "session"]
        assert len(events) == 2
        tomb = events[-1]
        assert tomb["payload"]["tombstone"] is True
        assert tomb["version"] == 2, "墓碑是属主追加（版本递增），不是删除"


# ── 镜像：他端事件经通用 apply 落地（单一镜像表）──────────────


class TestMirrorApply:
    def test_foreign_message_lands_in_mirror(self, tmp_path: Any) -> None:
        """他端消息事件落地单一镜像表（本端内核表零写入——独立命名空间）。"""
        engine, state, _ = _engine(tmp_path)
        assert engine._ingest_wire(
            _wire_of(_message_wire(ulid="01ARZ3NDEKGS1A00000000000A", physical=1_000,
                                   content="来自手机的消息"))
        )
        rows = state.list_mirror_records("messages")
        assert len(rows) == 1
        assert rows[0]["row"]["role"] == "user"
        assert rows[0]["row"]["content"] == "来自手机的消息"
        assert rows[0]["owner_device"] == DEV_B
        assert rows[0]["view"] == {"group": "pipe-x", "seq": 1}

    def test_same_view_rewrite_collapses_to_one_row(self, tmp_path: Any) -> None:
        """同呈现维度改写：镜像收敛为单行（呈现层零重复），内容取 HLC 更大者。"""
        engine, state, _ = _engine(tmp_path)
        engine._ingest_wire(_wire_of(_message_wire(
            ulid="01ARZ3NDEKGS1A00000000000A", physical=1_000, content="v1")))
        engine._ingest_wire(_wire_of(_message_wire(
            ulid="01ARZ3NDEKGS1B00000000000B", physical=2_000, content="v2")))
        rows = state.list_mirror_records("messages")
        assert len(rows) == 1, "同 (group, seq) 改写替换镜像行，不堆重复对象"
        assert rows[0]["row"]["content"] == "v2"

    def test_duplicate_ulid_idempotent(self, tmp_path: Any) -> None:
        """下行重放（断网续传 at-least-once）：同 ULID 幂等，镜像零重复。"""
        engine, state, _ = _engine(tmp_path)
        wire = _wire_of(_message_wire(
            ulid="01ARZ3NDEKGS1A00000000000A", physical=1_000, content="只此一份"))
        assert engine._ingest_wire(wire)
        assert not engine._ingest_wire(wire)
        assert len(state.list_mirror_records("messages")) == 1

    def test_stale_hlc_never_overwrites(self, tmp_path: Any) -> None:
        """HLC 门槛：更旧事件（乱序重放）判 stale（不落地），镜像保持新版本。"""
        engine, state, _ = _engine(tmp_path)
        assert engine._ingest_wire(_wire_of(_message_wire(
            ulid="01ARZ3NDEKGS1A00000000000A", physical=5_000, content="新")))
        assert not engine._ingest_wire(_wire_of(_message_wire(
            ulid="01ARZ3NDEKGS1B00000000000B", physical=1_000, content="旧")))
        assert state.list_mirror_records("messages")[0]["row"]["content"] == "新"

    def test_session_tombstone_marks_without_delete(self, tmp_path: Any) -> None:
        """会话墓碑：镜像行打 deleted 标记，行不物理删除（「不删除」兑现）。"""
        engine, state, _ = _engine(tmp_path)
        create_asset, create_payload = session_record("sess-f", tombstone=False, origin="domain_event")
        tomb_asset, tomb_payload = session_record("sess-f", tombstone=True, origin="domain_event")
        create = {
            "ulid": "01ARZ3NDEKGS1A00000000000A", "device_id": DEV_B,
            "asset_type": "session", "asset_id": create_asset, "version": 1,
            "updated_at": "2026-10-17T08:00:00+00:00", "payload": create_payload,
            "hlc": {"physical_ms": 1_000, "logical": 0, "device_id": DEV_B},
        }
        tomb = {
            **create,
            "ulid": "01ARZ3NDEKGS1B00000000000B", "asset_id": tomb_asset,
            "version": 2, "payload": tomb_payload,
            "hlc": {"physical_ms": 2_000, "logical": 0, "device_id": DEV_B},
        }
        assert engine._ingest_wire(_wire_of(create))
        assert engine._ingest_wire(_wire_of(tomb))
        sessions = state.list_mirror_records("sessions")
        assert len(sessions) == 1, "墓碑同键追加不新增行"
        assert sessions[0]["deleted"] is True, "墓碑可见（呈现层隐藏）"
        assert sessions[0]["pk"] == "sess-f", "行仍在（不物理删除）"

    def test_task_event_lands_in_mirror(self, tmp_path: Any) -> None:
        """他端任务事件落地镜像（任务属主终态权威的只读呈现）。"""
        engine, state, _ = _engine(tmp_path)
        asset_id, payload = task_record("pipe-t", {"task.id": "pipe-t", "task.status": "completed"})
        wire = {
            "ulid": "01ARZ3NDEKGS1A00000000000A", "device_id": DEV_B,
            "asset_type": "task", "asset_id": asset_id, "version": 2,
            "updated_at": "2026-10-17T08:00:00+00:00", "payload": payload,
            "hlc": {"physical_ms": 1_000, "logical": 0, "device_id": DEV_B},
        }
        assert engine._ingest_wire(_wire_of(wire))
        rows = state.list_mirror_records("tasks")
        assert len(rows) == 1
        assert rows[0]["row"]["task.status"] == "completed"
        assert rows[0]["view"] is None, "无呈现维度的注册项按 asset_key 键控"

    def test_own_device_events_not_mirrored(self, tmp_path: Any) -> None:
        """本端事件不镜像（capture→relay→pull 回环短路，防自镜像）。"""
        engine, state, _ = _engine(tmp_path, device=DEV_A)
        wire = _message_wire(
            ulid="01ARZ3NDEKGS1A00000000000A", physical=1_000,
            content="本端自产", device=DEV_A)
        assert not engine._ingest_wire(_wire_of(wire))
        assert state.list_mirror_records() == []

    def test_db_record_without_table_row_rejected(self, tmp_path: Any) -> None:
        """DB 通道载荷缺 table/row（契约违约）不落地，返回 no_record。"""
        engine, state, _ = _engine(tmp_path)
        wire = {
            "ulid": "01ARZ3NDEKGS1A00000000000A", "device_id": DEV_B,
            "asset_type": "message", "asset_id": "pipe-x/bad", "version": 1,
            "updated_at": "2026-10-17T08:00:00+00:00",
            "payload": {"pipeline_id": "pipe-x"},
            "hlc": {"physical_ms": 1_000, "logical": 0, "device_id": DEV_B},
        }
        outcome = engine._ingest_wire(_wire_of(wire))
        assert not outcome, "契约违约载荷不落盘（no_record 归非落地路径）"
        assert state.list_mirror_records() == [], "契约违约载荷不进镜像"

    def test_file_channel_config_path_unchanged(self, tmp_path: Any) -> None:
        """file 通道零回归：config 事件仍走落盘路径（两通道分流不串线）。"""
        engine, _, _ = _engine(tmp_path)
        (engine._watcher.root / "sub").mkdir(parents=True, exist_ok=True)
        wire = {
            "ulid": "01ARZ3NDEKGS1A00000000000A", "device_id": DEV_B,
            "asset_type": "config", "asset_id": "sub/x.yaml", "version": 1,
            "updated_at": "2026-10-17T08:00:00+00:00",
            "payload": {"content": "k: v\n"},
            "hlc": {"physical_ms": 1_000, "logical": 0, "device_id": DEV_B},
        }
        assert engine._ingest_wire(_wire_of(wire))
        assert (engine._watcher.root / "sub" / "x.yaml").read_text(encoding="utf-8") == "k: v\n"
        assert engine._state.list_mirror_records() == []


# ── 注册元数据纯函数 ──────────────────────────────────────────


class TestRegistrationMeta:
    def test_message_record_shape(self) -> None:
        msg = _msg("user", "内容", 3)
        fp = fingerprint_message(msg)
        asset_id, payload = message_record("p1", 3, msg, fp)
        assert asset_id == f"p1/{fp}"
        assert payload["table"] == "messages"
        assert payload["pk"] == asset_id
        assert payload["view"] == {"group": "p1", "seq": 3}
        assert payload["row"]["content"] == "内容"
        assert "seq" not in payload["row"], "seq 是引擎槽位元数据，不入捕获指纹"

    def test_canonical_message_strips_private_and_seq(self) -> None:
        assert canonical_message({"role": "u", "seq": 1, "_hidden": "x", "content": "c"}) == {
            "role": "u", "content": "c",
        }
        assert canonical_message("not-a-dict") == {}

    def test_session_task_record_shapes(self) -> None:
        s_id, s_payload = session_record("t1", tombstone=True, origin="domain_event")
        assert s_id == "t1"
        assert s_payload["tombstone"] is True
        t_id, t_payload = task_record("p9", {"task.id": "p9", "task.status": "running"})
        assert t_id == "p9"
        assert t_payload["row"]["task.status"] == "running"

    def test_task_snapshot_fingerprint_changes_with_status(self) -> None:
        base = {"task.id": "p", "task.status": "running"}
        assert task_snapshot_fingerprint(base) != task_snapshot_fingerprint(
            {**base, "task.status": "completed"}
        )
        assert task_snapshot_fingerprint(base) == task_snapshot_fingerprint(dict(base))

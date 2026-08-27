# @feature: 聊天消息顺序对等（一轮 = 一条消息） | @ci: python-e2e
"""
E2E：聊天消息顺序对等（2026-08-27「顺序混乱」修复的实时回归锚）。

验证对象（真实 LLM + 真实引擎 + 真实 WS 事件链）：
1. 每次 agent 循环（一轮 = 一次 LLM 回合）独立 stream_start/new_message/
   stream_end（message_id 各不相同，a_ 前缀）；
2. 8 事件块增量（block_start/text_delta）携带本轮 message_id（llm_core
   _call_context 信封接通——此前生产链路恒为空串，事件被双端丢弃）；
3. 后端顺序不变式：/messages 返回的 assistant 记录按 seq 升序 == 事件流
   new_message 到达顺序（后端"写面"与"读面"同序）——流式期间看到的顺序
   就是结束后重放的顺序。

运行前提（与 test_06 相同）：内核 :9100 已启动且含真实 LLM key。

[来源] docs/working/chat_stream_order_diagnosis_20260827.md
"""
import asyncio
import json
import os
import uuid

import pytest
from e2e_helpers import KERNEL_URL, create_session, http_get_with_auth, ws_chat_url

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ZHIPU_API_KEY") or os.environ.get("MINIMAX_API_KEY")),
        reason="需要真实 LLM key（流式回复）",
    ),
]

# 提示词：用户显式要求搜索 → L1 允许调用 enhanced_search（工具轮）+ 二次回复轮
CHAT_PROMPT = "请用 enhanced_search 工具搜索 agentos 这个词的相关资料，然后把搜索结果的摘要告诉我。"

TERMINAL_SILENCE_SECONDS = 45  # > keepalive(30s) + 裕量：静默超此窗口 = run 已结束
STREAM_TIMEOUT_SECONDS = 300
RECV_POLL_SECONDS = 5
RECV_TIMEOUT_SECONDS = 60


async def _collect_run_events(ws, send_payload):
    """发送 user_input 并收集整轮 run 的事件（含每轮的 message_id）。

    结束判定：最后事件后连续 TERMINAL_SILENCE_SECONDS 无新事件（逐轮 stream_end
    没有 run 级终结信号，整轮结束 = 全体静默；keepalive(30s)/cost_update 等
    也在静默窗口内继续产出）。超时兜底由断言暴露。
    """
    await ws.send(json.dumps(send_payload))
    events: list[tuple[str, dict]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STREAM_TIMEOUT_SECONDS
    last_active = loop.time()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(RECV_POLL_SECONDS, remaining))
        except asyncio.TimeoutError:
            if loop.time() - last_active > TERMINAL_SILENCE_SECONDS:
                break  # run 结束：整体静默
            continue
        last_active = loop.time()
        data = json.loads(raw)
        t = data.get("type", "?")
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        events.append((t, payload if isinstance(payload, dict) else {}))
    return events


def _rounds_from_events(events):
    """从事件流提取逐轮 (message_id, seq) 与 8 事件路由键证据。"""
    starts: list[str] = []
    new_messages: list[tuple[str, int]] = []  # (message_id, sequence)
    ends: list[str] = []
    deltas_with_mid: list[str] = []
    deltas_total = 0
    seen_types: list[str] = []
    for t, p in events:
        if t not in seen_types:
            seen_types.append(t)
        mid = p.get("message_id") or ""
        if t == "stream_start" and mid:
            starts.append(mid)
        elif t == "new_message":
            seq = p.get("sequence")
            new_messages.append((mid, seq))
        elif t == "stream_end" and mid:
            ends.append(mid)
        elif t in ("block_start", "text_delta", "block_end", "tool_call_delta"):
            deltas_total += 1
            if mid:
                deltas_with_mid.append(mid)
    return seen_types, starts, new_messages, ends, deltas_total, deltas_with_mid


def _fetch_backend_assistants(token, session_id):
    """读取后端消息表（按 seq 升序），返回 assistant 记录的 (message_id, seq) 列表。"""
    status, body, _ = http_get_with_auth(
        f"{KERNEL_URL}/api/v1/sessions/{session_id}/messages",
        token=token,
        timeout=15,
    )
    if not isinstance(body, dict) or body.get("messages") is None:
        raise RuntimeError(f"拉取后端消息失败: status={status}, body={str(body)[:200]}")
    msgs = body["messages"]
    assistants = [
        (m.get("id") or m.get("record_id") or "", m.get("sequence"))
        for m in msgs
        if (m.get("role") or m.get("msg_type")) == "assistant"
    ]
    # 后端读面按 seq 升序（message_slots ORDER BY seq ASC）
    assistants = sorted(assistants, key=lambda r: (r[1] if r[1] is not None else -1))
    return assistants, msgs


def _target_token(auth_token):
    """测试身份选择：
    - 默认 admin（CI / 无同身份浏览器共存的干净环境）；
    - E2E_PARITY_USERNAME 设置时改为该用户的登录 token——本地与用户浏览器
      共存时，同身份的新 WS 连接会 4000 replaced_by_new_connection 互踢，
      需用预提升为 admin 的独立用户（本地手工提升，见 run 说明）。
    """
    username = os.environ.get("E2E_PARITY_USERNAME")
    if not username:
        return auth_token
    from e2e_helpers import http_post_json

    status, body, _ = http_post_json(
        f"{KERNEL_URL}/api/v1/auth/login",
        {"username": username, "password": os.environ.get("E2E_PARITY_PASSWORD", "parity12345")},
        timeout=10,
    )
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        raise RuntimeError(f"E2E_PARITY_USERNAME 登录失败: status={status}, body={str(body)[:200]}")
    return body["access_token"]


class TestChatOrderParity:
    """聊天消息顺序对等（真实 LLM 全链路）。"""

    @pytest.mark.timeout(360)
    def test_round_order_matches_backend(self, auth_token, cleanup_sessions):
        token = _target_token(auth_token)
        session = create_session(token, title="e2e-order-parity")
        cleanup_sessions(session["thread_id"])
        sid = session["thread_id"]

        async def _run():
            url = ws_chat_url(token)
            import websockets

            async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                return await _collect_run_events(ws, {
                    "type": "user_input",
                    "thread_id": sid,
                    "content": CHAT_PROMPT,
                    "pipeline_id": session.get("active_pipeline_id") or "",
                    "attachments": [],
                    "enable_thinking": False,
                    "thinking_strength": "",
                    "client_message_id": f"e2e-parity-{uuid.uuid4().hex[:8]}",
                })

        events = asyncio.run(_run())
        backend_assistants, _ = _fetch_backend_assistants(token, sid)
        seen_types, starts, new_messages, ends, deltas_total, deltas_with_mid = _rounds_from_events(events)

        # ── 1. 事件链完整性 ──
        for required in ("stream_start", "new_message", "stream_end"):
            assert required in seen_types, f"事件链应含 {required}，实际: {seen_types}"

        # ── 2. 每轮独立 message_id：start ⊇ new_message/end（init/exit 轮只有 start+end）──
        msg_ids = [m for m, _ in new_messages]
        assert len(new_messages) >= 1, "应至少产生一轮 LLM 回复"
        assert len(msg_ids) == len(set(msg_ids)), (
            f"new_message 的 message_id 应互不相同（每轮一条消息），实际重复: {msg_ids}"
        )
        assert all(str(m).startswith("a_") for m in msg_ids), (
            f"消息 id 应为内核 a_ 命名空间，实际: {msg_ids}"
        )
        assert set(msg_ids) <= set(starts), (
            f"new_message 的 message_id 均应有对应 stream_start，缺失: {set(msg_ids) - set(starts)}"
        )
        assert set(ends) <= set(starts), (
            f"stream_end 的 message_id 应始于 stream_start，越界: {set(ends) - set(starts)}"
        )

        # ── 3. 8 事件信封接通：块增量携带本轮 message_id（非空串）──
        if deltas_total > 0:
            assert deltas_with_mid, (
                f"8 事件增量 {deltas_total} 条应全部携带非空 message_id（_call_context 信封接通），"
                f"实际携带 id 的: {len(deltas_with_mid)}"
            )
        else:
            pytest.skip("本轮 LLM 未产生块增量；请重跑以验证流式事件信封")
        assert set(deltas_with_mid) <= set(msg_ids), (
            f"块增量的 message_id 应归属本轮消息，实际越界: {set(deltas_with_mid) - set(msg_ids)}"
        )

        # ── 4. 顺序对等：事件流 new_message 顺序 == 后端 assistant 记录 seq 升序 ──
        assert len(backend_assistants) == len(new_messages), (
            f"后端 assistant 记录数 {len(backend_assistants)} 应等于 new_message 轮数 "
            f"{len(new_messages)}（消息不丢不重）"
        )
        backend_ids = [m for m, _ in backend_assistants]
        backend_seqs = [s for _, s in backend_assistants]
        assert backend_ids == msg_ids, (
            f"后端 assistant 顺序（seq 升序）应等于事件流 new_message 顺序：\n"
            f"backend: {list(zip(backend_ids, backend_seqs))}\n"
            f"events : {new_messages}"
        )
        # 事件携带的 sequence 与后端一致（权威 seq 对位）
        event_seqs = [s for _, s in new_messages]
        assert event_seqs == backend_seqs, (
            f"new_message.sequence 应与后端一致: events={event_seqs} backend={backend_seqs}"
        )

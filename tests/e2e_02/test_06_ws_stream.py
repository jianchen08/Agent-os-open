# @feature: FP-0.2.〇 管道引擎 | @vision: V2 全能闭环 | @ci: python-e2e
"""
E2E 测试：WS 流式全链路（FP-0.2.〇 串行循环）

验证 /ws/chat 从连接到流式收尾的完整事件链（仅依赖运行中的内核 :9100）：
  连接 ws://localhost:9100/ws/chat?token=<token>&version=1
    → connection_confirmation（连接/鉴权确认）
  → 发 user_input
    → stream_start → termination_status → thinking_start/chunk/end
      → stream_chunk → cost_update → new_message → stream_end
  断言：事件序列包含 stream_start 与 stream_end（中间类型宽松匹配），
  stream_chunk 行数 > 0 且累计文本非空（真实 LLM 流式产出）。

运行前提：
- 内核已启动（AGENTOS_DB_PATH=":memory:" AGENTOS_KERNEL_PORT=9100
  ./kernel/target/release/agentos-kernel.exe），9100 端口可访问。
- 手动运行（不在 CI）：python -m pytest tests/e2e_02/test_06_ws_stream.py -q
"""
import asyncio
import json
import os
import uuid

import pytest
from e2e_helpers import create_session, ws_chat_url

pytestmark = [
    pytest.mark.e2e,
    # 依赖真实 LLM 流式回复：无 key 时跳过（CI 有 secrets.ZHIPU_API_KEY 才跑全量）
    pytest.mark.skipif(
        not os.environ.get("ZHIPU_API_KEY"),
        reason="需要 ZHIPU_API_KEY（真实 LLM 流式回复）",
    ),
]

CHAT_PROMPT = "你好，请回复一句话介绍你自己"

# 终端事件：收到其一即视为本次流式结束
TERMINAL_TYPES = ("stream_end", "stream_error", "error")

# 单条 recv 等待上限 + 整轮流式等待上限（LLM 生成宽松超时）
RECV_TIMEOUT_SECONDS = 120
STREAM_TIMEOUT_SECONDS = 200


async def _collect_stream(ws, send_payload):
    """发送 user_input 并收集事件，返回 (types_seen, chunk_count, full_text)。

    事件统一形态为 {"type": ..., "sequence": ..., "data": {业务字段...}}，
    文本内容（stream_chunk.content）位于 data 内；顶层字段作兼容回退。
    """
    await ws.send(json.dumps(send_payload))
    types_seen: list[str] = []
    chunk_count = 0
    full_text = ""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STREAM_TIMEOUT_SECONDS
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break  # 超时兜底，由断言暴露缺事件
        raw = await asyncio.wait_for(ws.recv(), timeout=min(RECV_TIMEOUT_SECONDS, remaining))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = data.get("type", "?")
        if t not in types_seen:
            types_seen.append(t)
        # 业务字段优先取 data 嵌套，顶层作兼容回退
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        if t == "stream_chunk":
            chunk = payload.get("content") or payload.get("text") or ""
            if chunk:
                chunk_count += 1
                full_text += chunk
        elif t == "new_message":
            content = payload.get("content") or data.get("content") or ""
            if content:
                full_text += content
        elif t in TERMINAL_TYPES:
            break
    return types_seen, chunk_count, full_text


class TestWsStreamChain:
    """WS 流式全链路：连接确认 + 流式事件序列 + 文本产出。"""

    @pytest.mark.timeout(120)
    def test_ws_connection_confirmation(self, auth_token, cleanup_sessions):
        """连接 /ws/chat 后第一条消息应为 connection_confirmation。"""
        import websockets

        token = auth_token
        session = create_session(token, title="e2e-ws-confirm")
        cleanup_sessions(session["thread_id"])

        async def _run():
            url = ws_chat_url(token)
            async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                return json.loads(raw)

        data = asyncio.run(_run())
        assert data.get("type") == "connection_confirmation", (
            f"首条消息 type 期望 'connection_confirmation'，实际 '{data.get('type')}'"
        )
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        assert inner.get("status") == "connected", (
            f"connection_confirmation.data.status 期望 'connected'，实际 '{inner.get('status')}'"
        )

    @pytest.mark.timeout(300)
    def test_ws_stream_contains_start_and_end(self, auth_token, cleanup_sessions):
        """发 user_input 后应收到完整流式链：stream_start ... stream_end，
        且 stream_chunk 行数 > 0、累计文本非空。"""
        import websockets

        token = auth_token
        session = create_session(token, title="e2e-ws-stream")
        cleanup_sessions(session["thread_id"])
        sid = session["thread_id"]

        async def _run():
            url = ws_chat_url(token)
            async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
                # 消费连接确认
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                # 发 user_input（与前端 sendUserInput 同构）
                return await _collect_stream(ws, {
                    "type": "user_input",
                    "thread_id": sid,
                    "content": CHAT_PROMPT,
                    "pipeline_id": "",
                    "attachments": [],
                    "enable_thinking": False,
                    "thinking_strength": "",
                    "client_message_id": f"e2e-ws-{uuid.uuid4().hex[:8]}",
                })

        types_seen, chunk_count, full_text = asyncio.run(_run())
        assert "stream_start" in types_seen, (
            f"事件序列应包含 stream_start，实际收到: {types_seen}"
        )
        assert "stream_end" in types_seen, (
            f"事件序列应包含 stream_end（流式正常收尾），实际收到: {types_seen}"
        )
        assert chunk_count > 0, (
            f"stream_chunk 行数应 > 0（真实 LLM 流式产出），实际 {chunk_count}；"
            f"事件序列: {types_seen}"
        )
        assert len(full_text.strip()) > 0, (
            f"累计流式文本不应为空；事件序列: {types_seen}"
        )

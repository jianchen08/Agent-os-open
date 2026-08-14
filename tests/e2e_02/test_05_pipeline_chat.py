# @feature: FP-0.2.〇 管道引擎 | @vision: V2 全能闭环 | @ci: e2e-manual
"""
E2E 测试：管道 chat 全链路（FP-0.2.〇 串行循环）

验证 REST 管道 chat 端到端链路（仅依赖运行中的内核 :9100，不依赖前端）：
  1. 登录（admin/admin12345）→ 创建会话（POST /api/v1/sessions）→
     POST /api/v1/chat {message, session_id} → 200，content 非空
     （真实 LLM 回复，灵汐 L1 自我介绍），type=message / session_id 透传。
  2. GET /api/v1/sessions/{thread_id}/messages 确认消息已持久化
     （当前内核按会话 active_pipeline_id 维度落库，查询时带
     pipeline_run_id=<active_pipeline_id>；无则回退 thread_id 路径）。

运行前提：
- 内核已启动（AGENTOS_DB_PATH=":memory:" AGENTOS_KERNEL_PORT=9100
  ./kernel/target/release/agentos-kernel.exe），9100 端口可访问。
- 手动运行（不在 CI）：python -m pytest tests/e2e_02/test_05_pipeline_chat.py -q
"""
import pytest

from e2e_helpers import (
    KERNEL_URL,
    create_session,
    http_get_with_auth,
    http_post_json,
)

import os

pytestmark = [
    pytest.mark.e2e,
    # 依赖真实 LLM（灵汐回复）：无 key 时跳过（CI 有 secrets.ZHIPU_API_KEY 才跑全量）
    pytest.mark.skipif(
        not os.environ.get("ZHIPU_API_KEY"),
        reason="需要 ZHIPU_API_KEY（真实 LLM 回复）",
    ),
]

CHAT_PROMPT = "你好，请回复一句话介绍你自己"


@pytest.fixture(scope="module")
def chat_flow(auth_token):
    """模块级 fixture：登录 + 建会话 + 一次真实 LLM chat 调用，供各断言复用。

    只做一次 LLM 调用（耗时 ~30s），避免每个断言重复触发。
    """
    token = auth_token
    session = create_session(token, title="e2e-pipeline-chat")
    status, body, _ = http_post_json(
        f"{KERNEL_URL}/api/v1/chat",
        {"message": CHAT_PROMPT, "session_id": session["thread_id"]},
        timeout=150,  # LLM 生成宽松超时
    )
    return {
        "token": token,
        "session": session,
        "chat_status": status,
        "chat_body": body if isinstance(body, dict) else {},
    }


class TestPipelineChat:
    """REST 管道 chat：LLM 回复非空 + 响应结构 + 消息持久化。"""

    @pytest.mark.timeout(200)
    def test_chat_returns_non_empty_llm_content(self, chat_flow):
        """POST /api/v1/chat 应 200 且 content 非空（真实 LLM 回复而非 echo）。"""
        assert chat_flow["chat_status"] == 200, (
            f"/api/v1/chat 期望 200，实际 {chat_flow['chat_status']}"
        )
        content = chat_flow["chat_body"].get("content")
        assert isinstance(content, str), f"content 应为 str，实际 {type(content)}"
        assert len(content.strip()) > 0, "content 不应为空（期望 LLM 回复）"

    @pytest.mark.timeout(200)
    def test_chat_response_shape(self, chat_flow):
        """chat 响应应含 type='message'、session_id 透传、timestamp 字段。"""
        body = chat_flow["chat_body"]
        assert body.get("type") == "message", (
            f"type 期望 'message'，实际 '{body.get('type')}'"
        )
        assert body.get("session_id") == chat_flow["session"]["thread_id"], (
            f"session_id 应透传会话 id，实际 '{body.get('session_id')}'"
        )
        assert isinstance(body.get("timestamp"), str) and body["timestamp"], (
            "响应缺少 timestamp 字段"
        )

    @pytest.mark.timeout(30)
    def test_messages_persisted_after_chat(self, chat_flow):
        """chat 后 GET /api/v1/sessions/{id}/messages 应能查到 user + assistant 消息。

        当前内核按会话 active_pipeline_id 维度落库，优先用
        pipeline_run_id=<active_pipeline_id> 查询；若为空再回退
        thread_id 路径（保留兼容断言，文档化差异）。
        """
        token = chat_flow["token"]
        sid = chat_flow["session"]["thread_id"]
        pid = chat_flow["session"].get("active_pipeline_id") or ""

        messages = []
        # 首选：带 pipeline_run_id（与前端按 active_pipeline_id 查询一致）
        if pid:
            status, body, _ = http_get_with_auth(
                f"{KERNEL_URL}/api/v1/sessions/{sid}/messages?pipeline_run_id={pid}",
                token=token, timeout=15,
            )
            assert status == 200, f"messages 期望 200，实际 {status}"
            messages = (body.get("messages") if isinstance(body, dict) else []) or []
        # 回退：thread_id 路径（内核早期行为/空 pid 场景）
        if not messages:
            status, body, _ = http_get_with_auth(
                f"{KERNEL_URL}/api/v1/sessions/{sid}/messages",
                token=token, timeout=15,
            )
            assert status == 200, f"messages 期望 200，实际 {status}"
            messages = (body.get("messages") if isinstance(body, dict) else []) or []

        assert messages, "chat 后应能查到持久化消息（user + assistant）"
        roles = [m.get("role") for m in messages if isinstance(m, dict)]
        assert "user" in roles, f"应有 user 消息，实际 roles={roles}"
        assert "assistant" in roles, f"应有 assistant 消息，实际 roles={roles}"
        assistant = next(
            (m for m in messages if isinstance(m, dict) and m.get("role") == "assistant"), {}
        )
        assert isinstance(assistant.get("content"), str) and assistant["content"].strip(), (
            "assistant 消息 content 不应为空"
        )

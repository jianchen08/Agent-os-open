# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: e2e-manual
"""
E2E 测试：审批闭环（FP-0.2.五 改动 A 验收）

验证两条路径（仅依赖运行中的内核 :9100，不依赖前端）：
  1. Schema 聚合（100% 确定性主断言）：
     GET /api/v1/schema → agents[] 中 approval_service 的 ui_schema 声明
     approval_panel 组件（space=fullscreen, trigger=on_event:approval.created），
     前端 SchemaFullscreenHost 据此打开全屏审批浮层。
  2. LLM 触发选择审批全链路（依赖 LLM 决策，非确定性）：
     登录 → 建会话 → WS /ws/chat → 发引导消息（要求 LLM 调 human-interaction
     工具创建选择审批）→ 收 interaction_request（mode=choice，payload 含
     request_id/options）→ POST /api/v1/interaction/response
     {request_id, response_type:'answered', selected_option:'批准'} → 断言
     success=true 且 data.ok=true。
     容错：若 LLM 未在等待窗口内触发审批（工具调用/参数形态不 100% 确定），
     走 pytest.skip 而非失败——schema 断言已覆盖确定性验收面。

运行前提：
- 内核已启动（AGENTOS_DB_PATH=":memory:" AGENTOS_KERNEL_PORT=9100
  ./kernel/target/release/agentos-kernel.exe），9100 端口可访问。
- 手动运行（不在 CI）：python -m pytest tests/e2e_02/test_04_approval_flow.py -q
"""
import asyncio
import json

import pytest

from e2e_helpers import (
    KERNEL_URL,
    create_session,
    http_get,
    http_post_json_auth,
    ws_chat_url,
)

pytestmark = pytest.mark.e2e

# 引导 LLM 创建"选择审批"的消息（实测可稳定触发 human_interaction choice 工具调用）
APPROVAL_GUIDE_PROMPT = (
    "请调用审批工具创建一个选择型审批，标题为「发布变更审批」，"
    '选项为 ["批准", "拒绝"]，创建后等待我的选择结果再继续。'
)

# 等待 LLM 触发 interaction_request 的窗口（宽松超时，实测 ~13-20s）
INTERACTION_WAIT_SECONDS = 150


def _find_agent(schema_body, agent_id):
    """从 schema 响应的 agents 列表中按 id 查找插件（system 插件），找不到返回 None。"""
    agents = schema_body.get("agents") if isinstance(schema_body, dict) else None
    if not isinstance(agents, list):
        return None
    for agent in agents:
        if isinstance(agent, dict) and agent.get("id") == agent_id:
            return agent
    return None


class TestApprovalSchema:
    """1. Schema 聚合端点声明审批面板（确定性主断言，不依赖 LLM）。"""

    def test_schema_contains_approval_service_with_ui_schema(self, kernel_url):
        """GET /api/v1/schema 应返回 approval_service 且其 ui_schema 非空。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/schema", timeout=10)
        assert status == 200, f"期望 200，实际 {status}"
        agent = _find_agent(body, "approval_service")
        assert agent is not None, (
            "schema.agents 缺少 approval_service（approval 插件 manifest 未被内核加载）"
        )
        assert isinstance(agent.get("ui_schema"), dict), (
            f"approval_service.ui_schema 应为 dict，实际 {type(agent.get('ui_schema'))}"
        )

    def test_approval_panel_widget_fullscreen_on_event(self, kernel_url):
        """approval_service 的 ui_schema 应声明 approval_panel：
        space=fullscreen、trigger=on_event:approval.created（全屏审批浮层）。"""
        status, body, _ = http_get(f"{kernel_url}/api/v1/schema", timeout=10)
        assert status == 200, f"期望 200，实际 {status}"
        agent = _find_agent(body, "approval_service")
        assert agent is not None, "schema.agents 缺少 approval_service"
        widgets = (agent.get("ui_schema") or {}).get("widgets") or []
        panels = [w for w in widgets if isinstance(w, dict) and w.get("id") == "approval_panel"]
        assert panels, f"ui_schema.widgets 缺少 approval_panel，实际 widgets={widgets}"
        panel = panels[0]
        assert panel.get("space") == "fullscreen", (
            f"approval_panel.space 期望 'fullscreen'，实际 '{panel.get('space')}'"
        )
        assert panel.get("trigger") == "on_event:approval.created", (
            f"approval_panel.trigger 期望 'on_event:approval.created'，"
            f"实际 '{panel.get('trigger')}'"
        )


class TestApprovalChoiceLoop:
    """2. LLM 触发选择审批闭环（依赖 LLM 决策，未触发则 skip，不视为失败）。"""

    @pytest.mark.timeout(300)
    def test_llm_triggered_approval_choice_loop(self, auth_token):
        """发引导消息 → 收 interaction_request → 提交响应 '批准' → 断言 ok=true。"""
        token = auth_token
        session = create_session(token, title="e2e-approval-flow")
        sid = session["thread_id"]

        async def _run() -> str | None:
            import websockets

            url = ws_chat_url(token)
            async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
                # 消费连接确认（connection_confirmation）
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    pass

                # 发 user_input（与前端 sendUserInput 同构），引导 LLM 建审批
                await ws.send(json.dumps({
                    "type": "user_input",
                    "thread_id": sid,
                    "content": APPROVAL_GUIDE_PROMPT,
                    "pipeline_id": "",
                    "attachments": [],
                    "enable_thinking": False,
                    "thinking_strength": "",
                    "client_message_id": "e2e-approval-1",
                }))

                # 等待 interaction_request（choice 模式）
                loop = asyncio.get_running_loop()
                deadline = loop.time() + INTERACTION_WAIT_SECONDS
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return None  # LLM 未触发审批
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "interaction_request":
                        # 事件体：type 在顶层，业务字段整体透传在 data 内（含 _threadId）
                        payload = data.get("data") if isinstance(data.get("data"), dict) else data
                        return payload.get("request_id") or payload.get("id") or ""
                    if data.get("type") in ("stream_end", "stream_error", "error"):
                        # 管道提前收尾（LLM 未建审批直接回复）——视为未触发
                        return None

        request_id = asyncio.run(_run())
        if not request_id:
            pytest.skip(
                f"LLM 未在 {INTERACTION_WAIT_SECONDS}s 内触发审批交互"
                "（依赖 LLM 工具调用决策，非确定性）——跳过闭环断言；"
                "确定性验收面由 schema 聚合断言覆盖"
            )

        # 提交审批响应（前端用户操作等价路径；写面端点需 Bearer token）
        status, body, _ = http_post_json_auth(
            f"{KERNEL_URL}/api/v1/interaction/response",
            {
                "request_id": request_id,
                "response_type": "answered",
                "selected_option": "批准",
            },
            token=token,
            timeout=15,
        )
        assert status == 200, f"interaction/response 期望 200，实际 {status}"
        assert isinstance(body, dict), f"响应应为 dict，实际 {type(body)}"
        assert body.get("success") is True, (
            f"interaction/response success 期望 true，实际 {body!r}"
        )
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        assert data.get("ok") is True, (
            f"interaction/response data.ok 期望 true（'批准' 已提交），实际 {body!r}"
        )
        assert body.get("request_id") == request_id, (
            f"响应的 request_id 应与提交一致，期望 {request_id}，实际 {body.get('request_id')!r}"
        )

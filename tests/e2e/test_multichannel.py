"""多通道接入 E2E 测试。

验证网关层的消息解析、标准化和路由逻辑。
由于钉钉/飞书等需要外部 webhook，测试聚焦 MessageNormalizer 和 ChannelGateway 的内部行为。
对应 features.md 场景 9。

测试用例：
- test_normalize_feishu_message：飞书消息标准化
- test_normalize_dingtalk_message：钉钉消息标准化
- test_normalize_wecom_message：企业微信消息标准化
- test_normalize_qq_message：QQ 消息标准化
- test_normalize_unsupported_channel：不支持的渠道报 ValueError
- test_denormalize_feishu_response：飞书响应反标准化
- test_denormalize_dingtalk_response：钉钉响应反标准化
- test_gateway_handle_message：网关消息处理流程（含管道回调）
- test_gateway_unsupported_channel_drop：网关对不支持的渠道静默丢弃
"""

from __future__ import annotations

from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixture — 提供 MessageNormalizer 实例
# ---------------------------------------------------------------------------

@pytest.fixture
def normalizer() -> Any:
    """提供 MessageNormalizer 实例。

    Returns:
        MessageNormalizer 实例
    """
    from channels.gateway.message_normalizer import MessageNormalizer

    return MessageNormalizer()


@pytest.fixture
def gateway() -> Any:
    """提供 ChannelGateway 实例（不注册适配器）。

    Returns:
        ChannelGateway 实例
    """
    from channels.gateway.channel_gateway import ChannelGateway

    return ChannelGateway()


# ---------------------------------------------------------------------------
# 飞书消息标准化测试
# ---------------------------------------------------------------------------

def test_normalize_feishu_message(normalizer: Any) -> None:
    """飞书 im.message.receive_v1 事件标准化为 UnifiedMessage。

    验证点：
    - normalize("feishu", raw) 返回 UnifiedMessage
    - channel_type 为 "feishu"
    - content 提取了文本内容
    - unified_user_id 格式为 "feishu:{open_id}"
    """
    raw_feishu = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "evt_001"},
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_feishu_test_001"},
            },
            "message": {
                "message_id": "msg_feishu_001",
                "message_type": "text",
                "content": '{"text":"飞书测试消息"}',
                "create_time": "1700000000000",
            },
        },
    }

    unified = normalizer.normalize("feishu", raw_feishu)

    assert unified.channel_type == "feishu", f"channel_type 应为 feishu，得到 {unified.channel_type}"
    assert unified.content == "飞书测试消息", f"content 不匹配: {unified.content}"
    assert unified.unified_user_id == "feishu:ou_feishu_test_001", (
        f"unified_user_id 格式错误: {unified.unified_user_id}"
    )
    assert unified.message_id == "msg_feishu_001"
    assert unified.timestamp > 0


# ---------------------------------------------------------------------------
# 钉钉消息标准化测试
# ---------------------------------------------------------------------------

def test_normalize_dingtalk_message(normalizer: Any) -> None:
    """钉钉 Stream 消息事件标准化为 UnifiedMessage。

    验证点：
    - normalize("dingtalk", raw) 返回 UnifiedMessage
    - channel_type 为 "dingtalk"
    - content 提取了文本内容
    - unified_user_id 格式为 "dingtalk:{sender_staff_id}"
    """
    raw_dingtalk = {
        "messageId": "msg_dingtalk_001",
        "senderStaffId": "staff_dt_001",
        "senderId": "sender_id_001",
        "msgtype": "text",
        "text": {"content": "钉钉测试消息"},
        "createAt": "1700000000000",
        "conversationId": "conv_001",
    }

    unified = normalizer.normalize("dingtalk", raw_dingtalk)

    assert unified.channel_type == "dingtalk"
    assert unified.content == "钉钉测试消息", f"content 不匹配: {unified.content}"
    assert unified.unified_user_id == "dingtalk:staff_dt_001"
    assert unified.message_id == "msg_dingtalk_001"


# ---------------------------------------------------------------------------
# 企业微信消息标准化测试
# ---------------------------------------------------------------------------

def test_normalize_wecom_message(normalizer: Any) -> None:
    """企业微信回调消息标准化为 UnifiedMessage。

    验证点：
    - normalize("wecom", raw) 返回 UnifiedMessage
    - channel_type 为 "wecom"
    - content 提取了文本内容
    - unified_user_id 格式为 "wecom:{from_user}"
    """
    raw_wecom = {
        "MsgId": "msg_wecom_001",
        "FromUserName": "wecom_user_001",
        "ToUserName": "agent_bot",
        "MsgType": "text",
        "Content": "企业微信测试消息",
        "CreateTime": "1700000000",
        "AgentID": "1000002",
    }

    unified = normalizer.normalize("wecom", raw_wecom)

    assert unified.channel_type == "wecom"
    assert unified.content == "企业微信测试消息", f"content 不匹配: {unified.content}"
    assert unified.unified_user_id == "wecom:wecom_user_001"
    assert unified.message_id == "msg_wecom_001"


# ---------------------------------------------------------------------------
# QQ 消息标准化测试
# ---------------------------------------------------------------------------

def test_normalize_qq_message(normalizer: Any) -> None:
    """QQ OneBot v11 消息事件标准化为 UnifiedMessage。

    验证点：
    - normalize("qq", raw) 返回 UnifiedMessage
    - channel_type 为 "qq"
    - content 提取了文本内容（Array 格式消息段）
    - unified_user_id 格式为 "qq:{user_id}"
    """
    raw_qq = {
        "message_id": 123456,
        "user_id": 100200300,
        "message_type": "private",
        "time": 1700000000,
        "self_id": 999888777,
        "message": [
            {"type": "text", "data": {"text": "QQ测试消息"}},
        ],
        "sender": {"nickname": "QQ用户"},
    }

    unified = normalizer.normalize("qq", raw_qq)

    assert unified.channel_type == "qq"
    assert "QQ测试消息" in unified.content, f"content 不匹配: {unified.content}"
    assert unified.unified_user_id == "qq:100200300"
    assert unified.message_id == "123456"


# ---------------------------------------------------------------------------
# 异常场景测试
# ---------------------------------------------------------------------------

def test_normalize_unsupported_channel(normalizer: Any) -> None:
    """不支持的渠道标准化应抛出 ValueError。

    验证点：
    - normalize("unknown_channel", {}) 抛出 ValueError
    - 异常信息包含渠道名
    """
    with pytest.raises(ValueError, match="Unsupported channel type"):
        normalizer.normalize("unknown_channel", {})


# ---------------------------------------------------------------------------
# 反标准化测试
# ---------------------------------------------------------------------------

def test_denormalize_feishu_response(normalizer: Any) -> None:
    """飞书响应反标准化为发送消息格式。

    验证点：
    - denormalize("feishu", response) 返回 dict
    - 文本响应 msg_type 为 "text"
    - content 中包含响应文本
    """
    from channels.gateway.unified_types import UnifiedResponse

    response = UnifiedResponse(
        message_id="msg_001",
        channel_type="feishu",
        content="飞书回复消息",
        content_type="text",
    )

    result = normalizer.denormalize("feishu", response)

    assert result["msg_type"] == "text", f"msg_type 应为 text，得到 {result['msg_type']}"
    assert "飞书回复消息" in result["content"]["text"], (
        f"回复内容不匹配: {result['content']}"
    )


def test_denormalize_dingtalk_response(normalizer: Any) -> None:
    """钉钉响应反标准化为发送消息格式。

    验证点：
    - denormalize("dingtalk", response) 返回 dict
    - 文本响应 msgtype 为 "text"
    - content 中包含响应文本
    """
    from channels.gateway.unified_types import UnifiedResponse

    response = UnifiedResponse(
        message_id="msg_002",
        channel_type="dingtalk",
        content="钉钉回复消息",
        content_type="text",
    )

    result = normalizer.denormalize("dingtalk", response)

    assert result["msgtype"] == "text", f"msgtype 应为 text，得到 {result['msgtype']}"
    assert "钉钉回复消息" in result["text"]["content"], (
        f"回复内容不匹配: {result['text']}"
    )


# ---------------------------------------------------------------------------
# 网关消息处理流程测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gateway_handle_message(gateway: Any) -> None:
    """网关处理消息流程：标准化 → 获取会话 → 构建状态 → 回调管道。

    设置 on_pipeline_request 回调验证 state 被正确构建。

    验证点：
    - handle_message 不抛异常
    - 回调被调用且 initial_state 包含正确字段
    """
    received_states: list[dict[str, Any]] = []

    async def mock_pipeline_request(state: dict[str, Any]) -> None:
        received_states.append(state)

    gateway.on_pipeline_request = mock_pipeline_request

    raw_feishu = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_gateway_test"}},
            "message": {
                "message_id": "msg_gw_001",
                "message_type": "text",
                "content": '{"text":"网关测试"}',
                "create_time": "1700000000000",
            },
        },
    }

    await gateway.handle_message("feishu", raw_feishu)

    assert len(received_states) == 1, "管道回调应被调用一次"
    state = received_states[0]
    assert "user_input" in state, "initial_state 应包含 user_input"
    assert state["user_input"] == "网关测试", (
        f"user_input 应为 '网关测试'，得到 {state.get('user_input')}"
    )


@pytest.mark.asyncio
async def test_gateway_unsupported_channel(gateway: Any) -> None:
    """网关处理不支持的渠道消息应静默处理（不崩溃）。

    验证点：
    - handle_message 对不支持的渠道不抛异常
    - 管道回调不被调用（消息被丢弃）
    """
    callback_called = False

    async def mock_pipeline_request(state: dict[str, Any]) -> None:
        nonlocal callback_called
        callback_called = True

    gateway.on_pipeline_request = mock_pipeline_request

    # 不支持的渠道应被 ValueError 捕获，不影响系统稳定性
    await gateway.handle_message("unsupported_channel", {"test": "data"})

    assert not callback_called, "不支持的渠道不应触发管道回调"


# ---------------------------------------------------------------------------
# 网关适配器注册测试
# ---------------------------------------------------------------------------

def test_gateway_register_adapter(gateway: Any) -> None:
    """注册通道适配器。

    验证点：
    - register_adapter 注册成功
    - 重复注册同一渠道抛出 ValueError
    """
    class FakeAdapter:
        """测试用适配器桩。"""

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    gateway.register_adapter("test_channel", FakeAdapter())

    with pytest.raises(ValueError, match="already registered"):
        gateway.register_adapter("test_channel", FakeAdapter())

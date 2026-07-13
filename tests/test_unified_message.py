"""统一消息格式系统测试。

测试覆盖：
- MessageType 枚举覆盖所有状态
- MessageSubtype 枚举覆盖所有子类型
- UnifiedMessage 模型字段验证
- 消息创建工具函数
- 时间戳格式化
- 消息格式验证
- 序列化/反序列化
- WebSocket 和 HTTP API 使用相同结构

NOTE: schemas.message 模块已在死代码清理中删除（0 外部生产引用）。
此测试文件作为引用清理的一部分标记为 skip，待消息格式统一方案确定后再更新。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

# schemas.message 已作为死代码删除，整个测试文件 skip
pytestmark = pytest.mark.skip(reason="schemas.message 已在死代码清理中删除，测试待更新")


class TestMessageType:
    """MessageType 枚举测试。"""

    def test_enum_has_all_required_types(self):
        """AC1: MessageType 枚举覆盖所有状态。"""
        from schemas.message import MessageType

        required = ["thinking", "executing", "waiting", "completed", "failed", "cancelled"]
        for name in required:
            assert hasattr(MessageType, name.upper()), f"Missing MessageType.{name.upper()}"
            assert getattr(MessageType, name.upper()).value == name

    def test_enum_values_are_strings(self):
        """枚举值必须是字符串。"""
        from schemas.message import MessageType

        for member in MessageType:
            assert isinstance(member.value, str)

    def test_enum_member_count(self):
        """确保恰好 6 个枚举成员。"""
        from schemas.message import MessageType

        assert len(MessageType) == 6

    def test_enum_is_str_enum(self):
        """MessageType 继承自 str，可以直接比较字符串。"""
        from schemas.message import MessageType

        assert MessageType.THINKING == "thinking"
        assert MessageType.EXECUTING == "executing"
        assert MessageType.COMPLETED == "completed"


class TestMessageSubtype:
    """MessageSubtype 枚举测试。"""

    def test_enum_has_all_required_subtypes(self):
        """枚举覆盖所有子类型。"""
        from schemas.message import MessageSubtype

        required = ["text", "error", "progress", "status", "system"]
        for name in required:
            assert hasattr(MessageSubtype, name.upper()), f"Missing MessageSubtype.{name.upper()}"
            assert getattr(MessageSubtype, name.upper()).value == name

    def test_enum_is_str_enum(self):
        """MessageSubtype 继承自 str。"""
        from schemas.message import MessageSubtype

        assert MessageSubtype.TEXT == "text"
        assert MessageSubtype.ERROR == "error"


class TestUnifiedMessage:
    """UnifiedMessage 模型测试。"""

    def test_create_minimal_message(self):
        """创建最小消息（仅必填字段）。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.THINKING)
        assert msg.type == MessageType.THINKING
        assert msg.subtype is None
        assert msg.status == MessageType.THINKING.value  # AC4: status 默认跟随 type
        assert isinstance(msg.content, dict)
        assert isinstance(msg.timestamp, str)
        assert isinstance(msg.metadata, dict)

    def test_create_full_message(self):
        """创建完整消息（所有字段）。"""
        from schemas.message import MessageType, MessageSubtype, UnifiedMessage

        metadata = {"task_id": "t1", "agent_id": "a1", "session_id": "s1"}
        content = {"text": "Hello"}
        msg = UnifiedMessage(
            type=MessageType.EXECUTING,
            subtype=MessageSubtype.PROGRESS,
            status="running",
            content=content,
            metadata=metadata,
        )
        assert msg.type == MessageType.EXECUTING
        assert msg.subtype == MessageSubtype.PROGRESS
        assert msg.status == "running"
        assert msg.content == content
        assert msg.metadata == metadata

    def test_timestamp_is_iso8601_with_timezone(self):
        """AC3: 时间戳统一使用 ISO 8601 格式（带时区）。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.THINKING)
        ts = msg.timestamp
        # 验证可以解析为 datetime
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None, "Timestamp must have timezone info"

    def test_type_field_must_be_message_type(self):
        """type 字段必须是 MessageType 枚举值。"""
        from schemas.message import UnifiedMessage

        with pytest.raises(Exception):
            UnifiedMessage(type="invalid_type")

    def test_subtype_optional_accepts_none(self):
        """subtype 字段可选。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.COMPLETED, subtype=None)
        assert msg.subtype is None

    def test_content_defaults_to_empty_dict(self):
        """content 默认为空字典。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.COMPLETED)
        assert msg.content == {}

    def test_metadata_defaults_to_empty_dict(self):
        """metadata 默认为空字典。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.COMPLETED)
        assert msg.metadata == {}

    def test_metadata_accepts_common_fields(self):
        """metadata 接受 task_id, agent_id, session_id 等字段。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(
            type=MessageType.EXECUTING,
            metadata={
                "task_id": "task-123",
                "agent_id": "agent-456",
                "session_id": "sess-789",
            },
        )
        assert msg.metadata["task_id"] == "task-123"
        assert msg.metadata["agent_id"] == "agent-456"
        assert msg.metadata["session_id"] == "sess-789"

    def test_status_defaults_to_type_value(self):
        """AC4: status 默认跟随 type 的枚举值。"""
        from schemas.message import MessageType, UnifiedMessage

        for mt in MessageType:
            msg = UnifiedMessage(type=mt)
            assert msg.status == mt.value

    def test_status_can_be_overridden(self):
        """status 可以被显式覆盖。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.EXECUTING, status="custom_status")
        assert msg.status == "custom_status"


class TestUnifiedMessageSerialization:
    """UnifiedMessage 序列化/反序列化测试。"""

    def test_to_dict(self):
        """to_dict 方法生成可 JSON 序列化的字典。"""
        from schemas.message import MessageType, MessageSubtype, UnifiedMessage

        msg = UnifiedMessage(
            type=MessageType.EXECUTING,
            subtype=MessageSubtype.PROGRESS,
            content={"progress": 50},
            metadata={"task_id": "t1"},
        )
        d = msg.to_dict()
        assert isinstance(d, dict)
        assert d["type"] == "executing"
        assert d["subtype"] == "progress"
        assert d["content"]["progress"] == 50
        assert d["metadata"]["task_id"] == "t1"
        assert "timestamp" in d

        # 确保可以 JSON 序列化
        json_str = json.dumps(d)
        assert isinstance(json_str, str)

    def test_from_dict(self):
        """from_dict 从字典反序列化。"""
        from schemas.message import MessageType, UnifiedMessage

        d = {
            "type": "thinking",
            "subtype": "text",
            "status": "thinking",
            "content": {"text": "思考中..."},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {"task_id": "t1"},
        }
        msg = UnifiedMessage.from_dict(d)
        assert msg.type == MessageType.THINKING
        assert msg.content["text"] == "思考中..."

    def test_roundtrip_serialization(self):
        """序列化 -> 反序列化 保持数据一致。"""
        from schemas.message import MessageType, MessageSubtype, UnifiedMessage

        original = UnifiedMessage(
            type=MessageType.FAILED,
            subtype=MessageSubtype.ERROR,
            content={"error": "timeout"},
            metadata={"task_id": "t1", "agent_id": "a1"},
        )
        d = original.to_dict()
        restored = UnifiedMessage.from_dict(d)
        assert restored.type == original.type
        assert restored.subtype == original.subtype
        assert restored.content == original.content
        assert restored.metadata == original.metadata

    def test_from_dict_minimal(self):
        """from_dict 处理最小字典（仅 type 字段）。"""
        from schemas.message import MessageType, UnifiedMessage

        d = {"type": "completed"}
        msg = UnifiedMessage.from_dict(d)
        assert msg.type == MessageType.COMPLETED

    def test_from_dict_invalid_type_raises(self):
        """from_dict 遇到无效 type 应抛出异常。"""
        from schemas.message import UnifiedMessage

        d = {"type": "nonexistent"}
        with pytest.raises(Exception):
            UnifiedMessage.from_dict(d)


class TestMessageFactory:
    """消息创建工具函数测试。"""

    def test_create_message_basic(self):
        """AC6: create_message 创建基本消息。"""
        from schemas.message import MessageType, create_message

        msg = create_message(
            msg_type=MessageType.THINKING,
            content={"text": "正在分析需求"},
        )
        assert msg.type == MessageType.THINKING
        assert msg.content["text"] == "正在分析需求"
        assert msg.status == "thinking"

    def test_create_message_with_metadata(self):
        """create_message 带元数据。"""
        from schemas.message import MessageType, create_message

        msg = create_message(
            msg_type=MessageType.EXECUTING,
            content={"tool": "bash"},
            metadata={"task_id": "t1", "agent_id": "a1"},
        )
        assert msg.metadata["task_id"] == "t1"

    def test_create_thinking_message(self):
        """便捷函数：创建思考中消息。"""
        from schemas.message import MessageType, create_thinking_message

        msg = create_thinking_message(text="正在思考", task_id="t1")
        assert msg.type == MessageType.THINKING
        assert msg.content["text"] == "正在思考"
        assert msg.metadata["task_id"] == "t1"

    def test_create_executing_message(self):
        """便捷函数：创建执行中消息。"""
        from schemas.message import MessageType, create_executing_message

        msg = create_executing_message(tool_name="bash", task_id="t1")
        assert msg.type == MessageType.EXECUTING
        assert msg.content["tool_name"] == "bash"

    def test_create_completed_message(self):
        """便捷函数：创建完成消息。"""
        from schemas.message import MessageType, create_completed_message

        msg = create_completed_message(result="成功", task_id="t1")
        assert msg.type == MessageType.COMPLETED
        assert msg.content["result"] == "成功"

    def test_create_failed_message(self):
        """便捷函数：创建失败消息。"""
        from schemas.message import MessageType, create_failed_message

        msg = create_failed_message(error="超时", task_id="t1")
        assert msg.type == MessageType.FAILED
        assert msg.content["error"] == "超时"

    def test_create_waiting_message(self):
        """便捷函数：创建等待中消息。"""
        from schemas.message import MessageType, create_waiting_message

        msg = create_waiting_message(reason="等待用户输入", task_id="t1")
        assert msg.type == MessageType.WAITING
        assert msg.content["reason"] == "等待用户输入"

    def test_create_cancelled_message(self):
        """便捷函数：创建已取消消息。"""
        from schemas.message import MessageType, create_cancelled_message

        msg = create_cancelled_message(reason="用户取消", task_id="t1")
        assert msg.type == MessageType.CANCELLED
        assert msg.content["reason"] == "用户取消"

    def test_create_progress_message(self):
        """便捷函数：创建进度消息。"""
        from schemas.message import MessageType, MessageSubtype, create_progress_message

        msg = create_progress_message(progress=75, description="下载中", task_id="t1")
        assert msg.type == MessageType.EXECUTING
        assert msg.subtype == MessageSubtype.PROGRESS
        assert msg.content["progress"] == 75
        assert msg.content["description"] == "下载中"


class TestTimestampFormatting:
    """时间戳格式化测试。"""

    def test_format_timestamp_utc(self):
        """AC3: 格式化 UTC 时间戳为 ISO 8601。"""
        from schemas.message import format_timestamp

        dt = datetime(2026, 5, 15, 10, 30, 0, tzinfo=timezone.utc)
        ts = format_timestamp(dt)
        assert "2026-05-15" in ts
        assert "10:30:00" in ts
        # 带时区标识
        assert "+00:00" in ts or "Z" in ts

    def test_format_timestamp_auto_utc(self):
        """不传参数时使用当前 UTC 时间。"""
        from schemas.message import format_timestamp

        ts = format_timestamp()
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None

    def test_format_timestamp_with_different_timezone(self):
        """AC3: 支持非 UTC 时区。"""
        from schemas.message import format_timestamp
        from datetime import timedelta

        tz = timezone(timedelta(hours=8))
        dt = datetime(2026, 5, 15, 18, 0, 0, tzinfo=tz)
        ts = format_timestamp(dt)
        dt_parsed = datetime.fromisoformat(ts)
        assert dt_parsed.tzinfo is not None
        assert "+08:00" in ts


class TestMessageValidation:
    """消息格式验证测试。"""

    def test_validate_valid_message(self):
        """验证通过的消息。"""
        from schemas.message import MessageType, validate_message_dict

        d = {
            "type": "thinking",
            "status": "thinking",
            "content": {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {},
        }
        assert validate_message_dict(d) is True

    def test_validate_missing_type(self):
        """缺少 type 字段验证失败。"""
        from schemas.message import validate_message_dict

        d = {"status": "thinking", "content": {}}
        assert validate_message_dict(d) is False

    def test_validate_invalid_type(self):
        """无效 type 值验证失败。"""
        from schemas.message import validate_message_dict

        d = {"type": "nonexistent", "content": {}}
        assert validate_message_dict(d) is False

    def test_validate_missing_timestamp_auto_ok(self):
        """缺少 timestamp 时仍可验证通过（会自动生成）。"""
        from schemas.message import validate_message_dict

        d = {"type": "completed"}
        assert validate_message_dict(d) is True


class TestWebSocketAndHttpSameStructure:
    """AC2: WebSocket 和 HTTP API 使用相同的消息结构体。"""

    def test_ws_message_uses_unified_structure(self):
        """WebSocket 消息使用 UnifiedMessage 结构。"""
        from schemas.message import MessageType, UnifiedMessage, create_message

        # 模拟 WebSocket 推送消息
        ws_msg = create_message(
            msg_type=MessageType.EXECUTING,
            content={"tool_name": "bash", "command": "ls"},
            metadata={"task_id": "t1", "session_id": "s1"},
        )
        ws_dict = ws_msg.to_dict()

        # 验证 WebSocket 消息结构一致性
        assert "type" in ws_dict
        assert "subtype" in ws_dict
        assert "status" in ws_dict
        assert "content" in ws_dict
        assert "timestamp" in ws_dict
        assert "metadata" in ws_dict

    def test_http_response_uses_unified_structure(self):
        """HTTP API 响应使用 UnifiedMessage 结构。"""
        from schemas.message import MessageType, UnifiedMessage, create_message

        # 模拟 HTTP API 响应
        http_msg = create_message(
            msg_type=MessageType.COMPLETED,
            content={"result": "success"},
            metadata={"task_id": "t1"},
        )
        http_dict = http_msg.to_dict()

        # 与 WebSocket 结构完全相同
        assert set(http_dict.keys()) == {"type", "subtype", "status", "content", "timestamp", "metadata"}

    def test_ws_and_http_structures_identical(self):
        """AC2: WS 和 HTTP 的消息结构体完全一致。"""
        from schemas.message import MessageType, create_message

        ws_msg = create_message(msg_type=MessageType.THINKING, content={"text": "思考中"})
        http_msg = create_message(msg_type=MessageType.THINKING, content={"text": "思考中"})

        ws_keys = set(ws_msg.to_dict().keys())
        http_keys = set(http_msg.to_dict().keys())
        assert ws_keys == http_keys


class TestFrontendMapping:
    """AC5: 前端能根据消息类型渲染对应的 UI 状态。"""

    def test_message_type_ui_mapping_complete(self):
        """所有 MessageType 都有对应的 UI 映射。"""
        from schemas.message import MESSAGE_TYPE_UI_MAP, MessageType

        for mt in MessageType:
            assert mt in MESSAGE_TYPE_UI_MAP, f"Missing UI mapping for {mt}"
            mapping = MESSAGE_TYPE_UI_MAP[mt]
            assert "color" in mapping
            assert "icon" in mapping
            assert "label" in mapping

    def test_thinking_mapping(self):
        """thinking 状态的 UI 映射。"""
        from schemas.message import MESSAGE_TYPE_UI_MAP, MessageType

        m = MESSAGE_TYPE_UI_MAP[MessageType.THINKING]
        assert m["color"] is not None
        assert m["icon"] is not None
        assert "思考" in m["label"]

    def test_executing_mapping(self):
        """executing 状态的 UI 映射。"""
        from schemas.message import MESSAGE_TYPE_UI_MAP, MessageType

        m = MESSAGE_TYPE_UI_MAP[MessageType.EXECUTING]
        assert "执行" in m["label"]

    def test_completed_mapping(self):
        """completed 状态的 UI 映射。"""
        from schemas.message import MESSAGE_TYPE_UI_MAP, MessageType

        m = MESSAGE_TYPE_UI_MAP[MessageType.COMPLETED]
        assert "完成" in m["label"]

    def test_failed_mapping(self):
        """failed 状态的 UI 映射。"""
        from schemas.message import MESSAGE_TYPE_UI_MAP, MessageType

        m = MESSAGE_TYPE_UI_MAP[MessageType.FAILED]
        assert "失败" in m["label"] or "错误" in m["label"]

    def test_cancelled_mapping(self):
        """cancelled 状态的 UI 映射。"""
        from schemas.message import MESSAGE_TYPE_UI_MAP, MessageType

        m = MESSAGE_TYPE_UI_MAP[MessageType.CANCELLED]
        assert "取消" in m["label"]

    def test_waiting_mapping(self):
        """waiting 状态的 UI 映射。"""
        from schemas.message import MESSAGE_TYPE_UI_MAP, MessageType

        m = MESSAGE_TYPE_UI_MAP[MessageType.WAITING]
        assert "等待" in m["label"]

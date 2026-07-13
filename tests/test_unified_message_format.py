"""统一消息格式系统全面测试。

测试覆盖：
- MessageType 枚举完整性（thinking/executing/waiting/completed/failed/cancelled）
- MessageSubType 枚举完整性（text/error/progress/status/system）
- UnifiedMessage 模型字段完整性与类型校验
- 时间戳 ISO 8601 格式带时区、序列化/反序列化一致性
- WebSocket 消息与 HTTP API 响应结构一致性
- 消息格式化工具函数（create_message/format_timestamp/validate_message）
- 状态字段统一使用 MessageType 枚举值
- 边界场景：空消息内容、未知消息类型、超长内容、无效时间戳

NOTE: schemas.message 模块已在死代码清理中删除（0 外部生产引用）。
此测试文件作为引用清理的一部分标记为 skip，待消息格式统一方案确定后再更新。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

# schemas.message 已作为死代码删除，整个测试文件 skip
pytestmark = pytest.mark.skip(reason="schemas.message 已在死代码清理中删除，测试待更新")


# =============================================================================
# 1. MessageType 枚举完整性测试
# =============================================================================


class TestMessageTypeEnum:
    """MessageType 枚举完整性测试。"""

    def test_all_required_types_exist(self):
        """验证所有必需状态存在：thinking/executing/waiting/completed/failed/cancelled。"""
        from schemas.message import MessageType

        required = {
            "THINKING": "thinking",
            "EXECUTING": "executing",
            "WAITING": "waiting",
            "COMPLETED": "completed",
            "FAILED": "failed",
            "CANCELLED": "cancelled",
        }
        for name, value in required.items():
            assert hasattr(MessageType, name), f"缺少 MessageType.{name}"
            assert getattr(MessageType, name).value == value, (
                f"MessageType.{name} 的值应为 '{value}'"
            )

    def test_exactly_six_members(self):
        """确保恰好 6 个枚举成员，不多不少。"""
        from schemas.message import MessageType

        assert len(MessageType) == 6

    def test_all_values_are_unique(self):
        """所有枚举值唯一，无重复。"""
        from schemas.message import MessageType

        values = [m.value for m in MessageType]
        assert len(values) == len(set(values))

    def test_inherits_from_str(self):
        """MessageType 继承自 str，可以直接与字符串比较。"""
        from schemas.message import MessageType

        for member in MessageType:
            assert isinstance(member, str)
            assert isinstance(member.value, str)
            assert member == member.value

    def test_string_comparison_works(self):
        """枚举成员可以直接与字符串做相等比较。"""
        from schemas.message import MessageType

        assert MessageType.THINKING == "thinking"
        assert MessageType.EXECUTING == "executing"
        assert MessageType.WAITING == "waiting"
        assert MessageType.COMPLETED == "completed"
        assert MessageType.FAILED == "failed"
        assert MessageType.CANCELLED == "cancelled"

    def test_enum_iteration(self):
        """枚举可正常迭代遍历所有成员。"""
        from schemas.message import MessageType

        members = list(MessageType)
        assert len(members) == 6

    def test_construct_from_value(self):
        """可以通过字符串值构造枚举成员。"""
        from schemas.message import MessageType

        assert MessageType("thinking") == MessageType.THINKING
        assert MessageType("completed") == MessageType.COMPLETED
        assert MessageType("failed") == MessageType.FAILED

    def test_invalid_value_raises(self):
        """无效字符串值构造枚举应抛出 ValueError。"""
        from schemas.message import MessageType

        with pytest.raises(ValueError):
            MessageType("invalid_type")
        with pytest.raises(ValueError):
            MessageType("")


# =============================================================================
# 2. MessageSubType 枚举测试
# =============================================================================


class TestMessageSubtypeEnum:
    """MessageSubType 枚举测试。"""

    def test_all_required_subtypes_exist(self):
        """验证所有子类型存在：text/error/progress/status/system。"""
        from schemas.message import MessageSubtype

        required = {
            "TEXT": "text",
            "ERROR": "error",
            "PROGRESS": "progress",
            "STATUS": "status",
            "SYSTEM": "system",
        }
        for name, value in required.items():
            assert hasattr(MessageSubtype, name), f"缺少 MessageSubtype.{name}"
            assert getattr(MessageSubtype, name).value == value, (
                f"MessageSubtype.{name} 的值应为 '{value}'"
            )

    def test_exactly_five_members(self):
        """确保恰好 5 个枚举成员。"""
        from schemas.message import MessageSubtype

        assert len(MessageSubtype) == 5

    def test_all_values_are_unique(self):
        """所有枚举值唯一。"""
        from schemas.message import MessageSubtype

        values = [m.value for m in MessageSubtype]
        assert len(values) == len(set(values))

    def test_inherits_from_str(self):
        """MessageSubtype 继承自 str。"""
        from schemas.message import MessageSubtype

        for member in MessageSubtype:
            assert isinstance(member, str)
            assert member == member.value

    def test_string_comparison_works(self):
        """枚举成员可以直接与字符串做相等比较。"""
        from schemas.message import MessageSubtype

        assert MessageSubtype.TEXT == "text"
        assert MessageSubtype.ERROR == "error"
        assert MessageSubtype.PROGRESS == "progress"
        assert MessageSubtype.STATUS == "status"
        assert MessageSubtype.SYSTEM == "system"

    def test_construct_from_value(self):
        """可以通过字符串值构造枚举成员。"""
        from schemas.message import MessageSubtype

        assert MessageSubtype("text") == MessageSubtype.TEXT
        assert MessageSubtype("system") == MessageSubtype.SYSTEM

    def test_invalid_value_raises(self):
        """无效值应抛出 ValueError。"""
        from schemas.message import MessageSubtype

        with pytest.raises(ValueError):
            MessageSubtype("nonexistent")


# =============================================================================
# 3. UnifiedMessage 模型测试
# =============================================================================


class TestUnifiedMessageModel:
    """UnifiedMessage 模型字段完整性、类型校验、默认值测试。"""

    def test_create_minimal_message(self):
        """创建最小消息（仅必填字段 type）。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.THINKING)
        assert msg.type == MessageType.THINKING
        assert msg.subtype is None
        assert isinstance(msg.content, dict)
        assert msg.content == {}
        assert isinstance(msg.timestamp, str)
        assert len(msg.timestamp) > 0
        assert isinstance(msg.metadata, dict)
        assert msg.metadata == {}

    def test_create_full_message(self):
        """创建完整消息（所有字段显式赋值）。"""
        from schemas.message import MessageType, MessageSubtype, UnifiedMessage

        content = {"text": "执行结果", "data": [1, 2, 3]}
        metadata = {"task_id": "t-001", "agent_id": "a-002", "session_id": "s-003"}
        ts = "2026-05-15T10:30:00+00:00"

        msg = UnifiedMessage(
            type=MessageType.EXECUTING,
            subtype=MessageSubtype.PROGRESS,
            status="running",
            content=content,
            timestamp=ts,
            metadata=metadata,
        )
        assert msg.type == MessageType.EXECUTING
        assert msg.subtype == MessageSubtype.PROGRESS
        assert msg.status == "running"
        assert msg.content == content
        assert msg.timestamp == ts
        assert msg.metadata == metadata

    def test_type_field_required(self):
        """type 字段必填，缺失应报错。"""
        from schemas.message import UnifiedMessage

        with pytest.raises(Exception):
            UnifiedMessage()

    def test_type_field_must_be_enum(self):
        """type 字段必须是合法的 MessageType 枚举值。"""
        from schemas.message import UnifiedMessage

        with pytest.raises(Exception):
            UnifiedMessage(type="invalid_type")

    def test_type_field_accepts_string_value(self):
        """type 字段接受合法的 MessageType 字符串值（Pydantic 自动转换）。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type="thinking")
        assert msg.type == MessageType.THINKING

    def test_subtype_optional_none(self):
        """subtype 可选，默认为 None。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.COMPLETED)
        assert msg.subtype is None

    def test_subtype_accepts_enum(self):
        """subtype 接受 MessageSubtype 枚举值。"""
        from schemas.message import MessageType, MessageSubtype, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.FAILED, subtype=MessageSubtype.ERROR)
        assert msg.subtype == MessageSubtype.ERROR

    def test_status_defaults_to_type_value(self):
        """status 默认跟随 type 的枚举值。"""
        from schemas.message import MessageType, UnifiedMessage

        for mt in MessageType:
            msg = UnifiedMessage(type=mt)
            assert msg.status == mt.value, (
                f"status 默认值应为 '{mt.value}'，实际为 '{msg.status}'"
            )

    def test_status_can_be_overridden(self):
        """status 可以被显式覆盖。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.EXECUTING, status="custom_running")
        assert msg.status == "custom_running"

    def test_content_defaults_to_empty_dict(self):
        """content 默认为空字典。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.COMPLETED)
        assert msg.content == {}
        assert isinstance(msg.content, dict)

    def test_content_accepts_nested_data(self):
        """content 接受嵌套数据结构。"""
        from schemas.message import MessageType, UnifiedMessage

        content = {
            "result": "success",
            "details": {"steps": 5, "errors": 0},
            "items": [1, 2, 3],
        }
        msg = UnifiedMessage(type=MessageType.COMPLETED, content=content)
        assert msg.content["details"]["steps"] == 5
        assert msg.content["items"] == [1, 2, 3]

    def test_metadata_defaults_to_empty_dict(self):
        """metadata 默认为空字典。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.WAITING)
        assert msg.metadata == {}

    def test_metadata_accepts_task_fields(self):
        """metadata 接受 task_id, agent_id, session_id 等字段。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(
            type=MessageType.EXECUTING,
            metadata={
                "task_id": "task-001",
                "agent_id": "agent-002",
                "session_id": "session-003",
            },
        )
        assert msg.metadata["task_id"] == "task-001"
        assert msg.metadata["agent_id"] == "agent-002"
        assert msg.metadata["session_id"] == "session-003"

    def test_timestamp_auto_generated(self):
        """timestamp 不传时自动生成。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.THINKING)
        assert isinstance(msg.timestamp, str)
        assert len(msg.timestamp) > 0
        # 验证可被解析为 datetime
        dt = datetime.fromisoformat(msg.timestamp)
        assert dt.tzinfo is not None

    def test_timestamp_can_be_set(self):
        """timestamp 可以被显式设置。"""
        from schemas.message import MessageType, UnifiedMessage

        ts = "2026-01-01T00:00:00+08:00"
        msg = UnifiedMessage(type=MessageType.COMPLETED, timestamp=ts)
        assert msg.timestamp == ts

    def test_each_message_type_creates_valid_message(self):
        """每个 MessageType 都能创建有效的 UnifiedMessage。"""
        from schemas.message import MessageType, UnifiedMessage

        for mt in MessageType:
            msg = UnifiedMessage(type=mt)
            assert msg.type == mt
            assert msg.status == mt.value


# =============================================================================
# 4. 时间戳格式测试
# =============================================================================


class TestTimestampFormat:
    """时间戳 ISO 8601 格式、带时区、序列化/反序列化一致性测试。"""

    def test_format_timestamp_utc(self):
        """格式化 UTC 时间戳为 ISO 8601。"""
        from schemas.message import format_timestamp

        dt = datetime(2026, 5, 15, 10, 30, 0, tzinfo=timezone.utc)
        ts = format_timestamp(dt)
        assert "2026-05-15" in ts
        assert "10:30:00" in ts
        assert "+00:00" in ts or "Z" in ts

    def test_format_timestamp_auto_utc(self):
        """不传参数时使用当前 UTC 时间。"""
        from schemas.message import format_timestamp

        ts = format_timestamp()
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None

    def test_format_timestamp_with_non_utc_timezone(self):
        """支持非 UTC 时区。"""
        from schemas.message import format_timestamp

        tz = timezone(timedelta(hours=8))
        dt = datetime(2026, 5, 15, 18, 0, 0, tzinfo=tz)
        ts = format_timestamp(dt)
        dt_parsed = datetime.fromisoformat(ts)
        assert dt_parsed.tzinfo is not None
        assert "+08:00" in ts

    def test_format_timestamp_with_negative_timezone(self):
        """支持负时区偏移。"""
        from schemas.message import format_timestamp

        tz = timezone(timedelta(hours=-5))
        dt = datetime(2026, 5, 15, 10, 0, 0, tzinfo=tz)
        ts = format_timestamp(dt)
        dt_parsed = datetime.fromisoformat(ts)
        assert dt_parsed.tzinfo is not None
        assert "-05:00" in ts

    def test_timestamp_always_has_timezone(self):
        """消息的时间戳始终带时区信息。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.THINKING)
        dt = datetime.fromisoformat(msg.timestamp)
        assert dt.tzinfo is not None, "时间戳必须包含时区信息"

    def test_timestamp_microseconds_preserved(self):
        """时间戳保留微秒精度。"""
        from schemas.message import format_timestamp

        dt = datetime(2026, 5, 15, 10, 30, 0, 123456, tzinfo=timezone.utc)
        ts = format_timestamp(dt)
        # 微秒部分应出现在 ISO 8601 字符串中
        assert "123456" in ts

    def test_timestamp_roundtrip_consistency(self):
        """序列化 -> 反序列化 -> 序列化保持一致。"""
        from schemas.message import format_timestamp

        original_dt = datetime(2026, 5, 15, 14, 30, 0, tzinfo=timezone.utc)
        ts1 = format_timestamp(original_dt)
        parsed_dt = datetime.fromisoformat(ts1)
        ts2 = format_timestamp(parsed_dt)
        assert ts1 == ts2

    def test_message_timestamp_roundtrip(self):
        """消息的 to_dict/from_dict 时间戳保持一致。"""
        from schemas.message import MessageType, UnifiedMessage

        original = UnifiedMessage(type=MessageType.THINKING)
        d = original.to_dict()
        restored = UnifiedMessage.from_dict(d)
        assert restored.timestamp == original.timestamp

    def test_timestamps_generated_close_in_time(self):
        """连续生成的两个时间戳非常接近。"""
        from schemas.message import MessageType, UnifiedMessage

        msg1 = UnifiedMessage(type=MessageType.THINKING)
        msg2 = UnifiedMessage(type=MessageType.THINKING)
        dt1 = datetime.fromisoformat(msg1.timestamp)
        dt2 = datetime.fromisoformat(msg2.timestamp)
        diff = abs((dt2 - dt1).total_seconds())
        assert diff < 2.0, f"两个连续时间戳差异应小于 2 秒，实际为 {diff}"


# =============================================================================
# 5. WebSocket 消息与 HTTP API 响应一致性测试
# =============================================================================


class TestWebSocketHttpConsistency:
    """WebSocket 消息与 HTTP API 响应使用相同的消息结构。"""

    def test_ws_message_has_all_required_fields(self):
        """WebSocket 消息包含所有必需字段。"""
        from schemas.message import MessageType, create_message

        ws_msg = create_message(
            msg_type=MessageType.EXECUTING,
            content={"tool_name": "bash", "command": "ls"},
            metadata={"task_id": "t1", "session_id": "s1"},
        )
        d = ws_msg.to_dict()
        required_fields = {"type", "subtype", "status", "content", "timestamp", "metadata"}
        assert required_fields.issubset(set(d.keys()))

    def test_http_message_has_all_required_fields(self):
        """HTTP API 响应消息包含所有必需字段。"""
        from schemas.message import MessageType, create_message

        http_msg = create_message(
            msg_type=MessageType.COMPLETED,
            content={"result": "success"},
            metadata={"task_id": "t1"},
        )
        d = http_msg.to_dict()
        required_fields = {"type", "subtype", "status", "content", "timestamp", "metadata"}
        assert required_fields.issubset(set(d.keys()))

    def test_ws_and_http_identical_structure(self):
        """WS 和 HTTP 的消息结构体完全一致（相同 key 集合）。"""
        from schemas.message import MessageType, create_message

        ws_msg = create_message(msg_type=MessageType.THINKING, content={"text": "思考中"})
        http_msg = create_message(msg_type=MessageType.THINKING, content={"text": "思考中"})

        ws_keys = set(ws_msg.to_dict().keys())
        http_keys = set(http_msg.to_dict().keys())
        assert ws_keys == http_keys

    def test_ws_and_http_use_same_model(self):
        """WS 和 HTTP 都使用 UnifiedMessage 模型。"""
        from schemas.message import UnifiedMessage, create_message, MessageType

        ws_msg = create_message(msg_type=MessageType.EXECUTING)
        http_msg = create_message(msg_type=MessageType.COMPLETED)
        assert isinstance(ws_msg, UnifiedMessage)
        assert isinstance(http_msg, UnifiedMessage)

    def test_ws_http_json_serialization_identical(self):
        """WS 和 HTTP 消息的 JSON 序列化结构一致。"""
        from schemas.message import MessageType, MessageSubtype, create_message

        msg = create_message(
            msg_type=MessageType.FAILED,
            subtype=MessageSubtype.ERROR,
            content={"error": "timeout"},
            metadata={"task_id": "t1"},
        )
        d = msg.to_dict()
        # 确保可以 JSON 序列化
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        assert parsed["type"] == "failed"
        assert parsed["subtype"] == "error"
        assert parsed["content"]["error"] == "timeout"

    def test_all_message_types_produce_consistent_structure(self):
        """所有 MessageType 生成的消息结构完全一致。"""
        from schemas.message import MessageType, create_message

        expected_keys = {"type", "subtype", "status", "content", "timestamp", "metadata"}
        for mt in MessageType:
            msg = create_message(msg_type=mt)
            actual_keys = set(msg.to_dict().keys())
            assert actual_keys == expected_keys, (
                f"MessageType.{mt.name} 的消息结构不一致: {actual_keys} != {expected_keys}"
            )


# =============================================================================
# 6. 消息格式化工具函数测试
# =============================================================================


class TestCreateMessage:
    """create_message 工厂函数测试。"""

    def test_create_basic_message(self):
        """create_message 创建基本消息。"""
        from schemas.message import MessageType, create_message

        msg = create_message(
            msg_type=MessageType.THINKING,
            content={"text": "分析需求"},
        )
        assert msg.type == MessageType.THINKING
        assert msg.content["text"] == "分析需求"
        assert msg.status == "thinking"

    def test_create_message_with_all_params(self):
        """create_message 使用所有参数。"""
        from schemas.message import MessageType, MessageSubtype, create_message

        msg = create_message(
            msg_type=MessageType.EXECUTING,
            content={"tool": "bash"},
            metadata={"task_id": "t1"},
            subtype=MessageSubtype.STATUS,
            status="custom",
        )
        assert msg.type == MessageType.EXECUTING
        assert msg.subtype == MessageSubtype.STATUS
        assert msg.status == "custom"
        assert msg.metadata["task_id"] == "t1"

    def test_create_message_no_content_or_metadata(self):
        """不传 content 和 metadata 时使用空字典。"""
        from schemas.message import MessageType, create_message

        msg = create_message(msg_type=MessageType.COMPLETED)
        assert msg.content == {}
        assert msg.metadata == {}

    def test_create_message_with_custom_status(self):
        """create_message 支持自定义 status。"""
        from schemas.message import MessageType, create_message

        msg = create_message(
            msg_type=MessageType.EXECUTING,
            status="processing",
        )
        assert msg.status == "processing"


class TestConvenienceFunctions:
    """便捷消息创建函数测试。"""

    def test_create_thinking_message(self):
        """创建思考中消息。"""
        from schemas.message import MessageType, create_thinking_message

        msg = create_thinking_message(text="正在分析", task_id="t1")
        assert msg.type == MessageType.THINKING
        assert msg.content["text"] == "正在分析"
        assert msg.metadata["task_id"] == "t1"
        assert msg.status == "thinking"

    def test_create_thinking_message_default_args(self):
        """思考消息使用默认参数。"""
        from schemas.message import MessageType, create_thinking_message

        msg = create_thinking_message()
        assert msg.type == MessageType.THINKING
        assert msg.content["text"] == ""

    def test_create_executing_message(self):
        """创建执行中消息。"""
        from schemas.message import MessageType, create_executing_message

        msg = create_executing_message(tool_name="bash", task_id="t1")
        assert msg.type == MessageType.EXECUTING
        assert msg.content["tool_name"] == "bash"
        assert msg.status == "executing"

    def test_create_waiting_message(self):
        """创建等待中消息。"""
        from schemas.message import MessageType, create_waiting_message

        msg = create_waiting_message(reason="等待用户输入", task_id="t1")
        assert msg.type == MessageType.WAITING
        assert msg.content["reason"] == "等待用户输入"
        assert msg.status == "waiting"

    def test_create_completed_message(self):
        """创建完成消息。"""
        from schemas.message import MessageType, create_completed_message

        msg = create_completed_message(result="成功", task_id="t1")
        assert msg.type == MessageType.COMPLETED
        assert msg.content["result"] == "成功"
        assert msg.status == "completed"

    def test_create_failed_message(self):
        """创建失败消息（带 ERROR 子类型）。"""
        from schemas.message import MessageType, MessageSubtype, create_failed_message

        msg = create_failed_message(error="超时", task_id="t1")
        assert msg.type == MessageType.FAILED
        assert msg.subtype == MessageSubtype.ERROR
        assert msg.content["error"] == "超时"
        assert msg.status == "failed"

    def test_create_cancelled_message(self):
        """创建已取消消息。"""
        from schemas.message import MessageType, create_cancelled_message

        msg = create_cancelled_message(reason="用户取消", task_id="t1")
        assert msg.type == MessageType.CANCELLED
        assert msg.content["reason"] == "用户取消"
        assert msg.status == "cancelled"

    def test_create_progress_message(self):
        """创建进度消息（EXECUTING + PROGRESS 子类型）。"""
        from schemas.message import MessageType, MessageSubtype, create_progress_message

        msg = create_progress_message(progress=75, description="下载中", task_id="t1")
        assert msg.type == MessageType.EXECUTING
        assert msg.subtype == MessageSubtype.PROGRESS
        assert msg.content["progress"] == 75
        assert msg.content["description"] == "下载中"

    def test_convenience_functions_with_extra_kwargs(self):
        """便捷函数接受 **extra_content 扩展 content 字段。"""
        from schemas.message import create_thinking_message

        msg = create_thinking_message(text="分析中", extra_field="value")
        assert msg.content["extra_field"] == "value"

    def test_convenience_functions_build_metadata_only_non_empty(self):
        """便捷函数的 _build_metadata 仅包含非空字段。"""
        from schemas.message import create_completed_message

        msg = create_completed_message(result="ok", task_id="t1")
        # 只传了 task_id，metadata 里不应有 agent_id 和 session_id
        assert "task_id" in msg.metadata
        assert "agent_id" not in msg.metadata
        assert "session_id" not in msg.metadata

    def test_convenience_functions_all_ids(self):
        """便捷函数同时传入所有 ID。"""
        from schemas.message import create_completed_message

        msg = create_completed_message(
            result="ok",
            task_id="t1",
            agent_id="a1",
            session_id="s1",
        )
        assert msg.metadata["task_id"] == "t1"
        assert msg.metadata["agent_id"] == "a1"
        assert msg.metadata["session_id"] == "s1"


class TestFormatTimestamp:
    """format_timestamp 函数测试。"""

    def test_returns_iso8601_string(self):
        """返回 ISO 8601 格式字符串。"""
        from schemas.message import format_timestamp

        ts = format_timestamp()
        # ISO 8601 可被 fromisoformat 解析
        dt = datetime.fromisoformat(ts)
        assert isinstance(dt, datetime)

    def test_with_explicit_datetime(self):
        """传入 datetime 对象，返回其 ISO 8601 格式。"""
        from schemas.message import format_timestamp

        dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        ts = format_timestamp(dt)
        assert "2026-01-01" in ts
        assert "00:00:00" in ts

    def test_without_args_uses_current_utc(self):
        """不传参数使用当前 UTC 时间。"""
        from schemas.message import format_timestamp

        before = datetime.now(timezone.utc)
        ts = format_timestamp()
        after = datetime.now(timezone.utc)

        dt = datetime.fromisoformat(ts)
        assert before <= dt <= after or abs((dt - before).total_seconds()) < 1

    def test_none_argument_uses_current_utc(self):
        """传入 None 使用当前 UTC 时间。"""
        from schemas.message import format_timestamp

        ts = format_timestamp(None)
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None


class TestValidateMessageDict:
    """validate_message_dict 函数测试。"""

    def test_valid_message(self):
        """验证通过的消息。"""
        from schemas.message import validate_message_dict

        d = {
            "type": "thinking",
            "status": "thinking",
            "content": {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {},
        }
        assert validate_message_dict(d) is True

    def test_missing_type_returns_false(self):
        """缺少 type 字段验证失败。"""
        from schemas.message import validate_message_dict

        assert validate_message_dict({"status": "thinking"}) is False

    def test_invalid_type_returns_false(self):
        """无效 type 值验证失败。"""
        from schemas.message import validate_message_dict

        assert validate_message_dict({"type": "nonexistent"}) is False

    def test_empty_type_returns_false(self):
        """空字符串 type 验证失败。"""
        from schemas.message import validate_message_dict

        assert validate_message_dict({"type": ""}) is False

    def test_minimal_valid_message(self):
        """只有 type 字段的消息验证通过。"""
        from schemas.message import validate_message_dict

        for type_val in ["thinking", "executing", "waiting", "completed", "failed", "cancelled"]:
            assert validate_message_dict({"type": type_val}) is True

    def test_all_valid_types(self):
        """所有合法 MessageType 值都验证通过。"""
        from schemas.message import MessageType, validate_message_dict

        for mt in MessageType:
            assert validate_message_dict({"type": mt.value}) is True

    def test_non_dict_input_returns_false(self):
        """非字典输入验证失败（返回 False 或抛出异常）。"""
        from schemas.message import validate_message_dict

        # 字符串和列表不是合法 dict，应返回 False
        assert validate_message_dict("not a dict") is False
        assert validate_message_dict([]) is False
        # 数字不支持 in 操作，应抛出异常或返回 False
        with pytest.raises((TypeError, Exception)):
            validate_message_dict(42)

    def test_empty_dict_returns_false(self):
        """空字典验证失败。"""
        from schemas.message import validate_message_dict

        assert validate_message_dict({}) is False


# =============================================================================
# 7. 状态字段枚举值测试
# =============================================================================


class TestStatusFieldEnumValues:
    """所有状态字段使用 MessageType 枚举而非字符串。"""

    def test_status_defaults_are_enum_values(self):
        """status 默认值来自 MessageType 枚举的 value。"""
        from schemas.message import MessageType, UnifiedMessage

        for mt in MessageType:
            msg = UnifiedMessage(type=mt)
            assert msg.status == mt.value
            # 确认 msg.status 是 MessageType 枚举成员的值，可通过 MessageType() 反查
            assert MessageType(msg.status) == mt

    def test_to_dict_status_is_enum_string_value(self):
        """to_dict 中 status 是枚举的字符串值。"""
        from schemas.message import MessageType, UnifiedMessage

        for mt in MessageType:
            msg = UnifiedMessage(type=mt)
            d = msg.to_dict()
            assert d["status"] == mt.value
            # 验证可以通过 MessageType() 反查
            assert MessageType(d["status"]) == mt

    def test_to_dict_type_is_string_value(self):
        """to_dict 中 type 字段是枚举的字符串值。"""
        from schemas.message import MessageType, UnifiedMessage

        for mt in MessageType:
            msg = UnifiedMessage(type=mt)
            d = msg.to_dict()
            assert d["type"] == mt.value
            assert MessageType(d["type"]) == mt

    def test_from_dict_type_converts_to_enum(self):
        """from_dict 将字符串 type 转换为 MessageType 枚举。"""
        from schemas.message import MessageType, UnifiedMessage

        for mt in MessageType:
            d = {"type": mt.value}
            msg = UnifiedMessage.from_dict(d)
            assert isinstance(msg.type, MessageType)
            assert msg.type == mt

    def test_custom_status_still_string(self):
        """自定义 status 保持为字符串。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.EXECUTING, status="custom_state")
        assert msg.status == "custom_state"
        assert isinstance(msg.status, str)


# =============================================================================
# 8. 序列化/反序列化测试
# =============================================================================


class TestSerialization:
    """UnifiedMessage 序列化/反序列化测试。"""

    def test_to_dict_produces_json_serializable_dict(self):
        """to_dict 生成可 JSON 序列化的字典。"""
        from schemas.message import MessageType, MessageSubtype, UnifiedMessage

        msg = UnifiedMessage(
            type=MessageType.EXECUTING,
            subtype=MessageSubtype.PROGRESS,
            content={"progress": 50},
            metadata={"task_id": "t1"},
        )
        d = msg.to_dict()
        json_str = json.dumps(d)
        assert isinstance(json_str, str)

        # 反序列化验证
        parsed = json.loads(json_str)
        assert parsed["type"] == "executing"
        assert parsed["subtype"] == "progress"

    def test_to_dict_subtype_none(self):
        """subtype 为 None 时 to_dict 输出 None。"""
        from schemas.message import MessageType, UnifiedMessage

        msg = UnifiedMessage(type=MessageType.THINKING)
        d = msg.to_dict()
        assert d["subtype"] is None

    def test_from_dict_roundtrip(self):
        """to_dict -> from_dict 往返保持数据一致。"""
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
        assert restored.timestamp == original.timestamp

    def test_from_dict_minimal(self):
        """from_dict 处理最小字典（仅 type 字段）。"""
        from schemas.message import MessageType, UnifiedMessage

        d = {"type": "completed"}
        msg = UnifiedMessage.from_dict(d)
        assert msg.type == MessageType.COMPLETED
        assert msg.content == {}
        assert msg.metadata == {}

    def test_from_dict_invalid_type_raises(self):
        """from_dict 遇到无效 type 应抛出异常。"""
        from schemas.message import UnifiedMessage

        d = {"type": "nonexistent"}
        with pytest.raises(Exception):
            UnifiedMessage.from_dict(d)

    def test_json_roundtrip(self):
        """JSON 完整往返测试。"""
        from schemas.message import MessageType, MessageSubtype, UnifiedMessage

        original = UnifiedMessage(
            type=MessageType.EXECUTING,
            subtype=MessageSubtype.PROGRESS,
            status="running",
            content={"progress": 42, "description": "处理中"},
            metadata={"task_id": "t-100", "session_id": "s-200"},
        )
        json_str = json.dumps(original.to_dict())
        parsed_dict = json.loads(json_str)
        restored = UnifiedMessage.from_dict(parsed_dict)

        assert restored.type == original.type
        assert restored.subtype == original.subtype
        assert restored.status == original.status
        assert restored.content == original.content
        assert restored.metadata == original.metadata


# =============================================================================
# 9. 前端 UI 状态映射测试
# =============================================================================


class TestFrontendMapping:
    """前端 UI 状态映射测试。"""

    def test_all_types_have_ui_mapping(self):
        """所有 MessageType 都有 UI 映射。"""
        from schemas.message import MESSAGE_TYPE_UI_MAP, MessageType

        for mt in MessageType:
            assert mt in MESSAGE_TYPE_UI_MAP, f"缺少 {mt} 的 UI 映射"

    def test_mapping_has_required_fields(self):
        """UI 映射包含 color/icon/label 字段。"""
        from schemas.message import MESSAGE_TYPE_UI_MAP, MessageType

        for mt in MessageType:
            mapping = MESSAGE_TYPE_UI_MAP[mt]
            assert "color" in mapping, f"{mt} 映射缺少 color"
            assert "icon" in mapping, f"{mt} 映射缺少 icon"
            assert "label" in mapping, f"{mt} 映射缺少 label"

    def test_mapping_values_not_empty(self):
        """UI 映射值非空。"""
        from schemas.message import MESSAGE_TYPE_UI_MAP, MessageType

        for mt in MessageType:
            mapping = MESSAGE_TYPE_UI_MAP[mt]
            assert mapping["color"], f"{mt} 的 color 不应为空"
            assert mapping["icon"], f"{mt} 的 icon 不应为空"
            assert mapping["label"], f"{mt} 的 label 不应为空"


# =============================================================================
# 10. 边界场景测试
# =============================================================================


class TestBoundaryScenarios:
    """边界场景测试：空消息、未知类型、超长内容、无效时间戳。"""

    def test_empty_content(self):
        """空消息内容：content 为空字典。"""
        from schemas.message import MessageType, UnifiedMessage, create_message

        # 直接创建
        msg = UnifiedMessage(type=MessageType.COMPLETED)
        assert msg.content == {}

        # 通过工厂创建
        msg2 = create_message(msg_type=MessageType.COMPLETED, content={})
        assert msg2.content == {}

    def test_empty_content_text_field(self):
        """content.text 为空字符串。"""
        from schemas.message import create_thinking_message

        msg = create_thinking_message(text="")
        assert msg.content["text"] == ""

    def test_very_long_content(self):
        """超长消息内容（10000 字符）。"""
        from schemas.message import MessageType, UnifiedMessage, create_message

        long_text = "A" * 10000
        content = {"text": long_text}
        msg = create_message(msg_type=MessageType.THINKING, content=content)
        assert len(msg.content["text"]) == 10000
        assert msg.content["text"] == long_text

        # 确保序列化正常
        d = msg.to_dict()
        json_str = json.dumps(d)
        assert len(json_str) > 10000

    def test_very_long_metadata_value(self):
        """超长 metadata 值。"""
        from schemas.message import MessageType, UnifiedMessage

        long_value = "x" * 5000
        msg = UnifiedMessage(
            type=MessageType.COMPLETED,
            metadata={"task_id": long_value},
        )
        assert msg.metadata["task_id"] == long_value

    def test_unicode_content(self):
        """Unicode 内容（中文、emoji）。"""
        from schemas.message import MessageType, UnifiedMessage

        content = {
            "text": "你好世界 🌍 Hello 🎉",
            "description": "中文测试 & émojis ñ",
        }
        msg = UnifiedMessage(type=MessageType.THINKING, content=content)
        assert msg.content["text"] == "你好世界 🌍 Hello 🎉"

        # JSON 序列化验证
        d = msg.to_dict()
        json_str = json.dumps(d, ensure_ascii=False)
        assert "你好世界" in json_str

    def test_special_characters_in_content(self):
        """特殊字符内容（引号、换行、制表符）。"""
        from schemas.message import MessageType, UnifiedMessage

        content = {
            "text": 'Line 1\nLine 2\t"quoted"\r\n',
            "code": "print('hello\\nworld')",
        }
        msg = UnifiedMessage(type=MessageType.EXECUTING, content=content)
        d = msg.to_dict()
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        assert parsed["content"]["text"] == content["text"]

    def test_nested_content_structure(self):
        """深层嵌套 content 结构。"""
        from schemas.message import MessageType, UnifiedMessage

        content = {
            "level1": {
                "level2": {
                    "level3": {
                        "value": "deep",
                        "count": 42,
                    },
                },
            },
        }
        msg = UnifiedMessage(type=MessageType.COMPLETED, content=content)
        assert msg.content["level1"]["level2"]["level3"]["value"] == "deep"

    def test_content_with_various_types(self):
        """content 包含多种数据类型。"""
        from schemas.message import MessageType, UnifiedMessage

        content = {
            "string": "text",
            "integer": 42,
            "float": 3.14,
            "boolean": True,
            "null": None,
            "list": [1, 2, 3],
            "nested_dict": {"key": "value"},
        }
        msg = UnifiedMessage(type=MessageType.COMPLETED, content=content)
        d = msg.to_dict()
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        assert parsed["content"]["integer"] == 42
        assert parsed["content"]["boolean"] is True
        assert parsed["content"]["null"] is None

    def test_unknown_message_type_in_dict(self):
        """from_dict 遇到未知消息类型应抛出异常。"""
        from schemas.message import UnifiedMessage

        with pytest.raises(Exception):
            UnifiedMessage.from_dict({"type": "unknown_type"})

    def test_invalid_message_type_string(self):
        """无效字符串作为 type 应抛出异常。"""
        from schemas.message import UnifiedMessage

        with pytest.raises(Exception):
            UnifiedMessage(type="random_string")

    def test_empty_string_type(self):
        """空字符串 type 应抛出异常。"""
        from schemas.message import UnifiedMessage

        with pytest.raises(Exception):
            UnifiedMessage(type="")

    def test_numeric_type_raises(self):
        """数字作为 type 应抛出异常。"""
        from schemas.message import UnifiedMessage

        with pytest.raises(Exception):
            UnifiedMessage(type=123)

    def test_none_type_raises(self):
        """None 作为 type 应抛出异常。"""
        from schemas.message import UnifiedMessage

        with pytest.raises(Exception):
            UnifiedMessage(type=None)

    def test_invalid_timestamp_format(self):
        """无效时间戳字符串仍然被保存（不做格式校验）。"""
        from schemas.message import MessageType, UnifiedMessage

        # 空字符串 timestamp 会被 model_validator 替换为自动生成的
        msg = UnifiedMessage(type=MessageType.THINKING, timestamp="")
        # 空字符串被 model_validator 处理，应被替换为有效时间戳
        assert msg.timestamp != ""
        dt = datetime.fromisoformat(msg.timestamp)
        assert dt.tzinfo is not None

    def test_explicit_valid_timestamp_preserved(self):
        """显式设置的有效时间戳应被保留。"""
        from schemas.message import MessageType, UnifiedMessage

        ts = "2026-05-15T10:30:00+08:00"
        msg = UnifiedMessage(type=MessageType.THINKING, timestamp=ts)
        assert msg.timestamp == ts

    def test_many_messages_independent(self):
        """多条消息互不影响（独立创建）。"""
        from schemas.message import MessageType, UnifiedMessage

        messages = []
        for i in range(100):
            msg = UnifiedMessage(
                type=MessageType.EXECUTING,
                content={"index": i},
                metadata={"batch": "test"},
            )
            messages.append(msg)

        for i, msg in enumerate(messages):
            assert msg.content["index"] == i
            assert msg.type == MessageType.EXECUTING

    def test_message_immutability_after_creation(self):
        """消息创建后字段可读（非 frozen 模式，但内容应独立）。"""
        from schemas.message import MessageType, UnifiedMessage

        content = {"text": "original"}
        msg = UnifiedMessage(type=MessageType.THINKING, content=content)
        # 修改原始字典不应影响已创建的消息（Pydantic 会复制）
        content["text"] = "modified"
        assert msg.content["text"] == "original"

    def test_validate_message_dict_with_extra_fields(self):
        """验证消息包含额外字段时仍通过。"""
        from schemas.message import validate_message_dict

        d = {
            "type": "thinking",
            "extra_field": "ignored",
            "another": 123,
        }
        assert validate_message_dict(d) is True

    def test_validate_message_dict_type_is_number(self):
        """type 为数字时验证失败。"""
        from schemas.message import validate_message_dict

        assert validate_message_dict({"type": 123}) is False

    def test_validate_message_dict_type_is_none(self):
        """type 为 None 时验证失败。"""
        from schemas.message import validate_message_dict

        assert validate_message_dict({"type": None}) is False

    def test_progress_boundary_values(self):
        """进度消息边界值（0 和 100）。"""
        from schemas.message import create_progress_message

        msg_0 = create_progress_message(progress=0)
        assert msg_0.content["progress"] == 0

        msg_100 = create_progress_message(progress=100)
        assert msg_100.content["progress"] == 100

    def test_progress_float_values(self):
        """进度消息接受浮点数值。"""
        from schemas.message import create_progress_message

        msg = create_progress_message(progress=33.33)
        assert msg.content["progress"] == 33.33

    def test_failed_message_auto_sets_error_subtype(self):
        """失败消息自动设置 ERROR 子类型。"""
        from schemas.message import MessageSubtype, create_failed_message

        msg = create_failed_message(error="error msg")
        assert msg.subtype == MessageSubtype.ERROR

    def test_progress_message_auto_sets_progress_subtype(self):
        """进度消息自动设置 PROGRESS 子类型。"""
        from schemas.message import MessageSubtype, create_progress_message

        msg = create_progress_message(progress=50)
        assert msg.subtype == MessageSubtype.PROGRESS

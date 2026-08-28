# @feature: FP-0.2.〇 state 字段契约 | @ci: python-coverage
"""state_fields 统一读取契约测试。

背景（2026-08-28 管道 b8b92a56ad72 事故族）：state 结构化字段跨
引擎内存 → 持久层 TEXT → 消费端三个边界，任何一条读写路径漏掉 JSON
还原，消费点 isinstance(dict) 就静默拿空（上游写了值、下游读 None、
零报错痕迹）。本契约 = 还原 + 校验一体，形态错显式报错/留痕。

契约：
1. as_dict 三形态：dict 原样 / JSON 对象字符串还原 / 缺失与非法按语义处理；
2. required=True（强契约位）：缺失、空串、非 dict JSON（如 "[1,2]"）、
   损坏 JSON → StateFieldError（消息含字段名与实际形态）；
3. required=False（可选位）：缺失 → None；非法形态 → None + warning 留痕；
4. 性质：还原幂等（对已还原值再调用结果不变）；round-trip
   （json.dumps(dict) 后 as_dict 等价原 dict）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SHARED_DIR = Path(__file__).resolve().parents[2] / "plugins" / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import state_fields  # noqa: E402
from state_fields import StateFieldError, as_dict, optional_dict, require_dict  # noqa: E402


class TestAsDictThreeShapes:
    """三形态：dict 原样 / JSON 字符串还原 / 缺失与非法。"""

    def test_dict_passthrough_is_identity(self) -> None:
        """dict 原样返回（同一对象，零拷贝）。"""
        meta = {"mode": "plain", "path": "D:/ws"}
        assert as_dict(meta, field="ws_meta") is meta

    def test_json_object_string_is_restored(self) -> None:
        """JSON 对象字符串（DB TEXT 形态）→ 还原为等价 dict。"""
        meta = {"mode": "shared", "path": "D:/ws/t1"}
        raw = json.dumps(meta)
        assert as_dict(raw, field="ws_meta") == meta

    def test_json_string_with_whitespace_prefix_is_restored(self) -> None:
        """带前导空白的 JSON 字符串（部分序列化器行为）→ 仍还原。"""
        assert as_dict('  {"a": 1}', field="f") == {"a": 1}

    def test_none_returns_none_lenient(self) -> None:
        """None（字段未写）→ 宽松语义 None。"""
        assert as_dict(None, field="ws_meta") is None

    def test_empty_string_returns_none_lenient(self) -> None:
        """空串（DB 空值形态）→ 宽松语义 None（不算非法，不告警）。"""
        assert as_dict("", field="ws_meta") is None

    def test_json_array_string_is_not_dict(self) -> None:
        """JSON 数组字符串（"[1,2]"）解析成功但非 dict → 宽松 None。

        （形态漂移防护：值被错误编码为数组时不得当 dict 消费。）"""
        assert as_dict("[1,2]", field="f") is None


class TestRequiredSemantics:
    """required=True：缺失/形态错显式抛错（强契约位）。"""

    def test_missing_raises_with_field_name(self) -> None:
        """None + required → StateFieldError，消息含字段名与实际形态。"""
        with pytest.raises(StateFieldError) as exc_info:
            as_dict(None, field="task.acceptance_criteria", required=True)
        msg = str(exc_info.value)
        assert "task.acceptance_criteria" in msg
        assert "NoneType" in msg

    @pytest.mark.parametrize(
        ("bad_value", "shape"),
        [
            ("", "空串"),
            ("[1,2]", "数组"),
            ('{"broken', "损坏 JSON"),
            (123, "int"),
        ],
        ids=["empty", "array-json", "broken-json", "int"],
    )
    def test_invalid_shapes_raise(self, bad_value: Any, shape: str) -> None:
        """非法形态 → StateFieldError（参数化：4 组区分度输入）。"""
        with pytest.raises(StateFieldError):
            as_dict(bad_value, field="ws_meta", required=True)

    def test_error_message_carries_actual_value_preview(self) -> None:
        """报错消息携带实际值预览（诊断定位：上游写了什么一眼可见）。"""
        with pytest.raises(StateFieldError) as exc_info:
            as_dict('{"mode": "shared", "path": "x"}'[:-5], field="ws_meta", required=True)
        assert "ws_meta" in str(exc_info.value)


class TestConvenienceWrappers:
    """require_dict / optional_dict 便捷封装。"""

    def test_require_dict_returns_dict(self) -> None:
        assert require_dict('{"a": 1}', field="f") == {"a": 1}

    def test_require_dict_raises_on_missing(self) -> None:
        with pytest.raises(StateFieldError):
            require_dict(None, field="f")

    def test_optional_dict_returns_empty_on_missing(self) -> None:
        assert optional_dict(None, field="f") == {}

    def test_optional_dict_returns_empty_on_invalid(self) -> None:
        assert optional_dict("[1,2]", field="f") == {}


class TestRoundTripProperty:
    """性质断言：还原幂等 + 序列化 round-trip 等价。"""

    def test_restore_is_idempotent(self) -> None:
        """对已还原值再调用结果不变（幂等）。"""
        meta = {"mode": "worktree", "nested": {"a": [1, 2]}}
        once = as_dict(meta, field="f")
        assert as_dict(once, field="f") == meta

    def test_round_trip_dict_to_text_to_dict(self) -> None:
        """json.dumps → as_dict 往返等价（DB TEXT 形态的完整生命周期）。"""
        meta = {
            "mode": "shared",
            "path": "D:/ws/parent",
            "project_root": "D:/ws/parent",
            "deep": {"list": [1, "x", {"k": True}], "n": None},
        }
        restored = as_dict(json.dumps(meta), field="f")
        assert restored == meta
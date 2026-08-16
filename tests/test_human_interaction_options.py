# @feature: FP-0.2.五 审批闭环 | @ci: none-local（不在任何 CI 车道：python-coverage 的 BASE_TEST_PATHS 未收集本文件）
"""human_interaction 工具 options 传参形态归一化测试。

背景（2026-08-14 实测）：MiniMax-M3 会把 options 传成对象包裹
（``{"item": ["批准", "拒绝"]}``），也有裸字符串数组。未归一化时前端拿不到
合法选项——审批卡片只剩输入框 + 发送按钮，没有选项按钮（用户报告的缺陷）。

工具入口（tool.py _normalize_options）统一收敛为 ``[{id, label}]``，
后续 record / WS 事件 / selected_option 全链路只见干净形态。
"""

from __future__ import annotations

import os
import sys

import pytest

_HUMAN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "shared", "tools", "human")
_TOOLS_DIR = os.path.dirname(_HUMAN_DIR)
for _d in (_HUMAN_DIR, _TOOLS_DIR):
    if os.path.isdir(_d) and _d not in sys.path:
        sys.path.insert(0, _d)


@pytest.fixture
def normalize():
    from human.tool import HumanInteractionTool  # noqa: PLC0415

    return HumanInteractionTool._normalize_options


class TestNormalizeOptions:
    def test_dict_wrapped_array_unwrapped(self, normalize):
        """MiniMax 实测形态：{"item": ["批准","拒绝"]} → [{id,label}]。"""
        out = normalize({"item": ["批准", "拒绝"]})
        assert out == [
            {"id": "0", "label": "批准"},
            {"id": "1", "label": "拒绝"},
        ]

    def test_string_array(self, normalize):
        out = normalize(["批准", "拒绝"])
        assert [o["label"] for o in out] == ["批准", "拒绝"]
        assert all(o["id"] for o in out)

    def test_object_array_passthrough(self, normalize):
        src = [
            {"id": "1", "label": "批准", "description": "同意变更"},
            {"id": "2", "label": "拒绝"},
        ]
        assert normalize(src) == src

    def test_mixed_elements(self, normalize):
        out = normalize(["批准", {"label": "拒绝"}])
        assert [o["label"] for o in out] == ["批准", "拒绝"]

    def test_dict_other_array_key(self, normalize):
        out = normalize({"list": ["A", "B"]})
        assert [o["label"] for o in out] == ["A", "B"]

    def test_invalid_returns_none(self, normalize):
        assert normalize(None) is None
        assert normalize([]) is None
        assert normalize({"foo": "bar"}) is None
        assert normalize("not-a-list") is None
        assert normalize([{"no_label": 1}]) is None

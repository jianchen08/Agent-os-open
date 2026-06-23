"""TrackPlugin AI 记录 record_id 解析的回归测试。

BUG-FIX-fix_20260623_ai_record_id_broken 的守护测试。

核心契约：AI 记录落库的 record_id 必须始终等于前端 stream_start 下发的
message_id（即 bridge 当前 turn 的 message_id）。一旦断裂，切 Tab / 补漏
拉回 API 消息后会与前端流式占位符共存，表现为"流式气泡下多出一个固定气泡"
（同一逻辑消息渲染两遍）。

修复前的 bug：多轮 iteration / resume 场景下 `_has_prev_ai` 分支把 preset
置空，storage 自动生成新 id，与前端占位符 id 不一致 → 重复渲染。

测试通过 mock registry + bridge 验证 _resolve_ai_record_id 的解析逻辑，
不依赖真实 pipeline / engine。
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plugins.output.track.plugin import TrackPlugin  # noqa: E402

PIPELINE_ID = "pipeline_test_001"


def _registry_with_bridge(message_id: str):
    """构造 mock registry：.get() 返回带 bridge 的 entry。"""
    bridge = SimpleNamespace(message_id=message_id)
    entry = SimpleNamespace(bridge=bridge)
    return SimpleNamespace(get=lambda _pid: entry)


def _registry_no_entry():
    """构造 mock registry：.get() 返回 None（bridge 不可用场景）。"""
    return SimpleNamespace(get=lambda _pid: None)


def test_bridge_available_returns_bridge_message_id():
    """bridge 可用时，record_id 必须取 bridge 当前 turn 的 message_id。"""
    plugin = TrackPlugin()
    registry = _registry_with_bridge(message_id="hex_bridge_cur")
    with patch("pipeline.registry.get_engine_registry", return_value=registry):
        result = plugin._resolve_ai_record_id(PIPELINE_ID, "hex_preset_old")
    assert result == "hex_bridge_cur"


def test_bridge_id_follows_new_turn_over_stale_preset():
    """多轮 turn：第二轮 resume 后 bridge 已刷新为新 id，
    record_id 必须跟随新 bridge id，而非陈旧的 preset。

    这是修复的核心场景（修复前第二轮 record_id 被 _has_prev_ai 置空）。
    """
    plugin = TrackPlugin()

    # 第一轮：bridge id = hex_A，preset 也是 hex_A
    with patch("pipeline.registry.get_engine_registry",
               return_value=_registry_with_bridge("hex_A")):
        id_turn1 = plugin._resolve_ai_record_id(PIPELINE_ID, "hex_A")
    assert id_turn1 == "hex_A"

    # 第二轮 resume：bridge 刷新为 hex_B，preset 仍是 hex_A（state 未及时更新）
    with patch("pipeline.registry.get_engine_registry",
               return_value=_registry_with_bridge("hex_B")):
        id_turn2 = plugin._resolve_ai_record_id(PIPELINE_ID, "hex_A")

    # 关键断言：第二轮 record_id 跟随 bridge 的新 id，而非陈旧的 preset
    assert id_turn2 == "hex_B"


def test_registry_returns_none_falls_back_to_preset():
    """bridge 不可用（registry.get 返回 None）时回退到 preset。"""
    plugin = TrackPlugin()
    with patch("pipeline.registry.get_engine_registry",
               return_value=_registry_no_entry()):
        result = plugin._resolve_ai_record_id(PIPELINE_ID, "hex_preset")
    assert result == "hex_preset"


def test_empty_bridge_message_id_falls_back_to_preset():
    """bridge.message_id 为空时回退到 preset（bridge 异常的兜底）。"""
    plugin = TrackPlugin()
    with patch("pipeline.registry.get_engine_registry",
               return_value=_registry_with_bridge("")):
        result = plugin._resolve_ai_record_id(PIPELINE_ID, "hex_preset")
    assert result == "hex_preset"


def test_registry_exception_falls_back_to_preset():
    """registry 访问抛异常时不中断流程，回退到 preset。"""
    plugin = TrackPlugin()

    def _raise(_pid):
        raise RuntimeError("registry corrupted")

    registry = SimpleNamespace(get=_raise)
    with patch("pipeline.registry.get_engine_registry", return_value=registry):
        result = plugin._resolve_ai_record_id(PIPELINE_ID, "hex_preset")
    assert result == "hex_preset"


def test_empty_preset_and_no_bridge_returns_empty():
    """所有解析路径都失败时返回空串，交由 storage 自动生成（向后兼容）。"""
    plugin = TrackPlugin()
    with patch("pipeline.registry.get_engine_registry",
               return_value=_registry_no_entry()):
        result = plugin._resolve_ai_record_id(PIPELINE_ID, "")
    assert result == ""

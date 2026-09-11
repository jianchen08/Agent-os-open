# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""security_check 审批闸底座缺失可观测性测试（C2）。

锁定契约：
- human-interaction capability 解析异常（KeyError/AttributeError，manifest 未
  声明 / 注入链断）不再无痕返回 None：warn 留痕（含解析来源与异常摘要），
  审批请求落地时软拦截决策显式带 approval_channel_missing 状态标记；
- 权限模式表首启无持久化文件（FileNotFoundError）保持既有正常语义：
  静默空表、不报错不告警。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "security_check")

import plugin as sc_mod  # noqa: E402
from pipeline.plugin import PluginContext  # noqa: E402
from pipeline.types import StateKeys  # noqa: E402
from plugin import SecurityCheckPlugin  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_globals():
    """测试后恢复模块级注入态（plugin 引用 / 审批通道 / 模式表）。"""
    yield
    sc_mod._PLUGIN_REF = None
    sc_mod.set_human_interaction_cap(None)
    sc_mod._PERMISSION_MODES.clear()


class _BrokenPluginRef:
    """get_capability 抛异常的坏 plugin 引用（manifest 未声明/注入链断形态）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_capability(self, name: str) -> Any:
        raise self._exc


class TestCapabilityResolutionWarns:
    @pytest.mark.parametrize("exc", [
        KeyError("human-interaction"),
        AttributeError("plugin ref has no get_capability"),
    ])
    def test_broken_ref_returns_none_with_warn(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, exc: Exception
    ) -> None:
        """解析异常 → 返回 None 且 warn 含解析来源与异常摘要（两组输入）。"""
        monkeypatch.setattr(sc_mod, "_PLUGIN_REF", _BrokenPluginRef(exc))
        with caplog.at_level(logging.WARNING, logger=sc_mod.__name__):
            assert sc_mod._get_human_interaction_cap() is None
        records = [r for r in caplog.records if "human-interaction" in r.getMessage()]
        assert records, "解析失败必须留 warn"
        assert "get_capability" in records[0].getMessage()  # 解析来源可观测
        assert repr(exc) in records[0].getMessage()  # 异常摘要可观测

    def test_healthy_ref_still_silent(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """解析成功不产生告警（不误报）。"""
        monkeypatch.setattr(
            sc_mod, "_PLUGIN_REF", SimpleNamespace(get_capability=lambda name: object())
        )
        with caplog.at_level(logging.WARNING, logger=sc_mod.__name__):
            assert sc_mod._get_human_interaction_cap() is not None
        assert not [r for r in caplog.records if "解析失败" in r.getMessage()]


def _make_plugin() -> SecurityCheckPlugin:
    """构造危险工具判定就绪的插件（mock policy + 轨道 2 数据源）。"""
    mock_policy = MagicMock()
    mock_policy.execution = "host_direct"
    sc_mod._policy_loader.resolve = MagicMock(return_value=mock_policy)  # type: ignore[method-assign]
    p = SecurityCheckPlugin(config={"enabled": True, "rules": []})
    p._dangerous_ops_by_tool = {"bash_execute": ["rm -rf"]}
    return p


def _dangerous_ctx() -> PluginContext:
    return PluginContext(
        state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "bash_execute", "args": {"command": "rm -rf /x"}}],
            "messages": [],
        },
        config={},
    )


class TestApprovalChannelMissingMarker:
    @pytest.mark.asyncio
    async def test_soft_block_decision_carries_missing_marker(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """审批底座缺失 → 软拦截 + security.decision 显式带 approval_channel_missing。"""
        monkeypatch.setattr(sc_mod, "_PLUGIN_REF", _BrokenPluginRef(KeyError("human-interaction")))
        p = _make_plugin()

        with caplog.at_level(logging.WARNING, logger=sc_mod.__name__):
            result = await p.execute(_dangerous_ctx())

        decision = result.state_updates["security.decision"]
        assert decision["approval_channel_missing"] is True
        assert "soft_block" in decision["reason"]
        # 软拦截契约保持：工具调用清空，拒绝结果回传 LLM
        assert result.state_updates[StateKeys.RAW_TOOL_CALLS] == []
        assert any("解析失败" in r.getMessage() for r in caplog.records)


class TestPermissionModesFirstBootUnchanged:
    def test_missing_file_stays_silent_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """首启无持久化文件（FileNotFoundError）：行为不变——静默空表、无告警。"""
        monkeypatch.setattr(
            sc_mod, "_PERMISSION_MODES_FILE", str(tmp_path / "nonexistent" / "permission_modes.json")
        )
        with caplog.at_level(logging.WARNING, logger=sc_mod.__name__):
            sc_mod._load_permission_modes()
        assert sc_mod._PERMISSION_MODES == {}
        assert not [r for r in caplog.records if "权限模式表" in r.getMessage()]

# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""prompt_build 工具面缩水通告注入测试（卡4①：真缺席显式告知）。

契约：tool_schema 每轮把「声明但真正不在工具面」的工具写入
state["tool_surface.unavailable"]；_build_dynamic_vars 读到非空表时在
dynamic_vars 消息追加模型可读通告行——真缺失不再无声，模型无需盲猜
「上轮还能用的 file 工具这轮没了」（2026-10-17 装机事故定因之一）。
零配置 dynamic_vars 的 agent 也必须收到通告（早退 return None 在通告
判定之后，不能吞掉故障告知）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_THIS_DIR = Path(__file__).resolve().parent
_SHARED_DIR = Path(__file__).resolve().parents[1]  # plugins/shared/
for _d in (str(_SHARED_DIR), str(_THIS_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "prompt_build_plugin_surface_notice", str(_THIS_DIR / "plugin.py")
)
assert _spec is not None and _spec.loader is not None  # noqa: PT018 （装载守卫对，拆开无新信息）
_mod = importlib.util.module_from_spec(_spec)
sys.modules["prompt_build_plugin_surface_notice"] = _mod
_spec.loader.exec_module(_mod)
PromptBuildPlugin: Any = _mod.PromptBuildPlugin


def _plugin(config: dict[str, Any] | None = None) -> Any:
    return PromptBuildPlugin(config=config or {})


def _ctx(state: dict[str, Any] | None = None) -> Any:
    return PluginContext(state=dict(state or {}), _services={})


def _dynamic_vars_content(state: dict[str, Any], config: dict[str, Any] | None = None) -> str | None:
    """驱动公开面 _build_dynamic_vars，返回消息 content（无动态变量时 None）。"""
    p = _plugin(config)
    msg = asyncio.run(p._build_dynamic_vars(_ctx(state)))  # noqa: SLF001
    if msg is None:
        return None
    assert msg["role"] == "user"
    content = msg["content"]
    assert isinstance(content, str)
    return content


class TestToolSurfaceNoticeInjection:
    def test_unavailable_tools_appended_to_configured_vars(self) -> None:
        """有配置动态变量且工具缩水 → 通告行并入，含「不在本轮工具面」与工具名。"""
        content = _dynamic_vars_content(
            state={
                "context.dynamic_vars": [{"type": "timestamp", "name": "当前时间"}],
                "tool_surface.unavailable": ["file_read", "human_interaction"],
            },
        )
        assert content is not None
        assert "不在本轮工具面" in content
        assert "file_read" in content
        assert "human_interaction" in content
        # 通告是工具面事实，禁止替模型做「已恢复」类编造引导
        assert "已恢复" not in content

    def test_unavailable_notice_survives_zero_configured_vars(self) -> None:
        """零配置动态变量 + 工具缩水 → 仅含通告的消息仍产出（不 return None 吞告知）。"""
        content = _dynamic_vars_content(
            state={"tool_surface.unavailable": ["file_read"]},
        )
        assert content is not None
        assert "不在本轮工具面" in content
        assert "file_read" in content

    def test_unavailable_notice_matches_adr_wording(self) -> None:
        """文案与 ADR 2026-11-12 一致：真缺席才提示「不在工具面」+ 原因层。"""
        content = _dynamic_vars_content(
            state={"tool_surface.unavailable": ["file_read"]},
        )
        assert content is not None
        assert "未注册/插件未启用/提供方不可用" in content
        assert "不要规划或调用" in content
        # G2 失败桩不在本通告面：不得出现调用期失败文案，
        # 避免模型把「可调用但会显式失败」误读成「不可调用」。
        assert "声明与实现不一致" not in content

    def test_no_unavailable_key_no_notice(self) -> None:
        """state 无该键（老内核/未触发缩水）→ 行为零变化，无通告字样。"""
        content = _dynamic_vars_content(
            state={"context.dynamic_vars": [{"type": "timestamp", "name": "当前时间"}]},
        )
        assert content is not None
        assert "不在本轮工具面" not in content

    def test_empty_unavailable_list_no_notice(self) -> None:
        """空表（工具面完好）→ 不注入通告（性质：通告只由缺席触发）。"""
        content = _dynamic_vars_content(
            state={
                "context.dynamic_vars": [{"type": "timestamp", "name": "当前时间"}],
                "tool_surface.unavailable": [],
            },
        )
        assert content is not None
        assert "不在本轮工具面" not in content

    def test_zero_vars_and_no_shrink_returns_none(self) -> None:
        """零配置变量且无缩水 → 维持零兜底契约（返回 None 不注入）。"""
        assert _dynamic_vars_content(state={}) is None

    def test_non_list_unavailable_value_ignored(self) -> None:
        """键值非列表（脏数据）→ 忽略不炸：动态变量构建不许被坏键阻断。"""
        content = _dynamic_vars_content(
            state={
                "context.dynamic_vars": [{"type": "timestamp", "name": "当前时间"}],
                "tool_surface.unavailable": "file_read",
            },
        )
        assert content is not None
        assert "不在本轮工具面" not in content

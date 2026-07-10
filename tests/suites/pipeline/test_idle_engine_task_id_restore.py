"""验证 _start_idle_engine 在调用方未传 task_id 时从注册表 tags 恢复。

回归 fix_20260619_idle_lost_task_id：
前端「停止→再发送」走 WS 路径，调用方不知 task_id，idle 重启重建 state
会导致 state[TASK_ID]=''，L2 task_submit 报 L2_REQUIRES_PARENT_TASK。
修复：_start_idle_engine 与 agent 身份恢复同源，从 registry tags 补全 task_id。

测试用 mock 引擎（避免真实 run 执行），捕获 engine.run 的 task_id kwarg。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.message_bus import _start_idle_engine


def _make_mock_engine() -> MagicMock:
    """构建 mock 引擎：_agent_config 已绑定（跳过 agent 解析失败分支）。"""
    engine = MagicMock()
    engine._agent_config = MagicMock()  # 引擎自带身份，避免走到 agent 解析失败
    # run 返回一个简单 state，避免 _run_loop 真实执行
    engine.run = AsyncMock(return_value={})
    return engine


def _make_mock_entry(task_id: str = "", workspace: str = "") -> SimpleNamespace:
    """构建 mock 注册表条目，含 tags。"""
    tags: dict[str, str] = {"agent_id": "test_agent"}
    if task_id:
        tags["task_id"] = task_id
    if workspace:
        tags["workspace"] = workspace
    return SimpleNamespace(tags=tags, thread_id="t1")


@pytest.mark.asyncio
async def test_restores_task_id_from_tags_when_caller_omits():
    """调用方未传 task_id → 从 tags 恢复并传给 engine.run。"""
    engine = _make_mock_engine()
    _entry = _make_mock_entry(task_id="task_from_tags")

    with patch("pipeline.registry.get_engine_registry") as mock_get_reg, \
         patch("pipeline.drain_manager.create_sink", return_value=MagicMock()):
        _reg = MagicMock()
        _reg.get.return_value = _entry
        _reg.ensure_bridge.return_value = MagicMock()
        mock_get_reg.return_value = _reg

        await _start_idle_engine(
            pipeline_id="pid_test",
            engine=engine,
            message="hello",
            agent_config=engine._agent_config,
            # 关键：不传 task_id，模拟 WS 路径
        )

    # engine.run 的 task_id kwarg 应等于 tags 里的值
    _kwargs = engine.run.call_args.kwargs
    assert _kwargs.get("task_id") == "task_from_tags", (
        f"task_id 应从 tags 恢复为 'task_from_tags'，实际: {_kwargs.get('task_id')!r}"
    )


@pytest.mark.asyncio
async def test_caller_task_id_not_overridden_by_tags():
    """调用方显式传入有效 task_id → 不被 tags 覆盖。"""
    engine = _make_mock_engine()
    _entry = _make_mock_entry(task_id="task_in_tags")

    with patch("pipeline.registry.get_engine_registry") as mock_get_reg, \
         patch("pipeline.drain_manager.create_sink", return_value=MagicMock()):
        _reg = MagicMock()
        _reg.get.return_value = _entry
        _reg.ensure_bridge.return_value = MagicMock()
        mock_get_reg.return_value = _reg

        await _start_idle_engine(
            pipeline_id="pid_test",
            engine=engine,
            message="hello",
            agent_config=engine._agent_config,
            task_id="caller_explicit_id",  # 调用方显式传值
        )

    _kwargs = engine.run.call_args.kwargs
    assert _kwargs.get("task_id") == "caller_explicit_id", (
        "调用方显式传入的 task_id 不应被 tags 覆盖"
    )


@pytest.mark.asyncio
async def test_empty_caller_task_id_overridden_by_tags():
    """调用方传空 task_id（WS 路径默认 ''）→ 仍用 tags 恢复。

    回归核心场景：send_pipeline_message 默认 task_id=''，
    修复前 state[TASK_ID]='' 导致 L2 报错。
    """
    engine = _make_mock_engine()
    _entry = _make_mock_entry(task_id="real_task_123")

    with patch("pipeline.registry.get_engine_registry") as mock_get_reg, \
         patch("pipeline.drain_manager.create_sink", return_value=MagicMock()):
        _reg = MagicMock()
        _reg.get.return_value = _entry
        _reg.ensure_bridge.return_value = MagicMock()
        mock_get_reg.return_value = _reg

        await _start_idle_engine(
            pipeline_id="pid_test",
            engine=engine,
            message="hello",
            agent_config=engine._agent_config,
            task_id="",  # WS 路径默认空值
        )

    _kwargs = engine.run.call_args.kwargs
    assert _kwargs.get("task_id") == "real_task_123", (
        "空 task_id 应被 tags 恢复，这是停止→再发送场景的核心修复"
    )


@pytest.mark.asyncio
async def test_no_task_id_anywhere_stays_empty():
    """tags 也没有 task_id（纯会话管道）→ task_id 保持空，行为不变。"""
    engine = _make_mock_engine()
    _entry = _make_mock_entry(task_id="")  # 会话管道，tags 无 task_id

    with patch("pipeline.registry.get_engine_registry") as mock_get_reg, \
         patch("pipeline.drain_manager.create_sink", return_value=MagicMock()):
        _reg = MagicMock()
        _reg.get.return_value = _entry
        _reg.ensure_bridge.return_value = MagicMock()
        mock_get_reg.return_value = _reg

        await _start_idle_engine(
            pipeline_id="pid_test",
            engine=engine,
            message="hello",
            agent_config=engine._agent_config,
        )

    _kwargs = engine.run.call_args.kwargs
    # 会话管道本就无 task_id，保持空是正确行为（不报错）
    assert _kwargs.get("task_id") == ""

"""切 Agent 不应波及子任务管道的 agent_id 回归测试。

BUG-FIX-switch_agent_clobbers_subpipeline:
问题根因: 会话切 Agent 时（update_thread_agent 路由）调用
  _sync_agent_to_registry_tags，后者按 session_id == thread_id 批量匹配注册表
  entry 并【无差别覆盖】tags["agent_id"]。子任务管道注册时 session_id 继承自
  父会话 thread_id（task_executor.py:128-129），所以会命中匹配，它本应是该子
  任务的 target_id（task_executor.py:340）被覆盖成主 agent。
  后果：子任务被 stop_generation 停止后 idle 重启，引擎从被改写的 tags 解析出
  主 agent（message_bus._start_idle_engine:279），表现为"停止后再发消息 agent
  变成主对话的"。

正确语义: 注册表以 pipeline_id 为准。切 Agent 只精确更新【会话主管道】那一个
  entry（按 active_pipeline_id 匹配），同会话下的子任务管道（pipeline_id ≠
  主管道 id）匹配不上，绝不波及。

本测试复刻 update_thread_agent 中切 Agent 的精确更新逻辑，锁定：
  - 主管道 entry 的 agent_id 更新为新主 agent
  - 子任务管道 entry 的 agent_id 保持原 target 不变
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.engine_registry import get_engine_registry


@pytest.fixture(autouse=True)
def clean_registry():
    """每个测试前后清空全局 EngineRegistry。"""
    reg = get_engine_registry()
    reg._engines.clear()
    yield
    reg._engines.clear()


def _apply_switch_agent(thread: dict, agent_id: str) -> None:
    """复刻 routes_threads.update_thread_agent 中切 Agent 的精确更新逻辑。

    只按会话主管道的 active_pipeline_id 精确改一个 entry，与子任务管道隔离。
    """
    _main_pid = thread.get("active_pipeline_id") or ""
    if agent_id and _main_pid:
        _entry = get_engine_registry().get(_main_pid)
        if _entry and _entry.tags.get("agent_id") != agent_id:
            _entry.tags["agent_id"] = agent_id


class TestSwitchAgentDoesNotClobberSubpipeline:
    """切主 Agent 只改主管道 entry，不波及子任务管道。"""

    def test_main_updated_subpipeline_preserved(self) -> None:
        """切 Agent 后主管道 agent_id 更新，子任务管道 agent_id 保持原 target。"""
        reg = get_engine_registry()

        # 主管道：无 task_id / 无 parent_pipeline（与 routes_threads._register_session_pipeline 一致）
        reg.register(
            "main-pid-1",
            MagicMock(),
            thread_id="thread-1",
            tags={
                "mode": "interactive",
                "channel": "ws",
                "session_id": "thread-1",
                "agent_id": "lingxi",
                "user_id": "u1",
            },
        )
        # 子任务管道：有 task_id / parent_pipeline，agent_id = 子任务 target（与 task_executor 注册一致）
        reg.register(
            "sub-pid-1",
            MagicMock(),
            thread_id="thread-1",
            tags={
                "mode": "interactive",
                "task_id": "task-xyz",
                "parent_pipeline": "main-pid-1",
                "session_id": "thread-1",  # 继承自父会话 → 旧逻辑会误命中
                "agent_id": "programming_orchestrator",
                "user_id": "u1",
            },
        )

        thread = {"active_pipeline_id": "main-pid-1"}
        _apply_switch_agent(thread, "novel_orchestrator")

        assert reg.get("main-pid-1").tags["agent_id"] == "novel_orchestrator", (
            "主管道 agent_id 应更新为新主 agent"
        )
        assert reg.get("sub-pid-1").tags["agent_id"] == "programming_orchestrator", (
            "子任务管道 agent_id 必须保持原 target，切主 agent 绝不波及"
        )

    def test_no_main_pipeline_id_is_noop(self) -> None:
        """会话无 active_pipeline_id（主管道指针缺失）时不改任何 entry。"""
        reg = get_engine_registry()
        reg.register(
            "main-pid-2",
            MagicMock(),
            thread_id="thread-2",
            tags={"session_id": "thread-2", "agent_id": "lingxi"},
        )

        _apply_switch_agent({"active_pipeline_id": ""}, "novel_orchestrator")

        assert reg.get("main-pid-2").tags["agent_id"] == "lingxi", (
            "无主管道指针时不应改动任何 entry（重启时由 restore_session_pipelines 重建）"
        )

    def test_main_entry_absent_is_noop(self) -> None:
        """主管道 entry 不存在于注册表时静默跳过（不报错、不误伤其他 entry）。"""
        reg = get_engine_registry()
        reg.register(
            "sub-pid-3",
            MagicMock(),
            thread_id="thread-3",
            tags={"session_id": "thread-3", "agent_id": "sub_target"},
        )

        # active_pipeline_id 指向一个未注册的 id
        _apply_switch_agent({"active_pipeline_id": "ghost-main"}, "novel_orchestrator")

        assert reg.get("sub-pid-3").tags["agent_id"] == "sub_target", (
            "主管道 entry 缺失时不得误伤同会话其他管道"
        )
        assert reg.get("ghost-main") is None

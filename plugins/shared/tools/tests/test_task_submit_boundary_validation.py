# @feature: FP-0.2.〇 任务执行驱动 | @ci: python-coverage
"""task_submit 入参校验测试（2026-08-24 工具能力测试 → 参数瘦身两阶段）。

阶段 1（边界闸门）：input_schema 声明不在运行时强制（tool_core 只
fail-closed 校验 output_schema），越界值原样落 state——goal_description
必填闸门（MISSING_DESCRIPTION）本阶段落地并保留（描述有真实消费者：
kickoff 消息/task.goal state/面板展示）。

阶段 2（参数退役，同日）：priority/max_retries 经消费链审计确认执行层
零消费者（无调度队列读优先级、三套真实重试机制均读各自插件配置）——
两参数连 schema 带写路径整体删除，本文件的数值校验随之退役
（ADR 2026-08-24-task-submit-param-diet）。

现覆盖：
1. 退役守卫：schema 不再声明两参数；显式传入按未知参数忽略（不落
   普通派发 state、不落容器登记 state）
2. goal_description 缺失/空/纯空白 → MISSING_DESCRIPTION
3. 缺 goal_title 但有 description → MISSING_GOAL（既有行为回归护栏）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TS_DIR = Path(__file__).resolve().parent.parent / "task_submit"
_SYSTEM_ROOT = Path(__file__).resolve().parents[2] / "system"

for _d in [_SYSTEM_ROOT, _SYSTEM_ROOT / "tasks", _SYSTEM_ROOT / "channel_api"]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    """加载 task_submit/tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "task_submit_tool_boundary_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TS_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]
        raise
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


class _FakeSender:
    """记录 chat.send_message 参数的派发器 fake。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, params: dict) -> dict:
        self.calls.append(params)
        if params.get("no_dispatch"):
            return {"status": "recorded", "pipeline_id": params.get("pipeline_id", "")}
        return {"status": "created", "pipeline_id": "a1b2c3d4e5f64789abcdef0123456789"}


def _base_inputs(**over: Any) -> dict:
    base = {
        "goal_title": "喝水提醒",
        "goal_description": "每小时提醒喝水",
        "target_type": "agent",
        "target_id": "main",
        "parent_agent_level": 1,
        "pipeline_id": "pipe_parent_9",
        "user_id": "user-1",
    }
    base.update(over)
    return base


def _make_tool(mod: Any) -> Any:
    tool = mod.TaskSubmitTool()

    async def _ok(t, l):
        return (True, "", "")

    tool._validate_target_agent = _ok  # type: ignore[method-assign]
    return tool


async def _run(mod: Any, inputs: dict) -> Any:
    """执行一次提交并复位全局 sender（不泄漏到后续测试）。"""
    sender = _FakeSender()
    mod.set_chat_sender(sender)
    tool = _make_tool(mod)
    try:
        return await tool.execute(inputs), sender
    finally:
        mod._chat_sender = None


# ── 参数退役守卫（2026-08-24 阶段 2）─────────────────────────


class TestRetiredParams:
    async def test_schema_no_longer_declares_params(self, mod: Any) -> None:
        """schema 不再声明 priority/max_retries（LLM 工具面不可见）。"""
        definition = mod.TaskSubmitTool.get_tool_definition()
        props = definition.input_schema["properties"]
        assert "priority" not in props
        assert "max_retries" not in props

    async def test_passed_params_dropped_not_written(self, mod: Any) -> None:
        """显式传入（含越界值）按未知参数忽略：提交成功但不落派发 state——
        退役参数既不报错也不产生数据。"""
        for over in ({"priority": 8}, {"priority": 999}, {"max_retries": -1}, {"max_retries": 3}):
            r, sender = await _run(mod, _base_inputs(**over))
            assert r.success, f"{over} 应按未知参数忽略并正常派发: {r.error}"
            state = sender.calls[0]["state"]
            assert "task.priority" not in state, over
            assert "task.max_retries" not in state, over

    async def test_container_registration_drops_params(self, mod: Any) -> None:
        """容器登记分支同语义：传入不落 task.owned.<id>.* state。"""
        r, sender = await _run(
            mod, _base_inputs(task_scope="container", parent_agent_level=1, priority=8, max_retries=2)
        )
        assert r.success, r.error
        reg = sender.calls[0]
        assert not any(k.endswith(".priority") or k.endswith(".max_retries") for k in reg["state"])


# ── goal 内容校验（阶段 1 落地，保留）─────────────────────────


class TestGoalContentValidation:
    async def test_missing_or_empty_description_rejected(self, mod: Any) -> None:
        """缺失/空串/纯空白描述 → MISSING_DESCRIPTION（下级 Agent 无目标上下文）。"""
        for over in (
            {"goal_description": ""},
            {"goal_description": "   \n  "},
            {"goal_description": None},
        ):
            inputs = _base_inputs(**over)
            r, _ = await _run(mod, inputs)
            assert not r.success, f"{over} 应被拒绝"
            assert r.error_code == "MISSING_DESCRIPTION", r.error

        # 完全不带 goal_description 键
        inputs = _base_inputs()
        del inputs["goal_description"]
        r, _ = await _run(mod, inputs)
        assert not r.success
        assert r.error_code == "MISSING_DESCRIPTION", r.error

    async def test_goal_object_empty_description_rejected(self, mod: Any) -> None:
        """旧式 goal 对象格式同规则：description 空 → 拒绝。"""
        inputs = _base_inputs()
        del inputs["goal_title"]
        del inputs["goal_description"]
        inputs["goal"] = {"title": "喝水提醒", "description": ""}
        r, _ = await _run(mod, inputs)
        assert not r.success
        assert r.error_code == "MISSING_DESCRIPTION", r.error

    async def test_title_missing_with_description_rejected(self, mod: Any) -> None:
        """缺 goal_title 但有 description → MISSING_GOAL（既有行为回归护栏，
        2026-08-24 工具能力测试项：该输入在工具层本就拦截）。"""
        inputs = _base_inputs()
        del inputs["goal_title"]
        r, _ = await _run(mod, inputs)
        assert not r.success
        assert r.error_code == "MISSING_GOAL", r.error

    async def test_valid_submission_still_dispatches(self, mod: Any) -> None:
        """正常入参（标题+描述）不受影响——完整链路回归。"""
        r, sender = await _run(mod, _base_inputs())
        assert r.success, r.error
        assert len(sender.calls) == 2  # 创建执行管道 + no_dispatch 登记
        assert sender.calls[0]["state"]["task.goal"] == "喝水提醒"

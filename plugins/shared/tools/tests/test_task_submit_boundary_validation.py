# @feature: FP-0.2.〇 任务执行驱动 | @ci: python-coverage
"""task_submit 入参边界校验测试（2026-08-24 工具能力测试暴露的漏洞）。

背景：plugin.json / get_tool_definition 的 input_schema 声明了
priority(1-10)/max_retries(min=0)，但 input_schema 声明不在运行时强制
（tool_core 只 fail-closed 校验 output_schema），LLM 传越界值原样落
state——下游 TaskPriority 枚举、前端展示全部裸奔。

覆盖（普通 + 容器两条路径共用同一闸门）：
1. priority 越界（0/11/-5）与非整数（"8"/5.5/True）→ INVALID_PRIORITY
2. max_retries 越界（-1/999）与非整数 → INVALID_MAX_RETRIES；
   0 = 显式无重试，合法放行
3. goal_description 缺失/空/纯空白 → MISSING_DESCRIPTION
   （派发给下级 Agent 的任务只有标题没有描述 = 下级无目标上下文）
4. 缺 goal_title 但有 description → MISSING_GOAL（既有行为回归护栏）
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


# ── priority 边界 ──────────────────────────────────────────────


class TestPriorityBounds:
    async def test_out_of_range_rejected(self, mod: Any) -> None:
        """越界（0/11/-5）拒绝——用户测试实测三项全部派发成功。"""
        for bad in (0, 11, -5):
            r, _ = await _run(mod, _base_inputs(priority=bad))
            assert not r.success, f"priority={bad} 应被拒绝"
            assert r.error_code == "INVALID_PRIORITY", r.error
            assert "1-10" in r.error

    async def test_non_integer_rejected(self, mod: Any) -> None:
        """非整数（"8"/5.5/True）拒绝——bool 是 int 子类须显式排除。"""
        for bad in ("8", 5.5, True):
            r, _ = await _run(mod, _base_inputs(priority=bad))
            assert not r.success, f"priority={bad!r} 应被拒绝"
            assert r.error_code == "INVALID_PRIORITY", r.error

    async def test_boundary_values_accepted(self, mod: Any) -> None:
        """边界值 1/10 合法，显式传入落 state（对账语义不变）。"""
        for good in (1, 10):
            r, sender = await _run(mod, _base_inputs(priority=good))
            assert r.success, r.error
            assert sender.calls[0]["state"]["task.priority"] == good


# ── max_retries 边界 ───────────────────────────────────────────


class TestMaxRetriesBounds:
    async def test_out_of_range_rejected(self, mod: Any) -> None:
        """越界（-1/999/11）拒绝——无上限会放大失败任务的重试风暴。"""
        for bad in (-1, 999, 11):
            r, _ = await _run(mod, _base_inputs(max_retries=bad))
            assert not r.success, f"max_retries={bad} 应被拒绝"
            assert r.error_code == "INVALID_MAX_RETRIES", r.error
            assert "0-10" in r.error

    async def test_non_integer_rejected(self, mod: Any) -> None:
        for bad in ("3", 1.5, False):
            r, _ = await _run(mod, _base_inputs(max_retries=bad))
            assert not r.success, f"max_retries={bad!r} 应被拒绝"
            assert r.error_code == "INVALID_MAX_RETRIES", r.error

    async def test_zero_means_no_retry_allowed(self, mod: Any) -> None:
        """0 = 显式无重试，合法（schema min=0 语义），落 state 对账。"""
        r, sender = await _run(mod, _base_inputs(max_retries=0))
        assert r.success, r.error
        assert sender.calls[0]["state"]["task.max_retries"] == 0

    async def test_upper_boundary_accepted(self, mod: Any) -> None:
        r, sender = await _run(mod, _base_inputs(max_retries=10))
        assert r.success, r.error
        assert sender.calls[0]["state"]["task.max_retries"] == 10


# ── 容器路径同一闸门 ───────────────────────────────────────────


class TestContainerPathBounds:
    async def test_container_rejects_bad_priority_and_retries(self, mod: Any) -> None:
        """容器登记分支同受边界闸门（校验在分支前的公共段）。"""
        for over in ({"priority": 11}, {"max_retries": -1}):
            r, _ = await _run(
                mod, _base_inputs(task_scope="container", parent_agent_level=1, **over)
            )
            assert not r.success, f"容器任务 {over} 应被拒绝"
            assert r.error_code in ("INVALID_PRIORITY", "INVALID_MAX_RETRIES"), r.error

    async def test_container_accepts_valid_values(self, mod: Any) -> None:
        """容器任务合法值照常登记（不因新闸门误伤正常流）。"""
        r, sender = await _run(
            mod,
            _base_inputs(
                task_scope="container", parent_agent_level=1, priority=8, max_retries=2
            ),
        )
        assert r.success, r.error
        reg = sender.calls[0]
        assert any(k.endswith(".priority") and v == 8 for k, v in reg["state"].items())
        assert any(k.endswith(".max_retries") and v == 2 for k, v in reg["state"].items())


# ── goal 内容校验 ──────────────────────────────────────────────


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
        """正常入参（标题+描述+合法数值）不受影响——完整链路回归。"""
        r, sender = await _run(mod, _base_inputs(priority=5, max_retries=3))
        assert r.success, r.error
        assert len(sender.calls) == 2  # 创建执行管道 + no_dispatch 登记
        assert sender.calls[0]["state"]["task.priority"] == 5
        assert sender.calls[0]["state"]["task.max_retries"] == 3

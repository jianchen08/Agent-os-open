# @feature: FP-0.2.〇 任务出生协议四段式 | @vision: V2 全能闭环 | @ci: python-coverage
"""task_submit 提交期工作空间准备的行为测试（ADR-2026-10-17-task-birth-workspace-inclusion）。

出生协议由三段扩为四段后，提交返回的含义从「已受理」变为「已受理且准备完成」。
本文件在**提交接口面**断言该契约（协议内部顺序与失败语义由
tests/plugins/shared/test_task_birth.py 覆盖）：

1. 准备成功：坐标先落管道 state 再派发；出口 ``status`` 回显任务投影真实状态
   （出生态 pending），不再是无来源的字面量 "running"；
2. 准备失败：提交失败 + error_code=WORKSPACE_PREPARE_FAILED + 话术说明任务记录
   已保留并标 failed，且 chat 面确有 task.status=failed / task.error 随写；
3. 准备器缺席 / 返回空坐标：同判失败（无「跳过准备」路径、无幻影路径裸奔）。

替身边界：只替外部依赖（chat 派发器、workspace_lifecycle 准备服务、agent 闸门
读面），不替内部实现，不断言调用次数以外的实现细节。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

from tests import _task_birth_workspace as _birth_ws  # noqa: E402

_TS_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "task_submit"
_SHARED_ROOT = Path(__file__).resolve().parents[4] / "plugins" / "shared"

for _d in (_SHARED_ROOT, _SHARED_ROOT / "tasks"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    mod_name = "task_submit_tool_workspace_birth_test"
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
    """chat.send_message 通道 fake：记录全部出生协议写面调用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, params: dict) -> dict:
        self.calls.append(params)
        if params.get("create"):
            return {"status": "created", "pipeline_id": "pipe_engine_gen_1"}
        return {"status": "recorded", "pipeline_id": params.get("pipeline_id", "")}


def _make_tool(mod: Any) -> Any:
    """构造工具实例，stub 与工作空间无关的闸门读面。

    agent 存在性/归属/初始输入完备性三道闸门读的是 agent 配置与 pipeline 定义
    （随仓内编排演进，与本文件被测面无关）——按既有 test_task_submit_dispatch.py
    惯例在同层替身，本文件只断出生协议第四段的外部可观察行为。
    """
    tool = mod.TaskSubmitTool()

    async def _ok(t: Any, l: Any) -> tuple[bool, str, str]:
        return (True, "", "")

    async def _base_config(target_id: str) -> tuple[dict[str, Any] | None, None]:
        return (
            {
                "level": "L2",
                "is_active": True,
                "tool_ids": ["file_read"],
                "system_prompt": "stub-persona",
            },
            None,
        )

    async def _allow(level: Any, pid: Any) -> tuple[bool, None]:
        return (True, None)

    async def _orchestration(inputs: dict, target_id: Any) -> tuple[str, None]:
        return "autonomous", None

    tool._validate_target_agent = _ok  # type: ignore[method-assign]
    tool._get_agent_base_config = _base_config  # type: ignore[method-assign]
    tool._check_parent_ownership = _allow  # type: ignore[method-assign]
    tool._gate_orchestration = _orchestration  # type: ignore[method-assign]
    return tool


def _inputs(**over: Any) -> dict:
    base = {
        "goal": {"title": "喝水提醒", "description": "每小时提醒喝水"},
        "target_type": "agent",
        "target_id": "main",
        "parent_agent_level": 1,
        "pipeline_id": "pipe_parent_9",
        "user_id": "user-1",
        "session_id": "sess-1",
    }
    base.update(over)
    return base


@pytest.fixture
def wired(mod: Any):
    """接线 chat sender + 默认成功的准备器，并在用例后复位模块级注入点。"""
    sender = _FakeSender()
    mod.set_chat_sender(sender)
    _birth_ws.inject_preparer(mod)
    yield sender
    mod._chat_sender = None
    mod._workspace_preparer = None


class TestPrepareSuccess:
    async def test_workspace_coords_land_before_dispatch(self, mod: Any, wired: Any) -> None:
        """准备成功：坐标先落管道 state（no_dispatch），派发在其后。"""
        result = await _make_tool(mod).execute(_inputs())
        assert result.success, result.error

        ws_writes = [
            c for c in wired.calls
            if c.get("no_dispatch") and "workspace" in (c.get("state") or {})
        ]
        assert ws_writes, "工作空间坐标必须在派发前落进管道 state"
        dispatch_idx = next(
            i for i, c in enumerate(wired.calls) if c.get("background") is True
        )
        ws_idx = wired.calls.index(ws_writes[0])
        assert ws_idx < dispatch_idx, "工作空间未就绪即派发 = 旧事故形态"

    async def test_status_reflects_real_task_projection(self, mod: Any, wired: Any) -> None:
        """出口 status = 任务投影真实状态（出生态 pending），非字面量 running。"""
        result = await _make_tool(mod).execute(_inputs())
        assert result.success, result.error
        assert result.output["status"] == "pending"
        # 性质：与出生 state 写入的 task.status 同源，不是巧合常量
        birth_state = wired.calls[0]["state"]
        assert result.output["status"] == birth_state["task.status"]

    async def test_schema_status_no_longer_advertises_running(self) -> None:
        """output_schema 的 status 描述不得再宣称 running/submitted/active
        （出口已改为回显真实投影状态）。契约声明面在 plugin.json。"""
        import json

        manifest = json.loads(
            (_TS_DIR / "plugin.json").read_text(encoding="utf-8")
        )
        props = manifest["capabilities"]["tools"][0]["output_schema"]["properties"]
        assert "running" not in props["status"]["description"]


class TestPrepareFailure:
    async def test_prepare_error_keeps_failed_task_record(self, mod: Any, wired: Any) -> None:
        """准备失败：提交失败 + 任务记录保留并标 failed（清理锚点）。"""
        _birth_ws.inject_preparer(mod, _birth_ws.make_preparer(
            error=RuntimeError("worktree 分化未落地")
        ))
        result = await _make_tool(mod).execute(_inputs())
        assert not result.success
        assert result.error_code == "WORKSPACE_PREPARE_FAILED"
        assert "任务记录已保留" in result.error

        marker = [
            c for c in wired.calls
            if (c.get("state") or {}).get("task.status") == "failed"
        ]
        assert marker, "失败必须随写进任务记录（否则面板悬一条永不推进的 pending）"
        assert "worktree 分化未落地" in marker[0]["state"]["task.error"]
        assert all(c.get("background") is not True for c in wired.calls), "失败后不得派发"

    @pytest.mark.parametrize(
        ("result_payload", "label"),
        [
            ({}, "空坐标"),
            ({"workspace": ""}, "空串"),
            ({"task.ws_meta": {"mode": "plain"}}, "缺 workspace"),
        ],
    )
    async def test_unready_result_fails_submission(
        self, mod: Any, wired: Any, result_payload: dict, label: str
    ) -> None:
        """未交出真实坐标 = 未就绪 = 提交失败（与成功路径构成有区分度两组）。"""
        _birth_ws.inject_preparer(mod, _birth_ws.make_preparer(result=result_payload))
        result = await _make_tool(mod).execute(_inputs())
        assert not result.success, f"{label}：不得以 success 掩盖"
        assert result.error_code == "WORKSPACE_PREPARE_FAILED"

    async def test_preparer_absent_fails_submission(self, mod: Any, wired: Any) -> None:
        """准备器缺席（workspace_lifecycle 服务未注入）= 出生第三段无执行者
        → 一律失败，不得静默跳过准备回到旧行为。"""
        mod._workspace_preparer = None
        result = await _make_tool(mod).execute(_inputs())
        assert not result.success
        assert result.error_code == "WORKSPACE_PREPARE_FAILED"
        assert all(c.get("background") is not True for c in wired.calls)


class TestPrepareIdempotence:
    async def test_repeated_submit_each_gets_own_workspace(self, mod: Any, wired: Any) -> None:
        """性质：同一准备器被多次调用互不串味（每次提交各自拿自己的坐标），
        幂等键在任务 id 上而非准备器实例上。"""
        seen: list[str] = []

        async def _preparer(*, pipeline_id: str, state: dict, execution_context: dict) -> dict:
            ws = tempfile.mkdtemp(prefix=f"ws_{pipeline_id}_")
            seen.append(f"{pipeline_id}:{ws}")
            return {"workspace": ws, "task.ws_meta": {"mode": "plain", "path": ws}}

        mod.set_workspace_preparer(_preparer)
        first = await _make_tool(mod).execute(_inputs())
        second = await _make_tool(mod).execute(_inputs())
        assert first.success and second.success
        assert len(seen) == 2
        assert seen[0] != seen[1]

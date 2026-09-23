# @feature: FP-0.2.二 内部模块 manifest(BUG-61 模式面板零跟进回归锁) | @ci: python-coverage
"""mode_coding 面板数据面契约测试（BUG-61 回归锁）。

面板会话/看板行源 = pipeline-state.list 摘要行，先按 state 顶层 mode 键过滤
（_norm_sessions）再按六列状态机落列（_board_column）。BUG-61 真根因在派发
链：task_submit 不把 mode 输入透传进 execution_context → 出生管道 state.mode
永不回写（context_build 唯一回写面读 execution_context.mode）→ 面板按 mode
过滤后派发任务零呈现。本文件锁两端契约：

- 读面：mode=coding 的行可见且按任务状态落列；无 mode 键的行被过滤
  （BUG-61 症状形态——即 task_submit 必须透传 mode 的原因）；
- 写面：_issue_args 派发参数携带 mode=coding，session_id 非空透传/空缺省。

面板 webview JS（coding_panel.html）无测试基建（插件 HTML 不进 vitest 车道），
其行为契约以后端数据面测试 + 装机回归（docs/working/packtest）共同覆盖。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODE_DIR = _REPO_ROOT / "plugins" / "shared" / "modes" / "mode_coding"

_MOD_NAME = "mode_coding_panel_test_mod"


@pytest.fixture(scope="module")
def mod():
    """以唯一模块名加载 mode_coding/server.py（插件 server 皆为平铺入口）。"""
    if str(_REPO_ROOT / "plugins" / "sdk" / "src") not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT / "plugins" / "sdk" / "src"))
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _MODE_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = m
    try:
        spec.loader.exec_module(m)
    except BaseException:
        sys.modules.pop(_MOD_NAME, None)
        raise
    yield m
    sys.modules.pop(_MOD_NAME, None)
    m.reset_providers()


def _dispatched_row(**overrides: Any) -> dict[str, Any]:
    """BUG-61 派发任务（ed88badf7913）修复后应有的 pipeline-state 摘要行形态。"""
    row: dict[str, Any] = {
        "pipeline_id": "ed88badf7913",
        "thread_id": "thread-52ad0f47",
        "agent_id": "mode_coding/programming_orchestrator_agent_v2",
        "task.id": "ed88badf7913",
        "task.goal": "R234派发冒烟",
        "task.status": "completed",
        "run_status": "succeeded",
        "mode": "coding",
        "message_count": 2,
        "ckpt_max_seq": 1,
    }
    row.update(overrides)
    return row


class TestSessionsFilter:
    def test_mode_tagged_row_visible_and_filed_merged(self, mod):
        """修复形态：mode=coding 的派发行可见，completed → merged 列。"""
        sessions = mod._norm_sessions([_dispatched_row()])
        assert [s["pipeline_id"] for s in sessions] == ["ed88badf7913"]
        assert sessions[0]["column"] == "merged"
        assert sessions[0]["task_status"] == "completed"

    def test_modeless_row_filtered_is_bug61_symptom(self, mod):
        """BUG-61 症状形态：行无 mode 键 → 被过滤（派发链透传 mode 的原因）。"""
        row = _dispatched_row()
        del row["mode"]
        assert mod._norm_sessions([row]) == []

    def test_other_mode_row_filtered(self, mod):
        """他模式行不进本模式面板（过滤键是值相等，非键存在性）。"""
        assert mod._norm_sessions([_dispatched_row(mode="writing")]) == []

    def test_pending_row_filed_issue(self, mod):
        """未开跑（task.status=pending）→ issue 列（派发即见的落列口径）。"""
        sessions = mod._norm_sessions([_dispatched_row(**{"task.status": "pending"})])
        assert sessions[0]["column"] == "issue"


class TestBoardPayload:
    def test_completed_row_lands_merged_column(self, mod):
        """board = 会话行按六列落列；修复后的派发任务在 merged 列可见。"""
        mod.reset_providers()

        async def fake_rows() -> list[dict[str, Any]]:
            return [_dispatched_row()]

        mod._set_provider("pipeline-state", fake_rows)
        try:
            payload = asyncio.run(mod._board_payload())
        finally:
            mod.reset_providers()
        columns = {c["key"]: c for c in payload["columns"]}
        assert [c["pipeline_id"] for c in columns["merged"]["cards"]] == ["ed88badf7913"]
        assert columns["issue"]["cards"] == []


class TestIssueArgs:
    def test_args_carry_mode_and_session(self, mod):
        """派发参数带 mode=coding（经 task_submit 透传 state.mode）+ session 锚。"""
        args = mod._issue_args("标题栏闪烁", "UI bug", "thread-52ad0f47")
        assert args["mode"] == "coding"
        assert args["session_id"] == "thread-52ad0f47"
        assert args["target_id"] == "mode_coding/programming_orchestrator_agent_v2"
        assert "标题栏闪烁" in args["goal_description"]

    def test_args_omit_session_when_blank(self, mod):
        """无宿主会话锚（ctx.sync 未下发）→ session_id 键省略，不传空串。"""
        assert "session_id" not in mod._issue_args("x", "", "")

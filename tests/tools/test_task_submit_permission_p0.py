"""P0-3 task_submit 权限面修复测试（TDD）。

回归安全缺口：``task_submit`` 完全无 ``_check_permission``——``_validate_parent_task_id``
仅校验 parent_task_id 的存在性/格式，不校验**归属**。L2/L3 可伪造 parent_task_id
指向他人的同级任务，越权把子任务挂到别人的任务树下（继承其管道/工作空间/上下文）。

契约（``_check_parent_ownership``）：
- 合法链：子任务只能挂在「比自己更高层级」的祖先任务下（L2→父须 submitted_by_level==1；
  L3→父须 < 3）。
- 父任务 submitted_by_level 缺失或 ≥ 本层级 → 视为他人任务，拒绝（INSUFFICIENT_PERMISSION）。
- L1 不受约束（可提交根任务或挂在任意已存在任务下，存在性由后续校验保证）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── 路径注入：task_submit 工具目录 ──
# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装，无需注入路径）。
# tool.py 顶部仅需 SDK；任务领域模块（service_access / task_types / agents_types）
# 在用到处懒加载，由 server.py 在真实运行时注入 system/tasks 路径，测试中不触达。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TS_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "task_submit"
for _p in (str(_TS_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# 弹出可能被先前测试缓存为别的 tool.py 的同名模块，确保本次拿到 task_submit 的 tool。
for _m in ("tool",):
    sys.modules.pop(_m, None)

import tool as ts_tool  # noqa: E402

TaskSubmitTool = ts_tool.TaskSubmitTool


def _make_service() -> MagicMock:
    """构造 TaskService mock（get_task 返回可控父任务）。"""
    svc = MagicMock()
    svc.get_task.return_value = None
    return svc


def _make_tool() -> TaskSubmitTool:
    """构造 TaskSubmitTool，注入 mock 服务（避免触达 infrastructure）。"""
    t = TaskSubmitTool()
    t._task_service = _make_service()
    return t


def _parent(submitted_by_level: int | None) -> MagicMock:
    """构造一个父任务 mock，metadata 含 submitted_by_level。"""
    p = MagicMock()
    p.metadata = (
        {"submitted_by_level": submitted_by_level} if submitted_by_level is not None else {}
    )
    return p


# ═══════════════════════════════════════════════════════════
# RED：_check_parent_ownership 不存在 → AttributeError；补齐后转绿
# ═══════════════════════════════════════════════════════════


def test_check_parent_ownership_rejects_peer_parent_for_l2() -> None:
    """L2 伪造 parent_task_id 指向另一 L2 任务 → 拒绝（防同级越权挂载）。"""
    tool = _make_tool()
    tool._task_service.get_task.return_value = _parent(submitted_by_level=2)

    ok, err = tool._check_parent_ownership(parent_agent_level=2, parent_task_id="p1")

    assert ok is False
    assert err is not None


def test_check_parent_ownership_allows_ancestor_parent_for_l2() -> None:
    """L2 挂在 L1 父任务下 → 合法（不破坏正常子任务提交）。"""
    tool = _make_tool()
    tool._task_service.get_task.return_value = _parent(submitted_by_level=1)

    ok, err = tool._check_parent_ownership(parent_agent_level=2, parent_task_id="p1")

    assert ok is True
    assert err is None


def test_check_parent_ownership_rejects_missing_parent_for_l2() -> None:
    """L2 的 parent_task_id 不存在 → 拒绝。"""
    tool = _make_tool()
    tool._task_service.get_task.return_value = None

    ok, err = tool._check_parent_ownership(parent_agent_level=2, parent_task_id="ghost")

    assert ok is False
    assert err is not None


def test_check_parent_ownership_rejects_legacy_parent_for_l2() -> None:
    """L2 父任务无 submitted_by_level（遗留任务）→ 拒绝（无法证明归属）。"""
    tool = _make_tool()
    tool._task_service.get_task.return_value = _parent(submitted_by_level=None)

    ok, err = tool._check_parent_ownership(parent_agent_level=2, parent_task_id="p1")

    assert ok is False


def test_check_parent_ownership_l1_unconstrained() -> None:
    """L1 不受归属约束（根 Agent，可提交根任务或挂在任意已存在任务下）。"""
    tool = _make_tool()

    ok, err = tool._check_parent_ownership(parent_agent_level=1, parent_task_id="anything")

    assert ok is True
    assert err is None


def test_check_parent_ownership_skips_when_no_parent_id() -> None:
    """无 parent_task_id（根任务）→ 放行（存在性/层级由其它校验保证）。"""
    tool = _make_tool()

    ok, err = tool._check_parent_ownership(parent_agent_level=2, parent_task_id=None)

    assert ok is True


# ═══════════════════════════════════════════════════════════
# 集成：execute() 伪造 parent_task_id → INSUFFICIENT_PERMISSION
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_rejects_forged_parent_task_id() -> None:
    """execute：L2 提交时 parent_task_id 指向另一 L2 任务 → INSUFFICIENT_PERMISSION。

    RED：无归属校验，伪造 parent_task_id 通过（落到后续 L2_CANNOT_SPECIFY_PARENT_TASK_ID
         或继续执行）。
    GREEN：_check_parent_ownership 早拦截 → INSUFFICIENT_PERMISSION。
    """
    tool = _make_tool()
    tool._task_service.get_task.return_value = _parent(submitted_by_level=2)

    result = await tool.execute(
        {
            "goal": {"title": "子任务"},
            "parent_agent_level": 2,
            "parent_task_id": "victim-l2-task",  # 伪造：指向另一 L2 的任务
            "task_scope": "non_container",
        }
    )

    assert not result.success
    assert result.error_code == "INSUFFICIENT_PERMISSION"

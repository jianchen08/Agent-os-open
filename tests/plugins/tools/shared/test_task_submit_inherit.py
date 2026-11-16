# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_submit 继承参数面（inherit_from 单参数）契约测试。

2026-10-20 用户裁定《继承机制简化方案》：

- ``inherit_from`` 只承载**源任务 ID**（系统生成，agent 不自选）→ 工具按 id
  正向查源任务 state 行 → **整份复制**其 ``task.ws_meta``（mode/path/
  project_root/branch/tree_path 一字不改）→ 新任务落在同一 worktree、同一
  分支干活，评估门控的"合并+清理"链路自然接续；
- ``inherit_mode`` 撤下（pipe 是空壳；上下文重新开始即重新开始）；
- 三道校验：源任务已终态（running ⇒ 拒绝，防同树双写）、坐标活着（目录存在
  + git 身份有效）、同 ``project_root`` 基座；
- 坐标已死 ⇒ 显式报错引导改派（不静默回落）；id 查不到 ⇒ 显式报错（不静默
  当普通任务）；
- ``inherit_from`` 与 ``workspace`` 互斥（同给即报错，不设静默优先级）；
- 子任务同样可用 ``inherit_from`` 接续失败任务（闸门不对称消解）；
- 机制回填坐标事实（问题 6）：接续场景的任务书空间陈述由机制给出，不靠模型
  自述。

装配：与 test_task_submit_dispatch.py 同款（平铺 sys.path + importlib 装载）。
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_TS_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "task_submit"
_SHARED_ROOT = Path(__file__).resolve().parents[4] / "plugins" / "shared"

for _d in [_SHARED_ROOT, _SHARED_ROOT / "tasks"]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    mod_name = "task_submit_tool_inherit_test"
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

    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result or {"status": "created", "pipeline_id": "pipe_engine_gen_1"}

    async def __call__(self, params: dict) -> dict:
        self.calls.append(params)
        return self._result


def _base_inputs(**over: Any) -> dict:
    base = {
        "goal": {"title": "接续失败任务", "description": "接续上一轮失败任务的工作空间继续"},
        "target_type": "agent",
        "target_id": "main",
        "parent_agent_level": 1,
        "pipeline_id": "pipe_parent_9",
        "user_id": "user-1",
        "session_id": "sess-1",
    }
    base.update(over)
    return base


def _make_tool(mod: Any) -> Any:
    tool = mod.TaskSubmitTool()

    async def _ok(t, l):
        return (True, "", "")

    tool._validate_target_agent = _ok  # type: ignore[method-assign]

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

    tool._get_agent_base_config = _base_config  # type: ignore[method-assign]
    return tool


def _make_preparer(mod: Any) -> Any:
    """出生第三段准备器替身：复刻 workspace_lifecycle 的幂等语义——
    state 已带 workspace（继承坐标已就位）→ 跳过创建，返回空坐标更新；
    否则创建临时目录并回传坐标。
    """
    ws_dir = Path(tempfile.mkdtemp(prefix="agentos_inherit_ws_"))

    async def _preparer(*, pipeline_id: str, state: dict, execution_context: dict) -> dict:
        if state.get("workspace"):
            return {}
        return {
            "workspace": str(ws_dir),
            "task.ws_meta": {"mode": "plain", "path": str(ws_dir)},
        }

    mod.set_workspace_preparer(_preparer)
    return _preparer


def _worktree_fixture(root: Path) -> tuple[Path, Path]:
    """造一个 worktree 形态坐标：<root>/.agentos-workspaces/wt（.git 文件形态）
    + 源仓 <root>（project_root）。"""
    wt = root / ".agentos-workspaces" / "wt_src"
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: " + str(root / ".git" / "worktrees" / "wt_src"), encoding="utf-8")
    return wt, root


def _source_row(
    task_id: str,
    ws_meta: dict,
    *,
    run_status: str = "failed",
    task_status: str = "failed",
) -> dict:
    row = {
        "pipeline_id": task_id,
        "task.id": task_id,
        "task.ws_meta": ws_meta,
    }
    if run_status is not None:
        row["run_status"] = run_status
    if task_status is not None:
        row["task.status"] = task_status
    return row


class TestInheritCoordinateCopy:
    """规则一：按 id 正向查表 → 整份复制 ws_meta → 同一 worktree/分支接续。"""

    async def test_inherit_copies_whole_ws_meta_verbatim(self, mod: Any) -> None:
        """复制成功：worktree 坐标整份承接（branch/tree_path/project_root 不丢）。"""
        root = Path(tempfile.mkdtemp(prefix="agentos_inherit_root_"))
        try:
            wt, proj = _worktree_fixture(root)
            src_meta = {
                "mode": "worktree",
                "path": str(wt),
                "project_root": str(proj),
                "branch": "task/src000000001",
                "tree_path": "devA/wt_src",
            }
            sender = _FakeSender()
            mod.set_chat_sender(sender)
            tool = _make_tool(mod)
            _make_preparer(mod)

            async def _rows() -> list[dict]:
                return [_source_row("src000000001", src_meta)]

            tool._read_state_rows = _rows  # type: ignore[method-assign]
            try:
                r = await tool.execute(_base_inputs(inherit_from="src000000001"))
            finally:
                mod._chat_sender = None
            assert r.success, r.error

            # 出生登记（阶段一）：整份复制的坐标一字不改
            birth_state = sender.calls[0]["state"]
            assert birth_state["task.ws_meta"] == src_meta
            assert birth_state["task.ws_meta"]["branch"] == "task/src000000001"
            assert birth_state["task.ws_meta"]["tree_path"] == "devA/wt_src"
            assert birth_state["workspace"] == str(wt)

            # 工作空间登记（阶段三）：坐标即源坐标（不新建 worktree、不换分支）
            ws_reg = sender.calls[2]["state"]
            assert ws_reg["workspace"] == str(wt)
            assert ws_reg["task.ws_meta"] == src_meta

            # 执行派发：execution_context 坐标 = 源坐标，拓扑随坐标自带
            ec = sender.calls[3]["execution_context"]
            assert ec["workspace"]["source_path"] == str(wt)
            assert ec["workspace"]["mode"] == "worktree"
            assert ec["workspace"]["explicit"] is False
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_inherit_does_not_downgrade_worktree_to_project_root(self, mod: Any) -> None:
        """回归护栏：worktree 源不得被降级为 project_root（旧实现的病根）。"""
        root = Path(tempfile.mkdtemp(prefix="agentos_inherit_root_"))
        try:
            wt, proj = _worktree_fixture(root)
            src_meta = {
                "mode": "worktree",
                "path": str(wt),
                "project_root": str(proj),
                "branch": "task/src000000002",
            }
            sender = _FakeSender()
            mod.set_chat_sender(sender)
            tool = _make_tool(mod)
            _make_preparer(mod)

            async def _rows() -> list[dict]:
                return [_source_row("src000000002", src_meta)]

            tool._read_state_rows = _rows  # type: ignore[method-assign]
            try:
                r = await tool.execute(_base_inputs(inherit_from="src000000002"))
            finally:
                mod._chat_sender = None
            assert r.success, r.error
            assert sender.calls[0]["state"]["task.ws_meta"]["path"] == str(wt)
            assert sender.calls[0]["state"]["task.ws_meta"]["path"] != str(proj)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_inherit_fact_backfilled_into_task_book(self, mod: Any) -> None:
        """问题 6：接续场景的空间陈述由机制给出（事实回填，不靠模型自述）。"""
        root = Path(tempfile.mkdtemp(prefix="agentos_inherit_root_"))
        try:
            wt, proj = _worktree_fixture(root)
            src_meta = {
                "mode": "worktree",
                "path": str(wt),
                "project_root": str(proj),
                "branch": "task/src000000003",
            }
            sender = _FakeSender()
            mod.set_chat_sender(sender)
            tool = _make_tool(mod)
            _make_preparer(mod)

            async def _rows() -> list[dict]:
                return [_source_row("src000000003", src_meta)]

            tool._read_state_rows = _rows  # type: ignore[method-assign]
            try:
                r = await tool.execute(_base_inputs(inherit_from="src000000003"))
            finally:
                mod._chat_sender = None
            assert r.success, r.error
            kickoff = sender.calls[3]["message"]
            assert "src000000003" in kickoff
            assert str(wt) in kickoff
            assert "task/src000000003" in kickoff
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestInheritGuards:
    """三道校验 + 显式报错（不静默回落）。"""

    async def test_running_source_rejected(self, mod: Any) -> None:
        """源任务仍 running → 拒绝（防同树双写）。"""
        root = Path(tempfile.mkdtemp(prefix="agentos_inherit_root_"))
        try:
            wt, proj = _worktree_fixture(root)
            meta = {"mode": "worktree", "path": str(wt), "project_root": str(proj)}
            sender = _FakeSender()
            mod.set_chat_sender(sender)
            tool = _make_tool(mod)
            _make_preparer(mod)

            async def _rows() -> list[dict]:
                return [_source_row("src000000010", meta, run_status="running", task_status="running")]

            tool._read_state_rows = _rows  # type: ignore[method-assign]
            try:
                r = await tool.execute(_base_inputs(inherit_from="src000000010"))
            finally:
                mod._chat_sender = None
            assert not r.success
            assert r.error_code == "INHERIT_SOURCE_NOT_TERMINAL", r.error
            assert "src000000010" in (r.error or "")
            assert not sender.calls, "被拒提交不得派发任何管道"
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_dead_coordinate_reported_not_silently_downgraded(self, mod: Any) -> None:
        """坐标已死（源已合并清理）→ 显式报错引导改派，不静默回落。"""
        root = Path(tempfile.mkdtemp(prefix="agentos_inherit_root_"))
        try:
            wt, proj = _worktree_fixture(root)
            shutil.rmtree(wt, ignore_errors=True)
            meta = {"mode": "worktree", "path": str(wt), "project_root": str(proj)}
            sender = _FakeSender()
            mod.set_chat_sender(sender)
            tool = _make_tool(mod)
            _make_preparer(mod)

            async def _rows() -> list[dict]:
                return [_source_row("src000000011", meta)]

            tool._read_state_rows = _rows  # type: ignore[method-assign]
            try:
                r = await tool.execute(_base_inputs(inherit_from="src000000011"))
            finally:
                mod._chat_sender = None
            assert not r.success
            assert r.error_code == "INHERIT_SOURCE_WS_DEAD", r.error
            assert not sender.calls
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_missing_source_id_reported(self, mod: Any) -> None:
        """id 查不到 → 显式报错（不静默当普通任务）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        _make_preparer(mod)

        async def _rows() -> list[dict]:
            return []

        tool._read_state_rows = _rows  # type: ignore[method-assign]
        try:
            r = await tool.execute(_base_inputs(inherit_from="ghost00000001"))
        finally:
            mod._chat_sender = None
        assert not r.success
        assert r.error_code == "INHERIT_SOURCE_NOT_FOUND", r.error
        assert not sender.calls

    async def test_foreign_project_root_rejected(self, mod: Any) -> None:
        """同 project_root 基座校验：不同基座 → 拒绝。"""
        root = Path(tempfile.mkdtemp(prefix="agentos_inherit_root_"))
        other = Path(tempfile.mkdtemp(prefix="agentos_inherit_other_"))
        try:
            wt, proj = _worktree_fixture(root)
            meta = {"mode": "worktree", "path": str(wt), "project_root": str(other)}
            sender = _FakeSender()
            mod.set_chat_sender(sender)
            tool = _make_tool(mod)
            _make_preparer(mod)

            async def _rows() -> list[dict]:
                return [_source_row("src000000012", meta)]

            tool._read_state_rows = _rows  # type: ignore[method-assign]
            try:
                r = await tool.execute(
                    _base_inputs(
                        inherit_from="src000000012",
                        parent_ws_meta={"mode": "worktree", "path": str(wt), "project_root": str(proj)},
                    )
                )
            finally:
                mod._chat_sender = None
            assert not r.success
            assert r.error_code == "INHERIT_PROJECT_MISMATCH", r.error
            assert not sender.calls
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(other, ignore_errors=True)

    async def test_inherit_and_workspace_are_mutually_exclusive(self, mod: Any) -> None:
        """inherit_from 与 workspace 互斥：同给即报错，不设静默优先级。"""
        root = Path(tempfile.mkdtemp(prefix="agentos_inherit_root_"))
        try:
            wt, proj = _worktree_fixture(root)
            meta = {"mode": "worktree", "path": str(wt), "project_root": str(proj)}
            sender = _FakeSender()
            mod.set_chat_sender(sender)
            tool = _make_tool(mod)
            _make_preparer(mod)

            async def _rows() -> list[dict]:
                return [_source_row("src000000013", meta)]

            tool._read_state_rows = _rows  # type: ignore[method-assign]
            try:
                r = await tool.execute(
                    _base_inputs(inherit_from="src000000013", workspace=str(proj))
                )
            finally:
                mod._chat_sender = None
            assert not r.success
            assert r.error_code == "INHERIT_WORKSPACE_CONFLICT", r.error
            assert not sender.calls
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestInheritPathsSymmetry:
    """问题 7：两条提交路径同规则。"""

    async def test_subtask_can_inherit_failed_task_workspace(self, mod: Any) -> None:
        """子任务同样可用 inherit_from 接续失败任务（闸门不对称消解）。"""
        root = Path(tempfile.mkdtemp(prefix="agentos_inherit_root_"))
        try:
            wt, proj = _worktree_fixture(root)
            meta = {
                "mode": "worktree",
                "path": str(wt),
                "project_root": str(proj),
                "branch": "task/src000000020",
            }
            sender = _FakeSender()
            mod.set_chat_sender(sender)
            tool = _make_tool(mod)
            _make_preparer(mod)

            async def _rows() -> list[dict]:
                return [
                    {
                        "pipeline_id": "parent_p",
                        "task.id": "parent_p",
                        "task.submitted_by_level": 1,
                    },
                    _source_row("src000000020", meta),
                ]

            tool._read_state_rows = _rows  # type: ignore[method-assign]
            try:
                r = await tool.execute(
                    _base_inputs(
                        inherit_from="src000000020",
                        parent_agent_level=2,
                        task_id="parent_p",
                    )
                )
            finally:
                mod._chat_sender = None
            assert r.success, r.error
            assert sender.calls[0]["state"]["task.ws_meta"] == meta
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_plain_task_without_inherit_unchanged(self, mod: Any) -> None:
        """普通任务提交（不填 inherit_from）行为不变：默认 worktree 隔离语义。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        _make_preparer(mod)

        async def _rows() -> list[dict]:
            return []

        tool._read_state_rows = _rows  # type: ignore[method-assign]
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        birth_state = sender.calls[0]["state"]
        assert "task.ws_meta" not in birth_state
        assert "workspace" not in birth_state
        ec = sender.calls[3]["execution_context"]
        assert ec["workspace"]["source_path"] == ""
        assert ec["workspace"]["explicit"] is False
        assert ec["isolation"]["level"] == "isolated"


class TestInheritParamSurface:
    """参数面：inherit_from 单参数；inherit_mode 撤下。"""

    def test_schema_has_inherit_from_only(self, mod: Any) -> None:
        props = mod._TASK_SUBMIT_INPUT_SCHEMA["properties"]
        assert "inherit_from" in props
        assert "inherit_mode" not in props, "inherit_mode 已撤下（pipe 空壳，上下文不建机制）"
        desc = props["inherit_from"]["description"]
        assert "源任务 ID" in desc
        assert "接续" in desc

    def test_workspace_description_points_to_inherit_from(self, mod: Any) -> None:
        props = mod._TASK_SUBMIT_INPUT_SCHEMA["properties"]
        assert "inherit_from" in props["workspace"]["description"]

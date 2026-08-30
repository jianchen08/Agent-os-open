# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_submit 任务执行驱动 TDD 测试（GAP-1 统一：state 单一真值，YAML 停写）。

0.2 收尾时 pipeline-executor.start_run 占位能力移除后，task_submit 提交即落库、
**无人派发执行**——任务永远 pending（e2e 缺口 GAP-1）。统一定案后本文件覆盖：

1. 提交经统一出生协议（plugins/shared/task_birth.py）创建执行管道，
   **不再调用 task_service.create_task**——YAML 存储无写路径；
2. 三段式：出生登记（create+no_dispatch，引擎生成 pipeline_id）→ 身份登记
   （task.id = 引擎管道 id，先于任何管道步骤执行）→ 执行派发（kickoff +
   execution_context 透传、background）；state 出生即带 task.*/lineage.* 字段；
3. 依赖校验读 state 聚合（pipeline-state.list capability）而非 YAML；
4. 不再 start_task/bind_pipeline_run（任务状态由内核 run 终态回写 state）；
5. 派发器不可用/出生失败 → 话术诚实（不声称执行中），结果携带明确 warning。

装配：conftest.py 注入 sdk / tools 共享层；task_submit 平铺目录与
system/tasks 同 test_task_submit_migration.py 的 sys.path 装配。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_TS_DIR = Path(__file__).resolve().parent.parent / "task_submit"
_SYSTEM_ROOT = Path(__file__).resolve().parents[2] / "system"

for _d in [_SYSTEM_ROOT, _SYSTEM_ROOT / "tasks", _SYSTEM_ROOT / "channel_api"]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    """加载 task_submit/tool.py（唯一模块名，进程内缓存）。"""
    mod_name = "task_submit_tool_dispatch_test"
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

    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result or {"status": "created", "pipeline_id": "pipe_engine_gen_1"}
        self._error = error

    async def __call__(self, params: dict) -> dict:
        self.calls.append(params)
        if self._error:
            raise self._error
        return self._result


def _base_inputs(**over: Any) -> dict:
    base = {
        "goal": {"title": "喝水提醒", "description": "每小时提醒喝水"},
        "target_type": "agent",
        "target_id": "main",
        "parent_agent_level": 1,
        "pipeline_id": "pipe_parent_9",
        "user_id": "user-1",
        "execution_id": "exec-1",
    }
    base.update(over)
    return base


def _make_tool(mod: Any) -> Any:
    """构造工具实例并 stub 纯参数校验（target 存在性等与存储无关的面）。"""
    tool = mod.TaskSubmitTool()

    async def _ok(t, l):
        return (True, "", "")

    tool._validate_target_agent = _ok  # type: ignore[method-assign]
    return tool


class TestTaskPipelineDispatch:
    def test_module_exposes_sender_injection_point(self, mod: Any) -> None:
        """server.py on_load 注入点存在（set_chat_sender / _get_chat_sender）。"""
        assert hasattr(mod, "set_chat_sender")
        assert hasattr(mod, "_get_chat_sender")
        assert mod._get_chat_sender() is None

    async def test_submit_creates_pipeline_without_task_service(self, mod: Any) -> None:
        """核心契约：不再调用 task_service.create_task——YAML 无写路径；
        统一出生协议三段式：出生登记（create+no_dispatch）→ 身份登记
        （task.id = 引擎管道 id）→ 执行派发；task.owned 登记回提交者管道。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        service = MagicMock()  # spy：断言任何存储写方法都未被触碰
        tool._get_task_service = lambda: service  # type: ignore[method-assign]
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success, r.error

        # 统一出生协议（plugins/shared/task_birth.py）：四次 chat.send_message——
        # ① 出生登记（create + no_dispatch 只登记不派发）；② 身份登记
        # （task.id = 引擎管道 id，先于执行）；③ 执行派发（kickoff+background）；
        # ④ no_dispatch 登记分支把新任务以 task.owned.<id>.* 写回提交者管道。
        assert len(sender.calls) == 4
        birth, identity, dispatch, reg = sender.calls
        # ① 出生登记：create + no_dispatch，出生 state 一次写全
        assert birth["create"] is True
        assert birth["no_dispatch"] is True
        assert "pipeline_id" not in birth
        assert birth["state"]["task.goal"] == "喝水提醒"
        assert birth["state"]["task.status"] == "pending"
        # 出生登记不派发：kickoff 不在此段
        assert "background" not in birth
        # 血缘：有父形式（state 扁平键出生即入，非顶层 lineage 字典）
        assert birth["state"]["lineage.parent_pipeline_id"] == "pipe_parent_9"
        assert birth["state"]["lineage.origin_session_id"] == "pipe_parent_9"
        assert birth["user_id"] == "user-1"

        # ② 身份登记：task.id = 引擎返回的 pipeline_id（身份权威统一），
        #    任何管道步骤执行前写入（init 体 workspace_lifecycle 的任务判据）
        assert identity["pipeline_id"] == "pipe_engine_gen_1"
        assert identity["no_dispatch"] is True
        assert identity["state"] == {"task.id": "pipe_engine_gen_1"}

        # ③ 执行派发：kickoff 指令 + background
        assert dispatch["pipeline_id"] == "pipe_engine_gen_1"
        assert "喝水提醒" in dispatch["message"]
        assert dispatch["user_id"] == "user-1"
        assert dispatch.get("background") is True

        # ④ 登记分支：只写提交者管道 state，不派发
        assert reg["pipeline_id"] == "pipe_parent_9"
        assert reg["no_dispatch"] is True
        assert reg["state"][f"task.owned.pipe_engine_gen_1.status"] == "running"
        assert reg["state"][f"task.owned.pipe_engine_gen_1.title"] == "喝水提醒"

        # YAML 写路径清零：create_task/start_task/bind_pipeline_run 全不触碰
        service.create_task.assert_not_called()
        service.start_task.assert_not_called()
        service.bind_pipeline_run.assert_not_called()
        # 响应身份（2026-08-22 短化定案 8db4c6b16）：LLM 工具面回传 12 位短 id
        # （引擎生成即短 id，登记分支的 task.owned 键同值）。
        short_id = "pipe_engine_gen_1"[:12]
        assert r.output["task_id"] == short_id
        assert r.output["pipeline_id"] == short_id
        assert short_id in r.output["message"]

    async def test_submit_root_lineage_without_parent_pipeline(self, mod: Any) -> None:
        """无调用方管道 → lineage 根形式（诚实声明）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs(pipeline_id=None))
        finally:
            mod._chat_sender = None
        assert r.success
        p = sender.calls[0]
        assert p["state"]["lineage.root"] is True
        assert p["state"]["lineage.origin.kind"] == "plugin"
        assert p["state"]["lineage.origin.source"] == "task_submit"

    async def test_submit_passes_execution_context(self, mod: Any) -> None:
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(
                _base_inputs(workspace="D:/proj/demo", workspace_mode="worktree", isolation_level="isolated")
            )
        finally:
            mod._chat_sender = None
        assert r.success
        ec = sender.calls[2]["execution_context"]
        assert ec["workspace"]["source_path"] == "D:/proj/demo"
        assert ec["workspace"]["mode"] == "worktree"
        assert ec["isolation"]["level"] == "isolated"

    async def test_submit_dependency_check_reads_state_aggregation(self, mod: Any) -> None:
        """依赖校验读 state 聚合（pipeline-state.list）而非 YAML 树。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)

        # 依赖存在（聚合行命中）。_read_state_rows 已是 async 方法（await 调用），
        # stub 需返回 coroutine。
        async def _rows_hit() -> list[dict]:
            return [{"pipeline_id": "dep_pipe_1", "task.status": "completed"}]

        tool._read_state_rows = _rows_hit  # type: ignore[method-assign]
        try:
            r = await tool.execute(_base_inputs(dependencies=["dep_pipe_1"]))
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        assert sender.calls[0]["state"]["task.dependencies"] == ["dep_pipe_1"]

        # 依赖缺失 → DEPENDENCY_NOT_FOUND（不派发）
        async def _rows_empty() -> list[dict]:
            return []

        tool2 = _make_tool(mod)
        tool2._read_state_rows = _rows_empty  # type: ignore[method-assign]
        mod.set_chat_sender(_FakeSender())
        try:
            r2 = await tool2.execute(_base_inputs(dependencies=["ghost_pipe"]))
        finally:
            mod._chat_sender = None
        assert not r2.success
        assert r2.error_code == "DEPENDENCY_NOT_FOUND"

    async def test_submit_drops_retired_priority_max_retries(self, mod: Any) -> None:
        """参数退役（2026-08-24）：priority/max_retries 执行层零消费者，
        schema 与写路径整体删除——显式传入按未知参数忽略，不落派发 state。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs(priority=8, max_retries=1))
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        p = sender.calls[0]
        assert "task.priority" not in p["state"]
        assert "task.max_retries" not in p["state"]

    async def test_submit_without_priority_max_retries_no_state_keys(self, mod: Any) -> None:
        """不传退役参数 → state 无两键（不写不补默认，语义不变）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        p = sender.calls[0]
        assert "task.priority" not in p["state"]
        assert "task.max_retries" not in p["state"]

    async def test_submit_without_sender_fails_honest(self, mod: Any) -> None:
        """派发器不可用 → 失败信封（DISPATCH_FAILED），不得声称执行中。"""
        tool = _make_tool(mod)
        mod._chat_sender = None
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success is False
        assert r.error_code == "DISPATCH_FAILED"
        assert "异步执行中" not in (r.error or ""), "未派发不得声称异步执行中"

    async def test_submit_sender_failure_honest(self, mod: Any) -> None:
        sender = _FakeSender(error=RuntimeError("kernel down"))
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success is False
        assert r.error_code == "DISPATCH_FAILED"
        assert "kernel down" in (r.error or "")


class TestDispatchInstructionRichness:
    """派发指令完整性（0.1 _build_full_task_input 移植）：评估指标详情、
    工作空间模式提示、路径使用规则、待办工作法必须进入下级指令。

    评估指标详情依赖真实 evaluation_metrics.yaml（与 task_evaluate 同源），
    断言取真实指标名与定义中的 description 前缀，不做任何 mock。
    """

    def test_kickoff_includes_evaluation_criteria_detail(self, mod: Any) -> None:
        """验收标准展开为指标详情（真实 yaml 定义），非原始 dict 直贴。"""
        msg = mod._build_evaluation_criteria_prompt(
            {
                "file_check": {"input_params": {"path": "docs/report.md", "check": "exists"}},
                "semantic_check": {"input_params": {"criteria": "覆盖全部需求"}},
            }
        )
        assert mod._EVALUATION_PROMPT_HEADER in msg
        assert "file_check" in msg
        assert "semantic_check" in msg
        # 0.2 yaml 定义中的说明注入（与 task_evaluate 同源）
        assert "文件" in msg
        # 评估参数序列化注入
        assert '"path": "docs/report.md"' in msg

    def test_kickoff_metric_detail_fail_open_empty(self, mod: Any) -> None:
        """无验收标准 / 定义加载失败 → 空串（fail-open，不阻断提交）。"""
        assert mod._build_evaluation_criteria_prompt({}) == ""
        assert mod._build_evaluation_criteria_prompt({"ghost_metric": {}}) == ""

    def test_metric_detail_fail_open_when_metrics_unreadable(
        self, mod: Any, monkeypatch: Any
    ) -> None:
        """指标定义文件不可读 → 详情空串 + 合法指标集合 fail-open（None）。

        校验路径（_get_valid_metric_ids 返回 None）由边界校验用例消费：
        无合法集合可查时跳过 key 校验，不阻断提交。
        """
        monkeypatch.setattr(
            mod,
            "_metrics_config_path",
            lambda: Path("Z:/nonexistent/evaluation_metrics.yaml"),
        )
        assert mod._build_evaluation_criteria_prompt({"file_check": {}}) == ""
        assert mod._get_valid_metric_ids() is None

    def test_kickoff_workspace_guidance_explicit_worktree(self, mod: Any) -> None:
        """显式 workspace + worktree：隔离副本提示 + 相对路径规则。"""
        msg = mod._build_workspace_guidance(
            {"workspace": {"source_path": "D:/proj/demo", "mode": "worktree", "explicit": True}}
        )
        assert "隔离副本" in msg
        assert "自动合并回目标项目" in msg
        assert "相对路径" in msg
        assert "file_write(path=\"docs/report.md\")" in msg

    def test_kickoff_workspace_guidance_explicit_plain(self, mod: Any) -> None:
        """显式 workspace + plain：直接操作目标目录，无合并语义。"""
        msg = mod._build_workspace_guidance(
            {"workspace": {"source_path": "D:/proj/demo", "mode": "plain", "explicit": True}}
        )
        assert "直接在目标目录中执行任务" in msg
        assert "合并" not in msg

    def test_kickoff_workspace_guidance_default(self, mod: Any) -> None:
        """无显式 workspace：默认隔离目录提示。"""
        msg = mod._build_workspace_guidance(
            {"workspace": {"source_path": "", "mode": "", "explicit": False}}
        )
        assert "隔离工作目录" in msg
        assert "相对路径" in msg

    def test_kickoff_workspace_guidance_missing_spec_empty(self, mod: Any) -> None:
        """execution_context 无 workspace 声明 → 空串（不注入误导性提示）。"""
        assert mod._build_workspace_guidance({}) == ""

    def test_kickoff_includes_task_progress_method(self, mod: Any) -> None:
        """待办工作法注入（0.1 同职移植）。"""
        msg = mod._build_task_progress_method()
        assert "进度跟踪工作法" in msg
        assert "- [ ]" in msg
        assert "task_evaluate" in msg

    async def test_dispatch_message_contains_all_guidance(self, mod: Any) -> None:
        """端到端：派发消息带评估详情/工作空间提示/路径规则/待办工作法。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(
                _base_inputs(
                    goal={
                        "title": "写报告",
                        "description": "基于调研数据撰写报告 docs/report.md",
                    },
                    acceptance_criteria={
                        "file_check": {"input_params": {"path": "docs/report.md", "check": "exists"}}
                    },
                    workspace="D:/proj/demo",
                    workspace_mode="worktree",
                )
            )
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        msg = sender.calls[2]["message"]
        assert "执行任务「写报告」" in msg
        assert "任务描述：基于调研数据撰写报告 docs/report.md" in msg
        assert "验收标准：" in msg
        # 增强段：评估详情 + 工作空间 + 路径规则 + 待办工作法
        assert mod._EVALUATION_PROMPT_HEADER in msg
        assert "隔离副本" in msg
        assert "相对路径" in msg
        assert "进度跟踪工作法" in msg


class TestPendingSubtaskRegistration:
    """子任务挂号键（ADR 2026-08-28-task-closure-three-signal-gate 信号③）。

    task_submit 登记分支在提交者管道 state 写 ``task.subtasks_pending.<task_id>``
    （值 = 提交时间戳）：父管道收束判据据此感知「已提交子任务等待回执」；
    子任务终态事件经 task_service 写 null 清除。
    """

    async def test_registration_writes_pending_subtask_key(self, mod: Any) -> None:
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success, r.error

        reg = sender.calls[3]
        assert reg["no_dispatch"] is True
        child_id = "pipe_engine_gen_1"
        value = reg["state"][f"task.subtasks_pending.{child_id}"]
        assert isinstance(value, str) and value, "挂号值 = 提交时间戳（非空标量）"
        # 与 task.owned 登记键同批发往提交者管道（一次 no_dispatch 写面）
        assert f"task.owned.{child_id}.status" in reg["state"]

    async def test_root_dispatch_without_parent_writes_no_registration(
        self, mod: Any
    ) -> None:
        """无调用方管道（根任务）→ 无登记分支，自然无挂号键可写。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        inputs = _base_inputs()
        inputs.pop("pipeline_id")
        try:
            r = await tool.execute(inputs)
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        assert len(sender.calls) == 3, "根任务只有出生三段，无登记分支"


class TestProjectCreateOnSubmit:
    """task_submit 新建项目挂靠（project_title/project_path，仅 L1）。"""

    async def test_l1_creates_project_and_attaches(
        self, mod: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L1 给 project_title → 创建项目（git init + 登记）并挂靠：出生 state
        带 task.parent_project_id（12hex 新项目 id），登记目录落一条 YAML。"""
        import project_registry as pr

        tasks_root = tmp_path / "tasks_data"
        ws_base = tmp_path / "ws"
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(tasks_root))
        monkeypatch.setattr(pr, "workspace_base_dir", lambda: ws_base)
        target = ws_base / "new_proj"
        target.mkdir(parents=True)

        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs(project_title="新项目", project_path=str(target)))
        finally:
            mod._chat_sender = None
        assert r.success, r.error

        birth = sender.calls[0]
        pid = birth["state"]["task.parent_project_id"]
        assert len(pid) == 12
        assert pid == r.metadata["project_id"]
        assert list((tasks_root / "projects").glob("*.yaml"))
        # 项目文件夹成为任务工作空间（inputs 覆写；执行派发段携带 execution_context）
        assert sender.calls[2]["execution_context"]["workspace"]["source_path"] == str(target)

    async def test_l1_reuses_registered_path(
        self, mod: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同路径已登记 → 复用既有项目（不重复登记），挂靠同 id。"""
        import project_registry as pr

        tasks_root = tmp_path / "tasks_data"
        ws_base = tmp_path / "ws"
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(tasks_root))
        monkeypatch.setattr(pr, "workspace_base_dir", lambda: ws_base)
        target = ws_base / "proj"
        target.mkdir(parents=True)

        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r1 = await tool.execute(_base_inputs(project_title="项目A", project_path=str(target)))
            r2 = await tool.execute(_base_inputs(project_title="项目B", project_path=str(target)))
        finally:
            mod._chat_sender = None
        assert r1.success, r1.error
        assert r2.success, r2.error
        pid1 = sender.calls[0]["state"]["task.parent_project_id"]
        pid2 = sender.calls[4]["state"]["task.parent_project_id"]
        assert pid1 == pid2
        assert len(list((tasks_root / "projects").glob("*.yaml"))) == 1

    async def test_l2_cannot_create_project(self, mod: Any) -> None:
        """L2/L3 显式新建项目被拦截（项目归属沿父链继承）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(
                _base_inputs(parent_agent_level=2, project_title="新项目")
            )
        finally:
            mod._chat_sender = None
        assert not r.success
        assert r.error_code == "L2_CANNOT_SPECIFY_PROJECT_ID"
        assert sender.calls == []

    async def test_project_title_and_id_conflict(self, mod: Any) -> None:
        """project_title 与 project_id 二选一：同时指定拒绝。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(
                _base_inputs(project_title="新项目", project_id="abc123def456")
            )
        finally:
            mod._chat_sender = None
        assert not r.success
        assert r.error_code == "PROJECT_CREATE_OR_ATTACH_CONFLICT"
        assert sender.calls == []

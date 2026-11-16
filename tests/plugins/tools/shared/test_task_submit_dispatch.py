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
import shutil
import sys
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_TS_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "task_submit"
_SHARED_ROOT = Path(__file__).resolve().parents[4] / "plugins" / "shared"

# 拍平布局（ADR 2026-10-13-plugin-flat-layout）：共享根平铺模块 + tasks 插件目录
# （task_types/orchestration 裸名解析）。channel_api 已并入 tasks/http_api.py。
for _d in [_SHARED_ROOT, _SHARED_ROOT / "tasks"]:
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


@pytest.fixture(autouse=True)
def _birth_ws(mod: Any) -> Any:
    """出生协议第三段（提交期同步建工作空间）准备器替身——驱动真实派发的
    用例必注入（ADR-2026-10-17-task-birth-workspace-inclusion：无跳过路径）。
    """
    from tests import _task_birth_workspace as _birth_ws_mod

    _birth_ws_mod.inject_preparer(mod)
    yield
    _birth_ws_mod.reset_preparer(mod)


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
        "session_id": "sess-1",
    }
    base.update(over)
    return base


def _make_tool(mod: Any) -> Any:
    """构造工具实例并 stub 纯参数校验（target 存在性等与存储无关的面）。

    `_get_agent_base_config` 同层替身：返回真实 agent yaml 的字段形态
    （身份性字段 tool_ids/system_prompt 齐备）——完备性闸门走真实声明
    （仓库 autonomous 派生输入集）按生产路径放行，本文件只断言派发机制。
    """
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

        # 统一出生协议（plugins/shared/task_birth.py）：五次 chat.send_message——
        # ① 出生登记（create + no_dispatch 只登记不派发）；② 身份登记
        # （task.id = 引擎管道 id，先于执行）；③ 工作空间登记（出生协议第三段，
        # 提交期同步建工作空间，坐标落任务管道）；④ 执行派发（kickoff+background）；
        # ⑤ no_dispatch 登记分支把新任务以 task.owned.<id>.* 写回提交者管道。
        assert len(sender.calls) == 5
        birth, identity, workspace_reg, dispatch, reg = sender.calls
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
        # origin_session_id：param_inject 注入的 session_id 优先（无会话坐标才回退管道 id）
        assert birth["state"]["lineage.origin_session_id"] == "sess-1"
        assert birth["user_id"] == "user-1"

        # ② 身份登记：task.id = 引擎返回的 pipeline_id（身份权威统一），
        #    任何管道步骤执行前写入（init 体 workspace_lifecycle 的任务判据）
        assert identity["pipeline_id"] == "pipe_engine_gen_1"
        assert identity["no_dispatch"] is True
        assert identity["state"] == {"task.id": "pipe_engine_gen_1"}

        # ③ 工作空间登记：坐标写任务管道（no_dispatch，不派发）
        assert workspace_reg["pipeline_id"] == "pipe_engine_gen_1"
        assert workspace_reg["no_dispatch"] is True

        # ④ 执行派发：kickoff 指令 + background
        assert dispatch["pipeline_id"] == "pipe_engine_gen_1"
        assert "喝水提醒" in dispatch["message"]
        assert dispatch["user_id"] == "user-1"
        assert dispatch.get("background") is True

        # ⑤ 登记分支：只写提交者管道 state，不派发
        assert reg["pipeline_id"] == "pipe_parent_9"
        assert reg["no_dispatch"] is True
        # 登记键瘦身（ADR 2026-11-02-state-key-governance，6d8375003）：
        # {title, created_at, submitted_by} 纯声明 + 子任务挂号——status 镜像退役，
        # 任务状态单一真源 = 子管道行 task.status
        assert set(reg["state"]) == {
            "task.owned.pipe_engine_gen_1.title",
            "task.owned.pipe_engine_gen_1.created_at",
            "task.owned.pipe_engine_gen_1.submitted_by",
            "task.subtasks_pending.pipe_engine_gen_1",
        }
        assert reg["state"]["task.owned.pipe_engine_gen_1.title"] == "喝水提醒"
        assert reg["state"]["task.owned.pipe_engine_gen_1.submitted_by"] == "user-1"

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
        ec = sender.calls[3]["execution_context"]
        assert ec["workspace"]["source_path"] == "D:/proj/demo"
        assert ec["workspace"]["mode"] == "worktree"
        # 显式 workspace = 调用方显式声明（落位锚定该路径，不被父链继承覆盖）
        assert ec["workspace"]["explicit"] is True
        assert ec["isolation"]["level"] == "isolated"

    async def test_submit_explicit_plain_birth_writes_task_ws_meta(self, mod: Any) -> None:
        """显式 workspace + plain 拓扑：出生登记（阶段一）即写 task.ws_meta。

        「任务 init 即写」单一真相源的创建段兑现——显式 plain 的最终坐标提交期
        即确定（workspace_lifecycle._start_plain_root 同形：直接操作该目录，
        path = project_root = source_path）。缺此预写，创建 → 管道 init 落
        task.ws_meta 之间是首查时序窗：文件树首查 no_workspace、重试才命中
        （2026-10-08 装机实锤 5dc60e00，tasks/root 直派）。
        """
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(
                _base_inputs(workspace="D:/proj/demo", workspace_mode="plain")
            )
        finally:
            mod._chat_sender = None
        assert r.success
        birth = sender.calls[0]
        assert birth["state"]["task.ws_meta"] == {
            "mode": "plain",
            "path": "D:/proj/demo",
            "project_root": "D:/proj/demo",
        }

    async def test_submit_non_plain_birth_does_not_prewrite_ws_meta(self, mod: Any) -> None:
        """对照（有区分度输入）：worktree 与无声明拓扑不预写 task.ws_meta。

        worktree 最终坐标（隔离副本路径）与默认拓扑（工作区根/{task_id} 目录
        创建）提交期不可知——预写即谎报坐标，仍由 init 段权威落笔。
        """
        for over in (
            {"workspace": "D:/proj/demo", "workspace_mode": "worktree"},
            {},
        ):
            sender = _FakeSender()
            mod.set_chat_sender(sender)
            tool = _make_tool(mod)
            try:
                r = await tool.execute(_base_inputs(**over))
            finally:
                mod._chat_sender = None
            assert r.success, r.error
            assert "task.ws_meta" not in sender.calls[0]["state"]


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

    async def test_submit_inherit_reads_source_task_coordinates(self, mod: Any) -> None:
        """inherit_from 读源任务自身坐标（task.ws_meta）——不得误报"没有工作空间坐标"。

        2026-10-20 用户裁定《继承机制简化方案》：inherit_from 单参数 = 源任务
        ID，工具按 id 正向查表并整份复制其坐标。
        """
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        # 源任务坐标目录须真实存在（坐标活性校验）
        ws = Path(
            tempfile.mkdtemp(prefix="test_inherit_ws_", dir=Path(__file__).resolve().parents[4])
        )
        try:
            async def _rows() -> list[dict]:
                return [
                    {
                        "pipeline_id": "src-task-001",
                        "task.id": "src-task-001",
                        "run_status": "failed",
                        "task.status": "failed",
                        "task.ws_meta": {"mode": "plain", "path": str(ws)},
                    }
                ]

            tool._read_state_rows = _rows  # type: ignore[method-assign]
            r = await tool.execute(_base_inputs(inherit_from="src-task-001"))
            assert r.success, r.error
            ec_call = next(c for c in sender.calls if "execution_context" in c)
            assert ec_call["execution_context"]["workspace"]["source_path"] == str(ws)
        finally:
            shutil.rmtree(ws, ignore_errors=True)
            mod._chat_sender = None

        # lineage.parent_ws_meta 是父链坐标而非源任务自身坐标，不得作为继承
        # 结果（继承路径曾静默漂移到父会话目录）——lineage-only 行 fail-closed
        sender2 = _FakeSender()
        mod.set_chat_sender(sender2)
        tool2 = _make_tool(mod)
        ws2 = Path(
            tempfile.mkdtemp(prefix="test_inherit_ws_", dir=Path(__file__).resolve().parents[4])
        )
        try:
            async def _rows_lineage_only() -> list[dict]:
                return [
                    {
                        "pipeline_id": "src-task-001",
                        "task.id": "src-task-001",
                        "run_status": "failed",
                        "task.status": "failed",
                        "lineage.parent_ws_meta": {"mode": "plain", "path": str(ws2)},
                    }
                ]

            tool2._read_state_rows = _rows_lineage_only  # type: ignore[method-assign]
            r2 = await tool2.execute(_base_inputs(inherit_from="src-task-001"))
            assert not r2.success
            assert "没有工作空间坐标" in (r2.error or "")
        finally:
            shutil.rmtree(ws2, ignore_errors=True)
            mod._chat_sender = None

    async def test_submit_inherit_source_without_coordinates_fails_closed(self, mod: Any) -> None:
        """源任务无坐标（task.ws_meta 缺）→ fail-closed 报"没有工作空间坐标"，
        不猜测不虚构。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)

        async def _rows() -> list[dict]:
            return [
                {
                    "pipeline_id": "src-task-002",
                    "task.id": "src-task-002",
                    "run_status": "failed",
                    "task.status": "failed",
                }
            ]

        tool._read_state_rows = _rows  # type: ignore[method-assign]
        try:
            r = await tool.execute(_base_inputs(inherit_from="src-task-002"))
            assert not r.success
            assert "没有工作空间坐标" in r.error
        finally:
            mod._chat_sender = None

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
    工作空间模式提示、路径使用规则必须进入下级指令。

    评估指标详情依赖真实 evaluation_metrics.yaml（与 task_evaluate 同源），
    断言取真实指标名与定义中的 description 前缀，不做任何 mock。
    进度跟踪工作法注入已退役（2026-10-25 用户裁定：待办由 todo 插件跟踪，
    派发任务不携带清单流程说明），派发消息以 not in 钉退役。
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

    async def test_dispatch_message_contains_all_guidance(self, mod: Any) -> None:
        """端到端：派发消息带评估详情/工作空间提示/路径规则，不带已退役的待办工作法。"""
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
        msg = sender.calls[3]["message"]
        assert "执行任务「写报告」" in msg
        assert "任务描述：基于调研数据撰写报告 docs/report.md" in msg
        assert "验收标准：" in msg
        # 增强段：评估详情 + 工作空间 + 路径规则
        assert mod._EVALUATION_PROMPT_HEADER in msg
        assert "隔离副本" in msg
        assert "相对路径" in msg
        # 退役钉（2026-10-25 用户裁定）：待办归 todo 插件，派发不携带清单流程说明
        assert "进度跟踪工作法" not in msg


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

        reg = sender.calls[4]
        assert reg["no_dispatch"] is True
        child_id = "pipe_engine_gen_1"
        value = reg["state"][f"task.subtasks_pending.{child_id}"]
        assert isinstance(value, str) and value, "挂号值 = 提交时间戳（非空标量）"
        # 与 task.owned 登记键同批发往提交者管道（一次 no_dispatch 写面）；
        # 登记键瘦身（ADR 2026-11-02-state-key-governance，6d8375003）：
        # status 镜像退役，声明键只余 {title, created_at, submitted_by}
        assert f"task.owned.{child_id}.title" in reg["state"]
        assert f"task.owned.{child_id}.status" not in reg["state"]

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
        assert len(sender.calls) == 4, "根任务出生四段（含工作空间登记），无登记分支"


class TestProjectAttachOnly:
    """task_submit 项目挂靠（project_id）：只挂靠不创建（创建与任务解耦，
    2026-08-30 裁定——创建走 project_create 工具/会话目录登记/前端表单）。"""

    async def test_l2_cannot_specify_project_id(self, mod: Any) -> None:
        """L2/L3 显式 project_id 被拦截（项目归属沿父链继承）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(
                _base_inputs(parent_agent_level=2, project_id="abc123def456")
            )
        finally:
            mod._chat_sender = None
        assert not r.success
        assert r.error_code == "L2_CANNOT_SPECIFY_PROJECT_ID"
        assert sender.calls == []


class TestWorkspaceExplicitness:
    """execution_context.workspace.explicit 只认调用方显式声明。

    子任务沿父链继承的项目挂靠（_inherit_project_id 注入 workspace=项目
    文件夹）不算显式——其落位由出生契约父链坐标（lineage.parent_ws_meta）
    决定，继承直接上级的工作空间（2026-09-03 用户裁定，收窄 2026-08-30
    「挂靠即显式锚」至显式挂靠）。L1/面板显式挂靠仍为显式。
    """

    @pytest.fixture
    def project_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[dict[str, Any]]:
        """登记项目 {pid: 项目文件夹}（仓库根下临时目录，过同容器路径闸）并
        以 fake 模块替换 project_registry（tool.py 函数内 import）。前缀禁
        test_ 开头：并发收集期 pytest 会把根下 test_* 目录当测试包 lstat，
        瞬态目录撞收集即 FileNotFoundError 中断整轮收集。"""
        proj_dir = Path(tempfile.mkdtemp(prefix="proj_ws_tmp_", dir=Path(__file__).resolve().parents[4]))
        fake_reg = types.ModuleType("project_registry")
        fake_reg.load_project_paths = lambda: {"proj00000001": str(proj_dir)}  # type: ignore[attr-defined]
        fake_reg.PLAN_DISPATCH_WHITELIST = frozenset({  # type: ignore[attr-defined]
            "research_agent", "environment_setup_agent"
        })

        class _FakeProjectRegistry:
            """登记查询假面：id 不在登记 → plan 态闸不生效（闸语义原样）。"""

            def get(self, pid: str):
                return None

        fake_reg.ProjectRegistry = _FakeProjectRegistry  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "project_registry", fake_reg)

        async def _project_service(method: str, args: dict):
            """project.* 服务替身（T6a 后 tool.py 经 _project_call 消费）：
            list_paths 出登记映射；get 保持 fake 登记假面语义（id 不在登记 →
            found=False，plan/mode 闸不生效）。"""
            if method == "list_paths":
                return {"paths": {"proj00000001": str(proj_dir)}}
            if method == "get":
                return {"found": False}
            raise KeyError(method)

        tool_mod = _load_module()
        tool_mod.set_project_service(_project_service)
        try:
            yield {"id": "proj00000001", "dir": proj_dir}
        finally:
            tool_mod.set_project_service(None)
            shutil.rmtree(proj_dir, ignore_errors=True)

    async def test_inherited_project_attach_is_not_explicit(
        self, mod: Any, project_env: dict[str, Any]
    ) -> None:
        """L2 子任务沿父链继承项目挂靠 → explicit=False（落位归父链坐标）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)

        async def _rows() -> list[dict]:
            return [
                {
                    "pipeline_id": "pipe_parent_task_1",
                    "task.parent_project_id": project_env["id"],
                }
            ]

        tool._read_state_rows = _rows  # type: ignore[method-assign]
        try:
            r = await tool.execute(
                _base_inputs(parent_agent_level=2, task_id="pipe_parent_task_1")
            )
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        ec = sender.calls[3]["execution_context"]
        # 项目归属继承成功：source_path 解析为项目文件夹供工具锚点使用，
        # 但不得标显式——否则子任务在项目根上另开副本、无视父链工作空间。
        assert ec["workspace"]["source_path"] == str(project_env["dir"])
        assert ec["workspace"]["explicit"] is False
        # 出生 state 仍携带挂靠键（任务树分组不依赖显式性）
        assert sender.calls[0]["state"]["task.parent_project_id"] == project_env["id"]

    async def test_explicit_project_attach_is_explicit(
        self, mod: Any, project_env: dict[str, Any]
    ) -> None:
        """L1 显式指定 project_id → explicit=True（挂靠即锚，项目根分叉）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(
                _base_inputs(project_id=project_env["id"])
            )
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        ec = sender.calls[3]["execution_context"]
        assert ec["workspace"]["source_path"] == str(project_env["dir"])
        assert ec["workspace"]["explicit"] is True

    async def test_root_task_without_workspace_or_project_not_explicit(
        self, mod: Any
    ) -> None:
        """无显式 workspace、无挂靠 → 无 workspace 显式声明（explicit 缺省路径）。"""
        sender = _FakeSender()
        mod.set_chat_sender(sender)
        tool = _make_tool(mod)
        try:
            r = await tool.execute(_base_inputs())
        finally:
            mod._chat_sender = None
        assert r.success, r.error
        ec = sender.calls[3]["execution_context"]
        assert ec["workspace"]["source_path"] == ""
        assert ec["workspace"]["explicit"] is False


class TestManifestSchemaLockstep:
    """plugin.json 声明 ↔ 代码实现 schema 锁步（G2 一致性闸的测试面）。

    G2 注册闸（invoker verify compare_tools）对声明了 input_schema 的工具做
    全等比对：声明与实现不一致 → 工具被剔除 → 「工具未注册」运行事故。
    改 get_tool_definition 的 schema 必须同步 plugin.json（本次事故即因此）。
    """

    def test_declared_input_schema_matches_implementation(self, mod: Any) -> None:
        import json

        manifest = json.loads(
            (Path(__file__).resolve().parents[4] / "plugins" / "shared" / "task_submit" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        declared = manifest["capabilities"]["tools"][0]["input_schema"]

        assert declared == mod._TASK_SUBMIT_INPUT_SCHEMA

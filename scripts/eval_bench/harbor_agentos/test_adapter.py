# @feature: FP-0.2.TB评测适配 | @ci: python-coverage
"""adapter.py 单元测试（AgentOSHarborAgent，BaseAgent 子类契约）。

需要 harbor 包（importorskip——harbor 是本模块硬依赖，缺席则车道跳过并在
台账声明；本仓开发机装 harbor 0.26.0 后全量跑，CI 无 harbor 时 skip）。

覆盖：
1. 契约：name()="agentos" / version() 非 None / capabilities 在位；
2. run 前置守卫：未 setup 显式报错（容器/工作空间/桥三件缺一）；
3. setup fail-loud：容器解析失败（环境不可自证）→ ContainerProbeError，
   且绑定未发生（内核零调用）；
4. setup 成功：容器 ref 解析 + 工作空间落盘（logs_dir 下真实目录）+ 绑定
   参数断言（workspace/container/workdir）；
5. run 两路（≥2 组有区分度输入，桥为记录型桩——外部依赖桩，不 mock 被测类）：
   - completed：context.metadata 回填审计坐标 + finally 解绑定；
   - failed（run_status=failed）：RuntimeError 抛出且解绑定仍发生
     （先解绑后抛错——清理与失败信息两不误）；
6. R17 任务接口派发（缺省通道）：选项缺省值 task / task 终态元数据分形 /
   failed raise 带任务坐标 / 非法 dispatch_mode 显式报错 / session 显式选项
   走既有链路 / 超限原题 base64 直写容器（无损往返 + 失败 fail-loud）/
   内联形态零 staging。
7. R19 轮询预算第一优先源 = 评测基准 task.toml [agent] timeout_sec
   （trial lock.json → task.path 解析）；option/env/缺省降级为兜底链；
   预算注记分钟数与解析值同源换算。

隔离策略：KernelBridge / Harbor 环境 / docker CLI 全为外部依赖，桩注入；
AgentOSHarborAgent 本体真实（BaseAgent 真基类）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "harbor",
    reason="harbor 包未安装（适配器硬依赖；开发机装 harbor 0.26.0 后本车道全跑）",
)

from harbor.models.agent.context import AgentContext  # noqa: E402

pytestmark = pytest.mark.unit

_PKG_DIR = Path(__file__).resolve().parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

import adapter as adapter_mod  # noqa: E402
from adapter import AgentOSHarborAgent  # noqa: E402
from container_probe import ContainerProbeError, ContainerRef  # noqa: E402
from kernel_bridge import RunOutcome  # noqa: E402


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _ExecResult:
    def __init__(self, rc: int, stdout: str) -> None:
        self.return_code = rc
        self.stdout = stdout
        self.stderr = ""


class StubEnv:
    def __init__(
        self,
        hostname_rc: int = 0,
        hostname: str = "abc123def456",
        stage_exec_rc: int = 0,
    ) -> None:
        self._rc = hostname_rc
        self._hostname = hostname
        self._stage_exec_rc = stage_exec_rc
        self.task_env_config = None
        self.exec_calls: list[str] = []

    async def exec(self, command: str, timeout_sec: int | None = None) -> _ExecResult:
        self.exec_calls.append(command)
        if "base64 -d" in command:
            # R17 指令文件 staging 面（容器写）；rc 可注入模拟失败
            return _ExecResult(self._stage_exec_rc, "")
        return _ExecResult(self._rc, self._hostname)


@dataclass
class StubBridge:
    """KernelBridge 记录型桩：调用序列全录，run_task 结果可配。"""

    outcome: RunOutcome = field(default_factory=RunOutcome)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    bind_result: dict[str, Any] = field(default_factory=dict)

    def bind_external_container(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("bind", kwargs))
        result = {"success": True, "container_name": kwargs["container"]}
        result.update(self.bind_result)
        return result

    def unbind_external_container(self, **kwargs: Any) -> None:
        self.calls.append(("unbind", kwargs))

    async def run_task(self, instruction: str, **kwargs: Any) -> RunOutcome:
        self.calls.append(("run_task", {"instruction": instruction, **kwargs}))
        # 指针路径回显（真桥契约：staging 路径写入 outcome 审计面）
        if kwargs.get("instruction_file_path"):
            self.outcome.instruction_file_path = str(kwargs["instruction_file_path"])
        return self.outcome


def _patch_probe(monkeypatch: pytest.MonkeyPatch, name: str = "tb-main-1") -> None:
    """容器解析桩（docker inspect subprocess 外部依赖一并绕开）。"""

    async def _fake_resolve(env: Any) -> ContainerRef:
        env.exec_calls = getattr(env, "exec_calls", [])
        if getattr(env, "_rc", 0) != 0:
            raise ContainerProbeError("容器自证失败（cat /etc/hostname rc=1）")
        return ContainerRef(name=name, workdir="/app")

    monkeypatch.setattr(adapter_mod, "resolve_container_async", _fake_resolve)


def _ok_outcome() -> RunOutcome:
    return RunOutcome(
        session_id="th-1",
        pipeline_id="p-1",
        stream_terminal="stream_end",
        run_status="completed",
        container_name="tb-main-1",
        workspace="D:\\ws",
    )


def _failed_outcome() -> RunOutcome:
    return RunOutcome(
        session_id="th-2",
        pipeline_id="p-2",
        stream_terminal="stream_error",
        run_status="failed",
        container_name="tb-main-1",
        workspace="D:\\ws",
    )


def _task_completed_outcome() -> RunOutcome:
    """R17 任务接口通道 completed 快照（缺省派发形态）。"""
    return RunOutcome(
        container_name="tb-main-1",
        workspace="D:\\ws",
        dispatch_mode="task",
        task_id="t-1a2b3c4d",
        pipeline_id="t-1a2b3c4d",
        task_status="completed",
        task_result={"artifacts": ["report.md"]},
    )


def _task_failed_outcome() -> RunOutcome:
    return RunOutcome(
        container_name="tb-main-1",
        workspace="D:\\ws",
        dispatch_mode="task",
        task_id="t-9f8e7d6c",
        pipeline_id="t-9f8e7d6c",
        task_status="failed",
        task_error="评估提醒耗尽",
    )


def _make_agent(tmp_path: Path) -> AgentOSHarborAgent:
    return AgentOSHarborAgent(logs_dir=tmp_path)


# ── 契约 ────────────────────────────────────────────────────────


class TestContract:
    def test_name_and_version(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path)
        assert AgentOSHarborAgent.name() == "agentos"
        assert agent.version()
        assert agent.capabilities is not None

    def test_options_schema_has_kernel_url(self) -> None:
        schema = AgentOSHarborAgent.options_schema()
        assert "kernel_url" in schema["properties"]
        assert "agent_id" in schema["properties"]
        # 口令不入 schema（防 trajectory 泄密面）
        assert "kernel_admin_password" not in schema["properties"]


# ── run 前置守卫 ────────────────────────────────────────────────


class TestPollTimeoutBudget:
    """R8 兜底链（R19 起位次在 task.toml 之后）：显式 option > env > 缺省。

    本组不供 task.toml 值（task_timeout_s=None）——只验证兜底子链内部
    位次不变。
    """

    def test_explicit_option_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTOS_POLL_TIMEOUT_S", "999")
        assert adapter_mod._resolve_poll_timeout(3600.0) == 3600.0

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTOS_POLL_TIMEOUT_S", "2700")
        assert adapter_mod._resolve_poll_timeout(0) == 2700.0
        assert adapter_mod._resolve_poll_timeout(None) == 2700.0

    def test_default_30min(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTOS_POLL_TIMEOUT_S", raising=False)
        assert adapter_mod._resolve_poll_timeout(0) == 1800.0
        assert adapter_mod._resolve_poll_timeout(-5) == 1800.0
        assert adapter_mod._resolve_poll_timeout(None) == 1800.0


class TestPollTimeoutOptionsDefaultPath:
    """R16：真实路径回归——options 对象缺省值必须把 env 分支留给解析器。

    修前缺陷：Field(default=1800.0) 非零缺省 → `_resolve_poll_timeout` 的
    `explicit > 0` 恒真 → env 分支（AGENTOS_POLL_TIMEOUT_S）在真实 harbor
    用法里永远走不到（env=5400 到达进程但轮询仍 1800 实证）。R8 既有单测
    直接调解析函数传显式值，没覆盖「options 缺省值 → env」这条真实路径
    ——测试绿实战死。
    """

    def test_options_default_zero_defers_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTOS_POLL_TIMEOUT_S", "5400")
        agent = _make_agent(tmp_path)
        # 真实 harbor 形态：BaseAgent 构造 options（无 kwargs）→ Field 缺省
        assert agent.options.poll_timeout_s == 0.0, "缺省必须是 0（未表达），不得短路 env"
        resolved = adapter_mod._resolve_poll_timeout(agent.options.poll_timeout_s)
        assert resolved == 5400.0, "env=5400 必须经解析链生效（修前恒 1800）"

    def test_options_default_env_unset_falls_back_1800(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENTOS_POLL_TIMEOUT_S", raising=False)
        agent = _make_agent(tmp_path)
        assert agent.options.poll_timeout_s == 0.0
        assert adapter_mod._resolve_poll_timeout(agent.options.poll_timeout_s) == 1800.0

    def test_options_explicit_kwargs_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """兜底链内显式值 > env 不回归（task.toml 缺席场景）。"""
        monkeypatch.setenv("AGENTOS_POLL_TIMEOUT_S", "999")
        agent = _make_agent(tmp_path)
        explicit = adapter_mod.AgentOSAgentOptions(poll_timeout_s=3600.0)
        assert explicit.poll_timeout_s == 3600.0
        assert adapter_mod._resolve_poll_timeout(explicit.poll_timeout_s) == 3600.0
        # 对照：缺省对象不被 env 之外的高优先级吞掉（本测试防显式路径回归）
        assert agent.options.poll_timeout_s == 0.0


class TestTaskTomlBudgetSource:
    """R19：轮询预算第一优先源 = 评测基准 task.toml [agent] timeout_sec。

    harbor Trial 构造期写 trial_dir/lock.json（task.path = 任务目录，
    先于 agent setup/run）；适配器经 logs_dir.parent 定位 trial_dir 解析
    同一基准值（harbor trial 对 agent 阶段执法的也是它）——「不要人为拍
    时间，以评测基准自己的配置为准」：option/env 拍定渠道全部降级兜底。
    """

    @staticmethod
    def _write_trial(task_dir: Path, trial_dir: Path, timeout_sec: float | None) -> None:
        """合成 harbor trial 布局：trial_dir/lock.json + task_dir/task.toml。

        timeout_sec=None 时 task.toml 不带 [agent] 节（兜底链场景）。
        """
        trial_dir.mkdir(parents=True, exist_ok=True)
        task_dir.mkdir(parents=True, exist_ok=True)
        lock = {
            "schema_version": 1,
            "task": {
                "name": "layout-config-recreation",
                "type": "local",
                "digest": "sha256:deadbeef",
                "path": str(task_dir),
            },
        }
        (trial_dir / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
        toml = '[task]\nname = "layout-config-recreation"\n'
        if timeout_sec is not None:
            toml += f'\n[agent]\ntimeout_sec = {timeout_sec}\n'
        (task_dir / "task.toml").write_text(toml, encoding="utf-8")

    def test_gate1_toml_value_wins_over_option_and_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """门①：task.toml [agent] timeout_sec=28800 → 解析 28800，
        且压过显式 option 与 env（第一优先源语义）。"""
        task_dir = tmp_path / "task"
        trial_dir = tmp_path / "trial"
        self._write_trial(task_dir, trial_dir, 28800)
        monkeypatch.setenv("AGENTOS_POLL_TIMEOUT_S", "999")
        assert adapter_mod.read_task_toml_agent_timeout_s(trial_dir) == 28800.0
        resolved = adapter_mod._resolve_poll_timeout(
            777.0, adapter_mod.read_task_toml_agent_timeout_s(trial_dir)
        )
        assert resolved == 28800.0

    def test_gate1_no_agent_section_env_then_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """门①：task.toml 无 [agent] 节 → env → 缺省 1800（兜底链生效）。"""
        task_dir = tmp_path / "task"
        trial_dir = tmp_path / "trial"
        self._write_trial(task_dir, trial_dir, None)
        assert adapter_mod.read_task_toml_agent_timeout_s(trial_dir) is None
        monkeypatch.delenv("AGENTOS_POLL_TIMEOUT_S", raising=False)
        assert adapter_mod._resolve_poll_timeout(0, None) == 1800.0
        monkeypatch.setenv("AGENTOS_POLL_TIMEOUT_S", "5400")
        assert adapter_mod._resolve_poll_timeout(0, None) == 5400.0

    @pytest.mark.parametrize(
        ("lock_body", "toml_body", "label"),
        [
            (None, None, "lock.json 缺失"),
            ('{"schema_version": 1}', None, "lock 无 task.path"),
            (
                '{"task": {"path": "<TASK_DIR>"}}',
                None,
                "task.toml 文件缺失",
            ),
            (
                '{"task": {"path": "<TASK_DIR>"}}',
                '[task]\nname = "x"\n',
                "task.toml 无 [agent] 节",
            ),
            (
                '{"task": {"path": "<TASK_DIR>"}}',
                '[agent]\ntimeout_sec = 0\n',
                "timeout_sec 非正数",
            ),
            (
                '{"task": {"path": "<TASK_DIR>"}}',
                '[agent]\ntimeout_sec = "28800"\n',
                "timeout_sec 非数值（字符串）",
            ),
        ],
    )
    def test_task_toml_source_unavailable_returns_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        lock_body: str | None,
        toml_body: str | None,
        label: str,
    ) -> None:
        """任一环缺失/非法 → None（降级兜底链，探针面不拍定兜底值）。"""
        monkeypatch.delenv("AGENTOS_POLL_TIMEOUT_S", raising=False)
        task_dir = tmp_path / "task"
        trial_dir = tmp_path / "trial"
        task_dir.mkdir(parents=True)
        trial_dir.mkdir(parents=True)
        if lock_body is not None:
            (trial_dir / "lock.json").write_text(
                lock_body.replace("<TASK_DIR>", str(task_dir)), encoding="utf-8"
            )
        if toml_body is not None:
            (task_dir / "task.toml").write_text(toml_body, encoding="utf-8")
        assert adapter_mod.read_task_toml_agent_timeout_s(trial_dir) is None, label

    def test_run_passes_task_toml_budget_to_bridge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真实 run 路径接线：trial 布局在场 → 桥收到的 poll_timeout_s =
        task.toml 值（非 env/缺省），预算注记分钟数同源换算。"""
        task_dir = tmp_path / "task"
        trial_dir = tmp_path / "trial"
        self._write_trial(task_dir, trial_dir, 28800)
        _patch_probe(monkeypatch)
        monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "pw")
        monkeypatch.delenv("AGENTOS_POLL_TIMEOUT_S", raising=False)
        bridge = StubBridge(outcome=_task_completed_outcome())
        agent = AgentOSHarborAgent(logs_dir=trial_dir / "agent")
        monkeypatch.setattr(agent, "_make_bridge", lambda: bridge)
        _run(agent.setup(StubEnv()))
        context = AgentContext()
        _run(agent.run("x", StubEnv(), context))
        kind, kwargs = bridge.calls[-2]
        assert kind == "run_task"
        assert kwargs["poll_timeout_s"] == 28800.0, (
            f"桥必须收到 task.toml 预算（兜底值 1800 不得出现）: {kwargs}"
        )
        assert bridge.calls[-1][0] == "unbind"

    def test_budget_note_minutes_follow_resolved_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """注记分钟数与解析值同源：28800s→480min；4580s→76min（四舍五入）。"""
        from kernel_bridge import compose_dispatch_message, harness_time_budget_note

        monkeypatch.delenv("AGENTOS_POLL_TIMEOUT_S", raising=False)
        task_dir = tmp_path / "task"
        trial_dir = tmp_path / "trial"
        self._write_trial(task_dir, trial_dir, 28800)
        resolved = adapter_mod._resolve_poll_timeout(
            0, adapter_mod.read_task_toml_agent_timeout_s(trial_dir)
        )
        assert "about 480 minutes" in harness_time_budget_note(resolved)
        assert "about 76 minutes" in harness_time_budget_note(4580.0)
        # 派发消息形态：原题逐字在前，注记在后（R12 契约不回归）
        message = compose_dispatch_message("do the layout task", resolved)
        assert message.startswith("do the layout task\n\nTime budget:")
        assert "about 480 minutes" in message


class TestRunGuard:
    def test_run_without_setup_explicit_error(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path)
        with pytest.raises(RuntimeError, match="setup"):
            _run(agent.run("instruction", StubEnv(), AgentContext()))


# ── setup ───────────────────────────────────────────────────────


class TestSetup:
    def test_setup_probe_failure_no_bind(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_probe(monkeypatch, name="")  # 触发探针失败
        monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "pw")
        agent = _make_agent(tmp_path)
        calls: list[tuple[str, dict[str, Any]]] = []

        class NoBindBridge(StubBridge):
            def bind_external_container(self, **kwargs: Any) -> dict[str, Any]:
                calls.append(("bind", kwargs))
                return {"success": True}

        def _no_bind_bridge() -> NoBindBridge:
            return NoBindBridge()

        monkeypatch.setattr(agent, "_make_bridge", _no_bind_bridge)
        with pytest.raises(ContainerProbeError):
            _run(agent.setup(StubEnv(hostname_rc=1)))
        # 内核零调用：探针失败发生在绑定之前（fail-loud 于烧 token 前）
        assert calls == []

    def test_setup_success_binds_container(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_probe(monkeypatch)
        monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "pw")
        agent = _make_agent(tmp_path)
        bridge = StubBridge()
        monkeypatch.setattr(agent, "_make_bridge", lambda: bridge)
        _run(agent.setup(StubEnv(hostname="abc123def456")))
        assert len(bridge.calls) == 1
        kind, kwargs = bridge.calls[0]
        assert kind == "bind"
        assert kwargs["container"] == "tb-main-1"
        assert kwargs["workdir"] == "/app"
        # 工作空间真实落盘（guard 悬空挂载防护要求）
        ws = Path(kwargs["workspace"])
        assert ws.is_dir()
        assert ws.is_relative_to(tmp_path)

    def test_setup_adopts_kernel_canonical_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """跨宿主编排（R7）：bind 回传内核侧规范 workspace → 适配器采用之。

        适配器在编排器宿主、内核在另一宿主时，本地 workspace 路径在内核侧
        不存在（guard 悬空挂载预检会拒）——bind 端落内核侧锚并回传规范路径，
        适配器必须用它下发 execution_context，否则 bash 全线 container_
        create_failed（真内核实证）。
        """
        _patch_probe(monkeypatch)
        monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "pw")
        agent = _make_agent(tmp_path)
        kernel_anchor = "D:\\kernel-side\\.ai_workspaces\\external-bind\\tb-abc"
        bridge = StubBridge(bind_result={"workspace": kernel_anchor})
        monkeypatch.setattr(agent, "_make_bridge", lambda: bridge)
        _run(agent.setup(StubEnv()))
        assert agent._workspace == Path(kernel_anchor)
        # setup 阶段只有 bind 一次调用（后续 run 才产生 run_task/unbind）
        assert [k for k, _ in bridge.calls] == ["bind"]


# ── run 两路 ────────────────────────────────────────────────────


def _setup_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bridge: StubBridge) -> AgentOSHarborAgent:
    _patch_probe(monkeypatch)
    monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "pw")
    agent = _make_agent(tmp_path)
    monkeypatch.setattr(agent, "_make_bridge", lambda: bridge)
    _run(agent.setup(StubEnv()))
    return agent


class TestRunPaths:
    def test_completed_populates_context_and_unbinds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bridge = StubBridge(outcome=_ok_outcome())
        agent = _setup_agent(tmp_path, monkeypatch, bridge)
        context = AgentContext()
        _run(agent.run("finish the layout task", StubEnv(), context))
        # context 回填审计坐标
        assert context.metadata["agentos"]["run_status"] == "completed"
        assert context.metadata["agentos"]["pipeline_id"] == "p-1"
        # run_task 收到原样 instruction + 绑定工作空间
        kind, kwargs = bridge.calls[-2]
        assert kind == "run_task"
        assert kwargs["instruction"] == "finish the layout task"
        assert kwargs["agent_id"] == adapter_mod.DEFAULT_EVAL_AGENT_ID
        # finally 解绑定（先 run_task 后 unbind）
        assert bridge.calls[-1][0] == "unbind"

    def test_failed_raises_after_unbind(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bridge = StubBridge(outcome=_failed_outcome())
        agent = _setup_agent(tmp_path, monkeypatch, bridge)
        context = AgentContext()
        with pytest.raises(RuntimeError, match="未正常收敛"):
            _run(agent.run("x", StubEnv(), context))
        # 失败路径仍回填 metadata + 解绑定（run_task → unbind 顺序）
        assert context.metadata["agentos"]["run_status"] == "failed"
        assert bridge.calls[-1][0] == "unbind"
        # 审计面保留失败坐标（trajectory 排查用）
        assert context.metadata["agentos"]["stream_terminal"] == "stream_error"


# ── R17 任务接口派发（缺省通道）────────────────────────────────


class TestTaskDispatch:
    """缺省 task 通道：选项缺省值 / 元数据 / raise 语义 / 指令文件 staging。"""

    def test_default_dispatch_mode_is_task(self, tmp_path: Path) -> None:
        """R17 方向裁定：缺省走任务接口（产品原样派发），session 为显式选项。"""
        agent = _make_agent(tmp_path)
        assert agent.options.dispatch_mode == "task"

    def test_task_completed_metadata_and_unbind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = StubBridge(outcome=_task_completed_outcome())
        agent = _setup_agent(tmp_path, monkeypatch, bridge)
        context = AgentContext()
        _run(agent.run("write config.json", StubEnv(), context))
        kind, kwargs = bridge.calls[-2]
        assert kind == "run_task"
        # 缺省派发参数面：task 通道 + 无指令文件（短题内联）
        assert kwargs["dispatch_mode"] == "task"
        assert kwargs["agent_id"] == adapter_mod.DEFAULT_EVAL_AGENT_ID
        assert kwargs["instruction_file_path"] == ""
        # 元数据 = 任务域分形
        meta = context.metadata["agentos"]
        assert meta["dispatch_mode"] == "task"
        assert meta["task_status"] == "completed"
        assert meta["task_id"] == "t-1a2b3c4d"
        assert meta["task_result"] == {"artifacts": ["report.md"]}
        # finally 解绑定照常
        assert bridge.calls[-1][0] == "unbind"

    def test_task_failed_raises_with_task_coordinates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """task 终态 failed（评估闸门裁决）→ raise，消息带任务坐标归因。"""
        bridge = StubBridge(outcome=_task_failed_outcome())
        agent = _setup_agent(tmp_path, monkeypatch, bridge)
        context = AgentContext()
        with pytest.raises(RuntimeError, match="任务未正常收敛"):
            _run(agent.run("x", StubEnv(), context))
        meta = context.metadata["agentos"]
        assert meta["task_status"] == "failed"
        assert meta["task_error"] == "评估提醒耗尽"
        assert bridge.calls[-1][0] == "unbind"

    def test_invalid_dispatch_mode_explicit_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _setup_agent(tmp_path, monkeypatch, StubBridge(outcome=_task_completed_outcome()))
        agent.options = adapter_mod.AgentOSAgentOptions(dispatch_mode="chat")
        with pytest.raises(RuntimeError, match="dispatch_mode 非法"):
            _run(agent.run("x", StubEnv(), AgentContext()))

    def test_session_mode_option_preserves_legacy_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dispatch_mode=session 显式选项 → WS 会话链路（向后兼容）。"""
        bridge = StubBridge(outcome=_ok_outcome())
        agent = _setup_agent(tmp_path, monkeypatch, bridge)
        agent.options = adapter_mod.AgentOSAgentOptions(dispatch_mode="session")
        context = AgentContext()
        _run(agent.run("x", StubEnv(), context))
        _, kwargs = bridge.calls[-2]
        assert kwargs["dispatch_mode"] == "session"
        assert context.metadata["agentos"]["dispatch_mode"] == "session"
        assert context.metadata["agentos"]["run_status"] == "completed"

    def test_long_instruction_stages_container_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超限原题 → base64 直写容器指令文件，桥收指针路径；内容无损回读。

        task_submit 描述 2000 字硬闸的补偿路径：原题逐字进容器文件（base64
        往返即无损证明），描述只带指针。
        """
        bridge = StubBridge(outcome=_task_completed_outcome())
        agent = _setup_agent(tmp_path, monkeypatch, bridge)
        instruction = "完成布局任务。\n" + "详细步骤 x" * 500  # 远超内联上限
        env = StubEnv()
        context = AgentContext()
        _run(agent.run(instruction, env, context))
        _, kwargs = bridge.calls[-2]
        assert kwargs["dispatch_mode"] == "task"
        assert (
            kwargs["instruction_file_path"]
            == "/workspace/.agentos_task_instruction.md"
        )
        # staging 是唯一 exec 调用（探针在单测被桩替，真 env.exec 只发生在 run 期 staging）
        assert len(env.exec_calls) == 1
        stage_cmd = env.exec_calls[0]
        assert "mkdir -p /workspace" in stage_cmd
        assert "base64 -d > /workspace/.agentos_task_instruction.md" in stage_cmd
        # base64 往返无损：从命令里取编码段解码 == 原题逐字
        token = stage_cmd.split("printf %s ", 1)[1].split(" ", 1)[0]
        assert base64.b64decode(token).decode("utf-8") == instruction
        # 元数据留痕指针模式
        assert (
            context.metadata["agentos"]["instruction_file_path"]
            == "/workspace/.agentos_task_instruction.md"
        )

    def test_short_instruction_skips_staging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """内联形态不写容器文件（run 期零 exec 调用）。"""
        bridge = StubBridge(outcome=_task_completed_outcome())
        agent = _setup_agent(tmp_path, monkeypatch, bridge)
        env = StubEnv()
        _run(agent.run("short task", env, AgentContext()))
        _, kwargs = bridge.calls[-2]
        assert kwargs["instruction_file_path"] == ""
        assert len(env.exec_calls) == 0, "run 期不碰容器（内联无 staging）"

    def test_staging_failure_fails_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """容器写失败（rc!=0）→ 显式失败不静默降级（降级即撞 2000 字闸）。"""
        bridge = StubBridge(outcome=_task_completed_outcome())
        agent = _setup_agent(tmp_path, monkeypatch, bridge)
        instruction = "y" * 3000
        env = StubEnv(stage_exec_rc=1)
        with pytest.raises(RuntimeError, match="指令文件写入容器失败"):
            _run(agent.run(instruction, env, AgentContext()))
        # 失败路径解绑定照常（finally）
        assert bridge.calls[-1][0] == "unbind"

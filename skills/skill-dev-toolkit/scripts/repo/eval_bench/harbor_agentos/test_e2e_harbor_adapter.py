# @feature: FP-0.2.TB评测适配 | @ci: python-coverage
"""B2 全链路 e2e：适配器 → 任务接口建任务 → 任务管道 → 容器内执行 → 终态。

真链路面（非桩）：
- 真内核（spawn_test_kernel scratch 实例，AGENTOS_CONFIG_ROOT 指向临时
  配置副本：llm.yaml 全量改指 stub LLM——test_16_sync_gs1_dual_kernel
  _build_stub_config_root 同款形态，杜绝真实 LLM 外呼）；
- 真任务管道（R17 缺省任务接口派发：POST /ext/task_service/tasks/root 建真
  实任务，出生协议四段式（身份登记/工作空间同步准备/执行派发），task.id
  在场 → task_reminder / 评估闸门 / 失败轮 route 8 原生参与；autonomous
  管道：workspace_lifecycle / environment_lifecycle / agent_config_load /
  isolation_guard 全链在位）；
- 真隔离服务 + 真绑定（isolation.bind_external_env REST → manager 注册表
  → external provider 收养；绑定键 = workspace，setup 期先绑 → 任务管道
  guard 查询即命中，R17 绑定键对齐）；
- 真 bash/fs 工具 → docker exec 打进假任务容器（isolation_guard 注入
  _container_id 的服务端信任链）；
- 真适配器（AgentOSHarborAgent：setup 探测容器 → 绑定 → run 建任务 →
  轮询 task.status → context 回填）。

桩（仅两处，均为外部依赖）：LLM 上游（ScriptedLLMUpstream 脚本化工具调
用）与 Harbor 环境对象（DockerExecEnv：exec 直通 docker exec——与真
harbor 环境同鸭子形状，真 harbor 编排器只跑 Ubuntu 侧，见台账环境限制节）。

判分断言：容器内副作用（proof 文件真实写入任务容器）+ 容器保留 + context
审计坐标 task_status=completed + task.id 在场（pipelines/state）+ 评测
agent 配置实际加载（R10 起 agent=内置代码 agent general_agent 原样）。

R13 用例（改走任务通道）：LLM 失败轮注入（stub 恒 500）→ 回环重试至
duplicate_check 熔断 → task.status=failed 署名。

R17 增第三用例（本轮改向探针）：首轮纯文本（零工具调用）→ task_reminder
提醒注入拉回（修前会话通道此形态直接「无工具调用=完成」假绿）→ 工具执行
→ task_evaluate → task.status=completed 署名。

R19 增第四用例（预算源探针）：合成 trial 布局（lock.json → task.path →
task.toml [agent] timeout_sec=28800）→ dispatch 消息注记分钟数实证 = 480
——轮询预算第一优先源 = 评测基准自己的配置（env/option 兜底源全程不表达）。

门控：docker 与内核二进制任一缺席 → skip 并显式声明环境限制。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "harbor",
    reason="harbor 包未安装（适配器硬依赖；开发机装 harbor 0.26.0 后本车道全跑）",
)
import yaml  # noqa: E402

pytestmark = [pytest.mark.e2e]

_PKG_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PKG_DIR.parents[2]
_TOOLKIT_SCRIPTS = _REPO_ROOT / "skills" / "skill-dev-toolkit" / "scripts"
for _p in (str(_TOOLKIT_SCRIPTS), str(_PKG_DIR), str(_REPO_ROOT / "tests" / "e2e_02")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import adapter as adapter_mod  # noqa: E402
from adapter import AgentOSHarborAgent  # noqa: E402
from harbor.models.agent.context import AgentContext  # noqa: E402
from stub_llm_server import (  # noqa: E402
    ScriptedLLMUpstream,
    error_step,
    text_step,
    tool_call_step,
)

_IMAGE = "python:3.12-alpine"
_PROOF = "agentos-tb-e2e-proof-42"
_MARKER = "TB-E2E-PROOF-42"
# R11 fs 探针常量：绝对路径直通 / 相对路径锚 workdir / 容器内已有文件读回
_FS_PROBE_CONTENT = '{"agentos": "tb-r11", "fs_routing": true}'
_FS_REL_TARGET = "r11_rel_probe.txt"
_FS_REL_PROOF = "r11-relative-anchor-proof"


def _docker_ok() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=20, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _kernel_binary_ok() -> bool:
    for profile in ("release", "debug"):
        if (_REPO_ROOT / "kernel" / "target" / profile / "agentos-kernel.exe").is_file():
            return True
        if (_REPO_ROOT / "kernel" / "target" / profile / "agentos-kernel").exists():
            return True
    return False


DOCKER_OK = _docker_ok()
KERNEL_OK = _kernel_binary_ok()
E2E_SKIP_reason = (
    f"docker={'ok' if DOCKER_OK else 'missing'} / kernel_bin="
    f"{'ok' if KERNEL_OK else 'missing'}（环境限制显式声明，见台账）"
)

pytestmark.append(
    pytest.mark.skipif(not (DOCKER_OK and KERNEL_OK), reason=E2E_SKIP_reason)
)


class DockerExecEnv:
    """Harbor 环境鸭子桩：exec 直通真 docker exec（容器自证真通路）。"""

    def __init__(self, container: str) -> None:
        self._container = container
        self.task_env_config = None  # workdir 缺省 /workspace

    async def exec(self, command: str, timeout_sec: int | None = None) -> Any:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", self._container, "sh", "-c", command],
            capture_output=True,
            timeout=timeout_sec or 15,
            check=False,
        )
        return types.SimpleNamespace(
            return_code=proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )


def _docker(args: list[str], timeout: float = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def task_container() -> Any:
    """假任务容器（裸镜像 + 已知文件），module 级起停。"""
    name = "tb-e2e-adapter-chain"
    _docker(["docker", "rm", "-f", name], timeout=30)
    proc = _docker(
        [
            "docker", "run", "-d", "--name", name, _IMAGE, "sh", "-c",
            "mkdir -p /srv/task && echo known-e2e-anchor > /srv/task/known.txt"
            " && tail -f /dev/null",
        ]
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    yield name
    _docker(["docker", "rm", "-f", name], timeout=30)


@pytest.fixture(scope="module")
def scratch_kernel(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """scratch 内核实例（stub LLM 配置根），module 级起停。"""
    import spawn_test_kernel

    base_dir = tmp_path_factory.mktemp("tb-e2e-kernel")
    stub = ScriptedLLMUpstream()
    stub.start()

    # 1. 临时配置根：仓内 config/ 全量副本 + llm.yaml 全档位改指 stub
    config_root = base_dir / "config"
    shutil.copytree(_REPO_ROOT / "config", config_root)
    # 实例启动配置三根改指仓内插件真源（副本的相对根 ../../plugins/shared
    # 会解析到 tmp 下不存在的目录 → 启动守卫拒启；绝对路径钉回仓内）
    instance_path = config_root / "kernel" / "instance.yaml"
    instance_data = yaml.safe_load(instance_path.read_text(encoding="utf-8")) or {}
    repo_plugins = str((_REPO_ROOT / "plugins" / "shared").resolve())
    instance_data["tier1_root"] = repo_plugins
    instance_data["tier2_root"] = repo_plugins
    instance_data["shared_root"] = repo_plugins
    instance_path.write_text(yaml.safe_dump(instance_data, allow_unicode=True), encoding="utf-8")
    llm_path = config_root / "plugins" / "llm" / "llm.yaml"
    data = yaml.safe_load(llm_path.read_text(encoding="utf-8")) or {}
    data.setdefault("models", {})["stub-llm"] = {
        "api_base": stub.api_base,
        "api_key": "stub-key-tb-e2e",
        "context_window": 128000,
        "display_name": "TB E2E Scripted Stub",
        "model_name": "stub-model",
        "provider": "stub_openai",
    }
    data.setdefault("providers", {})["stub_openai"] = {
        "api_base": stub.api_base,
        "type": "openai",
        "keys": [
            {"api_key": "stub-key-tb-e2e", "id": "stub_main",
             "max_concurrent": 8, "rpm": 600, "token_quota": 0},
        ],
    }
    defaults = data.setdefault("defaults", {})
    defaults["chat"] = "stub-llm"
    tiers = defaults.setdefault("tiers", {})
    tiers.update({"large": "stub-llm", "medium": "stub-llm", "small": "stub-llm"})
    llm_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")

    # 2. 脚本化任务场景：bash 写 proof + fs 族容器路由探针（R11：file_write
    # 绝对路径直通 + 相对路径锚 workdir + file_read 容器内已有文件）→
    # task_evaluate 评估收束（R17 任务通道：评估闸门署名 completed）
    stub.register(
        "tb_e2e_chain",
        _MARKER,
        [
            tool_call_step(
                "bash",
                command=f"echo {_PROOF} > /workspace/proof.txt && cat /srv/task/known.txt",
            ),
            tool_call_step(
                "file_write",
                path="/app/output/config.json",
                content=_FS_PROBE_CONTENT,
            ),
            tool_call_step(
                "file_write",
                path=_FS_REL_TARGET,
                content=_FS_REL_PROOF,
            ),
            tool_call_step("file_read", path="/srv/task/known.txt"),
            tool_call_step(
                "task_evaluate",
                action="auto_complete",
                summary="proof 已写入容器，fs 族容器路由探针完成，交付物在位。",
            ),
            text_step("验收自查通过：proof 已写入 /workspace/proof.txt。"),
        ],
        default=None,
    )

    # 3. spawn 内核（user_root/data 隔离到 tmp，不碰 dev 用户空间）
    record = spawn_test_kernel.spawn(
        _REPO_ROOT, config_root=str(config_root), timeout_s=240.0,
        scratch_root=str(base_dir / ".test_kernel"),
        user_root=str(base_dir / "user"),
        env_extra={"AGENTOS_DATA_DIR": str(base_dir / "data")},
    )
    yield {"record": record, "stub": stub}
    try:
        spawn_test_kernel.stop(
            base_dir / ".test_kernel", str(record.get("run_id", "") or None)
        )
    except Exception as exc:  # noqa: BLE001 — teardown 尽力而为
        print(f"[tb-e2e] 内核停止失败（忽略）: {exc}")
    stub.stop()


@pytest.mark.timeout(600)
def test_full_chain_adapter_session_pipeline_container(
    task_container: str, scratch_kernel: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """适配器全链路（R17 任务接口）：setup 探测+绑定 → 建任务 → 任务管道
    bash/fs 工具在任务容器内落证 → task_evaluate → task.status 终态署名。"""
    record = scratch_kernel["record"]
    stub = scratch_kernel["stub"]
    port = int(record["port"])
    password = str(record["admin_password"])
    monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", password)
    monkeypatch.setenv("AGENTOS_KERNEL_URL", f"http://127.0.0.1:{port}")

    # ── 适配器（真 BaseAgent 子类，真内核桥，真容器探测）──
    logs_dir = tmp_path / "agent-logs"
    logs_dir.mkdir(parents=True)
    agent = AgentOSHarborAgent(logs_dir=logs_dir)
    env = DockerExecEnv(task_container)

    # setup：hostname 自证（真 docker exec）→ docker inspect → 绑定（真 REST）
    # 绑定在 run 之前（R17 键对齐：绑定键=workspace，任务管道 guard 查询即命中）
    _run(agent.setup(env))

    # run：instruction 带 marker（stub LLM 场景命中）→ 任务接口建真实任务 →
    # 任务管道 bash/fs 工具经 _container_id 注入链 docker exec 进任务容器 →
    # task_evaluate → task.status 终态
    context = AgentContext()
    instruction = (
        f"[{_MARKER}] 请完成验收任务：把验收标记写入容器内 /workspace/proof.txt，"
        f"并读取 /srv/task/known.txt 确认任务文件在位。"
    )
    _run(agent.run(instruction, env, context))

    # ── 判据 1：context 审计坐标（任务通道真实收敛）──
    meta = context.metadata["agentos"]
    assert meta["dispatch_mode"] == "task", f"缺省必须走任务接口: {meta}"
    assert meta["task_status"] == "completed", f"任务未收敛: {meta}"
    assert meta["task_id"], meta
    assert meta["pipeline_id"] == meta["task_id"], "任务即管道（pipeline_id=task_id）"

    # ── 判据 1b（R17 门②）：task.id 在场——任务管道 state 实证 ──
    client = _kernel_client(record)
    rows = client.list_pipeline_states()
    task_row = next(
        (r for r in rows if r.get("pipeline_id") == meta["task_id"]), None
    )
    assert task_row is not None, (
        f"pipelines/state 应有任务管道行 task={meta['task_id']}，"
        f"实有 {[r.get('pipeline_id') for r in rows]}"
    )
    task_state = task_row.get("state") or {}
    assert str(task_state.get("task.id") or "") == meta["task_id"], (
        f"任务管道 state.task.id 必须在场（task_reminder/评估闸门/route 8 "
        f"的参与前提）: {sorted(k for k in task_state if str(k).startswith('task'))}"
    )

    # ── 判据 2：容器内副作用（bash 工具真打进任务容器）──
    cat = _docker(["docker", "exec", task_container, "cat", "/workspace/proof.txt"])
    assert cat.returncode == 0, (
        f"proof 读取失败: rc={cat.returncode} out={cat.stdout!r} err={cat.stderr!r}"
    )
    assert _PROOF in cat.stdout.decode(), (
        f"proof 内容不符: out={cat.stdout!r} err={cat.stderr!r}"
    )

    # ── 判据 2b（R11 双门②）：fs 族容器路由——file_write 绝对路径直通 ──
    fs_cat = _docker(["docker", "exec", task_container, "cat", "/app/output/config.json"])
    assert fs_cat.returncode == 0, (
        f"file_write /app/output/config.json 未落容器: rc={fs_cat.returncode} "
        f"out={fs_cat.stdout!r} err={fs_cat.stderr!r}"
    )
    assert fs_cat.stdout.decode() == _FS_PROBE_CONTENT, (
        f"file_write 内容不符（读回 vs 写入）: {fs_cat.stdout!r} != {_FS_PROBE_CONTENT!r}"
    )

    # ── 判据 2c（R11 双门②）：file_write 相对路径锚绑定 workdir（/workspace）──
    rel_cat = _docker(["docker", "exec", task_container, "cat", f"/workspace/{_FS_REL_TARGET}"])
    assert rel_cat.returncode == 0, (
        f"file_write 相对路径未锚 workdir: rc={rel_cat.returncode} "
        f"out={rel_cat.stdout!r} err={rel_cat.stderr!r}"
    )
    assert rel_cat.stdout.decode() == _FS_REL_PROOF, (
        f"相对路径写入内容不符: {rel_cat.stdout!r} != {_FS_REL_PROOF!r}"
    )

    # ── 判据 2d（R11 双门②）：file_read 读容器内已有文件返回正确内容 ──
    # 证据 1：stub LLM 消息面出现 role=tool 且含文件内容（LLM 可见面）。
    fr_hits = [c for c in _tool_message_contents(stub) if "known-e2e-anchor" in c]
    assert fr_hits, (
        "file_read 内容未出现在 LLM 消息面"
        f"（样例: {_tool_message_contents(stub)[:3]!r}）"
    )
    # 证据 2：内核 blob 库的 tool_result 信封携带收养容器名 + 正确内容
    # （容器侧读取的服务端可证面；LLM 线格式不透传容器字段）。
    import sqlite3

    con = sqlite3.connect(f"file:{record['db']}?mode=ro", uri=True)
    blob_rows = con.execute("SELECT data FROM blobs WHERE data LIKE '%file_read%'").fetchall()
    con.close()
    envelope_hits = [
        str(r[0])
        for r in blob_rows
        if task_container in str(r[0]) and "known-e2e-anchor" in str(r[0])
    ]
    assert envelope_hits, (
        f"file_read 结果信封未携带收养容器名（blob 样例: {[str(r[0])[:200] for r in blob_rows[:3]]!r}）"
    )

    # ── 判据 3：容器保留（销毁面不删外部容器，verifier 判分对象在位）──
    inspect = _docker(["docker", "inspect", "-f", "{{.State.Status}}", task_container])
    assert inspect.stdout.decode().strip() == "running"

    # ── 判据 4（R10 方向变更后）：评测 agent = 内置代码 agent 原样（工具面
    # 含 bash 与 fs 族；R11 起 fs 族与 bash 同构路由进收养容器）──
    tool_names = _captured_tool_names(stub)
    assert "bash" in tool_names, f"bash 未入工具面: {tool_names}"
    assert "file_write" in tool_names, f"file_write 未入工具面: {tool_names}"
    assert "file_read" in tool_names, f"file_read 未入工具面: {tool_names}"

    # ── 判据 5（R12，任务通道形态）：任务书（kickoff 任务描述）= 原题逐字
    # 在前 + 时间预算注记在后；注记分钟数与轮询预算同源（缺省 1800s → 30）。
    user_msgs = _captured_user_messages(stub)
    assert user_msgs, "LLM 请求消息面未出现 user 消息"
    first_user = user_msgs[0]
    assert instruction in first_user, (
        f"原题必须逐字嵌入任务描述（kickoff 头 300 字: {first_user[:300]!r}）"
    )
    assert "Time budget: you have about 30 minutes in total for this task." in first_user, (
        f"时间预算注记缺失或分钟数不符（user 消息尾 400 字: {first_user[-400:]!r}）"
    )
    assert "write the final deliverable EARLY" in first_user, "产出纪律句缺失"

    # 清理：解绑定（真 REST，幂等；unbind 是同步方法）
    agent._bridge.unbind_external_container(workspace=str(agent._workspace))


@pytest.mark.timeout(300)
def test_llm_fail_rounds_retry_loop_and_budget_cap(
    task_container: str, scratch_kernel: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R13 回环语义（R17 起改走任务通道）+ 适配器预算封顶：LLM 失败轮真实
    回环重试（不静默收官），适配器轮询预算耗尽如实判失败（trial 判负）。

    修前缺陷（TB 终跑 887b8135f4c5 实证）：会话通道 llm 轮调用失败后兜底
    end 直接收官 → completed 假绿。修后（R13 + 任务接口）：task.id 在场，
    失败轮经 autonomous.yaml 回环路由反复重试——stub 被打 ≥ 3 次即回环在
    跑的直接证据；适配器侧 poll_timeout_s 预算封顶后返回非终态快照并 raise。

    注：duplicate_check 熔断署名（llm_fail_loop/model_unavailable）依赖失败
    指纹跨轮稳定，当前内核在两通道下均观察到每轮指纹漂移（连击恒 1、熔断
    不可达，任务无限回环）——内核侧既有缺陷，不属适配器改动面，本用例不
    断言熔断终态（台账 R17 留档）。适配器预算封顶保证 trial 不挂死。
    """
    record = scratch_kernel["record"]
    stub = scratch_kernel["stub"]
    port = int(record["port"])
    password = str(record["admin_password"])
    monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", password)
    monkeypatch.setenv("AGENTOS_KERNEL_URL", f"http://127.0.0.1:{port}")

    # 全程失败脚本：每次 LLM 请求都打 500（错误文本恒定）
    fail_step = error_step(500, "stub upstream exploded (r13 breaker probe)")
    stub.register(
        "tb_e2e_llm_fail",
        _LLM_FAIL_MARKER,
        [],
        default=fail_step,
    )

    logs_dir = tmp_path / "agent-logs-llmfail"
    logs_dir.mkdir(parents=True)
    agent = AgentOSHarborAgent(logs_dir=logs_dir)
    # 预算封顶：任务通道轮询 task.status 30s 后返回快照（熔断内核侧不署名时
    # 任务恒 running，适配器不得陪跑挂死——trial 面预算即护栏）
    agent.options = adapter_mod.AgentOSAgentOptions(
        dispatch_mode="task", poll_timeout_s=30.0
    )
    env = DockerExecEnv(task_container)
    _run(agent.setup(env))

    context = AgentContext()
    instruction = (
        f"[{_LLM_FAIL_MARKER}] 请分析任务（本场景 LLM 上游恒 500，管道应重试）。"
    )
    # 适配器契约：非 completed 快照 → RuntimeError（trial 判失败）
    with pytest.raises(RuntimeError, match="任务未正常收敛"):
        _run(agent.run(instruction, env, context))

    meta = context.metadata["agentos"]
    # 判据 1：任务通道 + 快照如实回报（非终态 running，不谎报终局）
    assert meta["dispatch_mode"] == "task"
    assert meta["task_status"] == "running", f"快照应如实回报 running: {meta}"
    assert meta["task_id"], meta

    # 判据 2：失败轮真实回环重试（stub 被打 ≥ 3 次；回环路由丢失即退化为
    # 首失败收官 completed 假绿——R13 修的正是这个）
    hits = stub.request_count("tb_e2e_llm_fail")
    assert hits >= 3, (
        f"失败轮未回环重试（stub 请求 {hits} 次；回环路由丢失即首失败收官）"
    )

    # 清理：解绑定（真 REST，幂等）
    agent._bridge.unbind_external_container(workspace=str(agent._workspace))


@pytest.mark.timeout(600)
def test_text_only_turn_pulled_back_by_task_reminder(
    task_container: str, scratch_kernel: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R17 改向探针（本轮根因的终跑形态回放）：首轮纯文本（零工具调用）不
    再静默完成——task_reminder 提醒注入拉回 → 工具执行 → task_evaluate →
    task.status=completed 署名。

    修前形态（pipeline 41fb0a08b7ac，MSG_COUNT=2）：会话通道 user 原题 +
    assistant 一条纯文本 → 「无工具调用=完成」产品语义收敛 → run completed
    假绿，交付物从未尝试。R17 任务接口派发后：task.id 在场 → task_reminder
    三信号检测（纯文本 + 未完成 + 无挂号）→ 注入提醒 → 模型被拉回干活；
    提醒耗尽仍不干活 → 任务 failed（闸门署名），两条路都不是假绿。
    """
    record = scratch_kernel["record"]
    stub = scratch_kernel["stub"]
    port = int(record["port"])
    password = str(record["admin_password"])
    monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", password)
    monkeypatch.setenv("AGENTOS_KERNEL_URL", f"http://127.0.0.1:{port}")

    # 脚本：第 1 轮纯文本（零工具调用——修前在此假绿收官）→ 第 2 轮 bash
    # 落证 → 第 3 轮 task_evaluate → 收尾文本。提醒若未注入，脚本第 2 轮
    # 不会被执行（管道已 end）。
    stub.register(
        "tb_e2e_textonly",
        _TEXT_ONLY_MARKER,
        [
            text_step("任务我已分析完毕，一切就绪（本轮纯文本，无任何工具调用）。"),
            tool_call_step(
                "bash",
                command=f"echo {_TEXT_ONLY_PROOF} > /workspace/proof_textonly.txt",
            ),
            tool_call_step(
                "task_evaluate",
                action="auto_complete",
                summary="proof 文件已写入容器，任务完成。",
            ),
            text_step("执行与评估均完成。"),
        ],
        default=None,
    )

    logs_dir = tmp_path / "agent-logs-textonly"
    logs_dir.mkdir(parents=True)
    agent = AgentOSHarborAgent(logs_dir=logs_dir)
    env = DockerExecEnv(task_container)
    _run(agent.setup(env))

    context = AgentContext()
    instruction = (
        f"[{_TEXT_ONLY_MARKER}] 请完成验收任务：把标记写入容器内 "
        f"/workspace/proof_textonly.txt。"
    )
    _run(agent.run(instruction, env, context))

    meta = context.metadata["agentos"]
    # 判据 1：终态 completed 且任务通道
    assert meta["dispatch_mode"] == "task"
    assert meta["task_status"] == "completed", f"纯文本轮拉回后未收敛: {meta}"
    task_id = str(meta["task_id"])

    # 判据 2：纯文本轮之后管道继续（stub 被打 ≥ 3 轮：text → bash →
    # evaluate；修前形态在第 1 轮即 end，后续轮不存在）
    hits = stub.request_count("tb_e2e_textonly")
    assert hits >= 3, (
        f"纯文本轮未拉回（stub 仅 {hits} 轮；task_reminder 未注入即退化为"
        f"首轮收官）"
    )

    # 判据 3：提醒真实注入——后续 LLM 请求消息面含 task_reminder 文案
    # （executor 提醒头「【系统提醒」——模型可见的拉回指令）。
    reminder_hits = [
        body
        for body in stub.requests("tb_e2e_textonly")
        if "【系统提醒" in str(body.get("messages", ""))
    ]
    assert reminder_hits, (
        "task_reminder 提醒未出现在 LLM 请求消息面（提醒机制未参与）"
    )

    # 判据 4：任务域证据——state.task.id 在场 + evaluate_reminder_count ≥ 1
    # （提醒额度记账；该键在 state 出口属诊断性，缺省时提醒参与已由判据 3
    # 消息面证明——tests/e2e_02 A3 同款处理）。
    client = _kernel_client(record)
    row = next(
        (r for r in client.list_pipeline_states() if r.get("pipeline_id") == task_id),
        None,
    )
    assert row is not None, f"任务管道行缺失 task={task_id}"
    task_state = row.get("state") or {}
    assert str(task_state.get("task.id") or "") == task_id, "state.task.id 必须在场"
    reminder_count = task_state.get("evaluate_reminder_count")
    if reminder_count is not None:
        assert int(reminder_count) >= 1, (
            f"evaluate_reminder_count 应 ≥ 1（提醒记账），实际 {reminder_count!r}"
        )

    # 判据 5：拉回后工具真实执行——容器内 proof 文件在位
    cat = _docker(["docker", "exec", task_container, "cat", "/workspace/proof_textonly.txt"])
    assert cat.returncode == 0, (
        f"proof 读取失败: rc={cat.returncode} out={cat.stdout!r} err={cat.stderr!r}"
    )
    assert _TEXT_ONLY_PROOF in cat.stdout.decode()

    # 清理：解绑定（真 REST，幂等）
    agent._bridge.unbind_external_container(workspace=str(agent._workspace))


_LLM_FAIL_MARKER = "TB-E2E-LLMFAIL-77"
_TEXT_ONLY_MARKER = "TB-E2E-TEXTONLY-88"
_TEXT_ONLY_PROOF = "agentos-tb-textonly-proof-88"


def _kernel_client(record: dict[str, Any]) -> Any:
    """scratch 内核 REST 客户端（state 聚合读面用；登录即带令牌）。"""
    from kernel_client import KernelClient

    client = KernelClient(f"http://127.0.0.1:{int(record['port'])}")
    client.login("admin", str(record["admin_password"]))
    return client


def _captured_tool_names(stub: ScriptedLLMUpstream) -> list[str]:
    """从 stub LLM 收到的请求体提取工具名全集（agent 配置加载的可证断言）。"""
    names: set[str] = set()
    for body in stub.requests("tb_e2e_chain"):
        tools = body.get("tools")
        if isinstance(tools, list):
            for t in tools:
                if isinstance(t, dict):
                    fn = t.get("function") or t
                    name = fn.get("name") or fn.get("tool_name")
                    if isinstance(name, str):
                        names.add(name)
    return sorted(names)


def _captured_user_messages(stub: ScriptedLLMUpstream) -> list[str]:
    """从 stub LLM 收到的请求体提取全部 role=user 消息文本（派发面可证）。"""
    contents: list[str] = []
    for body in stub.requests("tb_e2e_chain"):
        messages = body.get("messages")
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    contents.append(content)
    return contents


def _tool_message_contents(stub: ScriptedLLMUpstream) -> list[str]:
    """从 stub LLM 收到的请求消息面提取全部 role=tool 结果文本。

    内核把工具结果以 role=tool 消息回灌 LLM（多轮请求体）；消息 name 字段
    经内核格式化层不保留工具名（真跑实证恒 "unknown"），故按内容匹配。
    """
    contents: list[str] = []
    for body in stub.requests("tb_e2e_chain"):
        messages = body.get("messages")
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "tool":
                contents.append(str(msg.get("content") or ""))
    return contents


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_R19_MARKER = "TB-E2E-TASKTOML-99"
_R19_PROOF = "agentos-tb-tasktoml-proof-99"


@pytest.mark.timeout(600)
def test_poll_budget_note_sourced_from_task_toml(
    task_container: str, scratch_kernel: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R19 门②：轮询预算第一优先源 = 评测基准 task.toml [agent] timeout_sec。

    合成 harbor trial 布局（lock.json → task.path → task.toml 28800s，
    layout-config-recreation 同值形态）→ 真内核 dispatch → LLM 请求消息面
    实证注记分钟数 = 480（28800/60）。env / option 两兜底源全程不表达
    （delenv + options 缺省 0）——「不要人为拍时间，以评测基准自己的配置
    为准」的端到端证据。
    """
    record = scratch_kernel["record"]
    stub = scratch_kernel["stub"]
    port = int(record["port"])
    password = str(record["admin_password"])
    monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", password)
    monkeypatch.setenv("AGENTOS_KERNEL_URL", f"http://127.0.0.1:{port}")
    # 兜底源不表达：env 显式拔掉、option 用缺省 0 → 预算只能来自 task.toml
    monkeypatch.delenv("AGENTOS_POLL_TIMEOUT_S", raising=False)

    stub.register(
        "tb_e2e_tasktoml",
        _R19_MARKER,
        [
            tool_call_step(
                "bash",
                command=f"echo {_R19_PROOF} > /workspace/proof_tasktoml.txt",
            ),
            tool_call_step(
                "task_evaluate",
                action="auto_complete",
                summary="轮询预算源探针完成，proof 已落容器。",
            ),
            text_step("完成。"),
        ],
        default=None,
    )

    # 合成 trial 布局（harbor Trial 构造期同款）：trial_dir/lock.json 的
    # task.path 指向任务目录；task.toml [agent] timeout_sec = 28800；
    # agent logs_dir = trial_dir/agent（harbor 编排同形，适配器据此定位）。
    task_dir = tmp_path / "layout-config-recreation"
    trial_dir = tmp_path / "trial"
    logs_dir = trial_dir / "agent"
    logs_dir.mkdir(parents=True)
    task_dir.mkdir(parents=True)
    lock = {
        "schema_version": 1,
        "task": {
            "name": "layout-config-recreation",
            "type": "local",
            "digest": "sha256:r19-probe",
            "path": str(task_dir),
        },
    }
    (trial_dir / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (task_dir / "task.toml").write_text(
        '[task]\nname = "layout-config-recreation"\n\n[agent]\ntimeout_sec = 28800\n',
        encoding="utf-8",
    )

    agent = AgentOSHarborAgent(logs_dir=logs_dir)
    env = DockerExecEnv(task_container)
    _run(agent.setup(env))

    context = AgentContext()
    instruction = (
        f"[{_R19_MARKER}] 请完成验收任务：把标记写入容器内 "
        f"/workspace/proof_tasktoml.txt。"
    )
    _run(agent.run(instruction, env, context))

    meta = context.metadata["agentos"]
    assert meta["dispatch_mode"] == "task"
    assert meta["task_status"] == "completed", f"任务未收敛: {meta}"

    # 门②判据：kickoff 任务描述（LLM 请求消息面 user 消息）注记分钟数
    # = 480（28800s / 60）——该值只能来自 task.toml（env/option 未表达，
    # 缺省 1800 → 30 min，兜底值出现即证第一优先源失效）。
    user_msgs = [
        str(msg.get("content"))
        for body in stub.requests("tb_e2e_tasktoml")
        if isinstance(body.get("messages"), list)
        for msg in body["messages"]
        if isinstance(msg, dict) and msg.get("role") == "user" and isinstance(msg.get("content"), str)
    ]
    assert user_msgs, "LLM 请求消息面未出现 user 消息"
    first_user = user_msgs[0]
    assert instruction in first_user, (
        f"原题逐字缺失（头 300 字: {first_user[:300]!r}）"
    )
    assert (
        "Time budget: you have about 480 minutes in total for this task."
        in first_user
    ), (
        f"注记分钟数非 480（task.toml 第一优先源未生效；尾 400 字: "
        f"{first_user[-400:]!r}）"
    )

    # 链路活性旁证：proof 真实进任务容器
    cat = _docker(
        ["docker", "exec", task_container, "cat", "/workspace/proof_tasktoml.txt"]
    )
    assert cat.returncode == 0, (
        f"proof 读取失败: rc={cat.returncode} out={cat.stdout!r} err={cat.stderr!r}"
    )
    assert _R19_PROOF in cat.stdout.decode(), f"proof 内容不符: {cat.stdout!r}"

    # 清理：解绑定（真 REST，幂等）
    agent._bridge.unbind_external_container(workspace=str(agent._workspace))

# @feature: FP-0.2.〇 任务收束三信号闸门 路径矩阵 | @vision: V2 全能闭环 | @ci: python-e2e
"""
E2E 路径矩阵：任务执行闭环（ADR 2026-08-28-task-closure-three-signal-gate 测试范式）。

零 mock、输入条件驱动的路径矩阵：LLM 是唯一外部依赖，用脚本化 OpenAI 兼容
上游替身（stub_llm_server.py）注入——内核自带实例（:memory: DB + 独立端口 +
测试专属临时配置根），stub 以 provider 配置进入该实例的 models/llm.yaml，
共享配置文件零改动。断言全部走黑盒观察面：

  - GET /api/v1/pipelines/state 的 task.status / track.llm_usage 等聚合 state；
  - GET /api/v1/tools（工具面在位性）；
  - GET /api/v1/sessions/{id}/messages（会话消息，C2 唤醒链）；
  - llm_usage 全 0 = 假跑防呆断言（每次路径验证都要求 LLM 真实参与）。

路径矩阵（输入条件 → 期望终态）：
  A1 无指标任务 + bash 成功 + task_evaluate   → completed（核心回归）
  A2 有指标任务（file_check 全过，经 L1 task_submit 派发）→ 子任务 completed
  A3 bash 成功但 agent 从不评估（纯文本×N）   → 提醒耗尽 → failed
  B1 bash 真失败（exit != 0）且从不评估       → 合法 failed
  C1 连续调用不存在的工具（同工具同参重复）  → duplicate_check 硬上限 → 有界轮数终态 failed
  C2 子任务终态                               → 父会话唤醒收束出文本
  D1 评估提交但指标不过（文件不存在）         → 重试耗尽 → 有界 failed（不无限评估）
  D2 评估失败任务 failed → submit 重跑        → 修复后评估通过 → completed
  S2 指标缺必填 input_params                  → 提交期拒绝（INVALID_METRIC_PARAMS）→ 修正重提成功
  S1 运行中 stop_generation                   → 循环有界停止（不无限执行）
  R1 完成率混合批：8 任务并发（6 成功 + 2 设计失败）→ 逐任务结局映射 + 聚合完成率 6/8

真实 LLM 冒烟车道不在此文件（保留 test_12 的 skipif 门禁形态）；本套件为
脚本车道，无需任何 API key。

运行方式：
  python -m pytest tests/e2e_02/test_task_lifecycle_paths_e2e.py -q
前提：kernel/target/release/agentos-kernel.exe 已构建；本机 Docker 可用
（任务默认隔离执行，bash 走容器）。内核端口随机分配，不占用 9100。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from e2e_helpers import (
    http_delete_auth,
    http_get_with_auth,
    http_post_json,
    http_post_json_auth,
)
from stub_llm_server import ScriptedLLMUpstream, text_step, tool_call_step

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_KERNEL_EXE = os.path.join(_REPO_ROOT, "kernel", "target", "release", "agentos-kernel.exe")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.path.exists(_KERNEL_EXE),
        reason="需要内核二进制 kernel/target/release/agentos-kernel.exe（先 cargo build --release）",
    ),
]

# 轮询/等待窗口（秒）
_BOOT_WAIT_SECONDS = 300
_TERMINAL_WAIT_SECONDS = 360
_WAKE_WAIT_SECONDS = 300
_POLL_INTERVAL_SECONDS = 3
_CHAT_TIMEOUT_SECONDS = 300

# 场景 marker：出现在任务描述/聊天消息里，stub 按注册序做首个命中匹配。
# 父场景必须先于子场景注册——父请求的消息历史（含 task_submit 参数回显）
# 同时含父/子 marker，注册序即优先级。marker 之间不得互为子串（stub 校验）。
_M_A1 = "E2E-MTX-A1"
_M_A2P = "E2E-MTX-A2P"
_M_A2C = "E2E-MTX-A2C"
_M_A3 = "E2E-MTX-A3"
_M_B1 = "E2E-MTX-B1"
_M_C1 = "E2E-MTX-C1"
_M_C2P = "E2E-MTX-C2P"
_M_C2C = "E2E-MTX-C2C"
_M_D1P = "E2E-MTX-D1P"
_M_D1C = "E2E-MTX-D1X"
_M_D2P = "E2E-MTX-D2P"
_M_D2C = "E2E-MTX-D2X"
_M_S2P = "E2E-MTX-S2P"
_M_S2C = "E2E-MTX-S2X"
_M_S1 = "E2E-MTX-S1"
_M_P1P = "E2E-MTX-P1P"
_M_P1C = "E2E-MTX-P1C"

# 完成率混合批（R1）marker：S1..S6 成功设计（bash+task_evaluate）/ F1 bash 真
# 失败从不评估 / F2 bash 成功但从不评估（提醒耗尽）。与路径矩阵分属不同用例
# （autouse 先 reset stub），仅本批内部校验子串不重叠。
_M_RATE_S = [f"E2E-RATE-S{i}" for i in range(1, 7)]
_M_RATE_F1 = "E2E-RATE-F1"
_M_RATE_F2 = "E2E-RATE-F2"

_BASH_ECHO_OK = "echo E2E_BASH_OK > result.txt"


# ============================================================
# 自带内核实例（session 级：boot 一次，全矩阵复用）
# ============================================================


class StubKernel:
    """自带内核实例句柄：进程 + 独立端口 + 测试专属临时配置根。"""

    def __init__(self, proc: subprocess.Popen, port: int, base_dir: str) -> None:
        self.proc = proc
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.base_dir = base_dir

    def token(self) -> str:
        status, body, _ = http_post_json(
            f"{self.url}/api/v1/auth/login",
            {"username": "admin", "password": "admin12345"},
            timeout=15,
        )
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise RuntimeError(f"内核登录失败: status={status}, body={body}")
        return str(body["access_token"])


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build_tmp_config_root(base_dir: str, stub_api_base: str) -> str:
    """复制共享 config/ 到测试专属临时目录并改写 llm.yaml 注入 stub provider。

    硬约束兑现：运行中内核消费的共享配置文件零改动——内核实例的
    AGENTOS_CONFIG_ROOT 指向本副本，defaults（chat/tiers/compression/embedding）
    全部指向 stub 模型，杜绝任何真实 LLM/embedding 外呼。
    """
    src = os.path.join(_REPO_ROOT, "config")
    dst = os.path.join(base_dir, "config")
    shutil.copytree(src, dst)

    llm_path = os.path.join(dst, "models", "llm.yaml")
    with open(llm_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    data.setdefault("models", {})["stub-llm"] = {
        "api_base": stub_api_base,
        "api_key": "stub-key-e2e",
        "context_window": 128000,
        "display_name": "E2E Scripted Stub",
        "model_name": "stub-model",
        "provider": "stub_openai",
    }
    data["models"]["stub-embedding"] = {
        "api_base": stub_api_base,
        "api_key": "stub-key-e2e",
        "context_window": 8192,
        "dimension": 8,
        "model_name": "stub-embedding",
        "provider": "stub_openai",
    }
    data.setdefault("providers", {})["stub_openai"] = {
        "api_base": stub_api_base,
        "type": "openai",
        "keys": [
            {
                "api_key": "stub-key-e2e",
                "id": "stub_main",
                "max_concurrent": 8,
                "rpm": 600,
                "token_quota": 0,
            }
        ],
    }
    defaults = data.setdefault("defaults", {})
    defaults["chat"] = "stub-llm"
    defaults["compression"] = "stub-llm"
    defaults["embedding"] = "stub-embedding"
    tiers = defaults.setdefault("tiers", {})
    tiers["large"] = "stub-llm"
    tiers["medium"] = "stub-llm"
    tiers["small"] = "stub-llm"

    with open(llm_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=True)
    return dst


def _wait_health(url: str, proc: subprocess.Popen, log_path: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-4000:]
            raise RuntimeError(f"内核进程提前退出 rc={proc.returncode}，日志尾部：\n{tail}")
        try:
            status, _, _ = http_get_with_auth(f"{url}/health", timeout=3)
            if status == 200:
                return
        except Exception as exc:  # noqa: BLE001 —— 启动期连接失败属预期，轮询重试
            last_error = exc
        time.sleep(2)
    raise RuntimeError(f"内核 {timeout}s 内未就绪（最后错误: {last_error}）")


@pytest.fixture(scope="session")
def stub_llm():
    upstream = ScriptedLLMUpstream()
    upstream.start()
    yield upstream
    upstream.stop()


@pytest.fixture(scope="session")
def stub_kernel(stub_llm):
    """启动自带内核：:memory: DB + 随机端口 + 临时配置根（stub provider 注入）。"""
    base_dir = tempfile.mkdtemp(prefix="agentos-e2e-matrix-")
    config_root = _build_tmp_config_root(base_dir, stub_llm.api_base)
    port = _free_port()
    log_path = os.path.join(base_dir, "kernel.log")

    env = os.environ.copy()
    env.update(
        {
            "AGENTOS_KERNEL_PORT": str(port),
            "AGENTOS_KERNEL_HOST": "127.0.0.1",
            "AGENTOS_CONFIG_ROOT": config_root,
            "AGENTOS_DB_PATH": ":memory:",
            "AGENTOS_PLUGINS_DIR": os.path.join(_REPO_ROOT, "plugins", "shared"),
            # 本内核无监督者：cdylib 集合变更（并行会话可能触发）不自动退出
            "AGENTOS_AUTO_RESTART_ON_CDYLIB_CHANGE": "0",
            # debug 日志：停止链路（dispatch_stop 无 run 分支是 debug 级）可诊断
            "RUST_LOG": "info,agentos_api=debug,agentos_engine=debug",
            # 隔离环境上限放宽：全矩阵连跑时前序场景容器销毁滞后（clear-all
            # best-effort），默认 6/6 会让后续场景 bash 被隔离守卫拦截；
            # 完成率混合批（R1）8 任务并发各占环境，放宽到 16 留余量
            "AO_MAX_ENVIRONMENTS": "16",
        }
    )
    with open(log_path, "w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            [_KERNEL_EXE],
            cwd=_REPO_ROOT,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        kernel = StubKernel(proc, port, base_dir)
        try:
            _wait_health(kernel.url, proc, log_path, _BOOT_WAIT_SECONDS)
            yield kernel
        finally:
            if proc.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
        # 内核日志留档（路径失败归因需要；reports/ 为运行时产物目录）
        reports_dir = os.path.join(_REPO_ROOT, "reports")
        os.makedirs(reports_dir, exist_ok=True)
        kept_log = os.path.join(reports_dir, f"e2e_matrix_kernel_{port}.log")
        shutil.copyfile(log_path, kept_log)
        print(f"[matrix] 内核日志已留档: {kept_log}")
    shutil.rmtree(base_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def matrix_token(stub_kernel):
    return stub_kernel.token()


@pytest.fixture(autouse=True)
def _matrix_hygiene(stub_kernel, matrix_token, stub_llm):
    """用例间隔离：请求前清 stub 脚本；用例后清执行数据（best-effort）。"""
    stub_llm.reset()
    yield
    try:
        http_post_json_auth(
            f"{stub_kernel.url}/ext/monitoring/execution/records/clear-all",
            {},
            token=matrix_token,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 —— teardown 尽力而为
        print(f"[matrix-cleanup] clear-all 失败（忽略）: {exc}")


@pytest.fixture
def matrix_sessions(stub_kernel, matrix_token):
    """会话登记与 teardown 删除（chat 车道用例用）。"""
    created: list[str] = []

    def _register(session_id: str) -> str:
        created.append(session_id)
        return session_id

    yield _register
    for sid in created:
        try:
            http_delete_auth(
                f"{stub_kernel.url}/api/v1/sessions/{sid}", token=matrix_token, timeout=15
            )
        except Exception as exc:  # noqa: BLE001 —— teardown 尽力而为
            print(f"[matrix-cleanup] 删除会话失败（忽略）: {sid} | {exc}")


# ============================================================
# 黑盒观察助手（只走 HTTP 观察面）
# ============================================================


def _poll_until(fn: Callable[[], Any], timeout: float, interval: float = _POLL_INTERVAL_SECONDS):
    """轮询 fn 直到返回真值或超时（超时返回 None）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def _state_rows(kernel: StubKernel, token: str) -> list[dict[str, Any]]:
    status, body, _ = http_get_with_auth(
        f"{kernel.url}/api/v1/pipelines/state", token=token, timeout=10
    )
    if status != 200 or not isinstance(body, dict):
        return []
    return [row for row in body.get("items", []) if isinstance(row, dict)]


def _row_by_pipeline(rows: list[dict[str, Any]], pipeline_id: str) -> dict[str, Any] | None:
    return next((r for r in rows if r.get("pipeline_id") == pipeline_id), None)


def _row_by_marker(rows: list[dict[str, Any]], marker: str) -> dict[str, Any] | None:
    """按任务描述/目标里的场景 marker 找任务管道行（子任务 id 的黑盒获取方式）。"""
    for row in rows:
        state = row.get("state") or {}
        blob = json.dumps(
            [state.get("task.goal"), state.get("task.description")], ensure_ascii=False
        )
        if marker in blob:
            return row
    return None


def _state(row: dict[str, Any] | None) -> dict[str, Any]:
    return (row or {}).get("state") or {}


def _llm_usage(state: dict[str, Any]) -> dict[str, Any]:
    usage = state.get("track.llm_usage") or {}
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except json.JSONDecodeError:
            usage = {}
    return usage if isinstance(usage, dict) else {}


def _assert_real_llm_ran(
    kernel: StubKernel, token: str, task_id: str, state: dict[str, Any], context: str
) -> None:
    """假跑防呆：llm_usage 全 0 = LLM 未真实参与，路径验证无效。

    state 聚合行的键面随数据源（内存热行 / checkpoint 冷行）浮动，终态行可能
    瞬时缺 track.*——先在窗口内等一张带 llm_usage 的行（同观察面重读），仍缺
    则按观察面缺口报错（与"有键但全 0"的假跑形态分开表述）。
    """

    def _rich_row() -> dict[str, Any] | None:
        row = _row_by_pipeline(_state_rows(kernel, token), task_id)
        state_now = _state(row)
        return state_now if _llm_usage(state_now) else None

    rich = _poll_until(_rich_row, 15, interval=2)
    usage = _llm_usage(rich or state)
    total_in = int(usage.get("total_input_tokens") or usage.get("prompt_tokens") or 0)
    total_out = int(usage.get("total_output_tokens") or usage.get("completion_tokens") or 0)
    assert total_in > 0, (
        f"{context}: llm_usage total_input_tokens 应 > 0（LLM 真实调用）。实际 usage={usage}，"
        f"终态行 track 键={sorted(k for k in state if str(k).startswith('track'))}，"
        f"富行等待后仍缺=观察面出口缺口（track.* 声明/数据源分层问题）；"
        f"有键全 0=假跑形态"
    )
    assert total_out > 0, (
        f"{context}: llm_usage total_output_tokens 应 > 0（LLM 真实返回）。实际 usage={usage}"
    )


def _create_task(kernel: StubKernel, token: str, title: str, description: str) -> str:
    status, body, _ = http_post_json_auth(
        f"{kernel.url}/ext/task_service/tasks",
        {"title": title, "description": description, "agent_id": "general_agent"},
        token=token,
        timeout=15,
    )
    assert status == 200, f"创建任务应 200，实际 {status}: {body}"
    task_id = body.get("id") or body.get("task_id")
    assert task_id, f"创建任务应返回 id，实际 {body}"
    return str(task_id)


def _wait_task_terminal(
    kernel: StubKernel, token: str, task_id: str, timeout: float
) -> tuple[dict[str, Any], list[str]]:
    """轮询 state 聚合直到任务终态；返回 (终态 state, 观测到的状态序列)。"""
    seen: list[str] = []
    terminal: dict[str, Any] | None = None

    def _check() -> dict[str, Any] | None:
        row = _row_by_pipeline(_state_rows(kernel, token), task_id)
        if row is None:
            return None
        state = _state(row)
        status_now = str(state.get("task.status") or "")
        if status_now and status_now not in seen:
            seen.append(status_now)
        if status_now in ("completed", "failed", "pending_evaluation"):
            return state
        return None

    terminal = _poll_until(_check, timeout)
    assert terminal is not None, (
        f"任务 {task_id} 在 {timeout}s 内未达终态，状态序列 {seen}（疑似无限循环）"
    )
    print(f"[matrix] task={task_id} 状态序列: {seen}")
    print(
        f"[matrix] task={task_id} 诊断指纹: reminders={terminal.get('evaluate_reminder_count')} "
        f"stop_reason={terminal.get('router.stop_reason')!r} "
        f"duplicate_intercepts={terminal.get('router.duplicate_intercepts')} "
        f"usage={_llm_usage(terminal).get('total_tokens')}"
    )
    return terminal, seen


def _session_messages(kernel: StubKernel, token: str, session_id: str) -> list[dict[str, Any]]:
    status, body, _ = http_get_with_auth(
        f"{kernel.url}/api/v1/sessions/{session_id}/messages", token=token, timeout=10
    )
    if status != 200 or not isinstance(body, dict):
        return []
    return [m for m in body.get("messages", []) if isinstance(m, dict)]


def _create_session(kernel: StubKernel, token: str, title: str) -> str:
    status, body, _ = http_post_json_auth(
        f"{kernel.url}/api/v1/sessions", {"title": title}, token=token, timeout=15
    )
    assert status == 200, f"创建会话应 200，实际 {status}: {body}"
    assert body.get("thread_id"), f"创建会话应返回 thread_id，实际 {body}"
    return str(body["thread_id"])


def _chat(kernel: StubKernel, token: str, session_id: str, message: str) -> str:
    status, body, _ = http_post_json_auth(
        f"{kernel.url}/api/v1/chat",
        {"message": message, "session_id": session_id},
        token=token,
        timeout=_CHAT_TIMEOUT_SECONDS,
    )
    assert status == 200, f"/api/v1/chat 应 200，实际 {status}: {body}"
    content = body.get("content") if isinstance(body, dict) else None
    assert isinstance(content, str), f"chat content 应为字符串，实际 {body}"
    assert content.strip(), f"chat content 应非空，实际 {body}"
    return content


# ============================================================
# 脚本注册（输入条件 → stub 响应序列）
# ============================================================


def _register_general_agent_scripts(stub: ScriptedLLMUpstream) -> None:
    """注册 general_agent 车道各路径的执行脚本（不含父会话场景）。"""
    # A1：bash 成功 → task_evaluate → 文本（第 3 轮由信号②当轮收束，文本不作判据）
    stub.register(
        "A1",
        _M_A1,
        [
            tool_call_step("bash_execute", action="execute", command=_BASH_ECHO_OK),
            tool_call_step(
                "task_evaluate",
                action="auto_complete",
                summary="已用 bash_execute 生成 result.txt，任务完成。",
            ),
            text_step("A1：执行与评估均完成。"),
        ],
    )
    # A2 子任务：bash 生成 file_check 指标所需文件 → task_evaluate（指标全过）
    stub.register(
        "A2C",
        _M_A2C,
        [
            tool_call_step("bash_execute", action="execute", command=_BASH_ECHO_OK),
            tool_call_step(
                "task_evaluate",
                action="auto_complete",
                summary="已生成 result.txt，满足 file_check 指标。",
            ),
            text_step("A2 子任务：执行与评估完成。"),
        ],
    )
    # A3：bash 成功后纯文本且从不调 task_evaluate（提醒逐轮注入直至耗尽）
    stub.register(
        "A3",
        _M_A3,
        [
            tool_call_step("bash_execute", action="execute", command=_BASH_ECHO_OK),
            text_step("A3 第 1 轮：我已完成任务工作。"),
            text_step("A3 第 2 轮：工作确实已经全部完成。"),
            text_step("A3 第 3 轮：再次确认任务已全部完成。"),
            text_step("A3 第 4 轮：没有需要评估的内容。"),
        ],
        default=text_step("A3 兜底轮：任务已完成，无需进一步操作。"),
    )
    # B1：bash 真失败（ls 不存在的目录 → exit != 0），此后纯文本、从不评估
    stub.register(
        "B1",
        _M_B1,
        [
            tool_call_step(
                "bash_execute", action="execute", command="ls e2e_b1_missing_dir"
            ),
            text_step("B1 第 1 轮：命令执行失败了。"),
            text_step("B1 第 2 轮：失败原因无法修复。"),
            text_step("B1 第 3 轮：我声明本任务无法完成。"),
        ],
        default=text_step("B1 兜底轮：任务失败，无法继续。"),
    )
    # C1：连续调用不存在的工具（每轮参数可区分，绕过重复调用检测），永不文本收束
    bogus = "e2e_nonexistent_tool"
    stub.register(
        "C1",
        _M_C1,
        [
            tool_call_step(bogus, probe=1),
            tool_call_step(bogus, probe=2),
            tool_call_step(bogus, probe=3),
            tool_call_step(bogus, probe=4),
        ],
        default=tool_call_step(bogus, probe=99),
    )
    # C2 子任务：bash + task_evaluate → completed（触发父会话唤醒）
    stub.register(
        "C2C",
        _M_C2C,
        [
            tool_call_step("bash_execute", action="execute", command=_BASH_ECHO_OK),
            tool_call_step(
                "task_evaluate",
                action="auto_complete",
                summary="已生成 result.txt，子任务完成。",
            ),
            text_step("C2 子任务：执行与评估完成。"),
        ],
    )
    # D1C：指标指向不存在的文件 → 每次评估都不过 → default 持续重评 → 重试耗尽
    stub.register(
        "D1C",
        _M_D1C,
        [
            tool_call_step("bash_execute", action="execute", command=_BASH_ECHO_OK),
            tool_call_step(
                "task_evaluate",
                action="auto_complete",
                summary="已生成 result.txt，任务完成。",
            ),
        ],
        # 每轮 summary 可区分：绕过 duplicate_check 的相同参数拦截，
        # 让失败评估真实累积到重试耗尽
        default=lambda body, idx: tool_call_step(
            "task_evaluate",
            action="auto_complete",
            summary=f"再次确认任务完成（第 {idx} 次评估）。",
        ),
    )
    # D2C 第一次执行：flag 文件未创建 → 评估不过 → 重试耗尽 → failed
    stub.register(
        "D2C",
        _M_D2C,
        [
            tool_call_step("bash_execute", action="execute", command=_BASH_ECHO_OK),
            tool_call_step(
                "task_evaluate",
                action="auto_complete",
                summary="已生成 result.txt，任务完成。",
            ),
            text_step("D2C 第 1 次执行：评估未通过，等待重跑。"),
        ],
        default=lambda body, idx: tool_call_step(
            "task_evaluate",
            action="auto_complete",
            summary=f"再次确认任务完成（第 {idx} 次评估）。",
        ),
    )
    # S2C：修正重提后的子任务（指标带全参）→ 评估通过
    stub.register(
        "S2C",
        _M_S2C,
        [
            tool_call_step("bash_execute", action="execute", command=_BASH_ECHO_OK),
            tool_call_step(
                "task_evaluate",
                action="auto_complete",
                summary="已生成 result.txt，任务完成。",
            ),
            text_step("S2 子任务：执行与评估完成。"),
        ],
    )






def _register_rate_batch_scripts(stub: ScriptedLLMUpstream) -> None:
    """完成率混合批（R1）脚本：S1..S6 = bash 成功 + task_evaluate（completed）；
    F1 = bash 真失败从不评估（failed）；F2 = bash 成功但从不评估、提醒耗尽
    （failed）。脚本名 == marker，request_count(marker) 按场景查命中。"""
    for marker in _M_RATE_S:
        stub.register(
            marker,
            marker,
            [
                tool_call_step("bash_execute", action="execute", command=_BASH_ECHO_OK),
                tool_call_step(
                    "task_evaluate",
                    action="auto_complete",
                    summary=f"{marker}：已生成 result.txt，任务完成。",
                ),
                text_step(f"{marker}：执行与评估均完成。"),
            ],
        )
    stub.register(
        _M_RATE_F1,
        _M_RATE_F1,
        [
            tool_call_step(
                "bash_execute", action="execute", command="ls e2e_rate_f1_missing_dir"
            ),
            text_step("RATE-F1 第 1 轮：命令执行失败了。"),
            text_step("RATE-F1 第 2 轮：失败原因无法修复。"),
            text_step("RATE-F1 第 3 轮：我声明本任务无法完成。"),
        ],
        default=text_step("RATE-F1 兜底轮：任务失败，无法继续。"),
    )
    stub.register(
        _M_RATE_F2,
        _M_RATE_F2,
        [
            tool_call_step("bash_execute", action="execute", command=_BASH_ECHO_OK),
            text_step("RATE-F2 第 1 轮：我已完成任务工作。"),
            text_step("RATE-F2 第 2 轮：工作确实已经全部完成。"),
            text_step("RATE-F2 第 3 轮：再次确认任务已全部完成。"),
            text_step("RATE-F2 第 4 轮：没有需要评估的内容。"),
        ],
        default=text_step("RATE-F2 兜底轮：任务已完成，无需进一步操作。"),
    )


def _project_row_by_title(kernel: StubKernel, token: str, title: str) -> dict[str, Any] | None:
    """projects 域列表按标题找登记行（project_create 真实执行后观察面）。"""
    status, body, _ = http_get_with_auth(
        f"{kernel.url}/ext/task_service/projects?limit=100", token=token, timeout=10
    )
    if status != 200 or not isinstance(body, dict):
        return None
    for item in body.get("items", []):
        if isinstance(item, dict) and str(item.get("goal") or "") == title:
            return item
    return None


def _tool_result_field(body: dict[str, Any], key: str) -> str:
    """从请求 messages 历史提取工具结果字段（yaml 风格 key: value 或 JSON 信封）。

    stub 脚本步骤可为 callable (body, index)——P1 父会话第二步的 task_submit
    参数依赖前序真实工具结果（project_id 运行时生成，脚本无法预编码）。
    """
    for message in reversed(body.get("messages", [])):
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        stripped = content.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and parsed.get(key):
                    return str(parsed[key])
            except json.JSONDecodeError:
                pass
        for line in stripped.splitlines():
            if line.startswith(f"{key}:") and len(line) > len(key) + 1:
                return line[len(key) + 1:].strip()
    raise AssertionError(f"工具结果未找到字段 {key!r}: {body}")


def _tool_result_project_id(body: dict[str, Any]) -> str:
    """project_create 工具输出的 project_id（12hex，行解析防路径误扫）。"""
    pid = _tool_result_field(body, "project_id")
    assert len(pid) == 12, f"project_id 应为 12hex，实际 {pid!r}"
    return pid


def _tool_result_workspace_path(body: dict[str, Any]) -> str:
    """从请求 messages 历史提取 project_create 工具输出的项目文件夹路径。"""
    for message in reversed(body.get("messages", [])):
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        try:
            parsed = json.loads(content.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("path"):
            return str(parsed["path"])
    raise AssertionError(f"project_create 工具结果未找到 path: {body}")


def _task_submit_args(
    *, child_marker: str, acceptance_criteria: dict[str, Any] | None = None
) -> dict[str, Any]:
    """L1 父会话脚本中的 task_submit 工具调用参数（plain 拓扑避开合并门控 WIP）。"""
    args: dict[str, Any] = {
        "target_type": "agent",
        "target_id": "general_agent",
        "goal_title": f"路径矩阵子任务 {child_marker}",
        "goal_description": (
            "在当前工作目录执行命令 echo E2E_BASH_OK > result.txt 生成结果文件，"
            f"完成后调用 task_evaluate 工具评估。场景标记：{child_marker}"
        ),
        "workspace_mode": "plain",
    }
    if acceptance_criteria is not None:
        args["acceptance_criteria"] = acceptance_criteria
    return args


# ============================================================
# 观察面在位性
# ============================================================


class TestPathMatrixPreconditions:
    """路径验证的前置成立性：工具面在位 + 脚本上游可编程。"""

    def test_task_domain_tools_visible(self, stub_kernel, matrix_token):
        """task_evaluate/bash_execute/task_submit 必须在工具面注册（闸门的物质基础）。"""
        deadline = time.time() + 60
        names: list[str] = []
        while time.time() < deadline:
            status, body, _ = http_get_with_auth(
                f"{stub_kernel.url}/api/v1/tools", token=matrix_token, timeout=10
            )
            names = [t.get("name") for t in body.get("items", [])] if status == 200 else []
            if all(n in names for n in ("task_evaluate", "bash_execute", "task_submit")):
                return
            time.sleep(3)
        pytest.fail(f"任务域工具未在 /api/v1/tools 全部可见，实际含: {names[:20]}...")

    def test_stub_upstream_serves_scripted_rounds(self, stub_llm):
        """stub 上游本身可编程：marker 命中、步骤按序消费、usage 恒非零。"""
        stub_llm.register(
            "selfcheck", "E2E-MTX-SELFCHK", [text_step("round-1"), text_step("round-2")]
        )

        def _post(payload: dict) -> dict:
            req = urllib.request.Request(
                f"{stub_llm.api_base}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))

        messages = [{"role": "user", "content": "x E2E-MTX-SELFCHK"}]
        first = _post({"model": "stub-model", "stream": False, "messages": messages})
        assert first["choices"][0]["message"]["content"] == "round-1"
        assert first["usage"]["total_tokens"] > 0
        second = _post({"model": "stub-model", "stream": False, "messages": messages})
        assert second["choices"][0]["message"]["content"] == "round-2"
        assert stub_llm.request_count("selfcheck") == 2


# ============================================================
# 路径矩阵
# ============================================================


class TestA1CompleteWithoutMetrics:
    """A1 无指标任务 + bash 成功 + task_evaluate → completed（核心回归）。"""

    @pytest.mark.timeout(480)
    def test_bash_then_evaluate_completes_task(self, stub_kernel, matrix_token, stub_llm):
        _register_general_agent_scripts(stub_llm)
        task_id = _create_task(
            stub_kernel,
            matrix_token,
            f"路径矩阵 A1：执行并评估（{_M_A1}）",
            f"执行 bash 生成 result.txt 后调用 task_evaluate 评估。场景标记：{_M_A1}",
        )
        state, seen = _wait_task_terminal(
            stub_kernel, matrix_token, task_id, _TERMINAL_WAIT_SECONDS
        )
        assert state.get("task.status") == "completed", (
            f"A1 应 completed（信号②当轮收束），实际 {state.get('task.status')}，"
            f"序列 {seen}"
        )
        _assert_real_llm_ran(stub_kernel, matrix_token, task_id, state, "A1")
        assert stub_llm.request_count("A1") >= 2, (
            f"A1 至少 2 轮 LLM（bash→evaluate 当轮收束），实际 {stub_llm.request_count('A1')}"
        )
        # 工作空间/隔离实际执行证据（黑盒观察面）：ws_meta 拓扑 + 产出文件落位
        # + 隔离未拦截。state 行的 ws_meta.path 即任务工作区（工作空间根/{task_id}）。
        assert not state.get("isolation.blocked"), (
            f"A1 bash 不应被隔离拦截，实际 blocked={state.get('isolation.blocked')} "
            f"reason={state.get('isolation.block_reason')}"
        )
        # 工作区落位走文件系统观察：任务默认工作区 = {base_dir}/.ai_workspaces/
        # {task_id}（内核项目根 = 测试临时目录；state 出口的 ws_meta 受导出白名单
        # /时序影响不作硬断言，仅诊断打印）
        ws_meta = state.get("ws_meta") or {}
        if isinstance(ws_meta, str):
            try:
                ws_meta = json.loads(ws_meta)
            except json.JSONDecodeError:
                ws_meta = {}
        print(f"[matrix] A1 ws_meta 出口（诊断）: {ws_meta or '-'}")
        ws_base = os.path.join(stub_kernel.base_dir, ".ai_workspaces")
        assert (Path(ws_base) / str(task_id) / "result.txt").exists(), (
            f"A1 bash 产出应落在任务工作区 {ws_base}/{task_id}/result.txt"
        )


class TestA2MetricsAllPassed:
    """A2 有指标任务（file_check 全过，经 L1 task_submit 派发）→ 子任务 completed。"""

    @pytest.mark.timeout(600)
    def test_subtask_with_metrics_completes(
        self, stub_kernel, matrix_token, stub_llm, matrix_sessions
    ):
        # 父脚本先注册（注册序 = stub 匹配优先级，父请求历史里含子 marker）
        stub_llm.register(
            "A2P",
            _M_A2P,
            [
                tool_call_step(
                    "task_submit",
                    **_task_submit_args(
                        child_marker=_M_A2C,
                        acceptance_criteria={
                            "file_check": {
                                "input_params": {"path": "result.txt", "check": "exists"}
                            }
                        },
                    ),
                ),
                text_step("A2 父会话：带指标的子任务已提交。"),
            ],
        )
        _register_general_agent_scripts(stub_llm)

        session_id = _create_session(stub_kernel, matrix_token, "e2e-matrix-a2")
        matrix_sessions(session_id)
        _chat(
            stub_kernel,
            matrix_token,
            session_id,
            f"请派发一个带验收指标的任务。场景标记：{_M_A2P}",
        )

        child_row = _poll_until(
            lambda: _row_by_marker(_state_rows(stub_kernel, matrix_token), _M_A2C),
            _TERMINAL_WAIT_SECONDS,
        )
        assert child_row is not None, f"state 聚合应出现 A2 子任务（marker {_M_A2C}）"
        child_id = str(child_row["pipeline_id"])
        child_state, seen = _wait_task_terminal(
            stub_kernel, matrix_token, child_id, _TERMINAL_WAIT_SECONDS
        )

        criteria = child_state.get("task.acceptance_criteria") or {}
        if isinstance(criteria, str):
            criteria = json.loads(criteria) if criteria.startswith("{") else {}
        assert criteria, (
            f"A2 输入条件应含验收指标，实际 state.task.acceptance_criteria="
            f"{child_state.get('task.acceptance_criteria')}。"
            f"已知断点（2026-08-28 基线）：state 出口声明化过滤——tasks 插件 "
            f"export_fields 未声明 task.acceptance_criteria（仅 goal/status/"
            f"ended_at/submitted_by/parent_project_id/owned.*），该键被 "
            f"/pipelines/state 与 pipeline-state.list 同步过滤，task_evaluate "
            f"的 _ensure_criteria_from_state 读不到 → metric_ids=[] → "
            f"『跳过评估直接标记完成』（评估在无指标形态下假通过）"
        )
        assert child_state.get("task.status") == "completed", (
            f"A2 指标全过应 completed，实际 {child_state.get('task.status')}，序列 {seen}"
        )
        _assert_real_llm_ran(stub_kernel, matrix_token, child_id, child_state, "A2 子任务")


class TestA3NeverEvaluates:
    """A3 bash 成功但 agent 从不调 task_evaluate → 提醒耗尽 → failed。"""

    @pytest.mark.timeout(420)
    def test_reminder_exhaustion_fails_task(self, stub_kernel, matrix_token, stub_llm):
        _register_general_agent_scripts(stub_llm)
        task_id = _create_task(
            stub_kernel,
            matrix_token,
            f"路径矩阵 A3：从不评估（{_M_A3}）",
            f"执行 bash 生成 result.txt 即可。场景标记：{_M_A3}",
        )
        state, seen = _wait_task_terminal(
            stub_kernel, matrix_token, task_id, _TERMINAL_WAIT_SECONDS
        )
        assert state.get("task.status") == "failed", (
            f"A3 提醒耗尽应 failed，实际 {state.get('task.status')}，序列 {seen}"
        )
        _assert_real_llm_ran(stub_kernel, matrix_token, task_id, state, "A3")
        reminder_count = state.get("evaluate_reminder_count")
        if reminder_count is not None:
            assert int(reminder_count) == 3, (
                f"A3 应注入 3 次提醒后耗尽（general_agent max_reminders=3），"
                f"实际 {reminder_count}"
            )
        else:
            print("[matrix] A3: state 聚合未持久化 evaluate_reminder_count（诊断性键，跳过）")


class TestB1BashRealFailure:
    """B1 bash 真失败（exit != 0）且从不评估 → 合法 failed。"""

    @pytest.mark.timeout(480)
    def test_real_tool_failure_ends_failed(self, stub_kernel, matrix_token, stub_llm):
        _register_general_agent_scripts(stub_llm)
        task_id = _create_task(
            stub_kernel,
            matrix_token,
            f"路径矩阵 B1：bash 失败（{_M_B1}）",
            f"查看缺失目录内容。场景标记：{_M_B1}",
        )
        state, seen = _wait_task_terminal(
            stub_kernel, matrix_token, task_id, _TERMINAL_WAIT_SECONDS
        )
        assert state.get("task.status") == "failed", (
            f"B1 真失败应 failed，实际 {state.get('task.status')}，序列 {seen}"
        )
        _assert_real_llm_ran(stub_kernel, matrix_token, task_id, state, "B1")
        # 路径指纹：首轮回的是工具调用（bash），与 A3（纯工具成功+提醒）区分
        rounds = stub_llm.request_count("B1")
        assert rounds >= 4, f"B1 应至少 4 轮 LLM（bash + 3 轮提醒内文本），实际 {rounds}"


class TestC1BoundedLoopProtection:
    """C1 连续调用不存在的工具 → duplicate_check 硬上限 → 有界轮数终态。

    循环防护语义（用户裁定 2026-08-30）：重复检测以「同工具+相同参数」签名
    为准，由 duplicate_check 三级渐进承接——软提示 ×2 → 拦截剥离+回 LLM
    ×4（hard_limit_intercepts=4）→ should_stop 终止，署名 duplicate_loop
    映射 run failed，任务域终态对账补落 task.status=failed。
    """

    @pytest.mark.timeout(420)
    def test_duplicate_loop_forces_bounded_terminal(
        self, stub_kernel, matrix_token, stub_llm
    ):
        _register_general_agent_scripts(stub_llm)
        task_id = _create_task(
            stub_kernel,
            matrix_token,
            f"路径矩阵 C1：工具不可用（{_M_C1}）",
            f"使用专用工具完成检查。场景标记：{_M_C1}",
        )
        state, seen = _wait_task_terminal(
            stub_kernel, matrix_token, task_id, _TERMINAL_WAIT_SECONDS
        )
        assert state.get("task.status") == "failed", (
            f"C1 死循环终止后应 failed（无完成证据不可覆盖），"
            f"实际 {state.get('task.status')}，序列 {seen}"
        )
        _assert_real_llm_ran(stub_kernel, matrix_token, task_id, state, "C1")
        rounds = stub_llm.request_count("C1")
        # 有界性（duplicate_check 三级算术）：前 4 轮变参探测 + 首个同参轮
        # 计数归零 + 4 个拦截周期 × 3 轮（max_duplicate_calls=3）+ 终止轮
        # ≈ 18 轮，给硬上限 20（不无限循环即有界）。
        assert rounds <= 20, f"C1 应有界收束（<=20 轮），实际 {rounds} 轮（疑似无限循环）"
        print(f"[matrix] C1 收束轮数: {rounds}")


class TestC2SubtaskWakesParent:
    """C2 子任务终态 → 父会话唤醒收束出文本（受包1唤醒链影响，可能红）。"""

    @pytest.mark.timeout(600)
    def test_child_terminal_wakes_parent_session(
        self, stub_kernel, matrix_token, stub_llm, matrix_sessions
    ):
        # 父脚本先注册（父请求历史里含子 marker，注册序 = 匹配优先级）
        stub_llm.register(
            "C2P",
            _M_C2P,
            [
                tool_call_step("task_submit", **_task_submit_args(child_marker=_M_C2C)),
                text_step("C2 父会话：子任务已提交，等待完成通知。"),
                text_step("MTX-C2-WAKE-OK 父会话已收到子任务完成通知并收束。"),
            ],
            default=text_step("MTX-C2-WAKE-OK 父会话已收到子任务完成通知并收束。"),
        )
        _register_general_agent_scripts(stub_llm)

        session_id = _create_session(stub_kernel, matrix_token, "e2e-matrix-c2")
        matrix_sessions(session_id)
        first_reply = _chat(
            stub_kernel,
            matrix_token,
            session_id,
            f"请派发子任务并在完成后通知我。场景标记：{_M_C2P}",
        )
        assert "已提交" in first_reply, f"C2 首 run 应回提交确认文本，实际: {first_reply}"

        # 子任务跑到终态
        child_row = _poll_until(
            lambda: _row_by_marker(_state_rows(stub_kernel, matrix_token), _M_C2C),
            _TERMINAL_WAIT_SECONDS,
        )
        assert child_row is not None, f"state 聚合应出现 C2 子任务（marker {_M_C2C}）"
        child_id = str(child_row["pipeline_id"])
        child_state, seen = _wait_task_terminal(
            stub_kernel, matrix_token, child_id, _TERMINAL_WAIT_SECONDS
        )
        assert child_state.get("task.status") == "completed", (
            f"C2 子任务应 completed，实际 {child_state.get('task.status')}，序列 {seen}"
        )
        _assert_real_llm_ran(stub_kernel, matrix_token, child_id, child_state, "C2 子任务")

        # 唤醒链：父会话收到通知 → 新 run → 脚本文本收束
        def _woken() -> str | None:
            for msg in _session_messages(stub_kernel, matrix_token, session_id):
                content = str(msg.get("content") or "")
                if msg.get("role") == "assistant" and "MTX-C2-WAKE-OK" in content:
                    return content
            return None

        wake_text = _poll_until(_woken, _WAKE_WAIT_SECONDS)
        assert wake_text is not None, (
            "C2 唤醒链断裂：子任务 completed 后父会话未在窗口内收到唤醒收束文本"
            "（MTX-C2-WAKE-OK）。检查 task_completed 事件 → triggers_ext 父通知注入链。"
        )
        # 通知注入本身的黑盒痕迹：会话里应出现子任务通知消息
        blob = json.dumps(
            _session_messages(stub_kernel, matrix_token, session_id), ensure_ascii=False
        )
        assert "子任务" in blob, "C2：父会话消息里应含子任务通知注入痕迹"


# ============================================================
# 评估闭环与停止路径（D1/D2/S2/S1）
# ============================================================


class TestD1EvalFailBounded:
    """D1 评估提交但指标不过 → 反复评估有界停止（重试耗尽 → failed，不无限评估）。"""

    @pytest.mark.timeout(600)
    def test_failing_evaluation_exhausts_bounded(
        self, stub_kernel, matrix_token, stub_llm, matrix_sessions
    ):
        stub_llm.register(
            "D1P",
            _M_D1P,
            [
                tool_call_step(
                    "task_submit",
                    **_task_submit_args(
                        child_marker=_M_D1C,
                        acceptance_criteria={
                            "file_check": {
                                "input_params": {
                                    "path": "e2e_d1_missing.txt",
                                    "check": "exists",
                                }
                            }
                        },
                    ),
                ),
                text_step("D1 父会话：带指标的子任务已提交。"),
            ],
        )
        _register_general_agent_scripts(stub_llm)

        session_id = _create_session(stub_kernel, matrix_token, "e2e-matrix-d1")
        matrix_sessions(session_id)
        _chat(stub_kernel, matrix_token, session_id, f"请派发一个带验收指标的任务。场景标记：{_M_D1P}")

        child_row = _poll_until(
            lambda: _row_by_marker(_state_rows(stub_kernel, matrix_token), _M_D1C),
            _TERMINAL_WAIT_SECONDS,
        )
        assert child_row is not None, f"state 聚合应出现 D1 子任务（marker {_M_D1C}）"
        child_id = str(child_row["pipeline_id"])
        child_state, seen = _wait_task_terminal(
            stub_kernel, matrix_token, child_id, _TERMINAL_WAIT_SECONDS
        )
        assert child_state.get("task.status") == "failed", (
            f"D1 指标不过应 failed，实际 {child_state.get('task.status')}，序列 {seen}"
        )
        assert "completed" not in seen, f"D1 评估不过不得假完成，序列 {seen}"
        _assert_real_llm_ran(stub_kernel, matrix_token, child_id, child_state, "D1 子任务")
        rounds = stub_llm.request_count("D1C")
        print(f"[matrix] D1 收束轮数: {rounds}")
        # 有界性：bash + 3 次失败评估（重试耗尽 → stop_check 聚合检测当轮收束）
        assert 4 <= rounds <= 9, (
            f"D1 反复评估应有界停止（bash + 重试耗尽 ≈ 4 轮，上限 9），实际 {rounds} 轮"
        )
        # 耗尽计数落 state（跨调用累积的单一真值）
        retry = child_state.get("task.eval_retry_count")
        if isinstance(retry, str):
            try:
                retry = json.loads(retry)
            except json.JSONDecodeError:
                retry = {}
        if retry:
            assert int(retry.get("file_check") or 0) >= 3, (
                f"D1 重试计数应累积到耗尽（3），实际 {retry}"
            )
        else:
            print("[matrix] D1: state 未出口 task.eval_retry_count（诊断性键，跳过）")


class TestD2FailedResubmitCompletes:
    """D2 评估失败 → 任务 failed → submit 重跑 → 修复后评估通过 → completed（失败重跑闭环）。"""

    @pytest.mark.timeout(600)
    def test_failed_task_resubmit_completes(
        self, stub_kernel, matrix_token, stub_llm, matrix_sessions
    ):
        stub_llm.register(
            "D2P",
            _M_D2P,
            [
                tool_call_step(
                    "task_submit",
                    **_task_submit_args(
                        child_marker=_M_D2C,
                        acceptance_criteria={
                            "file_check": {
                                "input_params": {
                                    "path": "e2e_d2_flag.txt",
                                    "check": "exists",
                                }
                            }
                        },
                    ),
                ),
                text_step("D2 父会话：带指标的子任务已提交。"),
            ],
        )
        _register_general_agent_scripts(stub_llm)

        session_id = _create_session(stub_kernel, matrix_token, "e2e-matrix-d2")
        matrix_sessions(session_id)
        _chat(stub_kernel, matrix_token, session_id, f"请派发一个带验收指标的任务。场景标记：{_M_D2P}")

        child_row = _poll_until(
            lambda: _row_by_marker(_state_rows(stub_kernel, matrix_token), _M_D2C),
            _TERMINAL_WAIT_SECONDS,
        )
        assert child_row is not None, f"state 聚合应出现 D2 子任务（marker {_M_D2C}）"
        child_id = str(child_row["pipeline_id"])
        child_state, _seen = _wait_task_terminal(
            stub_kernel, matrix_token, child_id, _TERMINAL_WAIT_SECONDS
        )
        assert child_state.get("task.status") == "failed", (
            f"D2 第一次执行应 failed（指标不过耗尽），实际 {child_state.get('task.status')}"
        )

        # 第二次执行：修复产线（bash 创建 flag 文件）→ 评估通过 → completed。
        # 重跑是同一管道（同 marker 同脚本坐标），换脚本 + 清零游标。
        stub_llm.replace_steps(
            "D2C",
            [
                tool_call_step(
                    "bash_execute", action="execute", command="echo E2E_D2_FIXED > e2e_d2_flag.txt"
                ),
                tool_call_step(
                    "task_evaluate",
                    action="auto_complete",
                    summary="e2e_d2_flag.txt 已生成，任务完成。",
                ),
                text_step("D2C 第二次执行：评估通过。"),
            ],
        )
        status, body, _ = http_post_json_auth(
            f"{stub_kernel.url}/ext/task_service/tasks/{child_id}/submit",
            {},
            token=matrix_token,
            timeout=15,
        )
        assert status == 200, f"D2 重跑提交应 200，实际 {status}: {body}"

        # 竞态防护：重跑派发后 state 仍短暂停留 failed，等它先离开 failed
        # （推进 running）再等终态，否则轮询会把旧 failed 当成第二次终态早退
        def _left_failed() -> bool:
            row = _row_by_pipeline(_state_rows(stub_kernel, matrix_token), child_id)
            if row is None:
                return False
            return str((_state(row) or {}).get("task.status") or "") != "failed"

        left = _poll_until(_left_failed, 120)
        assert left, "D2 重跑后状态应离开 failed（推进 running）"

        final_state, seen2 = _wait_task_terminal(
            stub_kernel, matrix_token, child_id, _TERMINAL_WAIT_SECONDS
        )
        assert final_state.get("task.status") == "completed", (
            f"D2 重跑后应 completed（评估通过），实际 {final_state.get('task.status')}，"
            f"序列 {seen2}"
        )
        _assert_real_llm_ran(stub_kernel, matrix_token, child_id, final_state, "D2 重跑")


class TestS2MissingParamsRejected:
    """S2 提交期闸门：指标缺必填 input_params → 工具拒绝 → LLM 修正重提成功。"""

    @pytest.mark.timeout(600)
    def test_missing_metric_params_rejected_then_fixed(
        self, stub_kernel, matrix_token, stub_llm, matrix_sessions
    ):
        stub_llm.register(
            "S2P",
            _M_S2P,
            [
                # 第一次：file_check 缺 input_params.path → 提交期拒绝
                tool_call_step(
                    "task_submit",
                    **_task_submit_args(
                        child_marker=_M_S2C,
                        acceptance_criteria={"file_check": {}},
                    ),
                ),
                # 第二次：补齐 input_params → 提交成功
                tool_call_step(
                    "task_submit",
                    **_task_submit_args(
                        child_marker=_M_S2C,
                        acceptance_criteria={
                            "file_check": {
                                "input_params": {"path": "result.txt", "check": "exists"}
                            }
                        },
                    ),
                ),
                text_step("S2 父会话：修正后的子任务已提交。"),
            ],
        )
        _register_general_agent_scripts(stub_llm)

        session_id = _create_session(stub_kernel, matrix_token, "e2e-matrix-s2")
        matrix_sessions(session_id)
        _chat(stub_kernel, matrix_token, session_id, f"请派发一个带验收指标的任务。场景标记：{_M_S2P}")

        # 唯一子任务（缺参那次必须被拒绝，不得产生黑户任务）
        child_row = _poll_until(
            lambda: _row_by_marker(_state_rows(stub_kernel, matrix_token), _M_S2C),
            _TERMINAL_WAIT_SECONDS,
        )
        assert child_row is not None, f"state 聚合应出现 S2 子任务（marker {_M_S2C}）"
        child_id = str(child_row["pipeline_id"])
        rows = _state_rows(stub_kernel, matrix_token)
        dup = [
            r for r in rows
            if str(r.get("pipeline_id")) != child_id
            and _M_S2C in json.dumps(r, ensure_ascii=False, default=str)
            and (r.get("task.goal") or r.get("task.id"))
        ]
        assert not dup, f"S2 缺参提交应被拒绝（只允许一次成功提交），发现多余任务行: {dup}"

        child_state, seen = _wait_task_terminal(
            stub_kernel, matrix_token, child_id, _TERMINAL_WAIT_SECONDS
        )
        assert child_state.get("task.status") == "completed", (
            f"S2 修正后子任务应 completed，实际 {child_state.get('task.status')}，序列 {seen}"
        )
        # 拒绝反馈到达 LLM 的黑盒证据：父会话消费了两次 task_submit 步骤
        # （第一次被拒后 stub 才会走到第二次提交步骤——顺序消费语义）
        parent_rounds = stub_llm.request_count("S2P")
        assert parent_rounds >= 3, (
            f"S2 父会话应至少 3 轮（拒绝 → 修正重提 → 收束），实际 {parent_rounds}"
        )


class TestS1StopDuringRun:
    """S1 运行中 stop_generation → 感知中断 → 循环有界停止（不无限执行）。"""

    @pytest.mark.timeout(420)
    def test_stop_generation_stops_running_task(
        self, stub_kernel, matrix_token, stub_llm
    ):
        import asyncio

        import websockets

        stub_llm.register(
            "S1",
            _M_S1,
            [
                tool_call_step(
                    "bash_execute", action="execute", command="echo E2E_S1 > s1.txt"
                )
            ],
            default=lambda body, idx: tool_call_step(
                "bash_execute",
                action="execute",
                command=f"echo E2E_S1_R{idx} > s1_r{idx}.txt",
            ),
        )
        task_id = _create_task(
            stub_kernel,
            matrix_token,
            f"路径矩阵 S1：运行中停止（{_M_S1}）",
            f"循环执行 bash 检查。场景标记：{_M_S1}",
        )

        # 等循环真实推进：进度观察用 stub 轮数（测试自有观测，不受 state 出口
        # 时序影响）；至少 3 轮才停，停止才有意义
        def _rounds_advanced() -> bool:
            return stub_llm.request_count("S1") >= 3

        advanced = _poll_until(_rounds_advanced, 180)
        assert advanced, "S1 任务循环应真实推进（>=3 轮 LLM）"

        count_before = stub_llm.request_count("S1")
        deadline = time.time() + 90
        count_now = count_before
        while time.time() < deadline:
            count_now = stub_llm.request_count("S1")
            if count_now >= count_before + 2:
                break
            time.sleep(2)
        assert count_now >= count_before + 2, (
            "S1 停止前循环应仍在推进（否则无停止意义）"
        )

        async def _send_stop() -> None:
            # WS 目标 = 本测试自带内核实例（随机端口）——e2e_helpers.ws_chat_url
            # 固定连 9100，矩阵自带内核场景不能用（停止会打到别的内核上空转）
            url = (
                f"ws://127.0.0.1:{stub_kernel.port}/ws/chat"
                f"?token={matrix_token}&version=1"
            )
            async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
                # 连接确认帧
                try:
                    first = await asyncio.wait_for(ws.recv(), timeout=5)
                    print(f"[matrix] S1 WS 连接确认: {str(first)[:160]}")
                except asyncio.TimeoutError:
                    print("[matrix] S1 WS 连接确认: 5s 未收到（诊断）")
                # 先注册 thread→user 映射（前端切会话同款）：dispatch_stop 的
                # 租户解析读该注册表，未注册回退 default 会查不到任务 run
                await ws.send(
                    json.dumps({"type": "active_thread_changed", "thread_id": task_id})
                )
                await asyncio.sleep(0.5)
                await ws.send(
                    json.dumps(
                        {
                            "type": "stop_generation",
                            "thread_id": task_id,
                            "pipeline_id": task_id,
                            "reason": "e2e-matrix-stop",
                        }
                    )
                )
                # 收包窗口：打印一切回包（确认/错误帧可见）
                try:
                    for _ in range(3):
                        frame = await asyncio.wait_for(ws.recv(), timeout=3)
                        print(f"[matrix] S1 WS 回包: {str(frame)[:200]}")
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    print("[matrix] S1 WS 回包窗口结束")

        asyncio.run(_send_stop())

        # 循环停止：轮数在停止后稳定（有界）——本场景的核心回归断言
        time.sleep(3)
        count_after_stop = stub_llm.request_count("S1")
        time.sleep(20)
        count_settled = stub_llm.request_count("S1")
        assert count_settled - count_after_stop <= 1, (
            f"S1 停止后循环应终止（允许中断中的在飞轮），"
            f"实际停止时 {count_after_stop} 轮 → 20s 后 {count_settled} 轮"
        )
        # 停止的任务不得假完成
        row = _row_by_pipeline(_state_rows(stub_kernel, matrix_token), task_id)
        final_status = str((_state(row) if row else {}).get("task.status") or "")
        print(f"[matrix] S1 停止后 task.status={final_status or '-'} 轮数 {count_before}→{count_settled}")
        assert final_status != "completed", "S1 停止后任务不得假完成"


class TestP1ProjectAttachWorkspace:
    """P1 项目挂靠全链路：agent 用 project_create 建项目（无执行者）→ L1 父会话
    task_submit 带 project_id 派发子任务 → 子任务 workspace 真实落在项目文件夹
    的 worktree（branch=task/{id}），产出文件在 worktree 内。

    覆盖 2026-08-30 修复：挂 project 的子任务 workspace 曾被"继承父工作空间"
    吞掉（任务跑进会话目录）；以及 project_create 工具入口（杜绝 bash 调
    Python 的 WSL /mnt 路径坑）。
    """

    @pytest.mark.timeout(600)
    def test_project_attached_task_runs_in_project_worktree(
        self, stub_kernel, matrix_token, stub_llm, matrix_sessions
    ):
        # 项目标题不得含任何场景 marker：子任务挂靠项目后 context_build 注入
        # 项目上下文进管道消息，marker 串台会让 stub 误命中父脚本（fallback
        # 循环 → stalled）。
        project_goal = "P1项目e2e"

        def _step2_submit(body: dict[str, Any], index: int) -> dict[str, Any]:
            """第二步：从第一步 project_create 的真实输出提取 project_id，
            构造挂靠该项目的 task_submit 调用（worktree 拓扑缺省）。"""
            pid = _tool_result_project_id(body)
            args = _task_submit_args(child_marker=_M_P1C)
            args["project_id"] = pid
            args["workspace_mode"] = "worktree"
            return tool_call_step("task_submit", **args)

        # 父脚本先注册（注册序 = stub 匹配优先级）
        stub_llm.register(
            "P1P",
            _M_P1P,
            [
                tool_call_step("project_create", goal=project_goal),
                _step2_submit,
                text_step("P1 父会话：项目已建、子任务已挂靠提交。"),
            ],
        )
        # 子任务脚本（与 A1 同构：bash 生成 result.txt + task_evaluate）
        stub_llm.register(
            "P1C",
            _M_P1C,
            [
                tool_call_step(
                    "bash_execute",
                    action="execute",
                    command=_BASH_ECHO_OK + " && git add result.txt && git commit -m p1-output",
                ),
                tool_call_step(
                    "task_evaluate",
                    action="auto_complete",
                    summary="已用 bash_execute 生成 result.txt，任务完成。",
                ),
                text_step("P1 子任务：执行与评估均完成。"),
            ],
        )

        session_id = _create_session(stub_kernel, matrix_token, "e2e-matrix-p1")
        matrix_sessions(session_id)
        _chat(
            stub_kernel,
            matrix_token,
            session_id,
            f"请创建项目并派发一个挂靠该项目的任务。场景标记：{_M_P1P}",
        )

        # 项目已登记（project_create 真实执行：建文件夹 + git init + 登记）
        project_row = _poll_until(
            lambda: _project_row_by_title(stub_kernel, matrix_token, project_goal),
            _TERMINAL_WAIT_SECONDS,
        )
        assert project_row is not None, f"project_create 应登记项目 {project_goal}"
        project_id = str(project_row["id"])
        metadata = project_row.get("metadata") or {}
        project_path = str(metadata.get("path") or "")
        assert os.path.isdir(project_path), f"项目文件夹应存在: {project_path}"
        assert (Path(project_path) / ".git").is_dir(), f"项目文件夹应已 git init: {project_path}"

        # 子任务终态
        child_row = _poll_until(
            lambda: _row_by_marker(_state_rows(stub_kernel, matrix_token), _M_P1C),
            _TERMINAL_WAIT_SECONDS,
        )
        assert child_row is not None, f"state 聚合应出现 P1 子任务（marker {_M_P1C}）"
        child_id = str(child_row["pipeline_id"])
        child_state, seen = _wait_task_terminal(
            stub_kernel, matrix_token, child_id, _TERMINAL_WAIT_SECONDS
        )
        assert child_state.get("task.status") == "completed", (
            f"P1 子任务应 completed，实际 {child_state.get('task.status')}，序列 {seen}"
        )
        # 挂靠键
        assert str(child_state.get("task.parent_project_id") or "") == project_id, (
            f"子任务应挂靠项目 {project_id}，实际 "
            f"{child_state.get('task.parent_project_id')}"
        )
        # workspace 落位（问题1 修复的观察面）：worktree 拓扑 + 在项目文件夹上分叉
        ws_meta = child_state.get("ws_meta") or {}
        if isinstance(ws_meta, str):
            try:
                ws_meta = json.loads(ws_meta)
            except json.JSONDecodeError:
                ws_meta = {}
        assert ws_meta.get("mode") == "worktree", (
            f"挂项目子任务应 worktree 拓扑，实际 ws_meta={ws_meta}"
        )
        assert str(ws_meta.get("project_root") or "") == project_path, (
            f"worktree 源应为项目文件夹，实际 project_root={ws_meta.get('project_root')}"
        )
        worktree_dir = str(ws_meta.get("path") or "")
        assert worktree_dir, f"ws_meta 应记录 worktree 目录，实际 {ws_meta}"
        print(f"[matrix] P1 worktree 目录（终态可能已合并清理，仅诊断）: {worktree_dir}")
        # 分支契约：branch = task/{child_id}
        assert ws_meta.get("branch") == f"task/{child_id}", (
            f"worktree 分支应为 task/{child_id}，实际 {ws_meta.get('branch')}"
        )
        # 产出合并回项目文件夹（worktree 终态合并：分支提交合回主分支）。
        # 合并失败（保留文件）时产出仍在 worktree 目录——两路取一即真实执行证据。
        merged_out = Path(project_path) / "result.txt"
        kept_out = Path(worktree_dir) / "result.txt" if worktree_dir else None
        assert merged_out.exists() or (kept_out is not None and kept_out.exists()), (
            f"产出应落位：项目文件夹 {merged_out} 或 worktree {kept_out}"
        )
        _assert_real_llm_ran(stub_kernel, matrix_token, child_id, child_state, "P1 子任务")


# ============================================================
# 完成率混合批（R1）
# ============================================================


class TestCompletionRateBatch:
    """R1 完成率混合批：8 任务并发（6 成功设计 + 2 失败设计）→ 逐任务结局映射
    + 聚合完成率（输入条件推导的精确值 6/8=75%，非拍脑袋阈值）。

    比单任务路径多覆盖的行为面：并发批结局不串台（成功/失败互不污染）、
    无任务滞留（收束率 100%）、完成率口径（completed / 全部派发）。
    """

    # 单批窗口（轮询上限；实测 8 任务并发 ~1-3 分钟）
    _BATCH_WINDOW_SECONDS = 420

    @pytest.mark.timeout(480)
    def test_mixed_batch_reaches_design_completion_rate(
        self, stub_kernel, matrix_token, stub_llm
    ):
        _register_rate_batch_scripts(stub_llm)

        # 1. 批量创建（真实业务接口，background 并发派发独立管道）
        task_ids: dict[str, str] = {}  # task_id -> marker
        for marker in _M_RATE_S + [_M_RATE_F1, _M_RATE_F2]:
            task_ids[
                _create_task(
                    stub_kernel,
                    matrix_token,
                    f"完成率批 {marker}",
                    f"按场景标记执行并适时调用 task_evaluate 评估。场景标记：{marker}",
                )
            ] = marker
        assert len(task_ids) == 8, f"应创建 8 个任务，实际 {len(task_ids)}"

        # 2. 集体轮询至全部终态（收束率 100% 断言的数据基础）
        seen: dict[str, list[str]] = {tid: [] for tid in task_ids}
        terminal: dict[str, dict[str, Any]] = {}
        deadline = time.time() + self._BATCH_WINDOW_SECONDS
        while time.time() < deadline and len(terminal) < len(task_ids):
            rows = _state_rows(stub_kernel, matrix_token)
            for tid in task_ids:
                if tid in terminal:
                    continue
                row = _row_by_pipeline(rows, tid)
                if row is None:
                    continue
                state = _state(row)
                status_now = str(state.get("task.status") or "")
                if status_now and status_now not in seen[tid]:
                    seen[tid].append(status_now)
                if status_now in ("completed", "failed", "pending_evaluation"):
                    terminal[tid] = state
            time.sleep(_POLL_INTERVAL_SECONDS)

        # 3. 逐任务结局映射（真门禁）：成功组 completed、失败组 failed 且不得假完成
        completed: list[str] = []
        failed: list[str] = []
        for tid, marker in task_ids.items():
            assert tid in terminal, (
                f"收束率应 100%：{marker}（{tid}）未达终态，观测序列 {seen[tid]}"
            )
            state = terminal[tid]
            final_status = str(state.get("task.status") or "")
            if marker in _M_RATE_S:
                assert final_status == "completed", (
                    f"成功设计任务应 completed，实际 {final_status}，序列 {seen[tid]}"
                )
                completed.append(tid)
            else:
                assert final_status == "failed", (
                    f"失败设计任务应 failed，实际 {final_status}，序列 {seen[tid]}"
                )
                assert "completed" not in seen[tid], (
                    f"失败设计任务 {marker} 不得假完成，序列 {seen[tid]}"
                )
                failed.append(tid)
            _assert_real_llm_ran(stub_kernel, matrix_token, tid, state, f"R1 {marker}")
            assert not state.get("isolation.blocked"), (
                f"{marker} 不应被隔离拦截，blocked={state.get('isolation.blocked')}"
            )

        # 4. 聚合完成率（输入混合推导的精确值 6/8）+ 场景均被真实命中
        total = len(task_ids)
        rate = len(completed) / total
        assert len(completed) == len(_M_RATE_S), (
            f"完成数应 {len(_M_RATE_S)}，实际 {len(completed)}"
        )
        assert len(failed) == 2, f"失败数应 2，实际 {len(failed)}"
        assert rate == 6 / 8, f"完成率应 6/8=0.75，实际 {rate}"
        for marker in task_ids.values():
            assert stub_llm.request_count(marker) >= 2, (
                f"场景 {marker} 应被真实命中（>=2 轮 LLM），实际 "
                f"{stub_llm.request_count(marker)}（marker 串台/注入失败？）"
            )

        # 5. 逐任务核验报告（每个任务的真实终态与观测序列）
        print(f"[rate] 完成率 {rate:.0%}（{len(completed)}/{total}）")
        for tid, marker in task_ids.items():
            print(
                f"[rate]   {marker} task={tid} 终态={terminal[tid].get('task.status')} "
                f"序列={seen[tid]} 轮数={stub_llm.request_count(marker)}"
            )

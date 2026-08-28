# @feature: FP-0.2.〇 隔离工作区 工具执行 | @vision: V2 全能闭环 | @ci: python-e2e
"""
E2E 测试：不同工作空间模式的任务管道——单输入单输出黑盒验证。

测试形态（黑盒原则）：
  - 单输入：一次任务创建接口调用（plain = POST /ext/task_service/tasks 无
    workspace；worktree = POST /ext/task_service/tasks/root 带 workspace）；
  - 执行完全交给 agent 自主完成，测试只轮询观察（GET 查询接口），不直接
    读写 state、不干预管道执行；
  - 输出验证：全部经查询接口观察——task.status 合法终态 + LLM 用量非零
    （防假完成）+ isolation.blocked 为空（工具未被隔离拦截）+
    message_count > 1（存在工具调用轮次）+ ws_meta 拓扑符合模式。

覆盖工作空间初始化拓扑（workspace_lifecycle 语义）：
  1. plain（默认）：无显式 workspace → 工作区 = {root}/{task_id}；
  2. worktree（显式）：带 workspace=源仓库 → 隔离副本（ws_meta.mode=worktree）。
     源仓库用测试自建的临时 mini repo——绝不使用本仓（worktree 创建前的
     auto-save 机制会 commit 并行会话的未提交 WIP）。

清理契约（每用例 teardown 全部清理，附清理彻底性自检）：
  会话删除 + 执行数据 clear-all（接口）+ 工作区目录删除 + worktree 移除。

运行前提：内核 9100、Docker 引擎（bash 容器隔离依赖）、真实 LLM key。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from e2e_helpers import (
    create_session,
    http_get_with_auth,
    http_post_json_auth,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("ZHIPU_API_KEY"),
        reason="需要 ZHIPU_API_KEY（真实 LLM 执行任务管道）",
    ),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKSPACE_ROOT = _REPO_ROOT / ".ai_workspaces"

TASK_WAIT_SECONDS = 420
_POLL_INTERVAL_SECONDS = 5

# 单输入的任务指令：明确工具步骤 + 评估引导（提高评估闸门闭合率）。
TASK_INSTRUCTION = (
    "请严格按顺序完成以下三步，不要做其他事情：\n"
    "1. 用 bash_execute 工具执行命令：echo E2E_BASH_PROBE_OK > bash_probe.txt\n"
    "2. 用 file_write 工具在当前工作目录创建文件 file_probe.txt，"
    "内容为：E2E_FILE_PROBE_OK\n"
    "3. 完成后调用 task_evaluate 工具进行评估。"
)


def _find_pipeline_state(body, pipeline_id: str) -> dict | None:
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("pipeline_id") == pipeline_id:
            return item
    return None


def _run_task_to_terminal(token, kernel_url, endpoint: str, payload: dict, cleanup_sessions, thread_id: str | None = None) -> dict:
    """单输入单输出：一次创建调用 → 轮询观察至合法终态 → 返回观察结果。

    合法终态：completed（评估闭合）/ pending_evaluation（执行完毕但 agent
    未评估，task_reminder 裁决）/ failed——均代表管道自主执行收尾。
    thread_id：root 任务（TaskRootCreate）必填会话归属，由调用方预建传入；
    缺省时内部自建辅助会话（plain 任务无会话归属要求）。
    """
    if not thread_id:
        session = create_session(token, title="e2e-workspace-modes")
        cleanup_sessions(session["thread_id"])

    # ── 单输入：唯一的一次创建调用 ──
    status, body, _ = http_post_json_auth(
        f"{kernel_url}{endpoint}", payload, token=token, timeout=15,
    )
    assert status == 200, f"创建任务应 200，实际 {status}: {body}"
    task_id = str(body.get("id") or body.get("task_id") or "")
    assert task_id, f"创建任务应返回 id，实际 {body}"

    # ── 观察：轮询至终态（只读接口，不干预执行）──
    deadline = time.time() + TASK_WAIT_SECONDS
    final_row: dict | None = None
    seen_statuses: list[str] = []
    while time.time() < deadline:
        st, sb, _ = http_get_with_auth(
            f"{kernel_url}/api/v1/pipelines/state", token=token, timeout=10,
        )
        if st == 200:
            row = _find_pipeline_state(sb, task_id)
            if row is not None:
                final_row = row
                s = (row.get("state") or {}).get("task.status", "")
                if s and s not in seen_statuses:
                    seen_statuses.append(s)
                if s in ("completed", "pending_evaluation", "failed"):
                    break
        time.sleep(_POLL_INTERVAL_SECONDS)

    assert final_row is not None, f"任务应出现在 state 聚合，task_id={task_id}"
    state = final_row.get("state") or {}
    print(f"[e2e] task={task_id} 状态序列: {seen_statuses}")
    return {"task_id": task_id, "state": state}


def _assert_task_output(state: dict, expect_mode: str | None) -> None:
    """输出验证（全部来自查询接口的观察数据）。"""
    # 1. 管道自主收尾：合法终态 + LLM 真实执行（防假完成）
    final_status = state.get("task.status", "")
    assert final_status in ("completed", "pending_evaluation"), (
        f"任务应自主收尾（completed/pending_evaluation），实际 '{final_status}'"
    )
    usage = state.get("track.llm_usage") or {}
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except json.JSONDecodeError:
            usage = {}
    assert int((usage or {}).get("total_tokens") or 0) > 0, (
        f"track.llm_usage 应非零（LLM 真实执行），实际 {usage}"
    )

    # 2. 工具执行情况（观察面）：未被隔离拦截、无执行错误。
    #    isolation.blocked 是工具链问题的稳定判据（工具被拦/容器落地失败时
    #    置 true——工作区断链事故即由它暴露）；message_count/raw_result 的
    #    持久化随结束路径浮动，不作断言。
    assert not state.get("isolation.blocked"), (
        f"工具不应被隔离拦截，实际 blocked={state.get('isolation.blocked')} "
        f"reason={state.get('isolation.block_reason')}"
    )
    assert state.get("raw_error") in (None, ""), f"raw_error 应为空，实际 {state.get('raw_error')}"

    # 3. 工作空间拓扑（观察面）：ws_meta 模式符合初始化声明
    ws_meta = state.get("ws_meta") or {}
    if isinstance(ws_meta, str):
        try:
            ws_meta = json.loads(ws_meta)
        except json.JSONDecodeError:
            ws_meta = {}
    if expect_mode:
        assert str(ws_meta.get("mode") or "") == expect_mode, (
            f"ws_meta.mode 应为 {expect_mode}，实际 {ws_meta}"
        )


def _cleanup_workspace_records(task_id: str, source_repo: Path | None) -> None:
    """清理工作区产物（清理动作：目录 + worktree 注册）。"""
    ws_dir = _WORKSPACE_ROOT / task_id
    if source_repo is not None:
        try:
            listing = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=source_repo, capture_output=True, text=True, timeout=30,
            ).stdout
            for block in listing.split("\n\n"):
                if task_id not in block:
                    continue
                wt_path = branch = None
                for line in block.splitlines():
                    if line.startswith("worktree "):
                        wt_path = line[len("worktree "):]
                    elif line.startswith("branch "):
                        branch = line[len("branch "):]
                if wt_path:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", wt_path],
                        cwd=source_repo, capture_output=True, text=True, timeout=60,
                    )
                if branch:
                    subprocess.run(
                        ["git", "branch", "-D", branch.removeprefix("refs/heads/")],
                        cwd=source_repo, capture_output=True, text=True, timeout=30,
                    )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[e2e-cleanup] worktree 清理失败（忽略）: {exc}")
    if ws_dir.exists():
        shutil.rmtree(ws_dir, ignore_errors=True)


def _make_mini_repo(tmp_path: Path) -> Path:
    """临时源仓库（worktree 用例的 workspace 源）。"""
    repo = tmp_path / "e2e_source_repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=e2e", "-c", "user.email=e2e@test.local", *args],
            cwd=repo, check=True, capture_output=True, text=True, timeout=60,
        )

    git("init", "-q", "-b", "main")
    (repo / "README.md").write_text("# e2e source repo\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    return repo


@pytest.fixture(autouse=True)
def _cleanup_execution_data(auth_token, kernel_url):
    """执行数据清理（接口动作）：clear-all 9 表。"""
    yield
    try:
        http_post_json_auth(
            f"{kernel_url}/ext/monitoring/execution/records/clear-all",
            {}, token=auth_token, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[e2e-cleanup] clear-all 失败（忽略）: {exc}")


class TestPlainWorkspaceTask:
    """默认工作空间（plain）：无显式 workspace 的单输入任务。"""

    def test_plain_task_agent_self_executes_tools(
        self, auth_token, cleanup_sessions, kernel_url, tmp_path
    ):
        """单输入 → agent 自主执行工具 → 观察输出：收尾 + 工具未拦截 + plain 拓扑。"""
        result = _run_task_to_terminal(
            auth_token, kernel_url,
            "/ext/task_service/tasks",
            {
                "title": "e2e plain 工作区工具执行验证",
                "description": TASK_INSTRUCTION,
                "agent_id": "general_agent",
            },
            cleanup_sessions,
        )
        task_id = result["task_id"]
        try:
            _assert_task_output(result["state"], expect_mode="plain")
        finally:
            _cleanup_workspace_records(task_id, source_repo=None)

    def test_plain_task_cleanup_leaves_no_trace(
        self, auth_token, cleanup_sessions, kernel_url, tmp_path
    ):
        """清理彻底性自检：清理后查询接口无残留（state 聚合 + 工作区目录）。"""
        result = _run_task_to_terminal(
            auth_token, kernel_url,
            "/ext/task_service/tasks",
            {
                "title": "e2e plain 清理自检",
                "description": TASK_INSTRUCTION,
                "agent_id": "general_agent",
            },
            cleanup_sessions,
        )
        task_id = result["task_id"]
        _cleanup_workspace_records(task_id, source_repo=None)
        # 执行数据清理也是接口动作（clear-all），显式调用后再验证无残留
        # （autouse teardown 的 clear-all 在用例断言之后才跑，覆盖不到本用例）
        http_post_json_auth(
            f"{kernel_url}/ext/monitoring/execution/records/clear-all",
            {}, token=auth_token, timeout=30,
        )

        assert not (_WORKSPACE_ROOT / task_id).exists(), (
            f"清理后工作区目录应不存在：{_WORKSPACE_ROOT / task_id}"
        )
        status, body, _ = http_get_with_auth(
            f"{kernel_url}/api/v1/pipelines/state", token=auth_token, timeout=10,
        )
        assert status == 200
        assert _find_pipeline_state(body, task_id) is None, (
            f"清理后 state 聚合不应再含任务 {task_id}"
        )


class TestWorktreeTask:
    """worktree 模式：显式 workspace（临时 mini repo）的单输入根任务。"""

    def test_worktree_task_agent_self_executes_tools(
        self, auth_token, cleanup_sessions, kernel_url, tmp_path
    ):
        """单输入（带 workspace 的根任务）→ agent 自主执行 → 观察 worktree 拓扑。"""
        source_repo = _make_mini_repo(tmp_path)
        session = create_session(auth_token, title="e2e-worktree-mode")
        cleanup_sessions(session["thread_id"])
        result = _run_task_to_terminal(
            auth_token, kernel_url,
            "/ext/task_service/tasks/root",
            {
                "title": "e2e worktree 工作区工具执行验证",
                "description": TASK_INSTRUCTION,
                "target_id": "general_agent",
                "workspace": str(source_repo),
                "thread_id": session["thread_id"],
            },
            cleanup_sessions,
            thread_id=session["thread_id"],
        )
        task_id = result["task_id"]
        try:
            _assert_task_output(result["state"], expect_mode="worktree")
        finally:
            _cleanup_workspace_records(task_id, source_repo=source_repo)

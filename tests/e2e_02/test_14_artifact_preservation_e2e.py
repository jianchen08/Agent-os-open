# @feature: FP-0.2.〇 隔离工作区 工具执行 | @vision: V2 全能闭环 | @ci: python-e2e
"""
E2E 测试：任务 completed 后的产物保全（B5「shared 模式产物蒸发」边界）。

背景（docs/working/长稳测试bug清单_20260904.md B5★）：长稳 4732fb1c64d5 族
实测，shared 模式任务（未挂靠项目）产物写入共享工作区；completed 门控对
mode!=worktree 放行（无合并、无保送），工作区被清理后产物唯一副本蒸发。

被测不变量（产物保全契约）：**任务 completed 后，产物可恢复性不得依赖
「工作区目录继续存在」**——产物必须存在工作区之外的持久副本（合并到挂靠
项目根 / 归档保送），或 plain 语义下工作区本身保留。

用例矩阵（单输入单输出黑盒，观察面全走查询接口 + 磁盘只读）：
  1. worktree 对照（绿）：completed 后产物已合并到源仓库——test_13 只断言
     拓扑不断言产物，本用例补上「合并+清理一体」的合并半边；
  2. shared 蒸发复现（当前红★）：子任务共享父工作区写入产物 → completed →
     断言产物在工作区之外存在持久副本 → 当前实现无任何保送，红；
  3. plain 对照（绿）：plain 语义 = 工作区保留，completed 后产物仍在工作区。

运行前提：内核 9100、Docker 引擎（bash 容器隔离依赖）、真实 LLM key、
测试进程与内核同机（磁盘观察面）。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
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
_PROJECTS_ROOT = _REPO_ROOT / "projects"

TASK_WAIT_SECONDS = 420
_POLL_INTERVAL_SECONDS = 5

TASK_INSTRUCTION = (
    "请严格按顺序完成以下两步，不要做其他事情：\n"
    "1. 用 file_write 工具在当前工作目录创建文件 file_probe.txt，"
    "内容为：E2E_FILE_PROBE_OK\n"
    "2. 完成后调用 task_evaluate 工具进行评估。"
)


def _parse_state_dict(value) -> dict:  # noqa: ANN001
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _find_pipeline_state(body, pipeline_id: str) -> dict | None:  # noqa: ANN001
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("pipeline_id") == pipeline_id:
            return item
    return None


def _poll_until(poll_fn, timeout_seconds: int, what: str):  # noqa: ANN001
    """通用轮询：poll_fn() 返回非 None 即止，超时 pytest.fail。"""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        found = poll_fn()
        if found is not None:
            return found
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(f"轮询超时（{timeout_seconds}s）：{what}")


def _run_plain_or_worktree_task(token, kernel_url: str, endpoint: str, payload: dict, thread_id: str | None) -> dict:
    """单输入：一次创建调用；返回 task_id。终态轮询由用例自行观察。"""
    status, body, _ = http_post_json_auth(
        f"{kernel_url}{endpoint}", payload, token=token, timeout=15,
    )
    assert status == 200, f"创建任务应 200，实际 {status}: {body}"
    task_id = str(body.get("id") or body.get("task_id") or "")
    assert task_id, f"创建任务应返回 id，实际 {body}"
    return {"task_id": task_id}


def _wait_task_terminal(token, kernel_url: str, task_id: str) -> dict:
    """轮询任务到合法终态，返回该管道的 state 聚合行。"""
    def _poll() -> dict | None:
        st, sb, _ = http_get_with_auth(
            f"{kernel_url}/api/v1/pipelines/state", token=token, timeout=10,
        )
        if st != 200:
            return None
        row = _find_pipeline_state(sb, task_id)
        if row is None:
            return None
        status = str((row.get("state") or {}).get("task.status") or "")
        if status in ("completed", "pending_evaluation", "failed"):
            return row
        return None

    row = _poll_until(_poll, TASK_WAIT_SECONDS, f"任务 {task_id} 到达合法终态")
    return (row.get("state") or {})


def _cleanup_workspace_dir(task_id: str) -> None:
    ws_dir = _WORKSPACE_ROOT / task_id
    if ws_dir.exists():
        shutil.rmtree(ws_dir, ignore_errors=True)


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


def _make_mini_repo(tmp_path: Path) -> Path:
    """临时源仓库（worktree 用例的 workspace 源）。"""
    import subprocess

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


class TestWorktreeEscortProducts:
    """worktree 对照（绿）：completed 后产物应已合并到源仓库。

    test_13::TestWorktreeTask 只断言 ws_meta.mode=worktree（拓扑），从未断言
    产物落点——「合并+清理一体」的合并半边无测试持有，本用例补上。
    """

    def test_worktree_completed_products_merged_to_source_repo(
        self, auth_token, cleanup_sessions, kernel_url, tmp_path
    ):
        token = auth_token
        source_repo = _make_mini_repo(tmp_path)
        session = create_session(token, title="e2e-worktree-escort")
        cleanup_sessions(session["thread_id"])

        created = _run_plain_or_worktree_task(
            token, kernel_url,
            "/ext/task_service/tasks/root",
            {
                "title": "e2e worktree 产物保送验证",
                "description": TASK_INSTRUCTION,
                "target_id": "general_agent",
                "workspace": str(source_repo),
                "thread_id": session["thread_id"],
            },
            thread_id=session["thread_id"],
        )
        task_id = created["task_id"]
        try:
            state = _wait_task_terminal(token, kernel_url, task_id)
            ws_meta = _parse_state_dict(state.get("ws_meta"))
            assert str(ws_meta.get("mode")) == "worktree", f"应 worktree 模式: {ws_meta}"

            probe = source_repo / "file_probe.txt"
            assert probe.exists(), (
                f"worktree 任务 completed 后产物应已合并到源仓库: {source_repo}"
            )
            assert "E2E_FILE_PROBE_OK" in probe.read_text(encoding="utf-8")
        finally:
            import subprocess

            subprocess.run(
                ["git", "worktree", "prune"], cwd=source_repo,
                capture_output=True, text=True, timeout=60, check=False,
            )


class TestSharedModeArtifactEscort:
    """B5★复现（当前 xfail）：shared 子任务 completed 后产物须有工作区外持久副本。

    生产实证形态（4732fb1c64d5 族）：未挂靠 shared 任务的产物写入共享工作区
    （会话目录），completed 门控放行后无任何保送，工作区清理即蒸发。
    本用例断言产物保全不变量：completed 后产物在工作区之外存在可恢复副本
    （合并目标或归档，位置不锁定——修复落地即转绿）。
    """

    @pytest.mark.xfail(
        strict=True,
        reason="B5★ shared 产物蒸发未修（completed 放行后无保送）；修复落地后本标记必须移除转正",
    )
    def test_shared_completed_product_has_persistent_copy_outside_workspace(
        self, auth_token, cleanup_sessions, kernel_url, tmp_path
    ):
        token = auth_token
        artifact_token = uuid.uuid4().hex[:8]
        artifact_name = f"shared_artifact_{artifact_token}.txt"
        artifact_content = "E2E_SHARED_ARTIFACT_OK"

        session = create_session(token, title="e2e-shared-escort")
        cleanup_sessions(session["thread_id"])

        # ── 单输入：创建父任务，引导派发写产物的子任务 ──
        status, body, _ = http_post_json_auth(
            f"{kernel_url}/ext/task_service/tasks",
            {
                "title": "e2e shared 产物保送验证（父）",
                "description": (
                    "请调用 task_submit 工具派发一个子任务，子任务要求如下，"
                    "原样传达不要改写：标题=shared 产物写入验证，"
                    f"描述=请严格按顺序完成两步：1. 用 file_write 工具在当前工作"
                    f"目录创建文件 {artifact_name}，内容为：{artifact_content}；"
                    "2. 完成后调用 task_evaluate 工具进行评估。"
                    "目标 agent=general_agent。派发后汇报子任务 ID。"
                ),
                "agent_id": "agentos",
            },
            token=token,
            timeout=15,
        )
        assert status == 200, f"创建父任务应 200，实际 {status}: {body}"
        parent_id = str(body.get("id") or body.get("task_id") or "")
        assert parent_id, f"父任务应返回 id，实际 {body}"

        try:
            # ── 观察：子任务出生 → completed → 读共享工作区路径 ──
            def _find_sub():
                st, sb, _ = http_get_with_auth(
                    f"{kernel_url}/api/v1/pipelines/state", token=token, timeout=10,
                )
                if st != 200:
                    return None
                for row in sb.get("items") or []:
                    s = row.get("state") or {}
                    if str(s.get("lineage.parent_pipeline_id") or "") == parent_id:
                        return row
                return None

            sub_row = _poll_until(_find_sub, 300, "子任务出生（lineage 指向父）")
            sub_id = str(sub_row.get("pipeline_id"))
            sub_state = _wait_task_terminal(token, kernel_url, sub_id)

            ws_meta = _parse_state_dict(
                sub_state.get("ws_meta") or sub_state.get("task.ws_meta")
            )
            assert str(ws_meta.get("mode")) == "shared", (
                f"子任务应 shared 模式（复现前提），实际 {ws_meta}"
            )
            shared_ws = Path(str(ws_meta.get("path") or ""))
            assert shared_ws.is_dir(), f"共享工作区应存在: {shared_ws}"

            # ── 前置实锤：产物确实写在共享工作区（唯一副本）──
            in_ws = shared_ws / artifact_name
            assert in_ws.exists(), (
                f"前置实锤失效：产物应已写入共享工作区 {in_ws}（否则复现不成立）"
            )

            # ── 产物保全不变量：completed 后工作区之外必须存在持久副本 ──
            def _copy_outside_workspace() -> list[Path]:
                hits: list[Path] = []
                for domain in (_WORKSPACE_ROOT, _PROJECTS_ROOT):
                    if not domain.is_dir():
                        continue
                    for dirpath, dirnames, filenames in os.walk(domain):
                        dirnames[:] = [d for d in dirnames if d != ".git"]
                        if artifact_name in filenames:
                            f = Path(dirpath) / artifact_name
                            if f.resolve().is_relative_to(shared_ws.resolve()):
                                continue
                            try:
                                if artifact_content in f.read_text(encoding="utf-8"):
                                    hits.append(f)
                            except OSError:
                                continue
                return hits

            hits = _poll_until(
                lambda: _copy_outside_workspace() or None,
                30,
                "产物在工作区之外出现持久副本（合并/归档保送）",
            )
            assert hits, (
                "B5 产物保全契约违反：shared 任务 completed 后，产物仅存在于"
                f"共享工作区 {shared_ws}（唯一副本），工作区之外（{_WORKSPACE_ROOT} / "
                f"{_PROJECTS_ROOT}）无任何可恢复副本——工作区一旦被清理产物即蒸发"
            )
        finally:
            _cleanup_workspace_dir(parent_id)


class TestPlainRetainedSemantics:
    """plain 对照（绿）：plain 语义 = 工作区保留，completed 后产物仍在工作区。

    防保送修复误伤 plain：plain 无清理动作（cleanup_workspace 对 plain 保留
    目录），产物留在工作区即满足保全不变量。
    """

    def test_plain_completed_products_retained_in_workspace(
        self, auth_token, cleanup_sessions, kernel_url
    ):
        token = auth_token
        session = create_session(token, title="e2e-plain-retained")
        cleanup_sessions(session["thread_id"])

        created = _run_plain_or_worktree_task(
            token, kernel_url,
            "/ext/task_service/tasks",
            {
                "title": "e2e plain 工作区保留验证",
                "description": TASK_INSTRUCTION,
                "agent_id": "general_agent",
            },
            thread_id=None,
        )
        task_id = created["task_id"]
        try:
            state = _wait_task_terminal(token, kernel_url, task_id)
            ws_meta = _parse_state_dict(state.get("ws_meta"))
            assert str(ws_meta.get("mode")) == "plain", f"应 plain 模式: {ws_meta}"
            ws_path = Path(str(ws_meta.get("path") or ""))
            probe = ws_path / "file_probe.txt"
            assert probe.exists(), (
                f"plain 任务 completed 后工作区应保留，产物应仍在: {probe}"
            )
            assert "E2E_FILE_PROBE_OK" in probe.read_text(encoding="utf-8")
        finally:
            _cleanup_workspace_dir(task_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

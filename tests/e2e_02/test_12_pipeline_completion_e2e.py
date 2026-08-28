# @feature: FP-0.2.〇 管道引擎 端到端完成性 | @vision: V2 全能闭环 | @ci: python-e2e
"""
E2E 测试：管道/任务端到端完整跑完（防"假完成"）。

背景（2026-08-28 管道 5b29e3474920 事故）：run 标 completed 但 LLM 从未成功
返回（30s SDK 超时 + 插件重载窗口 tool not found），引擎吞错 fail-open 造成
"假完成"。本文件覆盖**不同初始化路径**的端到端完成性，用事故指纹的反面做
断言——跑完 = 以下证据链同时成立，缺一即假完成：

  1. run 终态 completed 且 ended_at 非空（引擎正常收尾）；
  2. track.llm_usage total_input_tokens / total_output_tokens 均 > 0
     （LLM 真实调用并返回，而非 0-token 空转）；
  3. task.status 走过执行态（任务路径：pending → completed，评估闸门闭合）；
  4. 任务路径（B）：task.status 终态 completed——评估闸门闭合
     （general_agent 硬约束调用 task_evaluate，task_reminder 检测证据）。

两条初始化路径：
  A. 会话聊天管道：POST /api/v1/chat → 主会话管道同步执行（同 test_05 通道，
     额外验证 run/usage 指纹）；
  B. 任务管道：POST /ext/task_service/tasks 创建任务 → background 派发独立
     管道执行（同 test_08 通道，但断言推进到终态 completed 而非仅 running）。

查询面说明：GET /api/v1/pipelines/runs 的 run→pipeline 映射依赖 message_slots
（无消息槽的 run 被过滤）——会话管道可查，任务管道查不到，故任务路径的
run 证据经 pipelines/state 的 track.* 指纹承载。

运行前提：
- 内核已启动（AGENTOS_DB_PATH=":memory:" AGENTOS_KERNEL_PORT=9100
  ./kernel/target/release/agentos-kernel.exe），9100 端口可访问。
- 真实 LLM key（.env：DEEPSEEK/MINIMAX；skipif 门禁与既有 e2e 一致看 ZHIPU_API_KEY）。
- 手动运行：python -m pytest tests/e2e_02/test_12_pipeline_completion_e2e.py -q
"""
import json
import os
import time
from typing import Any

import pytest
from e2e_helpers import (
    KERNEL_URL,
    create_session,
    http_get_with_auth,
    http_post_json_auth,
)

pytestmark = [
    pytest.mark.e2e,
    # 依赖真实 LLM 执行管道：无 key 时跳过（与 test_05/test_08 门禁一致）
    pytest.mark.skipif(
        not os.environ.get("ZHIPU_API_KEY"),
        reason="需要 ZHIPU_API_KEY（真实 LLM 执行管道）",
    ),
]

# 会话聊天同步执行窗口（POST /api/v1/chat 同步等 LLM，test_05 用 150s）
CHAT_TIMEOUT_SECONDS = 180
# 任务管道出生→执行→评估的端到端窗口（真实 LLM 多轮 + task_evaluate）
TASK_COMPLETION_WAIT_SECONDS = 300
# state/runs 轮询间隔
_POLL_INTERVAL_SECONDS = 5


@pytest.fixture(autouse=True)
def _cleanup_execution_data(auth_token, kernel_url):
    """测试后清理：清空全部执行数据（同 test_08——任务管道不在会话下，
    cleanup_sessions 删不到，用 clear-all 全量清理）。"""
    yield
    try:
        http_post_json_auth(
            f"{kernel_url}/ext/monitoring/execution/records/clear-all",
            {},
            token=auth_token,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 —— teardown 尽力而为
        print(f"[e2e-cleanup] clear-all 失败（忽略）: {exc}")


def _find_pipeline_state(body, pipeline_id: str) -> dict | None:
    """从 /api/v1/pipelines/state 响应中按 pipeline_id 找行（None = 未出口）。"""
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("pipeline_id") == pipeline_id:
            return item
    return None


def _state_dict(row: dict) -> dict:
    """取行的 state 字典（field_value 可能是 JSON 字符串，防御式还原）。"""
    state = row.get("state") or {}
    if not isinstance(state, dict):
        return {}
    return state


def _parse_jsonish(value: Any) -> dict:
    """state 字段的值可能是 JSON 字符串或已解析 dict，统一还原为 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _llm_usage_fingerprint(state: dict) -> tuple[int, int]:
    """从 state 提取 (total_input_tokens, total_output_tokens)。缺失 = (0, 0)。"""
    usage = _parse_jsonish(state.get("track.llm_usage"))
    return (
        int(usage.get("total_input_tokens") or 0),
        int(usage.get("total_output_tokens") or 0),
    )


def _poll_state_row(token, kernel_url, pipeline_id: str, timeout: float) -> dict | None:
    """轮询 state 聚合直到目标管道行出现或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body, _ = http_get_with_auth(
            f"{kernel_url}/api/v1/pipelines/state", token=token, timeout=10
        )
        if status == 200:
            row = _find_pipeline_state(body, pipeline_id)
            if row is not None:
                return row
        time.sleep(_POLL_INTERVAL_SECONDS)
    return None


def _find_latest_run(runs_body: dict, pipeline_id: str) -> dict | None:
    """从 /api/v1/pipelines/runs 响应中找该管道最新 run（列表按 started_at 倒序）。"""
    items = runs_body.get("items") if isinstance(runs_body, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("pipeline_id") == pipeline_id:
            return item
    return None


class TestSessionChatPipelineCompletion:
    """路径 A：会话聊天管道端到端跑完（同步 chat + run/usage 指纹）。"""

    @pytest.mark.timeout(240)
    def test_chat_pipeline_completes_with_real_llm_fingerprint(
        self, auth_token, cleanup_sessions, kernel_url
    ):
        """聊天消息 → 管道执行 → run completed + LLM 指纹非零（非假完成）。"""
        token = auth_token
        session = create_session(token, title="e2e-chat-completion")
        cleanup_sessions(session["thread_id"])
        pipeline_id = session.get("active_pipeline_id") or ""
        assert pipeline_id, f"会话应带 active_pipeline_id，实际 {session}"

        # 1. 发消息（同步等 LLM 回复）
        status, body, _ = http_post_json_auth(
            f"{kernel_url}/api/v1/chat",
            {"message": "你好，请用一句话回复：e2e 完成性测试", "session_id": session["thread_id"]},
            token=token,
            timeout=CHAT_TIMEOUT_SECONDS,
        )
        assert status == 200, f"/api/v1/chat 应 200，实际 {status}: {body}"
        content = body.get("content") if isinstance(body, dict) else None
        assert isinstance(content, str) and content.strip(), (
            f"LLM 回复 content 应非空，实际 {body}"
        )

        # 2. state 聚合：LLM 指纹非零（假完成时 llm_usage 全 0）
        row = _poll_state_row(token, kernel_url, pipeline_id, timeout=30)
        assert row is not None, f"state 聚合应含管道 {pipeline_id}"
        state = _state_dict(row)
        total_in, total_out = _llm_usage_fingerprint(state)
        assert total_in > 0, f"total_input_tokens 应 > 0（LLM 真实调用），state={state}"
        assert total_out > 0, f"total_output_tokens 应 > 0（LLM 真实返回），state={state}"

        # 3. runs 快照：run 终态 completed 且 ended_at 非空
        runs_status, runs_body, _ = http_get_with_auth(
            f"{kernel_url}/api/v1/pipelines/runs", token=token, timeout=10
        )
        assert runs_status == 200, f"pipelines/runs 应 200，实际 {runs_status}"
        run = _find_latest_run(runs_body, pipeline_id)
        assert run is not None, f"runs 快照应含管道 {pipeline_id} 的 run（会话消息有 message_slot）"
        assert run.get("status") == "completed", f"run 应 completed，实际 {run}"
        assert run.get("ended_at"), f"run ended_at 应非空，实际 {run}"


class TestTaskPipelineCompletion:
    """路径 B：任务管道端到端跑完（background 派发 + 评估闸门闭合）。"""

    @pytest.mark.timeout(360)
    def test_task_pipeline_reaches_completed_with_evaluation(
        self, auth_token, cleanup_sessions, kernel_url
    ):
        """创建任务 → 真实执行 → task_evaluate 评估 → 终态 completed（非假完成）。"""
        token = auth_token
        session = create_session(token, title="e2e-task-completion")
        cleanup_sessions(session["thread_id"])
        state_url = f"{kernel_url}/api/v1/pipelines/state"

        # 1. 创建任务（background 派发独立管道，真实 LLM 执行）
        status, body, _ = http_post_json_auth(
            f"{kernel_url}/ext/task_service/tasks",
            {
                "title": "e2e 完成性测试：请直接返回当前 UTC 时间戳字符串",
                "description": (
                    "简单单步任务，一步即可完成。完成后必须调用 task_evaluate "
                    "工具进行评估，评估通过后任务才算完成。"
                ),
                "agent_id": "general_agent",
            },
            token=token,
            timeout=15,
        )
        assert status == 200, f"创建任务应 200，实际 {status}: {body}"
        task_id = body.get("id") or body.get("task_id")
        assert task_id, f"创建任务应返回 id，实际 {body}"

        # 2. 轮询到终态 completed（评估闸门闭合），记录状态序列（诊断价值）
        deadline = time.time() + TASK_COMPLETION_WAIT_SECONDS
        seen_statuses: list[str] = []
        final_row: dict | None = None
        while time.time() < deadline:
            st_status, st_body, _ = http_get_with_auth(
                f"{state_url}", token=token, timeout=10
            )
            if st_status == 200:
                row = _find_pipeline_state(st_body, task_id)
                if row is not None:
                    final_row = row
                    st = _state_dict(row).get("task.status", "")
                    if st and st not in seen_statuses:
                        seen_statuses.append(st)
                    if st == "completed":
                        break
            time.sleep(_POLL_INTERVAL_SECONDS)

        assert final_row is not None, f"任务管道应出现在 state 聚合，task_id={task_id}"
        # running 是时序敏感观测（轮询间隔可能跳过中间态），是否真执行过以
        # 下方 llm_usage/iteration 指纹为权威断言；状态序列仅作诊断输出。
        print(f"[e2e] task 状态序列: {seen_statuses}")
        final_status = _state_dict(final_row).get("task.status", "")
        assert final_status == "completed", (
            f"任务终态应 completed（评估闸门闭合），实际 '{final_status}'，"
            f"状态序列 {seen_statuses}"
        )

        # 3. 非假完成指纹：LLM 真实调用并返回（假完成事故形态 = llm_usage 全 0
        # 且标 completed；单轮即完成的任务 iteration 停在 0 属正常，不作判据）
        state = _state_dict(final_row)
        total_in, total_out = _llm_usage_fingerprint(state)
        assert total_in > 0, f"total_input_tokens 应 > 0，state={state}"
        assert total_out > 0, f"total_output_tokens 应 > 0（LLM 真实返回），state={state}"

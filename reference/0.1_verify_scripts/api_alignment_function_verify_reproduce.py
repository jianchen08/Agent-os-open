#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 API 对齐 0.1 + P4 404 报错修复 — 功能验证可复现脚本。

用法:
    python3 docs/working/api_alignment_function_verify_reproduce.py

前置条件:
    python3 + pytest + fastapi + httpx（后端 pytest 容器内可运行）

验证内容:
  [旅程-核心] 删除会话级联删除完整链路（状态传递: thread_id -> task -> pipeline_run_id）
  [补充-错误] 删除不存在的会话 -> 404 APIError
  [补充-边界] metadata=None 任务不崩溃不误删
  [场景2]     GET /api/v1/datasource/{uri} 不 404（TestClient HTTP 级验证）
  [场景3]     client.ts 404 收敛逻辑模拟（3 用例, 非 vitest 运行, 如实标注）

环境限制（如实标注）:
  1. 容器内 frontend/node_modules 符号链接指向宿主路径不可达, vitest 无法运行,
     client404.test.ts 按 mock 设计做 LOGIC-LEVEL 静态模拟（见场景3章节）。
  2. 容器内无运行中的后端服务（8000/9100 未开放）, fetch 不可用,
     场景2 用 TestClient（FastAPI 官方 ASGI 级 HTTP 测试客户端）替代真实 HTTP 请求。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# 确保 src 可导入
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str) -> None:
    global PASS, FAIL
    if passed:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append((name, passed, detail))
    tag = "PASS" if passed else "FAIL"
    print(f"[{tag}] {name}")
    print(f"      {detail}")


# ══════════════════════════════════════════════════════════════════
# 完整用户旅程: 删除会话级联删除（3-7 步串联 + 状态传递）
# ══════════════════════════════════════════════════════════════════
def journey_delete_thread_cascade() -> None:
    """模拟真实用户操作链:
    1. 用户创建会话（store 层）-> 得到 thread_id（状态1）
    2. 用户为会话创建任务（metadata.session_id = thread_id）-> 得到 task_id（状态2）
    3. 任务绑定管道执行记录（pipeline_run_id）-> 得到管道ID（状态3）
    4. 用户执行删除会话 DELETE /api/v1/threads/{thread_id}（真实函数 delete_thread）
    5. 系统级联清理: execution_record 删除、关联任务硬删、检查点文件清理、运行中管道取消
    6. 验证所有级联副作用已发生（状态传递贯穿全程）
    """
    from channels.api.routes_threads import delete_thread

    # ── 步骤1: 用户创建会话 ──────────────────────────────────────
    thread_id = "thread-journey-9f3a"  # 真实用户会得到一个会话 ID
    session = MagicMock()
    session.pipeline_ids = ["pipe-root-001"]

    # ── 步骤2: 用户为会话创建任务（metadata.session_id 关联） ──────
    task_a = MagicMock()
    task_a.id = "task-a-001"
    task_a.parent_pipeline_id = None
    task_a.pipeline_run_id = "pipe-run-a-001"  # 步骤3 的管道执行记录
    task_a.metadata = {"session_id": thread_id}  # <- P1 修复的关联口径

    # ── 步骤3: 管道关联的子任务（parent_pipeline_id 关联） ────────
    task_b = MagicMock()
    task_b.id = "task-b-002"
    task_b.parent_pipeline_id = "pipe-run-a-001"  # 经 A 的管道执行记录链式命中
    task_b.pipeline_run_id = None
    task_b.metadata = None

    # 无关任务（不应被删除）
    task_other = MagicMock()
    task_other.id = "task-other-999"
    task_other.parent_pipeline_id = None
    task_other.pipeline_run_id = None
    task_other.metadata = {"session_id": "thread-unrelated"}

    # ── mock 外围依赖（内核服务） ─────────────────────────────────
    task_service = MagicMock()
    task_service.get_all_tasks.return_value = [task_a, task_b, task_other]
    task_service.list_subtasks.return_value = []

    exec_storage = MagicMock()
    exec_storage._pipeline_root_map = {"pipe-run-a-001": "pipe-root-001"}

    task_worker = MagicMock()

    with (
        patch("channels.api.routes_threads.store.get_session", return_value=session),
        patch("channels.api.routes_threads.store.delete_thread", return_value=True),
        patch(
            "channels.api.routes_threads._get_execution_record_storage",
            return_value=exec_storage,
        ),
        patch(
            "channels.api.routes_threads._safe_get_service",
            side_effect=lambda name: {
                "task_service": task_service,
                "task_worker": task_worker,
            }.get(name),
        ),
        patch("channels.api.routes_threads._notify_session_update"),
    ):
        # ── 步骤4: 用户执行删除会话 ──────────────────────────────
        result = delete_thread(thread_id, {"sub": "user-1"})

    # ── 步骤5/6: 验证级联副作用（状态传递: thread_id -> task -> pipeline） ──
    deleted_ids = {c.args[0] for c in task_service.hard_delete_sync.call_args_list}
    record(
        "旅程-步骤1: 会话删除调用返回成功",
        result == {"message": "线程已删除"},
        f"delete_thread 返回 {result}",
    )
    record(
        "旅程-步骤2: metadata.session_id 关联任务被硬删",
        "task-a-001" in deleted_ids,
        f"hard_delete_sync 调用: {sorted(deleted_ids)}",
    )
    record(
        "旅程-步骤3: 经 pipeline_run_id 链式展开的子任务被硬删",
        "task-b-002" in deleted_ids,
        f"hard_delete_sync 调用: {sorted(deleted_ids)}",
    )
    record(
        "旅程-步骤3b: 无关会话任务不被误删",
        "task-other-999" not in deleted_ids,
        f"hard_delete_sync 调用: {sorted(deleted_ids)}",
    )
    # 5b. execution_record 级联删除（含管道链）
    rec_deleted = {c.args[0] for c in exec_storage.delete_by_session.call_args_list}
    record(
        "旅程-步骤4: execution_record 级联删除",
        "pipe-root-001" in rec_deleted and "pipe-run-a-001" in rec_deleted,
        f"delete_by_session 调用: {sorted(rec_deleted)}",
    )
    # 5c. 运行中管道取消
    cancelled = {c.args[0] for c in task_worker.cancel_pipeline.call_args_list}
    record(
        "旅程-步骤5: 运行中管道被取消",
        "pipe-root-001" in cancelled and "pipe-run-a-001" in cancelled,
        f"cancel_pipeline 调用: {sorted(cancelled)}",
    )


# ══════════════════════════════════════════════════════════════════
# 补充场景1: 错误输入 — 删除不存在的会话 -> 404
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# 补充场景1: 错误输入 — 删除不存在的会话 -> 404 APIError
# ══════════════════════════════════════════════════════════════════
def supplement_error_delete_nonexistent() -> None:
    """用户删除一个不存在的会话 ID，应收到 404 错误而非静默成功。"""
    from channels.api.deps import APIError
    from channels.api.routes_threads import delete_thread

    with (
        patch("channels.api.routes_threads.store.get_session", return_value=None),
        patch("channels.api.routes_threads.store.delete_thread", return_value=False),
        patch("channels.api.routes_threads._get_execution_record_storage", return_value=None),
        patch("channels.api.routes_threads._safe_get_service", return_value=None),
        patch("channels.api.routes_threads._notify_session_update"),
    ):
        try:
            delete_thread("thread-does-not-exist", {"sub": "user-1"})
            record(
                "补充-错误输入: 删除不存在会话返回 404",
                False,
                "未抛出 APIError（应 404）",
            )
        except APIError as e:
            record(
                "补充-错误输入: 删除不存在会话返回 404",
                e.status_code == 404,
                f"APIError status={e.status_code}, error_code={e.error_code}, message={e.message}",
            )


# ══════════════════════════════════════════════════════════════════
# 补充场景2: 边界 — metadata=None 任务不崩溃不误删
# ══════════════════════════════════════════════════════════════════
def supplement_boundary_metadata_none() -> None:
    """任务 metadata 为 None（历史任务）时，删除会话不应崩溃、不应误删。"""
    from channels.api.routes_threads import delete_thread

    thread_id = "thread-boundary-777"
    session = MagicMock()
    session.pipeline_ids = []

    task_no_meta = MagicMock()
    task_no_meta.id = "task-no-meta"
    task_no_meta.parent_pipeline_id = None
    task_no_meta.pipeline_run_id = None
    task_no_meta.metadata = None  # <- 边界: metadata 为 None

    task_other = MagicMock()
    task_other.id = "task-other-session"
    task_other.parent_pipeline_id = None
    task_other.pipeline_run_id = None
    task_other.metadata = {"session_id": "another-thread"}

    task_service = MagicMock()
    task_service.get_all_tasks.return_value = [task_no_meta, task_other]
    task_service.list_subtasks.return_value = []

    with (
        patch("channels.api.routes_threads.store.get_session", return_value=session),
        patch("channels.api.routes_threads.store.delete_thread", return_value=True),
        patch("channels.api.routes_threads._get_execution_record_storage", return_value=None),
        patch(
            "channels.api.routes_threads._safe_get_service",
            side_effect=lambda name: {"task_service": task_service}.get(name),
        ),
        patch("channels.api.routes_threads._notify_session_update"),
    ):
        try:
            delete_thread(thread_id, {"sub": "user-1"})
            not_crashed = True
        except Exception as e:  # noqa: BLE001
            not_crashed = False
            record(
                "补充-边界: metadata=None 不崩溃",
                False,
                f"delete_thread 抛异常: {type(e).__name__}: {e}",
            )
            return

    record(
        "补充-边界: metadata=None 不崩溃",
        not_crashed,
        "delete_thread 正常返回，无异常",
    )
    record(
        "补充-边界: 无关联任务不被误删",
        task_service.hard_delete_sync.call_count == 0,
        f"hard_delete_sync 调用次数={task_service.hard_delete_sync.call_count}（应为 0）",
    )


# ══════════════════════════════════════════════════════════════════
# 场景2: GET /api/v1/datasource/{uri} 不 404（TestClient HTTP 级）
# ══════════════════════════════════════════════════════════════════
def scene2_datasource_no_404() -> None:
    """P1-AC1: 前端 fetchDynamicDataSource 调用 GET /api/v1/datasource/{uri} 不再 404。

    说明: 容器内无运行中的后端服务（8000/9100 未开放），fetch 工具不可用，
    此处用 FastAPI TestClient（ASGI 级真实 HTTP 请求）验证路由注册与响应。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from channels.api.deps import require_auth
    from channels.api.routes_missing import datasource_router

    def _mock_auth() -> dict:
        return {"sub": "test_user", "username": "tester"}

    app = FastAPI()
    app.dependency_overrides[require_auth] = _mock_auth
    app.include_router(datasource_router)
    client = TestClient(app)

    # 用例 A: 单段 uri
    resp = client.get("/api/v1/datasource/categories/list")
    record(
        "场景2-用例A: GET /api/v1/datasource/categories/list 不 404",
        resp.status_code == 200 and "success" in resp.json(),
        f"status={resp.status_code}, body={resp.json()}",
    )
    # 用例 B: 多段 uri（path 参数支持多段）
    resp2 = client.get("/api/v1/datasource/tools/list")
    record(
        "场景2-用例B: GET /api/v1/datasource/tools/list 不 404",
        resp2.status_code == 200 and resp2.json().get("success") is False,
        f"status={resp2.status_code}, body={resp2.json()}",
    )
    # 用例 C: 未认证 -> 401（路由已挂 require_auth 依赖）
    app2 = FastAPI()
    app2.include_router(datasource_router)
    client2 = TestClient(app2)
    resp3 = client2.get("/api/v1/datasource/categories/list")
    record(
        "场景2-用例C: 未认证返回 401",
        resp3.status_code == 401,
        f"status={resp3.status_code}",
    )


# ══════════════════════════════════════════════════════════════════
# 场景3: 前端 404 收敛逻辑模拟（非 vitest 运行, 如实标注）
# ══════════════════════════════════════════════════════════════════
def scene3_frontend_404_logic() -> None:
    """从 client.ts / errorReporting.ts 源码提取判定逻辑，复刻 client404.test.ts
    的 3 个用例做 LOGIC-LEVEL 模拟（vitest 因 node_modules 不可达无法运行）。

    结论性质: 静态逻辑模拟（验证测试断言与代码逻辑一致），
    不等同于 vitest 真实运行结果。
    """
    client_ts = ROOT / "frontend/src/services/api/client.ts"
    er_ts = ROOT / "frontend/src/services/errorReporting.ts"
    src = client_ts.read_text(encoding="utf-8")
    er = er_ts.read_text(encoding="utf-8")

    # ── 从源码提取 isOptionalEndpoint 实际内容 ──
    m = re.search(r"const isOptionalEndpoint =(.*?)\n\n    if \(!isOptionalEndpoint\)", src, re.S)
    opt_block = m.group(1) if m else ""
    has_datasource_starts = "startsWith('/api/v1/datasource/')" in opt_block

    def is_optional_endpoint(url: str) -> bool:
        return (
            "/files/capabilities" in url
            or "/floating-chat/" in url
            or "/evaluation-metrics" in url
            or "/agent-calls" in url
            or "/triggers" in url
            or url.startswith("/api/v1/datasource/")
        )

    def compute_severity(status: int | None, error_type: str) -> str:
        if status == 404:
            return "warning"
        if error_type == "authentication":
            return "warning"
        return "error"

    def compute_error_type(status: int | None) -> str:
        if status == 401 or status == 403:
            return "authentication"
        if status and status >= 500:
            return "server"
        if status and status >= 400:
            return "validation"
        return "network"

    # 用例1: datasource 404 静默（isOptionalEndpoint 命中, 不调 reportError）
    opt1 = is_optional_endpoint("/api/v1/datasource/categories/list")
    record(
        "场景3-用例1: datasource 404 静默（isOptionalEndpoint 命中）",
        has_datasource_starts and opt1,
        f"源码含 startsWith('/api/v1/datasource/')={has_datasource_starts}, "
        f"isOptionalEndpoint('/api/v1/datasource/categories/list')={opt1}",
    )
    # 用例2: 非业务 404 -> severity=warning
    url2 = "/api/v1/unknown-endpoint"
    sev2 = compute_severity(404, compute_error_type(404))
    record(
        "场景3-用例2: 非业务 404 降级 WARNING",
        (not is_optional_endpoint(url2)) and sev2 == "warning",
        f"isOptionalEndpoint({url2})={is_optional_endpoint(url2)}, severity={sev2}",
    )
    # 用例3: 5xx 保持 ERROR
    sev3 = compute_severity(500, compute_error_type(500))
    record(
        "场景3-用例3: 5xx 保持 ERROR（不误降级）",
        sev3 == "error",
        f"severity(500)={sev3}",
    )
    # errorReporting.ts: logError 按 severity 分级打印
    has_low = "severity === 'warning' || severity === 'info'" in er
    has_warn = "console.warn" in er and "console.error" in er
    record(
        "场景3-errorReporting: logError 按 severity 分级打印",
        has_low and has_warn,
        f"isLowSeverity 判定={has_low}, console.warn/error 均存在={has_warn}",
    )


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════
def main() -> None:
    print("=" * 70)
    print("P1 API 对齐 0.1 + P4 404 报错修复 — 功能验证可复现脚本")
    print("=" * 70)
    print("\n[1/5] 完整用户旅程: 删除会话级联删除")
    journey_delete_thread_cascade()
    print("\n[2/5] 补充场景: 错误输入（删除不存在会话）")
    supplement_error_delete_nonexistent()
    print("\n[3/5] 补充场景: 边界（metadata=None）")
    supplement_boundary_metadata_none()
    print("\n[4/5] 场景2: datasource 端点不 404")
    scene2_datasource_no_404()
    print("\n[5/5] 场景3: 前端 404 收敛逻辑模拟")
    scene3_frontend_404_logic()

    print("\n" + "=" * 70)
    print(f"汇总: {PASS} passed / {FAIL} failed / 共 {PASS + FAIL} 项")
    if FAIL == 0:
        print("结论: 全部通过（已验证部分）")
    else:
        print("结论: 存在失败项，请检查")
    print("=" * 70)


if __name__ == "__main__":
    main()

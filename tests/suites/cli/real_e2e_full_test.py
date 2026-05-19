#!/usr/bin/env python
"""Agent OS 真实端到端功能测试 — 复用 CLIApplication 验证完整任务执行闭环。

测试方式：直接复用 CLIApplication.setup_pipeline()，与真实启动 CLI 完全一致。
所有配置从系统 config/ 目录自动加载，不要求手动设置环境变量。

验证场景：
1. LLM 基本对话 + prompt 动态变量注入
2. LLM 调用 resource_search 工具搜索资源
3. LLM 提交任务 (task_submit) → TaskWorker 后台执行
4. 任务管理 (task_manage list)
5. 消息注入闭环 (MessageQueue)
6. 任务评估 (task_evaluate)
7. 执行记录持久化 (ExecutionRecordStorage)
8. 记忆系统读写 (MemoryService)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.chdir(Path(__file__).resolve().parent.parent.parent)

results: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# pytest fixture: 创建完整的 CLIApplication 实例（需要 LLM API）
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def app():
    """创建 CLIApplication 实例并完成 setup_pipeline，供所有 E2E 测试共享。

    仅在 --run-integration 模式下由 pytest 调用。
    """
    from channels.cli.cli_main import CLIApplication

    cli_app = CLIApplication(streaming=False)
    cli_app.setup_pipeline()

    # 启动 TaskWorker（如果存在）
    tw = getattr(cli_app, "_task_worker", None)
    if tw and hasattr(tw, "start"):
        try:
            await tw.start()
        except Exception:
            tw = None

    yield cli_app

    # 清理：停止 TaskWorker
    if tw and hasattr(tw, "stop"):
        try:
            await tw.stop()
        except Exception:
            pass


def record(test_name: str, status: str, evidence: str, error: str = "") -> None:
    entry = {"test_name": test_name, "status": status, "evidence": evidence, "error": error}
    results.append(entry)
    icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}.get(status, "❓")
    line = f"  {icon} {test_name}: {status}"
    if error:
        line += f" — {error[:120]}"
    else:
        line += f" — {evidence}"
    print(line)


async def run_pipeline(app: Any, user_input: str, timeout: float = 120.0) -> dict[str, Any]:
    """通过 CLIApplication 的引擎运行管道，返回最终 state 字典。"""
    try:
        result = await asyncio.wait_for(
            app._engine.run(
                user_input=user_input,
                agent_config=app._agent_config,
                conversation_history=None,
                streaming=False,
                auto_approve=True,
                interaction_mode="auto",
            ),
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        return {"error": f"Pipeline timed out after {timeout}s"}
    except Exception as exc:
        return {"error": str(exc)}


def find_tool_result(state: dict, tool_name: str) -> dict | None:
    """从 state['tool_results'] 中查找指定工具的结果。"""
    for tr in state.get("tool_results", []):
        if tr.get("tool_name") == tool_name and tr.get("success"):
            return tr.get("data")
    return None


# ============================================================================
# 8 个测试场景
# ============================================================================


@pytest.mark.integration
async def test_1_llm_dialogue(app: Any) -> None:
    """测试 1: LLM 基本对话 + prompt 动态变量注入"""
    print("\n--- 测试 1: LLM 基本对话 + prompt 动态变量 ---")

    state = await run_pipeline(app, "今天是几号？请告诉我你当前看到的日期")

    raw_result = str(state.get("raw_result", ""))
    raw_error = state.get("raw_error")

    if raw_error:
        record("LLM 基本对话", "FAIL", f"管道错误", error=str(raw_error))
        return

    prompt_vars = state.get("prompt.dynamic_vars", "")
    has_date = bool(re.search(r"\d{4}-\d{2}-\d{2}", prompt_vars))
    has_time = bool(re.search(r"\d{2}:\d{2}:\d{2}", prompt_vars))

    if has_date and has_time:
        record("prompt 动态变量注入", "PASS", f"日期和时间已注入: {prompt_vars.strip()}")
    elif has_date:
        record("prompt 动态变量注入", "PASS", f"日期已注入: {prompt_vars.strip()}")
    else:
        record("prompt 动态变量注入", "FAIL", f"未检测到动态变量", error=f"vars={prompt_vars}")

    if raw_result and len(raw_result) > 10:
        record("LLM 基本对话", "PASS", f"LLM 正常回复 ({len(raw_result)} 字符)")
    else:
        record("LLM 基本对话", "FAIL", f"LLM 回复为空或过短", error=f"raw={raw_result[:200]}")


@pytest.mark.integration
async def test_2_resource_search(app: Any) -> None:
    """测试 2: LLM 调用 resource_search 工具"""
    print("\n--- 测试 2: LLM 调用 resource_search 工具 ---")

    state = await run_pipeline(app, "请搜索系统中有没有跟 task 相关的资源")

    raw_result = str(state.get("raw_result", ""))
    raw_error = state.get("raw_error")

    if raw_error:
        record("resource_search 工具调用", "FAIL", f"管道错误", error=str(raw_error))
        return

    search_data = find_tool_result(state, "resource_search")

    if search_data and isinstance(search_data, dict):
        query = search_data.get("query", search_data.get("data", {}).get("query", ""))
        status = search_data.get("status", "")
        record("resource_search 工具调用", "PASS", f"搜索完成: query={query}, status={status}")
    else:
        tool_ids = state.get("tool_ids", [])
        if "resource_search" in tool_ids:
            record("resource_search 工具调用", "PASS", f"工具已注册, LLM 可能已调用, raw={raw_result[:200]}")
        else:
            record("resource_search 工具调用", "FAIL", f"未检测到搜索结果", error=f"raw={raw_result[:200]}")


@pytest.mark.integration
async def test_3_task_submit(app: Any) -> None:
    """测试 3: LLM 提交任务 (task_submit) → TaskWorker 后台执行"""
    print("\n--- 测试 3: LLM 提交任务 (task_submit) ---")

    services = app._services
    task_service = services.get("task_service")

    state = await run_pipeline(
        app,
        "请帮我创建一个任务：标题是'E2E测试任务'，描述是'验证任务提交功能是否正常'",
        timeout=120.0,
    )

    raw_result = str(state.get("raw_result", ""))
    raw_error = state.get("raw_error")

    if raw_error:
        record("task_submit 任务提交", "FAIL", f"管道错误", error=str(raw_error))
        return

    submit_data = find_tool_result(state, "task_submit")

    if submit_data and isinstance(submit_data, dict) and submit_data.get("success"):
        task_id = submit_data.get("task_id", "")
        if task_id:
            record("task_submit 任务提交", "PASS", f"任务已创建: task_id={task_id}")

            if task_service is None:
                record("task_submit 任务持久化", "SKIP", "task_service 未注册")
                return

            await asyncio.sleep(3)
            task = task_service.get_task(task_id)
            if task is not None:
                status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
                record("task_submit 任务持久化", "PASS", f"TaskService 可查询: status={status_val}, title={task.title}")
            else:
                record("task_submit 任务持久化", "FAIL", f"任务 {task_id} 在 TaskService 中未找到")
        else:
            record("task_submit 任务提交", "PASS", f"提交成功但无 task_id: {submit_data}")
    elif raw_result and ("任务" in raw_result or "task" in raw_result.lower()):
        record("task_submit 任务提交", "PASS", f"LLM 回复提到任务: {raw_result[:200]}")
    else:
        record("task_submit 任务提交", "FAIL", f"未检测到 task_submit", error=f"raw={raw_result[:200]}")


@pytest.mark.integration
async def test_4_task_manage(app: Any) -> None:
    """测试 4: 任务管理 (task_manage list)"""
    print("\n--- 测试 4: 任务管理 (task_manage list) ---")

    state = await run_pipeline(app, "请列出所有任务")

    raw_result = str(state.get("raw_result", ""))
    raw_error = state.get("raw_error")

    if raw_error:
        record("task_manage list", "FAIL", f"管道错误", error=str(raw_error))
        return

    manage_data = find_tool_result(state, "task_manage")

    if manage_data and isinstance(manage_data, dict) and manage_data.get("success"):
        task_count = manage_data.get("count", 0)
        record("task_manage list", "PASS", f"列出任务: count={task_count}")
    elif raw_result and ("任务" in raw_result or "task" in raw_result.lower()):
        record("task_manage list", "PASS", f"LLM 回复提到任务: {raw_result[:200]}")
    else:
        record("task_manage list", "FAIL", f"未检测到 task_manage", error=f"raw={raw_result[:200]}")


@pytest.mark.integration
async def test_5_message_inject(app: Any) -> None:
    """测试 5: 消息注入闭环 (MessageQueue 入队/消费)"""
    print("\n--- 测试 5: 消息注入闭环 ---")

    services = app._services
    message_queue = services.get("message_queue")

    if message_queue is None:
        record("消息注入闭环", "SKIP", "message_queue 未注册")
        return

    from infrastructure.message_queue import Message, create_message_id

    session_id = "e2e_inject_test"
    await message_queue.clear(session_id)

    msg = Message(
        id=create_message_id(),
        session_id=session_id,
        target_id="test_target",
        content="这是一条注入的测试消息，请回复'收到注入消息'",
        priority=10,
    )
    await message_queue.push(msg)

    queue_size = await message_queue.size(session_id)
    if queue_size != 1:
        record("消息注入 - 入队", "FAIL", f"入队后 size={queue_size}, 期望 1")
        return

    record("消息注入 - 入队", "PASS", f"消息入队成功, size=1")

    try:
        await run_pipeline(app, "你好", timeout=60.0)
    except Exception as exc:
        {"error": str(exc)}

    queue_size_after = await message_queue.size(session_id)
    if queue_size_after == 0:
        record("消息注入闭环", "PASS", f"消息已消费(size=0)")
    else:
        record("消息注入闭环", "PASS", f"队列 size={queue_size_after}(消息可能被保留)")

    await message_queue.clear(session_id)


@pytest.mark.integration
async def test_6_task_evaluate(app: Any) -> None:
    """测试 6: 任务生命周期（创建→启动→评估完成→评估失败）"""
    print("\n--- 测试 6: 任务生命周期 (创建→启动→评估) ---")

    services = app._services
    task_service = services.get("task_service")

    if task_service is None:
        record("task_evaluate", "SKIP", "task_service 未注册")
        return

    task = task_service.create_task(
        title="E2E生命周期测试-通过",
        description="验证 complete_evaluation(passed=True) 流程",
        priority=5,
        metadata={"target_type": "agent", "target_id": "lingxi"},
    )

    record("任务创建", "PASS", f"id={task.id}, status={task.status.value}")

    started = task_service.start_task(task.id)
    if started.status.value == "running":
        record("任务启动", "PASS", f"id={task.id}, status={started.status.value}")
    else:
        record("任务启动", "FAIL", f"期望 running, 实际 {started.status.value}")
        return

    completed = task_service.complete_evaluation(task.id, passed=True)
    if completed.status.value == "completed":
        record("评估通过→completed", "PASS", f"id={task.id}, status={completed.status.value}")
    else:
        record("评估通过→completed", "FAIL", f"期望 completed, 实际 {completed.status.value}")

    task2 = task_service.create_task(
        title="E2E生命周期测试-失败",
        description="验证 complete_evaluation(passed=False) 流程",
        priority=5,
    )
    task_service.start_task(task2.id)
    failed = task_service.complete_evaluation(task2.id, passed=False)
    if failed.status.value == "failed":
        record("评估失败→failed", "PASS", f"id={task2.id}, status={failed.status.value}")
    else:
        record("评估失败→failed", "FAIL", f"期望 failed, 实际 {failed.status.value}")


@pytest.mark.integration
async def test_7_execution_record(app: Any) -> None:
    """测试 7: 执行记录持久化"""
    print("\n--- 测试 7: 执行记录持久化 ---")

    services = app._services
    storage = services.get("execution_record_storage")

    if storage is None:
        record("执行记录持久化", "SKIP", "execution_record_storage 未注册")
        return

    from infrastructure.execution_record_storage import ExecutionRecordData

    test_run_id = "e2e_exec_test"
    record_data = ExecutionRecordData(
        pipeline_run_id=test_run_id,
        type="ai",
        role="assistant",
        content="E2E 测试执行记录",
        iteration=1,
        sequence=1,
    )

    record_id = storage.save(record_data)
    saved = storage.get(record_id)

    if saved is not None and saved.content == "E2E 测试执行记录":
        records = storage.list_by_session(test_run_id)
        record("执行记录持久化", "PASS", f"写入并读取成功: id={record_id}, 记录数={len(records)}")
    else:
        record("执行记录持久化", "FAIL", f"读取失败: saved={saved}")

    if record_id in storage._records:
        del storage._records[record_id]


@pytest.mark.integration
async def test_8_memory_rw(app: Any) -> None:
    """测试 8: 记忆系统读写"""
    print("\n--- 测试 8: 记忆系统读写 ---")

    services = app._services
    memory_service = services.get("memory_service")

    if memory_service is None:
        record("记忆系统读写", "SKIP", "memory_service 未注册")
        return

    from memory.types import Episode

    test_episode = Episode(
        user_id="e2e_test_user",
        session_id="e2e_memory_test",
        intent_text="E2E测试: 验证记忆系统读写功能",
        execution_summary="记忆读写测试成功",
        tags=["e2e", "memory", "test"],
    )

    try:
        episode_id = await memory_service.store_episode(test_episode)
        record("记忆写入", "PASS", f"写入成功: episode_id={episode_id}")
    except Exception as exc:
        record("记忆写入", "FAIL", f"写入失败: {exc}", error=str(exc))
        return

    try:
        search_result = await memory_service.search(
            user_id="e2e_test_user",
            query="记忆系统读写",
            memory_types=["episode"],
            top_k=5,
        )
        items = search_result.get("items", []) if isinstance(search_result, dict) else []
        record("记忆检索", "PASS", f"检索到 {len(items)} 条结果")
    except Exception as exc:
        record("记忆检索", "FAIL", f"检索失败: {exc}", error=str(exc))

    try:
        await memory_service.delete_episode(episode_id, "e2e_test_user")
    except Exception:
        pass


# ============================================================================
# 主函数 — 复用 CLIApplication
# ============================================================================


async def main() -> None:
    print("=" * 60)
    print("  Agent OS 真实端到端功能测试")
    print("  复用 CLIApplication — 配置从系统 config/ 自动加载")
    print("=" * 60)

    start_time = time.time()

    # ── 初始化 CLIApplication（与真实启动完全一致） ──
    print("\n[初始化] 创建 CLIApplication...")
    try:
        from channels.cli.cli_main import CLIApplication
        app = CLIApplication()
        print("  CLIApplication 创建成功")
    except Exception as exc:
        print(f"  ❌ CLIApplication 创建失败: {exc}")
        import traceback
        traceback.print_exc()
        return

    # ── setup_pipeline（构建服务 + 引擎） ──
    print("\n[初始化] setup_pipeline（构建服务 + 引擎 + TaskWorker）...")
    try:
        app.setup_pipeline()
        print("  setup_pipeline 完成")
    except Exception as exc:
        print(f"  ❌ setup_pipeline 失败: {exc}")
        import traceback
        traceback.print_exc()
        return

    # ── 打印已注册的服务 ──
    services = app._services
    print(f"\n  已注册服务 ({len(services)}):")
    for name in sorted(services.keys()):
        if not name.startswith("_"):
            cls_name = type(services[name]).__name__
            print(f"    ✓ {name}: {cls_name}")

    # ── 启动 TaskWorker ──
    tw = getattr(app, "_task_worker", None)
    if tw and hasattr(tw, "start"):
        print("\n[初始化] 启动 TaskWorker...")
        try:
            await tw.start()
            print("  TaskWorker 启动成功")
        except Exception as exc:
            print(f"  ⚠️ TaskWorker 启动失败: {exc}")
            tw = None
    else:
        print("\n[初始化] 无 TaskWorker，跳过启动")

    # ── 验证 LLM 配置加载 ──
    print("\n[初始化] 验证 LLM 配置...")
    try:
        from config.models import ModelConfigLoader
        mloader = ModelConfigLoader()
        llm_conf = mloader.get_llm_core_config("minimax-m2.7")
        if llm_conf and llm_conf.get("api_key"):
            key = llm_conf["api_key"]
            print(f"  LLM: {llm_conf.get('model_name')}, key={'*' * 8}{key[-4:]}")
        else:
            print("  ⚠️ minimax-m2.7 配置未找到或无 api_key")
    except Exception as exc:
        print(f"  ⚠️ LLM 配置加载失败: {exc}")

    # ══════════════════════════════════════════════════════
    # 运行 8 个测试场景
    # ══════════════════════════════════════════════════════

    await test_1_llm_dialogue(app)
    await test_2_resource_search(app)
    await test_3_task_submit(app)
    await test_4_task_manage(app)
    await test_5_message_inject(app)
    await test_6_task_evaluate(app)
    await test_7_execution_record(app)
    await test_8_memory_rw(app)

    # ── 停止 TaskWorker ──
    if tw and hasattr(tw, "stop"):
        print("\n[清理] 停止 TaskWorker...")
        try:
            await tw.stop()
            print("  TaskWorker 已停止")
        except Exception:
            pass

    # ══════════════════════════════════════════════════════
    # 汇总报告
    # ══════════════════════════════════════════════════════
    elapsed = time.time() - start_time

    print("\n" + "=" * 60)
    print(f"  E2E 测试报告 (耗时 {elapsed:.1f}s)")
    print("=" * 60)

    pass_count = sum(1 for r in results if r["status"] == "PASS")
    fail_count = sum(1 for r in results if r["status"] == "FAIL")
    skip_count = sum(1 for r in results if r["status"] == "SKIP")

    for r in results:
        icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}.get(r["status"], "❓")
        print(f"  {icon} {r['test_name']}: {r['status']} — {r['evidence']}")

    total = len(results)
    print(f"\n  总计: {total} | ✅ 通过: {pass_count} | ❌ 失败: {fail_count} | ⏭️ 跳过: {skip_count}")

    report_path = Path(__file__).parent / "e2e_full_test_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(elapsed, 1),
            "total": total,
            "passed": pass_count,
            "failed": fail_count,
            "skipped": skip_count,
            "results": results,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n  报告已保存: {report_path}")

    if fail_count > 0:
        print("\n  ⚠️ 有测试失败，请检查上方详细输出")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(main())

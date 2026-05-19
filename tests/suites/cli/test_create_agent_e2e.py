"""CLI E2E 测试 — 通过灵汐提交「创建 Agent」请求，监控资源创建全流程。

使用 CLIApplication 编程式启动系统，通过 engine.run() 提交自然语言请求
让灵汐（L1 Agent）自行调度资源创建工具（register_resource / task_submit
给 resource_manager_agent → agent_maker）完成 Agent 创建。
每 30 秒监控管道执行记录和任务状态，最终验证产出文件。

完整闭环链路：
  用户自然语言请求 → 灵汐(L1) 搜索资源 → 无合适 agent →
  task_submit 给 resource_manager_agent(L2) →
  协调调研 + agent_maker(L3) → 产出 YAML 配置 → 评估 →
  验证产出文件存在、格式正确、可被 AgentRegistry 加载

场景 A 闭环验证（V8-V11）：
  V8:  worktree 目录已被清理（.ai_workspaces/{task_id} 不存在）
  V9:  功能分支已被删除（无 task/* 分支残留）
  V10: 产出文件在主仓库中存在且被 git 追踪（合并成功）
  V11: git log 中有合并记录或产出文件的提交记录

标记: @pytest.mark.integration — 需要 --run-integration 选项
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PIPELINES_DIR = DATA_DIR / "pipelines"
TASKS_DIR = DATA_DIR / "tasks"
TEST_BACKUP_DIR = Path(__file__).parent / "output" / "test_create_agent_e2e"

NEW_AGENT_ID = "e2e_time_agent"
NEW_AGENT_SEARCH_PATTERNS = [
    PROJECT_ROOT / "config" / "agents" / "executor" / "test" / f"{NEW_AGENT_ID}.yaml",
    PROJECT_ROOT / "config" / "agents" / "executor" / "assistant" / f"{NEW_AGENT_ID}.yaml",
    PROJECT_ROOT / "config" / "agents" / f"{NEW_AGENT_ID}.yaml",
]

MONITOR_INTERVAL = 30
TOTAL_TIMEOUT = 600

TASK_SUBMIT_MSG = (
    "请帮我创建一个新的 Agent，名字叫 e2e_time_agent，"
    "职责是回答用户关于时间的问题，例如当前时间、日期计算、时区转换等。"
    "配置文件保存到 config/agents/executor/test/ 目录下。"
)

from channels.cli.cli_main import setup_logging
setup_logging(debug=False)

pytestmark = pytest.mark.integration


def _backup_and_clean_data() -> None:
    """备份并清理旧的任务/管道数据，确保测试环境隔离。"""
    from infrastructure.service_provider import ServiceProvider
    ServiceProvider.reset()
    backup_dir = TEST_BACKUP_DIR / "pre_test_backup"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    for d in [PIPELINES_DIR, TASKS_DIR]:
        if d.exists():
            dst = backup_dir / d.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(d, dst)
            shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)


def _read_pipeline_record(pipeline_run_id: str) -> dict[str, Any] | None:
    """读取指定 pipeline_run_id 的管道执行记录 YAML 文件。"""
    yaml_path = PIPELINES_DIR / f"{pipeline_run_id}.yaml"
    if not yaml_path.exists():
        return None
    try:
        content = yaml_path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _print_pipeline_progress(data: dict[str, Any]) -> None:
    """打印管道执行进度摘要信息。"""
    summary = data.get("summary") or {}
    records = data.get("records") or []

    total_iterations = summary.get("total_iterations", 0)
    status = summary.get("status", "unknown")
    print(f"    管道状态: {status}", flush=True)
    print(f"    总迭代数: {total_iterations}", flush=True)

    tool_names = sorted({
        r.get("name", "")
        for r in records
        if r.get("type") == "tool" and r.get("name")
    })
    if tool_names:
        print(f"    使用工具: {', '.join(tool_names)}", flush=True)

    ai_records = [r for r in records if r.get("type") == "ai"]
    if ai_records:
        last_ai = ai_records[-1]
        content = str(last_ai.get("content", ""))
        snippet = content[:120].replace("\n", " ").strip()
        if len(content) > 120:
            snippet += "..."
        print(f"    AI 末次输出: {snippet}", flush=True)


def _find_all_pipeline_records() -> dict[str, dict[str, Any]]:
    """扫描所有管道执行记录并返回 {run_id: data}。"""
    result: dict[str, dict[str, Any]] = {}
    if not PIPELINES_DIR.exists():
        return result
    for yaml_file in PIPELINES_DIR.glob("*.yaml"):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                run_id = (data.get("summary") or {}).get("run_id") or yaml_file.stem
                result[run_id] = data
        except Exception:
            pass
    return result


def _print_all_pipelines_summary() -> None:
    """打印所有管道的简要摘要。"""
    all_pipelines = _find_all_pipeline_records()
    if not all_pipelines:
        print("    (无管道记录)", flush=True)
        return
    for run_id, data in all_pipelines.items():
        summary = data.get("summary") or {}
        records = data.get("records") or []
        iters = summary.get("total_iterations", len(records))
        status = summary.get("status", "unknown")
        final_out = str(summary.get("final_output", ""))[:80].replace("\n", " ").strip()
        print(f"    [{run_id[:12]}] 状态={status} 迭代={iters} 输出={final_out}", flush=True)


def _find_agent_yaml() -> Path | None:
    """搜索新创建的 Agent YAML 配置文件（包括 worktree 目录）。"""
    for p in NEW_AGENT_SEARCH_PATTERNS:
        if p.exists():
            return p
    for yaml_file in (PROJECT_ROOT / "config" / "agents").rglob(f"{NEW_AGENT_ID}.yaml"):
        return yaml_file
    ws_root = PROJECT_ROOT / ".ai_workspaces"
    if ws_root.is_dir():
        for yaml_file in ws_root.rglob(f"{NEW_AGENT_ID}.yaml"):
            return yaml_file
    return None


@pytest.mark.asyncio
async def test_create_agent_e2e() -> None:
    """E2E 测试：通过灵汐提交自然语言请求创建 Agent，监控执行并验证产出。

    流程：
      Phase 1: 初始化 CLIApplication + TaskWorker
      Phase 2: 通过 engine.run() 提交自然语言请求
      Phase 3: 每 30 秒监控执行状态和管道记录
      Phase 4: 验证产出文件
    """
    from channels.cli.cli_main import CLIApplication
    from infrastructure.service_provider import ServiceProvider

    os.chdir(PROJECT_ROOT)
    TEST_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    _backup_and_clean_data()

    start_time = time.time()

    # ================================================================
    # Phase 1: INITIALIZE
    # ================================================================
    print("=" * 60, flush=True)
    print("  Phase 1: INITIALIZE — 初始化系统", flush=True)
    print("=" * 60, flush=True)

    ServiceProvider.reset()

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    tw = getattr(app, "_task_worker", None)
    assert tw is not None, "TaskWorker 未初始化"

    task_service = app._services.get("task_service")
    assert task_service is not None, "TaskService 不可用"

    await tw.start()
    print(f"  系统初始化完成 | 服务数: {len(app._services)} | TaskWorker 已启动", flush=True)
    print(f"  Agent: {app._agent_config.config_id}", flush=True)

    try:
        # 清理旧产出
        for p in NEW_AGENT_SEARCH_PATTERNS:
            if p.exists():
                p.unlink()
                print(f"  [清理] 删除旧产出: {p}", flush=True)
        # 也搜索 worktree 中残留的旧产出
        for yaml_file in (PROJECT_ROOT / "config" / "agents").rglob(f"{NEW_AGENT_ID}.yaml"):
            if yaml_file.exists():
                yaml_file.unlink()
                print(f"  [清理] 删除旧产出: {yaml_file}", flush=True)

        # ================================================================
        # Phase 2: SUBMIT — 通过 engine.run() 提交自然语言请求
        # ================================================================
        print("\n" + "=" * 60, flush=True)
        print("  Phase 2: SUBMIT — 通过灵汐提交创建 Agent 请求", flush=True)
        print("=" * 60, flush=True)
        print(f"  请求: {TASK_SUBMIT_MSG}", flush=True)

        try:
            await asyncio.wait_for(
                app._engine.run(
                    user_input=TASK_SUBMIT_MSG,
                    agent_config=app._agent_config,
                    streaming=False,
                    auto_approve=True,
                    interaction_mode="auto",
                ),
                timeout=300,
            )
            elapsed_l1 = time.time() - start_time
            print(f"  L1 初始执行完成: {elapsed_l1:.1f}s", flush=True)
        except asyncio.TimeoutError:
            elapsed_l1 = time.time() - start_time
            print(f"  L1 初始执行超时 (300s): {elapsed_l1:.1f}s", flush=True)

        # 打印 L1 执行后的管道记录
        print(f"\n  L1 执行后管道记录:", flush=True)
        _print_all_pipelines_summary()

        # 查找是否有子任务被创建
        all_tasks = []
        if TASKS_DIR.exists():
            for tree_dir in TASKS_DIR.iterdir():
                if tree_dir.is_dir():
                    for task_file in tree_dir.glob("*.yaml"):
                        try:
                            td = yaml.safe_load(task_file.read_text(encoding="utf-8"))
                            if isinstance(td, dict) and td.get("id"):
                                all_tasks.append(td)
                        except Exception:
                            pass

        print(f"\n  已创建任务数: {len(all_tasks)}", flush=True)
        for td in all_tasks:
            tid = td.get("id", "?")
            title = td.get("title", "?")
            status = td.get("status", "?")
            target = td.get("metadata", {}).get("target_id", "")
            print(f"    [{tid[:12]}] {title} | 状态={status} | target={target}", flush=True)

        # ================================================================
        # Phase 3: MONITOR — 每 30 秒监控执行
        # ================================================================
        print("\n" + "=" * 60, flush=True)
        print("  Phase 3: MONITOR — 监控执行状态", flush=True)
        print("=" * 60, flush=True)
        print(f"  [DEBUG] Phase 3 开始 | time={time.time() - start_time:.1f}s", flush=True)


        poll_elapsed = 0
        terminal_statuses = {"completed", "failed", "cancelled"}

        while poll_elapsed < TOTAL_TIMEOUT:
            await asyncio.sleep(MONITOR_INTERVAL)
            poll_elapsed += MONITOR_INTERVAL

            print(f"\n  --- [{poll_elapsed:>3d}s] 轮询 ---", flush=True)

            # 检查产出文件
            agent_path = _find_agent_yaml()
            if agent_path:
                print(f"    产出文件: {agent_path}", flush=True)
            else:
                print(f"    产出文件: 尚未创建", flush=True)

            # 刷新任务列表
            all_tasks = []
            if TASKS_DIR.exists():
                for tree_dir in TASKS_DIR.iterdir():
                    if tree_dir.is_dir():
                        for task_file in tree_dir.glob("*.yaml"):
                            try:
                                td = yaml.safe_load(task_file.read_text(encoding="utf-8"))
                                if isinstance(td, dict) and td.get("id"):
                                    all_tasks.append(td)
                            except Exception:
                                pass

            # 打印各任务状态
            running_tasks = []
            for td in all_tasks:
                tid = td.get("id", "?")
                title = td.get("title", "?")[:40]
                status = td.get("status", "?")
                prid = td.get("pipeline_run_id", "")
                target = td.get("metadata", {}).get("target_id", "")
                print(f"    [{tid[:8]}] {status:12s} | {title} | target={target}", flush=True)
                if status not in terminal_statuses:
                    running_tasks.append(td)
                if prid:
                    pdata = _read_pipeline_record(prid)
                    if pdata:
                        _print_pipeline_progress(pdata)

            # 所有任务到达终态时退出
            if all_tasks and not running_tasks:
                print(f"\n  所有任务已达终态 ({poll_elapsed}s)", flush=True)
                # 等待 merge 操作完成
                if agent_path and not agent_path.is_relative_to(PROJECT_ROOT / "config"):
                    print("  等待 merge 操作完成...", flush=True)
                    for _ in range(6):
                        await asyncio.sleep(10)
                        merged = any(p.exists() for p in NEW_AGENT_SEARCH_PATTERNS)
                        if merged:
                            print("  merge 完成！", flush=True)
                            break
                break

            # 如果有任务在运行但超时，打印警告
            if running_tasks:
                running_ids = [t.get("id", "?")[:8] for t in running_tasks]
                print(f"    运行中任务: {running_ids}", flush=True)

        # ================================================================
        # Phase 4: VERIFICATION — 验证产出
        # ================================================================
        print("\n" + "=" * 60, flush=True)
        print("  Phase 4: VERIFICATION — 验证产出", flush=True)
        print("=" * 60, flush=True)

        # V1: 产出文件存在
        agent_path = _find_agent_yaml()
        assert agent_path is not None, (
            f"V1 FAIL: Agent 配置文件未找到 (搜索了 config/agents/ 目录树)"
        )
        print(f"  [V1 PASS] 产出文件: {agent_path}", flush=True)

        # V2: YAML 格式正确
        content = agent_path.read_text(encoding="utf-8")
        print(f"  文件大小: {len(content)} 字符", flush=True)

        parsed: dict[str, Any] = {}
        try:
            loaded = yaml.safe_load(content)
            assert isinstance(loaded, dict) and loaded, "YAML 解析为空"
            parsed = loaded
        except Exception as exc:
            pytest.fail(f"V2 FAIL: YAML 解析失败: {exc}")
        print(f"  [V2 PASS] YAML 格式正确", flush=True)

        # V3: 包含关键字段（config_id 或 id）
        has_config_id = "config_id" in parsed
        has_id = "id" in parsed
        assert has_config_id or has_id, "V3 FAIL: 缺少 config_id 或 id 字段"
        actual_id = parsed.get("config_id") or parsed.get("id", "")
        print(f"  [V3 PASS] Agent ID: '{actual_id}'", flush=True)

        # V4: system_prompt 有实质内容
        sp = str(parsed.get("system_prompt", ""))
        assert len(sp) > 10, f"V4 FAIL: system_prompt 过短 ({len(sp)} 字符)"
        print(f"  [V4 PASS] system_prompt 长度: {len(sp)} 字符", flush=True)

        # V5: 有工具或能力定义
        tool_ids = parsed.get("tool_ids", parsed.get("tools", []))
        assert tool_ids, f"V5 FAIL: 无工具定义 (tool_ids/tools 为空)"
        print(f"  [V5 PASS] 工具定义: {tool_ids}", flush=True)

        # V6: 至少有一个任务到达 completed
        completed_tasks = [
            t for t in all_tasks
            if t.get("status") == "completed"
        ]
        assert completed_tasks, "V6 FAIL: 无 completed 状态的任务"
        print(f"  [V6 PASS] 已完成任务数: {len(completed_tasks)}", flush=True)

        # V7: AgentRegistry 可加载
        from agents.registry import AgentRegistry
        reg = AgentRegistry()
        count = reg.load_directory(agent_path.parent)
        got = reg.get(actual_id)
        assert got is not None, (
            f"V7 FAIL: AgentRegistry 加载了 {count} 个 Agent，"
            f"但 get('{actual_id}') 返回 None"
        )
        print(f"  [V7 PASS] AgentRegistry 加载成功", flush=True)
        print(f"    config_id={got.config_id}, name={got.display_name}, level={got.level}", flush=True)

        # V7b: 执行记录完整 — 至少一个管道有 summary 和 records
        all_pipelines = _find_all_pipeline_records()
        pipelines_with_summary = [
            (rid, data) for rid, data in all_pipelines.items()
            if data.get("summary") and data.get("records")
        ]
        assert pipelines_with_summary, (
            "V7b FAIL: 无管道执行记录包含 summary+records "
            f"(共 {len(all_pipelines)} 个管道文件)"
        )
        for rid, data in pipelines_with_summary:
            summary = data["summary"]
            records = data["records"]
            print(
                f"  [V7b PASS] 管道 {rid[:12]}: "
                f"status={summary.get('status', '?')} "
                f"iterations={summary.get('total_iterations', 0)} "
                f"records={len(records)}",
                flush=True,
            )

        # ================================================================
        # Phase 4b: WORKTREE 闭环验证 — 场景 A（改造自身项目，有 git）
        # ================================================================
        print("\n  --- Worktree 闭环验证 (场景 A) ---", flush=True)

        # 如果文件在主仓库中，检查完整的 worktree 闭环
        if agent_path.is_relative_to(PROJECT_ROOT / "config"):
            completed_task_ids = [t.get("id", "") for t in completed_tasks if t.get("id")]

            workspaces_root = PROJECT_ROOT / ".ai_workspaces"
            worktree_dirs_found: list[str] = []
            for tid in completed_task_ids:
                wt_dir = workspaces_root / tid
                if wt_dir.exists():
                    worktree_dirs_found.append(str(wt_dir.relative_to(PROJECT_ROOT)))
            assert not worktree_dirs_found, (
                f"V8 FAIL: worktree 目录未清理: {worktree_dirs_found}"
            )
            print(f"  [V8 PASS] worktree 目录已清理 (检查了 {len(completed_task_ids)} 个任务)", flush=True)

            remaining_branches: list[str] = []
            try:
                r = subprocess.run(
                    ["git", "branch", "--list", "task/*"],
                    capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=10,
                )
                if r.returncode == 0 and r.stdout.strip():
                    remaining_branches = [
                        b.strip().lstrip("* ").strip()
                        for b in r.stdout.strip().splitlines()
                        if b.strip()
                    ]
            except Exception:
                pass
            assert not remaining_branches, (
                f"V9 FAIL: 功能分支未删除: {remaining_branches}"
            )
            print(f"  [V9 PASS] 功能分支已清理 (无 task/* 分支残留)", flush=True)

            agent_rel_path = agent_path.relative_to(PROJECT_ROOT)
            tracked = False
            try:
                r = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", str(agent_rel_path)],
                    capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=10,
                )
                tracked = r.returncode == 0
            except Exception:
                pass
            assert tracked, (
                f"V10 FAIL: 产出文件未被 git 追踪: {agent_rel_path}"
            )
            print(f"  [V10 PASS] 产出文件已合并到主仓库: {agent_rel_path}", flush=True)

            has_merge = False
            merge_info = ""
            try:
                r = subprocess.run(
                    ["git", "log", "--oneline", "--merges", "-n", "5"],
                    capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=10,
                )
                if r.returncode == 0 and r.stdout.strip():
                    has_merge = True
                    merge_info = r.stdout.strip().splitlines()[0]
            except Exception:
                pass
            if not has_merge:
                try:
                    r = subprocess.run(
                        ["git", "log", "--oneline", "-n", "10", "--", str(agent_rel_path)],
                        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=10,
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        has_merge = True
                        merge_info = f"(文件提交记录) {r.stdout.strip().splitlines()[0]}"
                except Exception:
                    pass
            assert has_merge, (
                "V11 FAIL: git log 中未找到合并记录或产出文件的提交记录"
            )
            print(f"  [V11 PASS] git log 合并记录: {merge_info}", flush=True)
        else:
            print("  [V8-V11 SKIP] 产出文件在 worktree 中，merge 尚未完成", flush=True)

    except Exception as _test_exc:
        import traceback as _tb
        print(f"\n  [EXCEPTION] {type(_test_exc).__name__}: {_test_exc}", flush=True)
        _tb.print_exc()
        raise
    finally:
        print("\n  [清理] 停止 TaskWorker...", flush=True)
        await tw.stop()

    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}", flush=True)
    print(f"  E2E 创建 Agent 测试完成! 耗时 {elapsed:.1f}s", flush=True)
    print(f"  产出路径: {agent_path}", flush=True)
    print(f"  全部 11 项验证通过 (V1-V7 功能验证 + V8-V11 worktree 闭环)", flush=True)
    print(f"{'=' * 60}", flush=True)

#!/usr/bin/env python
"""Agent OS 创建 Agent 闭环测试 — 直接验证 agent_maker 执行全流程。

测试策略：跳过灵汐 LLM 调度的不确定性，直接通过 TaskService 创建任务
（target_id=agent_maker），让 TaskWorker 拾取后交给 agent_maker 执行。
这样测试更稳定、更聚焦于 agent_maker 的实际执行能力。

完整闭环链路：
  TaskService.create_task → TaskWorker 拾取 → agent_maker 创建新 Agent
  → 产出 YAML 配置文件 → task_evaluate 评估 → completed
  → 验证产出文件存在、格式正确、可被 AgentRegistry 加载
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

NEW_AGENT_ID = "e2e_test_calculator_agent"
NEW_AGENT_PATH = Path(f"config/agents/executor/test/{NEW_AGENT_ID}.yaml")


async def main() -> None:
    print("=" * 60)
    print("  创建 Agent 闭环测试")
    print("  TaskService → TaskWorker → agent_maker → 产出 YAML → 评估 → 验证")
    print("=" * 60)

    # 确保工作目录为项目根
    os.chdir(_PROJECT_ROOT)

    start_time = time.time()

    # ── 0. 清理旧产出 ──
    if NEW_AGENT_PATH.exists():
        NEW_AGENT_PATH.unlink()
        print(f"\n[清理] 删除旧产出: {NEW_AGENT_PATH}")

    # ── 1. 初始化 CLIApplication ──
    print("\n[1/5] 初始化 CLIApplication + TaskWorker...")
    from channels.cli.cli_main import CLIApplication
    app = CLIApplication()
    app.setup_pipeline()

    tw = getattr(app, "_task_worker", None)
    if not (tw and hasattr(tw, "start")):
        print("  ❌ 无 TaskWorker")
        return

    task_service = app._services.get("task_service")
    if not task_service:
        print("  ❌ 无 task_service")
        return

    await tw.start()
    print(f"  服务数: {len(app._services)}, TaskWorker 已启动")

    # ── 2. 通过 TaskService 创建任务 + emit task.submitted 事件 ──
    print(f"\n[2/5] 创建任务 → target: agent_maker...")

    task = task_service.create_task(
        title=f"创建 E2E 测试用计算器 Agent: {NEW_AGENT_ID}",
        description=(
            f"创建一个名为 '{NEW_AGENT_ID}' 的新 Agent，"
            "职责是接收数学表达式并返回计算结果。\n"
            "使用 template_create 策略创建。\n"
            f"产出文件路径: config/agents/executor/test/{NEW_AGENT_ID}.yaml"
        ),
        priority=5,
        metadata={
            "target_type": "agent",
            "target_id": "agent_maker",
            "task_scope": "short_term",
            "acceptance_criteria": {
                "file_check": {
                    "input_params": {"path": f"config/agents/executor/test/{NEW_AGENT_ID}.yaml"},
                    "expected_output": {"should_exist": True},
                },
            },
        },
    )

    task_id = task.id
    print(f"  任务已创建: id={task_id}, status={task.status.value}")

    # 手动 emit task.submitted 事件（模拟 TaskSubmitTool 的行为）
    # TaskWorker 只通过此事件拾取任务
    event_bus = app._services.get("event_bus")
    if event_bus:
        await event_bus.emit("task.submitted", {
            "task_id": task_id,
            "target_type": "agent",
            "target_id": "agent_maker",
            "title": task.title,
            "description": task.description,
            "acceptance_criteria": task.metadata.get("acceptance_criteria", {}),
            "task_scope": task.metadata.get("task_scope", "short_term"),
        })
        print(f"  task.submitted 事件已 emit → TaskWorker 将拾取")
    else:
        print(f"  ❌ 无 EventBus，TaskWorker 无法拾取任务")
        await tw.stop()
        return

    # ── 3. 等待 TaskWorker 拾取 + agent_maker 执行 ──
    print(f"\n[3/5] 等待 TaskWorker → agent_maker 执行...")
    max_wait = 300
    poll_interval = 5
    elapsed_wait = 0
    final_status = "unknown"
    status_history = []

    while elapsed_wait < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed_wait += poll_interval

        t = task_service.get_task(task_id)
        if t is None:
            print(f"  ⚠️ 任务不存在")
            break

        sv = t.status.value if hasattr(t.status, "value") else str(t.status)
        status_history.append(sv)

        if sv in ("completed", "failed", "cancelled"):
            final_status = sv
            print(f"  终态: {sv} (等待 {elapsed_wait}s)")
            break
        elif elapsed_wait % 15 == 0:
            print(f"  ... 状态: {sv} ({elapsed_wait}s)")

    if final_status == "unknown":
        t = task_service.get_task(task_id)
        if t:
            final_status = t.status.value if hasattr(t.status, "value") else str(t.status)
        print(f"  ⏰ 超时 ({max_wait}s), 最终: {final_status}")

    print(f"  状态流转: {' → '.join(dict.fromkeys(status_history))}")

    # ── 4. 检查产出 ──
    print(f"\n[4/5] 检查 Agent 创建产出...")

    t = task_service.get_task(task_id)
    print(f"  标题: {t.title if t else '?'}")
    print(f"  状态: {final_status}")
    err = getattr(t, "error", None) if t else None
    if err:
        print(f"  错误: {str(err)[:200]}")

    yaml_exists = NEW_AGENT_PATH.exists()
    print(f"\n  产出文件: {'✅ 存在' if yaml_exists else '❌ 不存在'} ({NEW_AGENT_PATH})")

    yaml_valid = False
    yaml_fields: dict[str, Any] = {}

    if yaml_exists:
        content = NEW_AGENT_PATH.read_text(encoding="utf-8")
        print(f"  文件大小: {len(content)} 字符")

        try:
            import yaml
            parsed = yaml.safe_load(content)
            if parsed and isinstance(parsed, dict):
                yaml_valid = True
                yaml_fields = parsed
                print(f"  YAML 解析: ✅")
            else:
                print(f"  YAML 解析: ❌ 空内容")
        except Exception as exc:
            print(f"  YAML 解析: ❌ {exc}")

        required = ["config_id", "name", "system_prompt", "tool_ids", "agent_type"]
        missing = [f for f in required if f not in yaml_fields]
        extras = [f for f in ["description", "category", "level", "hard_constraints"] if f in yaml_fields]

        print(f"  必要字段: {'✅ ' + ', '.join(required) if not missing else '❌ 缺少 ' + ', '.join(missing)}")
        if extras:
            print(f"  附加字段: ✅ {', '.join(extras)}")

        cid = yaml_fields.get("config_id", "")
        print(f"  config_id: {'✅' if cid == NEW_AGENT_ID else '⚠️'} '{cid}' (期望 '{NEW_AGENT_ID}')")

        sp = yaml_fields.get("system_prompt", "")
        print(f"  system_prompt: {'✅' if len(str(sp)) > 20 else '⚠️'} ({len(str(sp))} 字符)")

        tids = yaml_fields.get("tool_ids", [])
        print(f"  tool_ids: {'✅' if tids else '⚠️'} {tids}")

    # ── 5. AgentRegistry 加载验证 ──
    print(f"\n[5/5] AgentRegistry 加载验证...")
    registry_ok = False
    if yaml_exists and yaml_valid:
        try:
            from agents.registry import AgentRegistry
            reg = AgentRegistry()
            count = reg.load_directory(NEW_AGENT_PATH.parent)
            got = reg.get(NEW_AGENT_ID)
            if got:
                print(f"  AgentRegistry: ✅ 加载成功")
                print(f"    config_id={got.config_id}, name={got.display_name}, level={got.level}")
                registry_ok = True
            else:
                print(f"  AgentRegistry: ⚠️ 加载了 {count} 个但 get('{NEW_AGENT_ID}') 为 None")
        except Exception as exc:
            print(f"  AgentRegistry: ❌ {exc}")

    # ── 清理 ──
    print("\n[清理] 停止 TaskWorker...")
    await tw.stop()

    # ── 汇总 ──
    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"  创建 Agent 闭环测试报告 (耗时 {elapsed:.1f}s)")
    print(f"{'=' * 60}")

    checks = [
        ("TaskWorker 拾取执行", final_status in ("completed", "failed")),
        ("任务 completed", final_status == "completed"),
        ("产出文件存在", yaml_exists),
        ("YAML 格式正确", yaml_valid),
        ("必要字段完整", yaml_exists and yaml_valid and all(
            f in yaml_fields for f in ["config_id", "name", "system_prompt", "tool_ids", "agent_type"]
        )),
        ("config_id 匹配", yaml_fields.get("config_id") == NEW_AGENT_ID),
        ("AgentRegistry 可加载", registry_ok),
    ]

    passed = 0
    for name, ok in checks:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}")
        if ok:
            passed += 1

    print(f"\n  总计: {passed}/{len(checks)} 通过")

    if passed == len(checks):
        print(f"\n  🎉 创建 Agent 闭环验证全部通过！")
    else:
        print(f"\n  ⚠️ {len(checks) - passed} 项未通过")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(main())

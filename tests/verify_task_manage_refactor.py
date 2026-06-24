"""
task_manage 简化重构功能验证脚本（可独立运行）

验证内容：
1. TaskStatus 枚举 6 种状态
2. 状态转换矩阵覆盖所有合法/非法转换
3. task_manage action 简化为 get/continue/stop/delete/change
4. continue 合并 retry+inject+resume（message 参数可选）
5. stop 合并 pause+cancel（统一设 STOPPED，操作名=状态名）
6. src/tools/task_manage.yaml schema 与代码一致
7. 旧状态名和旧 action 引用已清理

使用方式：
    cd /mnt/d/myproject/container_08f57__wt_7dce57ec
    PYTHONPATH=src python3 tests/verify_task_manage_refactor.py
"""

from __future__ import annotations

import os
import sys
import inspect
import traceback

# 确保 src 在 Python 路径中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import yaml
from tasks.types import TaskStatus
from tasks.state_machine import (
    InvalidTransitionError,
    SimpleStateMachine,
    _TASK_TRANSITIONS,
    get_task_state_machine,
)
from tools.builtin.task.tool import TaskTool


# ── 工具函数 ──

passed_count = 0
failed_count = 0


def check(condition: bool, desc: str, detail: str = "") -> None:
    """断言并记录结果。"""
    global passed_count, failed_count
    if condition:
        passed_count += 1
        print(f"  ✅ {desc}")
    else:
        failed_count += 1
        print(f"  ❌ {desc}")
        if detail:
            print(f"     {detail}")


# ── STEP 1: TaskStatus 枚举 6 种状态 ──

def step1_task_status():
    print("\n═══ STEP 1: TaskStatus 枚举 6 种状态 ═══")
    expected_names = {"PENDING", "RUNNING", "STOPPED", "COMPLETED", "FAILED", "TIMEOUT"}
    actual_names = {s.name for s in TaskStatus}

    check(len(TaskStatus) == 6, "恰好 6 种状态", f"实际 {len(TaskStatus)} 种")
    check(actual_names == expected_names, "包含全部预期状态",
          f"缺失={expected_names - actual_names}, 多余={actual_names - expected_names}")

    for old in ["EVALUATING", "SUSPENDED", "CANCELLED", "BLOCKED", "SCHEDULED"]:
        check(old not in actual_names, f"旧状态 {old} 不存在")

    for s in TaskStatus:
        check(s.value == s.value.lower(), f"状态值小写: {s.name}={s.value}")


# ── STEP 2: 状态转换矩阵 ──

def step2_state_machine():
    print("\n═══ STEP 2: 状态转换矩阵 ═══")
    all_states = {"pending", "running", "stopped", "completed", "failed", "timeout"}

    check(set(_TASK_TRANSITIONS.keys()) == all_states, "转换规则覆盖全部 6 种状态")

    valid_transitions = [
        ("pending", "running"), ("pending", "stopped"),
        ("running", "completed"), ("running", "failed"), ("running", "stopped"), ("running", "timeout"),
        ("stopped", "running"), ("stopped", "pending"),
        ("completed", "pending"),
        ("failed", "pending"), ("failed", "running"),
        ("timeout", "running"), ("timeout", "pending"), ("timeout", "failed"),
    ]
    for src, dst in valid_transitions:
        sm = get_task_state_machine()
        sm._current_state = src
        check(sm.can_transition(dst), f"合法转换 {src}→{dst}")
        sm.transition(dst)
        check(sm.current_state == dst, f"  转换后状态正确: {dst}")

    invalid_transitions = [
        ("pending", "completed"), ("pending", "failed"), ("pending", "timeout"), ("pending", "pending"),
        ("running", "pending"), ("running", "running"),
        ("completed", "running"), ("completed", "stopped"), ("completed", "failed"),
        ("stopped", "completed"), ("stopped", "stopped"), ("stopped", "failed"), ("stopped", "timeout"),
        ("failed", "completed"), ("failed", "stopped"), ("failed", "failed"),
        ("timeout", "completed"), ("timeout", "stopped"), ("timeout", "timeout"),
    ]
    for src, dst in invalid_transitions:
        sm = get_task_state_machine()
        sm._current_state = src
        check(not sm.can_transition(dst), f"非法转换被拒绝 {src}→{dst}")
        try:
            sm.transition(dst)
            check(False, f"  应抛异常 {src}→{dst}")
        except InvalidTransitionError:
            check(True, f"  正确抛出 InvalidTransitionError {src}→{dst}")

    # 初始状态
    sm = get_task_state_machine()
    check(sm.current_state == "pending", "工厂函数初始状态为 pending")

    # stop → stopped（操作名=状态名）
    for src in ("pending", "running"):
        sm = get_task_state_machine()
        sm._current_state = src
        sm.transition("stopped")
        check(sm.current_state == "stopped", f"从 {src} 执行 stop 得到 stopped")


# ── STEP 3: YAML Schema ──

def step3_yaml_schema():
    print("\n═══ STEP 3: YAML Schema ═══")
    yaml_path = os.path.join(os.path.dirname(__file__), "..", "src", "tools", "task_manage.yaml")
    with open(yaml_path, "r", encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f)

    expected_actions = {"get", "continue", "stop", "delete", "change"}
    yaml_actions = set(yaml_data["parameters"]["properties"]["action"]["enum"])
    check(yaml_actions == expected_actions, "YAML action enum 包含 5 个操作",
          f"缺失={expected_actions - yaml_actions}, 多余={yaml_actions - expected_actions}")

    old_actions = {"retry", "inject", "resume", "pause", "cancel", "update",
                   "resume_completed", "complete_container", "fail_container",
                   "complete", "fail", "list", "status"}
    for old in old_actions:
        check(old not in yaml_actions, f"YAML 不含旧 action: {old}")

    expected_status = {"pending", "running", "stopped", "completed", "failed", "timeout"}
    yaml_status = set(yaml_data["parameters"]["properties"]["status"]["enum"])
    check(yaml_status == expected_status, "YAML status enum 包含 6 种状态")

    check("action" in yaml_data["parameters"]["required"], "YAML action 是必填参数")
    check("message" in yaml_data["parameters"]["properties"], "YAML 包含 message 参数")
    check("reason" in yaml_data["parameters"]["properties"], "YAML 包含 reason 参数")


# ── STEP 4: 代码 Schema 与 YAML 一致 ──

def step4_code_schema():
    print("\n═══ STEP 4: 代码 Schema 与 YAML 一致 ═══")
    yaml_path = os.path.join(os.path.dirname(__file__), "..", "src", "tools", "task_manage.yaml")
    with open(yaml_path, "r", encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f)

    tool_def = TaskTool.get_tool_definition()
    code_actions = set(tool_def.input_schema["properties"]["action"]["enum"])
    yaml_actions = set(yaml_data["parameters"]["properties"]["action"]["enum"])
    check(code_actions == yaml_actions, "代码与 YAML action enum 一致")

    code_status = set(tool_def.input_schema["properties"]["status"]["enum"])
    check(code_status == {"pending", "running", "stopped", "completed", "failed", "timeout"},
          "代码 status enum 包含 6 种状态")

    check("action" in tool_def.input_schema.get("required", []), "代码 action 是必填参数")
    check(tool_def.name == "task_manage", f"工具名正确: {tool_def.name}")


# ── STEP 5: continue 合并 retry+inject+resume ──

def step5_continue_logic():
    print("\n═══ STEP 5: continue 合并 retry+inject+resume ═══")
    # 核心方法存在
    check(hasattr(TaskTool, "_continue_task"), "_continue_task 方法存在")
    check(hasattr(TaskTool, "_inject_to_running"), "_inject_to_running 子方法存在")
    check(hasattr(TaskTool, "_resume_from_stopped"), "_resume_from_stopped 子方法存在")
    check(hasattr(TaskTool, "_retry_from_terminal"), "_retry_from_terminal 子方法存在")

    # continue 处理 4 种场景
    cont_source = inspect.getsource(TaskTool._continue_task)
    check("TaskStatus.RUNNING" in cont_source, "continue 处理 RUNNING 场景")
    check("TaskStatus.STOPPED" in cont_source, "continue 处理 STOPPED 场景")
    check("TaskStatus.FAILED" in cont_source, "continue 处理 FAILED 场景")
    check("TaskStatus.TIMEOUT" in cont_source, "continue 处理 TIMEOUT 场景")

    # message 参数可选
    check("message" in cont_source, "continue 使用 message 参数")


# ── STEP 6: stop 合并 pause+cancel ──

def step6_stop_logic():
    print("\n═══ STEP 6: stop 合并 pause+cancel ═══")
    check(hasattr(TaskTool, "_stop_task"), "_stop_task 方法存在")

    stop_source = inspect.getsource(TaskTool._stop_task)
    check("TaskStatus.STOPPED" in stop_source, "stop 设置 TaskStatus.STOPPED（操作名=状态名）")

    # execute 中无旧 action
    exec_source = inspect.getsource(TaskTool.execute)
    for old in ['"retry"', '"inject"', '"pause"', '"cancel"', '"resume"', '"update"']:
        check(old not in exec_source, f"execute() 不含旧 action: {old}")

    for new in ['"get"', '"continue"', '"stop"', '"delete"', '"change"']:
        check(new in exec_source, f"execute() 含新 action: {new}")


# ── STEP 7: 旧引用清理检查 ──

def step7_old_references():
    print("\n═══ STEP 7: 旧引用清理检查 ═══")
    target_files = [
        "src/tasks/types.py",
        "src/tasks/state_machine.py",
        "src/tools/builtin/task/tool.py",
        "src/tools/task_manage.yaml",
    ]
    old_states = ["SUSPENDED", "CANCELLED", "EVALUATING", "BLOCKED", "SCHEDULED"]
    old_actions_list = ["complete_container", "fail_container", "resume_completed",
                        "inject", "pause", "cancel"]

    issues = []
    for filepath in target_files:
        full_path = os.path.join(os.path.dirname(__file__), "..", filepath)
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        for old_state in old_states:
            for line_num, line in enumerate(content.split("\n"), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                if old_state in line and "旧" not in line and "合并" not in line:
                    if (f'TaskStatus.{old_state}' in line
                            or f'"{old_state.lower()}"' in line
                            or f"'{old_state.lower()}'" in line):
                        issues.append(f"{filepath}:{line_num}: old state '{old_state}' in code")

        for old_action in old_actions_list:
            for line_num, line in enumerate(content.split("\n"), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if (f'"{old_action}"' in line or f"'{old_action}'" in line):
                    if "旧" not in line and "合并" not in line:
                        issues.append(f"{filepath}:{line_num}: old action '{old_action}' reference")

    check(len(issues) == 0, "关键文件无旧状态/action 代码引用",
          f"发现 {len(issues)} 个问题: {issues}" if issues else "")


# ── 主函数 ──

def main():
    print("=" * 60)
    print("task_manage 简化重构功能验证")
    print("=" * 60)

    step1_task_status()
    step2_state_machine()
    step3_yaml_schema()
    step4_code_schema()
    step5_continue_logic()
    step6_stop_logic()
    step7_old_references()

    print("\n" + "=" * 60)
    total = passed_count + failed_count
    print(f"验证结果: {passed_count}/{total} 通过, {failed_count} 失败")
    if failed_count == 0:
        print("✅ 全部验证通过！")
    else:
        print("❌ 存在验证失败项！")
    print("=" * 60)
    sys.exit(0 if failed_count == 0 else 1)


if __name__ == "__main__":
    main()

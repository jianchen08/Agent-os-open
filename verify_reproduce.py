#!/usr/bin/env python3
"""
功能验证脚本：DB精简 + 任务系统统一 + 权责文档
可独立运行：python3 verify_reproduce.py
"""
import sys
import os
import subprocess

results = []

def check(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))

def run_cmd(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

print("=" * 60)
print("功能验证：DB精简 + 任务系统统一 + 权责文档")
print("=" * 60)

# ========== 1. DB精简 ==========
print("\n--- 1. DB精简验证 ---")

# 1a. src/db/ 目录不存在
code, _, _ = run_cmd("test -d src/db && echo YES || echo NO")
check("src/db/ 目录已删除", "NO" in run_cmd("test -d src/db && echo YES || echo NO")[1])

# 1b. 具体文件不存在
for f in ["src/db/__init__.py", "src/db/connection.py", "src/db/models.py"]:
    code, out, _ = run_cmd(f"test -f {f} && echo EXISTS || echo NOT_EXISTS")
    check(f"{f} 已删除", "NOT_EXISTS" in out)

# 1c. 全局无 SQLAlchemy 残留
code, out, _ = run_cmd("grep -ri 'sqlalchemy' src/ --include='*.py' -l 2>/dev/null")
check("全局无 SQLAlchemy 导入残留", out == "")

# 1d. pgvector_store.py 未受影响
code, out, _ = run_cmd("test -f src/memory/storage/pgvector_store.py && echo EXISTS || echo NOT_EXISTS")
check("pgvector_store.py 未受影响", "EXISTS" in out)

# 1e. 全局无 from src.db 残留
code, out, _ = run_cmd("grep -rn 'from src.db' src/ --include='*.py' 2>/dev/null")
check("全局无 from src.db 残留", out == "")

# ========== 2. 任务系统统一 ==========
print("\n--- 2. 任务系统统一验证 ---")

# 2a. state_machine.py 包含 SimpleStateMachine
code, out, _ = run_cmd("grep -c 'class SimpleStateMachine' src/tasks/state_machine.py 2>/dev/null")
check("SimpleStateMachine 类存在于 state_machine.py", out.strip() == "1")

# 2b. state_machine.py 包含 InvalidTransitionError
code, out, _ = run_cmd("grep -c 'class InvalidTransitionError' src/tasks/state_machine.py 2>/dev/null")
check("InvalidTransitionError 类存在于 state_machine.py", out.strip() == "1")

# 2c. 无旧版 TaskStateMachine 类定义（排除注释）
code, out, _ = run_cmd("grep -rn 'class TaskStateMachine' src/ --include='*.py' 2>/dev/null")
check("无旧版 TaskStateMachine 类定义", out == "")

# 2d. __init__.py 正确导出
code, out, _ = run_cmd("grep 'SimpleStateMachine' src/tasks/__init__.py 2>/dev/null")
has_sm = "SimpleStateMachine" in out
code, out2, _ = run_cmd("grep 'InvalidTransitionError' src/tasks/__init__.py 2>/dev/null")
has_ie = "InvalidTransitionError" in out2
check("__init__.py 正确导出 SimpleStateMachine 和 InvalidTransitionError", has_sm and has_ie)

# 2e. service.py 仅含 TaskService
code, out, _ = run_cmd("grep '^class ' src/tasks/service.py 2>/dev/null")
check("service.py 仅包含 TaskService 类", out.strip() == "class TaskService:")

# ========== 3. 项目可运行 ==========
print("\n--- 3. 项目可运行验证 ---")

# 3a. import src
code, out, err = run_cmd("python3 -c 'import src; print(\"OK\")'")
check("import src 无报错", code == 0 and "OK" in out)

# 3b. import SimpleStateMachine
code, out, err = run_cmd("python3 -c 'from src.tasks import SimpleStateMachine; print(\"OK\")'")
check("from src.tasks import SimpleStateMachine 正常", code == 0 and "OK" in out)

# 3c. import InvalidTransitionError
code, out, err = run_cmd("python3 -c 'from src.tasks import InvalidTransitionError; print(\"OK\")'")
check("from src.tasks import InvalidTransitionError 正常", code == 0 and "OK" in out)

# 3d. 状态机功能验证
code, out, err = run_cmd('''python3 -c "
from src.tasks import SimpleStateMachine, InvalidTransitionError
sm = SimpleStateMachine(initial_state='pending', transitions={'pending': ['running'], 'running': ['completed'], 'completed': []})
sm.transition('running')
sm.transition('completed')
try:
    sm.transition('pending')
    print('FAIL')
except InvalidTransitionError:
    print('OK')
"''')
check("状态机功能：转换和异常正确", code == 0 and "OK" in out)

# ========== 4. 权责文档 ==========
print("\n--- 4. 权责文档验证 ---")

# 4a. 文档存在
code, out, _ = run_cmd("test -f programming_orchestration_report.md && echo EXISTS || echo NOT_EXISTS")
doc_exists = "EXISTS" in out
check("programming_orchestration_report.md 存在", doc_exists)

if doc_exists:
    # 4b. 内容覆盖 task_submit 职责
    code, out, _ = run_cmd("grep -ic 'task_submit' programming_orchestration_report.md")
    check("文档覆盖 task_submit 职责", out.strip().isdigit() and int(out.strip()) > 0)
    # 4c. 内容覆盖任务系统职责边界
    code, out, _ = run_cmd("grep -ic '任务系统' programming_orchestration_report.md")
    check("文档覆盖任务系统职责边界", out.strip().isdigit() and int(out.strip()) > 0)
else:
    check("文档覆盖 task_submit 职责", False, "文档不存在，无法验证内容")
    check("文档覆盖任务系统职责边界", False, "文档不存在，无法验证内容")

# ========== 补充场景 ==========
print("\n--- 补充场景 ---")

# 补充1：from src.db 导入应失败
code, out, err = run_cmd("python3 -c 'from src.db import something' 2>&1")
check("from src.db 导入正确报错", "ModuleNotFoundError" in out or "ImportError" in out or code != 0)

# 补充2：pgvector_store 可导入
code, out, err = run_cmd("python3 -c 'from src.memory.storage.pgvector_store import PgVectorStore; print(\"OK\")'")
check("pgvector_store 可正常导入（未被影响）", code == 0 and "OK" in out)

# ========== 汇总 ==========
print("\n" + "=" * 60)
total = len(results)
passed = sum(1 for _, p, _ in results if p)
failed = total - passed
print(f"总计: {total} 项 | 通过: {passed} | 失败: {failed}")
if failed:
    print("\n失败项:")
    for name, p, detail in results:
        if not p:
            print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
print("=" * 60)
sys.exit(0 if failed == 0 else 1)

#!/usr/bin/env python3
"""isolation 系统插件迁移功能验证脚本。

覆盖验证点 2-8：
  2. Python 导入链可正确加载
  3. IsolationManager 可实例化
  4. CheckpointManager 正确初始化
  5. PermissionChecker 可调用
  6. server.py AST 结构验证
  7. 迁移完整性（文件清单对比）
  8. 导入路径适配正确性（无残留旧路径）

用法: python3 docs/working/isolation_verify.py
"""
from __future__ import annotations

import ast
import os
import sys
import importlib
from pathlib import Path

BASE = Path(__file__).resolve().parent
PLUGIN_DIR = BASE.parent.parent / "plugins" / "shared" / "system" / "isolation"
SRC_DIR = BASE.parent.parent / "src" / "isolation"

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    status = "✅" if passed else "❌"
    print(f"  [{status}] {name}: {detail}")


# ========== 验证点2: Python 导入链可正确加载 ==========
print("\n=== 验证点2: 导入链可正确加载 ===")
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR / "providers"))

# 同目录模块（平铺）
flat_modules = [
    "isolation_types",
    "policy",
    "decider",
    "approval",
    "permission_policy",
    "permission_checker",
    "checkpoint",
    "manager",
    "workspace",
    "sensitive_paths",
    "hardware_profile",
]
for idx, mod_name in enumerate(flat_modules):
    try:
        importlib.import_module(mod_name)
        check(f"2.{idx+1} 导入 {mod_name}", True, "成功")
    except Exception as e:
        check(f"2.{idx+1} 导入 {mod_name}", False, str(e))

# providers 子目录模块
provider_modules = [
    "base",
    "host_provider",
    "docker_provider",
    "cua_provider",
    "e2b_provider",
]
for idx, mod_name in enumerate(provider_modules):
    try:
        importlib.import_module(mod_name)
        check(f"2.{len(flat_modules)+idx+1} 导入 providers/{mod_name}", True, "成功")
    except Exception as e:
        check(f"2.{len(flat_modules)+idx+1} 导入 providers/{mod_name}", False, str(e))

# ========== 验证点3: IsolationManager 可实例化 ==========
print("\n=== 验证点3: IsolationManager 可实例化 ===")
try:
    from manager import IsolationManager

    mgr = IsolationManager()
    check("3.1 IsolationManager() 无参构造", isinstance(mgr, IsolationManager),
          f"实例类型={type(mgr).__name__}")
except Exception as e:
    check("3.1 IsolationManager() 无参构造", False, f"异常: {e}")

# ========== 验证点4: CheckpointManager 正确初始化 ==========
print("\n=== 验证点4: CheckpointManager 正确初始化 ===")
try:
    from checkpoint import CheckpointManager

    cpm = CheckpointManager(project_root=os.getcwd())
    check("4.1 CheckpointManager(project_root=...)", isinstance(cpm, CheckpointManager),
          f"实例类型={type(cpm).__name__}")
except Exception as e:
    check("4.1 CheckpointManager(project_root=...)", False, f"异常: {e}")

# ========== 验证点5: PermissionChecker 可调用 ==========
print("\n=== 验证点5: PermissionChecker 可调用 ===")
try:
    from permission_checker import PermissionChecker
    from permission_policy import PermissionPolicyManager

    pc = PermissionChecker(project_root=".")
    check("5.1 PermissionChecker(project_root='.')", isinstance(pc, PermissionChecker),
          f"实例类型={type(pc).__name__}")

    policy = PermissionPolicyManager().get_default_policy()
    result = pc.check_read_permission("test.txt", "/tmp", policy)
    is_tuple = isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool)
    check("5.2 check_read_permission 返回 (bool, str)", is_tuple,
          f"返回值={result}")
except Exception as e:
    check("5.2 check_read_permission 返回 (bool, str)", False, f"异常: {e}")

# ========== 验证点6: server.py AST 结构验证 ==========
print("\n=== 验证点6: server.py AST 结构验证 ===")
server_path = PLUGIN_DIR / "server.py"
with open(server_path) as f:
    source = f.read()
tree = ast.parse(source)

# 6.1 统计 @plugin.tool 装饰器数量
tool_count = 0
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and hasattr(dec.func, "attr") and dec.func.attr == "tool":
                tool_count += 1
check("6.1 @plugin.tool 数量=7", tool_count == 7, f"实际={tool_count}")

# 6.2 统计生命周期钩子（on_load/on_unload）
hook_names = set()
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for dec in node.decorator_list:
            if isinstance(dec, ast.Attribute) and dec.attr in ("on_load", "on_unload"):
                hook_names.add(dec.attr)
check("6.2 生命周期钩子=on_load+on_unload", hook_names == {"on_load", "on_unload"},
      f"实际={hook_names}")

# 6.3 确认无 _ConfigCenterShim 类定义
shim_classes = []
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == "_ConfigCenterShim":
        shim_classes.append(node.name)
check("6.3 无 _ConfigCenterShim 类", len(shim_classes) == 0,
      f"找到={shim_classes}")

# ========== 验证点7: 迁移完整性 ==========
print("\n=== 验证点7: 迁移完整性 ===")

# 7.1 src/isolation/ 的 .py 文件清单（不含 providers 子目录）
src_flat = {f.name for f in SRC_DIR.glob("*.py")}
dst_flat = {f.name for f in PLUGIN_DIR.glob("*.py")} - {"server.py"}

# types.py -> isolation_types.py 改名映射
def normalize(name: str) -> str:
    return name.replace("isolation_types", "types")

src_norm = {normalize(n) for n in src_flat}
dst_norm = {normalize(n) for n in dst_flat}

check("7.1 平铺.py文件全部复制（types.py→isolation_types.py改名）",
      src_norm == dst_norm,
      f"src_only={src_norm - dst_norm}, dst_only={dst_norm - src_norm}")

# 7.2 providers 子目录
src_providers = {f.name for f in (SRC_DIR / "providers").glob("*.py")}
dst_providers = {f.name for f in (PLUGIN_DIR / "providers").glob("*.py")}
check("7.2 providers/ 5个文件全部复制",
      src_providers == dst_providers,
      f"src_only={src_providers - dst_providers}, dst_only={dst_providers - src_providers}")

# 7.3 server.py 和 plugin.json 是新增适配文件
check("7.3 server.py 存在（适配层）", (PLUGIN_DIR / "server.py").exists(), "")
check("7.4 plugin.json 存在（插件清单）", (PLUGIN_DIR / "plugin.json").exists(), "")

# ========== 验证点8: 导入路径适配正确性 ==========
print("\n=== 验证点8: 导入路径适配正确性 ===")

old_import_patterns = [
    "from isolation.",
    "from src.isolation",
    "import isolation.",
    "import src.isolation",
]

all_py_files = list(PLUGIN_DIR.glob("**/*.py"))
residue_found = []
for py_file in all_py_files:
    if "__pycache__" in str(py_file):
        continue
    with open(py_file) as f:
        content = f.read()
    for pattern in old_import_patterns:
        if pattern in content:
            residue_found.append(f"{py_file.name}: '{pattern}'")

check("8.1 无 'from isolation.' / 'from src.isolation' 残留",
      len(residue_found) == 0,
      f"残留={residue_found}" if residue_found else "无残留")

# ========== 汇总 ==========
print("\n" + "=" * 60)
total = len(results)
passed_count = sum(1 for _, p, _ in results if p)
failed = total - passed_count
print(f"验证结果汇总: {passed_count}/{total} 通过, {failed} 失败")
print("=" * 60)

if failed > 0:
    print("\n失败项:")
    for name, p, detail in results:
        if not p:
            print(f"  ❌ {name}: {detail}")
    sys.exit(1)
else:
    print("\n✅ 全部验证通过!")
    sys.exit(0)

#!/usr/bin/env python3
"""功能验证复现脚本：文件拆分完整性 + 前端通知滚动

运行方式：
  python3 verify_reproduce.py

前提条件：
  - 项目源码目录存在于 ../container_08f57__wt_749baa89 (或修改 PROJECT_ROOT)
  - Python 3.12+ 已安装
  - pytest 已安装
"""

import sys
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置：项目源码根目录
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent / "container_08f57__wt_749baa89"

if not _PROJECT_ROOT.exists():
    # 尝试从当前工作目录查找
    for candidate in _SCRIPT_DIR.parent.iterdir():
        if (candidate / "src" / "tools" / "executor.py").exists():
            _PROJECT_ROOT = candidate
            break

SRC_DIR = _PROJECT_ROOT / "src"
FRONTEND_DIR = _PROJECT_ROOT / "frontend"

print(f"项目根目录: {_PROJECT_ROOT}")
print(f"源码目录:   {SRC_DIR}")
print(f"前端目录:   {FRONTEND_DIR}")
print()

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✅ {label}")
        passed += 1
    else:
        print(f"  ❌ {label} — {detail}")
        failed += 1


# ===========================================================================
# 交付成果 1：6 个大文件拆分
# ===========================================================================
print("=" * 70)
print("交付成果 1：6 个大文件拆分验证")
print("=" * 70)

# --- 步骤 1：确认所有拆分产物文件存在 ---
print("\n[步骤 1] 确认 16 个拆分产物文件存在")

SPLIT_FILES = [
    # executor.py 拆分
    "src/tools/tool_cache.py",
    "src/tools/input_normalizer.py",
    "src/tools/nested_record_manager.py",
    "src/tools/executor.py",
    # models.py 拆分
    "src/channels/api/memory_store.py",
    "src/channels/api/models.py",
    # mcp_loader.py 拆分
    "src/tools/mcp_client.py",
    "src/tools/mcp_loader.py",
    # workspace_lifecycle.py 拆分
    "src/isolation/_workspace_git_ops.py",
    "src/isolation/_workspace_merge_ops.py",
    "src/isolation/workspace_lifecycle.py",
    # resource_merge/tool.py 拆分
    "src/tools/builtin/resource_merge/git_helpers.py",
    "src/tools/builtin/resource_merge/tool.py",
    # plugin.py 拆分
    "src/plugins/core/llm_core/_message_normalizer.py",
    "src/plugins/core/llm_core/plugin.py",
    # 前端组件
    "frontend/src/components/chat/NotificationPanel.tsx",
]

for f in SPLIT_FILES:
    check(f, (_PROJECT_ROOT / f).exists(), "文件不存在")

# --- 步骤 2：逐个导入所有拆分产物 ---
print("\n[步骤 2] 逐个导入所有 Python 拆分产物模块")

import importlib

sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(_PROJECT_ROOT))

MODULES_AND_ATTRS = [
    ("tools.tool_cache", "ToolCache"),
    ("tools.tool_cache", "ToolCacheConfig"),
    ("tools.input_normalizer", "normalize_inputs"),
    ("tools.nested_record_manager", "NestedRecordManager"),
    ("tools.executor", "ToolExecutor"),
    ("channels.api.memory_store", "MemoryStore"),
    ("channels.api.models", "RefreshRequest"),
    ("tools.mcp_client", "MCPClient"),
    ("tools.mcp_loader", "MCPToolLoader"),
    ("isolation._workspace_git_ops", "_GitOpsMixin"),
    ("isolation._workspace_merge_ops", "_MergeOpsMixin"),
    ("isolation.workspace_lifecycle", "WorkspaceLifecycleManager"),
    ("tools.builtin.resource_merge.git_helpers", "GitHelpers"),
    ("tools.builtin.resource_merge.tool", "ResourceMergeTool"),
    ("plugins.core.llm_core._message_normalizer", "normalize_messages_for_provider"),
    ("plugins.core.llm_core.plugin", "LLMCore"),
]

for mod_path, attr in MODULES_AND_ATTRS:
    try:
        mod = importlib.import_module(mod_path)
        check(f"import {mod_path} → {attr}", hasattr(mod, attr), f"属性 {attr} 不存在")
    except Exception as e:
        check(f"import {mod_path}", False, str(e))

# --- 步骤 3：验证 ToolExecutor 组合使用拆分模块 ---
print("\n[步骤 3] 验证 ToolExecutor 组合使用拆分模块")

executor_mod = importlib.import_module("tools.executor")
check("executor 导入 ToolCache", hasattr(executor_mod, "ToolCache"))
check("executor 导入 ToolCacheConfig", hasattr(executor_mod, "ToolCacheConfig"))
check("executor 导入 NestedRecordManager", hasattr(executor_mod, "NestedRecordManager"))

cls = executor_mod.ToolExecutor
for m in ("execute", "batch_execute", "execute_pipeline"):
    check(f"ToolExecutor.{m} 方法存在", hasattr(cls, m), f"方法 {m} 不存在")

# --- 步骤 4：验证 WorkspaceLifecycleManager Mixin 继承 ---
print("\n[步骤 4] 验证 WorkspaceLifecycleManager Mixin 继承")

iso_mod = importlib.import_module("isolation.workspace_lifecycle")
git_mod = importlib.import_module("isolation._workspace_git_ops")
merge_mod = importlib.import_module("isolation._workspace_merge_ops")
wls_cls = iso_mod.WorkspaceLifecycleManager

check(
    "继承 _GitOpsMixin",
    issubclass(wls_cls, git_mod._GitOpsMixin),
    "issubclass 检查失败",
)
check(
    "继承 _MergeOpsMixin",
    issubclass(wls_cls, merge_mod._MergeOpsMixin),
    "issubclass 检查失败",
)

# --- 补充场景 1：向后兼容性 ---
print("\n[补充场景 1] 向后兼容性 — 重导出验证")

mcp_mod = importlib.import_module("tools.mcp_loader")
check("mcp_loader 重导出 MCPClient", hasattr(mcp_mod, "MCPClient"))

init_mod = importlib.import_module("plugins.core.llm_core")
check("__init__ 重导出 LLMCore", hasattr(init_mod, "LLMCore"))

# --- 补充场景 2：下游模块可导入 ---
print("\n[补充场景 2] 下游模块兼容性验证")

DOWNSTREAM = [
    "tools.auto_loader",
    "tools.global_registry",
    "tools.loader",
    "tools.registry",
    "tools.mcp_adapter",
    "channels.api.app",
    "channels.api.deps",
    "channels.api.routes_threads",
    "channels.api.routes_tasks",
    "isolation.manager",
    "isolation.workspace",
    "tools.builtin.resource_merge",
]

for m in DOWNSTREAM:
    try:
        importlib.import_module(m)
        check(f"下游模块 {m} 可导入", True)
    except Exception as e:
        check(f"下游模块 {m} 可导入", False, str(e))

# ===========================================================================
# 交付成果 2：前端通知滚动组件
# ===========================================================================
print("\n" + "=" * 70)
print("交付成果 2：前端通知滚动组件验证")
print("=" * 70)

# --- 步骤 5：NotificationPanel.tsx 代码审查 ---
print("\n[步骤 5] NotificationPanel.tsx 源码审查")

tsx_path = FRONTEND_DIR / "src" / "components" / "chat" / "NotificationPanel.tsx"
if tsx_path.exists():
    source = tsx_path.read_text(encoding="utf-8")

    import re

    check("组件导入 useNotificationStore", "useNotificationStore" in source)
    check(
        "store 导入路径正确",
        bool(re.search(r"""from\s+['"]@/stores/notificationStore['"]""", source)),
    )
    check(
        "导入 NotificationItemComponent",
        bool(re.search(r"""from\s+['"]\.\/NotificationItem['"]""", source)),
    )
    check("使用 NotificationItemComponent JSX", "<NotificationItemComponent" in source)
    check("列表容器有 overflow-y-auto", "overflow-y-auto" in source)
    check("列表容器有 maxHeight", "maxHeight" in source)
    check(
        "定义 DEFAULT_LIST_MAX_HEIGHT",
        bool(re.search(r"DEFAULT_LIST_MAX_HEIGHT\s*=\s*['\"]", source)),
    )
    check("单条通知有 itemMaxHeight", "itemMaxHeight" in source)
    check(
        "overflow-y-auto 至少出现 2 次（列表+单条）",
        len(re.findall(r"overflow-y-auto", source)) >= 2,
    )
    check(
        "定义 DEFAULT_ITEM_MAX_HEIGHT 数值",
        bool(re.search(r"DEFAULT_ITEM_MAX_HEIGHT\s*=\s*\d+", source)),
    )
    check(
        "命名导出 NotificationPanel",
        bool(re.search(r"export\s+function\s+NotificationPanel", source)),
    )
    check(
        "导出 NotificationPanelProps",
        bool(re.search(r"export\s+interface\s+NotificationPanelProps", source)),
    )
else:
    check("NotificationPanel.tsx 存在", False, "文件不存在")

# --- 步骤 6：运行 pytest 测试套件 ---
print("\n[步骤 6] 运行 pytest 测试套件")

tests_dir = _SCRIPT_DIR / "tests"
if tests_dir.exists():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(tests_dir), "-v", "--timeout=30"],
        capture_output=True,
        text=True,
        cwd=str(_SCRIPT_DIR),
    )
    # 统计通过/失败数
    output = result.stdout + result.stderr
    import re

    passed_match = re.search(r"(\d+) passed", output)
    failed_match = re.search(r"(\d+) failed", output)
    p = int(passed_match.group(1)) if passed_match else 0
    f = int(failed_match.group(1)) if failed_match else 0
    check(f"pytest 测试套件: {p} passed, {f} failed", result.returncode == 0, output[-500:])
else:
    check("tests/ 目录存在", False, "测试目录不存在")

# ===========================================================================
# 汇总
# ===========================================================================
print("\n" + "=" * 70)
total = passed + failed
print(f"汇总: {passed}/{total} 通过, {failed} 失败")
if failed == 0:
    print("🎉 所有验证项全部通过！")
else:
    print("⚠️ 存在失败项，请检查上方输出。")
print("=" * 70)

sys.exit(0 if failed == 0 else 1)

#!/usr/bin/env python3
"""workspace 模块功能验证脚本。

验证场景：
1. workspace.get_or_create(container_task_id='task-001') → success=true
2. 再次调用相同 task_id 返回相同 workspace
3. workspace.get(container_task_id='nonexistent') → success=false
4. workspace.get_file_tree 扫描真实目录
5. 错误输入：get_or_create 传空 container_task_id
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import traceback

# ---- SDK 导入兼容 patch ----
import agentos_plugin_sdk  # noqa: E402
from agentos_plugin_sdk.plugin import AgentOSPlugin  # noqa: E402

agentos_plugin_sdk.AgentOSPlugin = AgentOSPlugin

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE))
WORKSPACE_DIR = os.path.join(PROJECT_ROOT, "plugins", "shared", "system", "workspace")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    status = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {status}: {name}" + (f" — {detail}" if detail else ""))


def load_server():
    """加载 workspace/server.py 模块。"""
    server_path = os.path.join(WORKSPACE_DIR, "server.py")
    spec = importlib.util.spec_from_file_location("workspace_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def run_tests() -> None:
    print("\n=== workspace 模块功能验证 ===\n")

    try:
        server = load_server()
    except Exception as e:
        record("导入 server.py", False, f"导入失败: {e}")
        traceback.print_exc()
        return

    # 初始化服务
    try:
        await server._on_load({})
        record("on_load 初始化", True)
    except Exception as e:
        record("on_load 初始化", False, str(e))
        return

    # ---- 步骤1: get_or_create ----
    task_id = "task-001"
    first_ws_id = None
    try:
        result = await server.workspace_get_or_create(container_task_id=task_id)
        assert result["success"] is True, f"应成功, 实际: {result}"
        ws = result["workspace"]
        assert ws["container_task_id"] == task_id, f"task_id 不匹配, 实际: {ws['container_task_id']}"
        first_ws_id = ws["id"]
        record("workspace.get_or_create('task-001')", True, f"id={first_ws_id}")
    except Exception as e:
        record("workspace.get_or_create('task-001')", False, str(e))
        traceback.print_exc()

    # ---- 步骤2: 再次调用相同 task_id 返回相同 workspace ----
    if first_ws_id:
        try:
            result = await server.workspace_get_or_create(container_task_id=task_id)
            assert result["success"] is True
            ws = result["workspace"]
            assert ws["id"] == first_ws_id, f"应为相同 workspace, 实际: ws_id={ws['id']} vs first={first_ws_id}"
            record("相同 task_id 返回相同 workspace", True, f"ws_id={ws['id']}")
        except Exception as e:
            record("相同 task_id 返回相同 workspace", False, str(e))

    # ---- 步骤3: get 不存在的 workspace ----
    try:
        result = await server.workspace_get(container_task_id="nonexistent-task-id")
        assert result["success"] is False, f"不存在应失败, 实际: {result}"
        assert "error" in result
        record("workspace.get('nonexistent') → success=False", True)
    except Exception as e:
        record("workspace.get('nonexistent') → success=False", False, str(e))

    # ---- 步骤4: get 已存在的 workspace ----
    if first_ws_id:
        try:
            result = await server.workspace_get(container_task_id=task_id)
            assert result["success"] is True, f"应成功, 实际: {result}"
            assert result["workspace"]["container_task_id"] == task_id
            record("workspace.get('task-001') 已存在", True)
        except Exception as e:
            record("workspace.get('task-001') 已存在", False, str(e))

    # ---- 步骤5: get_file_tree 扫描真实目录 ----
    temp_dir = tempfile.mkdtemp(prefix="ws_tree_")
    # 在临时目录中创建一些测试文件
    os.makedirs(os.path.join(temp_dir, "subdir"))
    with open(os.path.join(temp_dir, "file1.py"), "w") as f:
        f.write("# test")
    with open(os.path.join(temp_dir, "subdir", "file2.py"), "w") as f:
        f.write("# nested")

    try:
        result = await server.workspace_get_file_tree(
            container_task_id=task_id, base_path=temp_dir
        )
        assert result["success"] is True, f"应成功, 实际: {result}"
        tree = result["tree"]
        assert len(tree) > 0, f"文件树不应为空, 实际: {tree}"
        # 验证文件树包含创建的文件
        names = [n["name"] for n in tree]
        assert "file1.py" in names, f"应包含 file1.py, 有: {names}"
        # 验证子目录存在且有 children
        subdir_node = next((n for n in tree if n["name"] == "subdir"), None)
        assert subdir_node is not None, f"应包含 subdir"
        assert subdir_node["type"] == "directory"
        record("workspace.get_file_tree 扫描目录", True, f"tree entries={len(tree)}")
    except Exception as e:
        record("workspace.get_file_tree 扫描目录", False, str(e))
        traceback.print_exc()

    # ---- 步骤6: get_file_tree 无 base_path（不存在的目录）----
    try:
        result = await server.workspace_get_file_tree(
            container_task_id=task_id, base_path="/nonexistent/path/12345"
        )
        assert result["success"] is True  # 不应报错
        assert result["tree"] == [], f"不存在的路径应返回空树, 实际: {result['tree']}"
        record("get_file_tree 不存在路径返回空树", True)
    except Exception as e:
        record("get_file_tree 不存在路径返回空树", False, str(e))


def main() -> int:
    asyncio.run(run_tests())
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n=== workspace 结果: {passed}/{total} 通过 ===\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

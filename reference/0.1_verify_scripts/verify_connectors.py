#!/usr/bin/env python3
"""connectors 模块功能验证脚本。

验证场景：
1. connector.list 初始返回 count=0
2. 注册 vscode 连接器后 list 返回 count=1
3. connector.degrade 降级模式 open_file → degraded=True
4. connector.get_adapter_status 返回 success=True
5. 错误输入：注册不支持的连接器类型
6. 错误输入：degrade 不支持的操作类型
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import traceback

# ---- SDK 导入兼容 patch ----
# SDK 的 __init__.py 未导出 AgentOSPlugin（已知 SDK 问题），此处手动桥接
import agentos_plugin_sdk  # noqa: E402
from agentos_plugin_sdk.plugin import AgentOSPlugin  # noqa: E402

agentos_plugin_sdk.AgentOSPlugin = AgentOSPlugin

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE))
CONNECTORS_DIR = os.path.join(PROJECT_ROOT, "plugins", "shared", "system", "connectors")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    status = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {status}: {name}" + (f" — {detail}" if detail else ""))


def load_server():
    """加载 connectors/server.py 模块。"""
    server_path = os.path.join(CONNECTORS_DIR, "server.py")
    spec = importlib.util.spec_from_file_location("connectors_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def run_tests() -> None:
    print("\n=== connectors 模块功能验证 ===\n")

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

    # ---- 步骤1: list 初始状态 ----
    try:
        result = await server.connector_list()
        assert result["success"] is True, f"success 应为 True, 实际: {result}"
        assert result["count"] == 0, f"初始 count 应为 0, 实际: {result['count']}"
        record("connector.list 初始 count=0", True, f"count={result['count']}")
    except Exception as e:
        record("connector.list 初始 count=0", False, str(e))

    # ---- 步骤2: 注册 vscode 连接器 ----
    try:
        result = await server.connector_register(connector_type="vscode")
        assert result["success"] is True, f"注册应成功, 实际: {result}"
        record("connector.register('vscode')", True, f"result={result}")
    except Exception as e:
        record("connector.register('vscode')", False, str(e))
        traceback.print_exc()

    # ---- 步骤3: list 注册后 ----
    try:
        result = await server.connector_list()
        assert result["success"] is True
        assert result["count"] == 1, f"注册后 count 应为 1, 实际: {result['count']}"
        # 验证连接器信息
        conn_info = result["connectors"][0]
        assert conn_info["connector_type"] == "vscode"
        assert "open_file" in conn_info["capabilities"]
        record("connector.list 注册后 count=1", True, f"capabilities={conn_info['capabilities']}")
    except Exception as e:
        record("connector.list 注册后 count=1", False, str(e))

    # ---- 步骤4: degrade 降级模式 open_file ----
    test_file = "/tmp/verify_batch3_test.txt"
    try:
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("hello connectors degrade test")
        result = await server.connector_degrade(
            action_type="open_file", params={"file_path": test_file}
        )
        assert result["success"] is True, f"degrade 应成功, 实际: {result}"
        assert result["data"]["degraded"] is True, f"应为降级模式, 实际: {result['data']}"
        assert "content" in result["data"], "应包含文件内容"
        record("connector.degrade('open_file') degraded=True", True, f"data.degraded={result['data']['degraded']}")
    except Exception as e:
        record("connector.degrade('open_file') degraded=True", False, str(e))

    # ---- 步骤5: get_adapter_status ----
    try:
        result = await server.connector_get_adapter_status()
        assert result["success"] is True, f"应返回 success, 实际: {result}"
        # 配置为空时应返回空 dict 而非报错
        assert "adapters" in result, f"应包含 adapters 字段, 实际: {result}"
        record("connector.get_adapter_status", True, f"adapters={result.get('adapters')}")
    except Exception as e:
        record("connector.get_adapter_status", False, str(e))

    # ---- 步骤6: get_active（无活跃连接器）----
    try:
        result = await server.connector_get_active()
        assert result["success"] is True
        assert result["connector"] is None, f"无活跃连接器应为 None, 实际: {result['connector']}"
        record("connector.get_active 初始为 None", True)
    except Exception as e:
        record("connector.get_active 初始为 None", False, str(e))

    # ---- 补充场景1: 注册不支持的类型 ----
    try:
        result = await server.connector_register(connector_type="nonexistent_ide")
        assert result["success"] is False, f"不支持类型应失败, 实际: {result}"
        assert "error" in result
        record("错误输入: register('nonexistent')", True, f"返回 success=False")
    except Exception as e:
        record("错误输入: register('nonexistent')", False, str(e))

    # ---- 补充场景2: degrade 不支持的操作 ----
    try:
        result = await server.connector_degrade(
            action_type="nonexistent_action", params={}
        )
        assert result["success"] is False, f"不支持操作应失败, 实际: {result}"
        record("错误输入: degrade('nonexistent_action')", True, f"返回 success=False")
    except Exception as e:
        record("错误输入: degrade('nonexistent_action')", False, str(e))

    # ---- 补充场景3: degrade show_diff 降级 ----
    try:
        result = await server.connector_degrade(
            action_type="show_diff",
            params={
                "original_content": "line1\nline2",
                "new_content": "line1\nline2_modified",
                "file_path": "test.py",
            },
        )
        assert result["success"] is True
        assert result["data"]["degraded"] is True
        assert "diff_text" in result["data"]
        record("degrade('show_diff') 降级 diff", True, f"diff 包含内容")
    except Exception as e:
        record("degrade('show_diff') 降级 diff", False, str(e))


def main() -> int:
    asyncio.run(run_tests())
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n=== connectors 结果: {passed}/{total} 通过 ===\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

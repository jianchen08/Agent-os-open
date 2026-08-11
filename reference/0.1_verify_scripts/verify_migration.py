#!/usr/bin/env python3
"""42个工具插件迁移功能验证脚本。

可独立运行：python3 docs/working/verify_migration.py
工作目录：项目根目录（/workspace）
依赖：Python 3.10+, pyyaml
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

# ── 路径设置 ──────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS_DIR = os.path.join(PROJECT_ROOT, "plugins", "shared", "tools")
SIMPLE_DIR = os.path.join(TOOLS_DIR, "simple")
SDK_SRC = os.path.join(PROJECT_ROOT, "plugins", "sdk", "src")

sys.path.insert(0, SIMPLE_DIR)
sys.path.insert(0, SDK_SRC)

results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}: {detail}")


# ── 场景1: 外部MCP工具配置验证 ────────────────────────────
def test_scenario1() -> None:
    print("\n" + "=" * 60)
    print("场景1 - 外部MCP工具配置验证 (7个)")
    print("=" * 60)

    mcp_dir = os.path.join(TOOLS_DIR, "external_mcp")
    expected = sorted(["browser_test", "design_generate", "design_review",
                       "mcp_registry", "resource_search", "smithery", "web_search"])
    actual = sorted(os.listdir(mcp_dir))

    if actual != expected:
        record("MCP目录列表", False, f"预期{expected}, 实际{actual}")
        return

    for name in expected:
        plugin_path = os.path.join(mcp_dir, name, "plugin.json")
        try:
            with open(plugin_path) as f:
                cfg = json.load(f)
            entry_ok = cfg.get("entry") == "mcp:external"
            transport_ok = cfg.get("mcp_endpoint", {}).get("transport") in ("stdio", "http")
            tools_ok = len(cfg.get("capabilities", {}).get("tools", [])) > 0
            record(f"MCP/{name}", entry_ok and transport_ok and tools_ok,
                   f"entry={cfg.get('entry')}, transport={cfg.get('mcp_endpoint', {}).get('transport')}, tools={len(cfg.get('capabilities', {}).get('tools', []))}")
        except Exception as e:
            record(f"MCP/{name}", False, str(e))


# ── 场景2: 简单工具核心功能验证 ──────────────────────────
async def test_scenario2() -> None:
    print("\n" + "=" * 60)
    print("场景2 - 简单工具核心功能验证")
    print("=" * 60)

    from converter_tools import unit_converter
    from calc_tools import scientific_calculator
    from system_tools import yaml_validate

    # 2.1 unit_converter
    try:
        r = await unit_converter(value=1, from_unit="kg", to_unit="g", category="weight")
        record("unit_converter kg->g", r.get("result") == 1000.0, f"result={r.get('result')}")
    except Exception as e:
        record("unit_converter kg->g", False, str(e))

    # 2.2 scientific_calculator
    try:
        r = await scientific_calculator(operation="calculate", expression="2+3*4")
        record("scientific_calculator 2+3*4", r.get("result") == 14, f"result={r.get('result')}")
    except Exception as e:
        record("scientific_calculator 2+3*4", False, str(e))

    # 2.3 yaml_validate
    try:
        r = await yaml_validate(content="name: test")
        record("yaml_validate name:test",
               r.get("valid") is True and r.get("parsed") == {"name": "test"},
               f"valid={r.get('valid')}, parsed={r.get('parsed')}")
    except Exception as e:
        record("yaml_validate name:test", False, str(e))


# ── 场景3: 复杂工具结构验证 ──────────────────────────────
def test_scenario3() -> None:
    print("\n" + "=" * 60)
    print("场景3 - 复杂工具结构验证")
    print("=" * 60)

    complex_dirs = [
        "bash", "download", "resource_merge", "task", "task_submit", "task_evaluate",
        "test_ext", "search", "triggers_ext", "media", "lsp", "memory", "human",
        "web_ext", "hot_swap",
    ]
    for name in complex_dirs:
        d = os.path.join(TOOLS_DIR, name)
        has_plugin = os.path.isfile(os.path.join(d, "plugin.json"))
        has_server = os.path.isfile(os.path.join(d, "server.py"))
        py_files = [f for f in os.listdir(d) if f.endswith(".py") and f != "server.py" and not f.startswith("test_")]
        record(f"Complex/{name}", has_plugin and has_server and len(py_files) > 0,
               f"plugin={has_plugin}, server={has_server}, sources={py_files}")


# ── 场景4: SDK集成验证 ──────────────────────────────────
def test_scenario4() -> None:
    print("\n" + "=" * 60)
    print("场景4 - SDK集成验证")
    print("=" * 60)

    from agentos_plugin_sdk import AgentOSPlugin
    from server import create_plugin

    plugin = create_plugin()
    is_instance = isinstance(plugin, AgentOSPlugin)
    record("create_plugin()返回类型", is_instance, f"type={type(plugin).__name__}")

    tool_names = list(plugin._tools.keys())
    expected_count = 11
    record("工具注册数量", len(tool_names) == expected_count,
           f"count={len(tool_names)} (expected={expected_count})")

    all_valid = all(td.schema is not None and td.handler is not None for td in plugin._tools.values())
    record("工具schema+handler完整性", all_valid, f"tools={tool_names}")


# ── 场景5: sys.path注入验证 ──────────────────────────────
def test_scenario5() -> None:
    print("\n" + "=" * 60)
    print("场景5 - sys.path注入验证")
    print("=" * 60)

    # 5.1 _SRC_ROOT 路径计算正确性
    server_file = os.path.join(TOOLS_DIR, "bash", "server.py")
    server_dir = os.path.dirname(os.path.abspath(server_file))
    project_root = os.path.abspath(os.path.join(server_dir, "..", "..", "..", ".."))
    src_root = os.path.join(project_root, "src")
    record("_SRC_ROOT路径计算", os.path.isdir(src_root), f"_SRC_ROOT={src_root}")

    # 5.2 所有复杂工具 server.py 的 sys.path 注入模式
    complex_dirs = [
        "bash", "download", "resource_merge", "task", "task_submit", "task_evaluate",
        "test_ext", "search", "triggers_ext", "media", "lsp", "memory", "human",
        "web_ext", "hot_swap",
    ]
    pattern_count = 0
    for name in complex_dirs:
        sp = os.path.join(TOOLS_DIR, name, "server.py")
        with open(sp) as f:
            content = f.read()
        if "_SRC_ROOT" in content and "_PROJECT_ROOT" in content and "sys.path.insert" in content:
            pattern_count += 1
    record("sys.path注入模式一致", pattern_count == len(complex_dirs),
           f"{pattern_count}/{len(complex_dirs)} 个 server.py 包含完整注入模式")

    # 5.3 导入测试
    base_path = os.path.join(src_root, "tools", "builtin", "base.py")
    record("base.py源文件存在", os.path.isfile(base_path), f"path={base_path}")

    try:
        sys.path.insert(0, src_root)
        from tools.builtin.base import BuiltinTool  # noqa: F401
        record("BuiltinTool导入", True, "导入成功")
    except ImportError as e:
        record("BuiltinTool导入", False,
               f"导入失败（运行时依赖断裂，非迁移缺陷）: {e}")


# ── 补充场景A: 错误输入处理 ──────────────────────────────
async def test_supplementary_a() -> None:
    print("\n" + "=" * 60)
    print("补充场景A - 错误输入处理")
    print("=" * 60)

    from converter_tools import unit_converter
    from calc_tools import scientific_calculator
    from system_tools import yaml_validate

    r = await unit_converter(value=1, from_unit="kg", to_unit="unknown", category="weight")
    record("不支持的单位", "error" in r, f"output={r}")

    r = await scientific_calculator(operation="calculate", expression="")
    record("空表达式", r == {"error": "表达式不能为空"}, f"output={r}")

    r = await yaml_validate(content=": invalid: yaml: :")
    record("无效YAML语法", r.get("valid") is False, f"valid={r.get('valid')}")

    r = await yaml_validate()
    record("空内容参数", r.get("valid") is False and "必须提供" in r.get("error", ""), f"output={r}")


# ── 补充场景B: 边界/扩展功能 ────────────────────────────
async def test_supplementary_b() -> None:
    print("\n" + "=" * 60)
    print("补充场景B - 边界/扩展功能")
    print("=" * 60)

    from converter_tools import unit_converter
    from calc_tools import scientific_calculator
    import math

    r = await unit_converter(value=0, from_unit="C", to_unit="F", category="temperature")
    record("温度 C->F", r["result"] == 32.0, f"0C={r['result']}F")

    r = await unit_converter(value=1, from_unit="x", to_unit="y", category="unknown")
    record("不支持类别", "不支持" in r.get("error", ""), f"output={r}")

    r = await scientific_calculator(operation="evaluate", func="sqrt", value=16)
    record("sqrt(16)", r["result"] == 4, f"result={r['result']}")

    r = await scientific_calculator(operation="evaluate", func="pi")
    record("常量pi", abs(r["result"] - math.pi) < 0.001, f"result={r['result']}")

    r = await unit_converter(value=1, from_unit="mi", to_unit="km", category="length")
    record("长度 mi->km", abs(r["result"] - 1.609344) < 0.001, f"1mi={r['result']}km")


# ── 主函数 ──────────────────────────────────────────────
def main() -> None:
    print("42个工具插件迁移功能验证")
    print(f"项目根目录: {PROJECT_ROOT}")
    print(f"工具目录: {TOOLS_DIR}")

    test_scenario1()
    asyncio.run(test_scenario2())
    test_scenario3()
    test_scenario4()
    test_scenario5()
    asyncio.run(test_supplementary_a())
    asyncio.run(test_supplementary_b())

    print("\n" + "=" * 60)
    print("验证汇总")
    print("=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)
    print(f"通过: {passed}/{total}, 失败: {failed}/{total}")

    if failed:
        print("\n失败项:")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}: {detail}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

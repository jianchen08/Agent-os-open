#!/usr/bin/env python3
"""工具直调试器（插件开发技能 skills/plugin-dev 自带脚本）——无 LLM 调工具看结果。

开发期最高频动作：改完 execute 想立刻看返回，不该每次都起 LLM 会话。两级：

- 单元级（默认，零实例）：importlib 加载插件实现模块（tool kind 形态：实现模块导出
  以工具名命名的纯函数），以 --args JSON 为入参直调，结果按 output_schema 校验
  （jsonschema 可用则硬校验，缺库降级为仅结构提示——降级显式声明不静默）。
- 集成级（--port 给了）：连运行中 scratch 实例查 /api/v1/tools 确认工具面可见。
  诚实边界：实例端"直接 invoke 工具"的 admin 端点待内核补件（ADR 待查证③），
  到位前集成级只做可见性确认，不做直调。

用法：
  python skills/plugin-dev/scripts/debug_tool.py <plugin_dir> \\
    [--args '{"expression": "1+1"}'] [--args-file f.json] [--port 9188]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any


class DebugError(Exception):
    """调试失败（目录/参数/加载非法）。"""


def load_impl_module(plugin_dir: Path, tool_name: str):
    """加载实现模块，返回模块对象。查找顺序：

    1. `<tool_name>.py` / `<插件目录名>.py` 模块（tool kind：实现函数与工具同名）；
    2. `server.py` 模块（功能性插件 http.handle 约定：模块级 handle 函数，
       http_endpoints.handler_capability 指向，plugin.json capabilities.tools 为空）。
    """
    for stem in (tool_name, plugin_dir.name):
        impl_path = plugin_dir / f"{stem}.py"
        if impl_path.is_file():
            spec = importlib.util.spec_from_file_location(f"debug_impl_{stem}", impl_path)
            if spec is None or spec.loader is None:
                raise DebugError(f"无法构造模块 spec: {impl_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"debug_impl_{stem}"] = module
            spec.loader.exec_module(module)
            return module
    server_path = plugin_dir / "server.py"
    if server_path.is_file():
        spec = importlib.util.spec_from_file_location("debug_impl_server", server_path)
        if spec is None or spec.loader is None:
            raise DebugError(f"无法构造模块 spec: {server_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["debug_impl_server"] = module
        spec.loader.exec_module(module)
        return module
    raise DebugError(
        f"实现模块未找到（试过 {tool_name}.py / {plugin_dir.name}.py / server.py）"
    )


def call_tool(plugin_dir: Path, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """单元级直调：实现函数查找顺序见 load_impl_module。

    - tool kind：模块级 <tool_name> 函数；
    - http.handle 型（theme/page）：server.py 模块级 handle 函数。
    """
    module = load_impl_module(plugin_dir, tool_name)
    func = getattr(module, tool_name, None)
    if not callable(func) and tool_name == "http.handle":
        func = getattr(module, "handle", None)
    if not callable(func):
        raise DebugError(
            f"模块无 {tool_name} 可调用函数（tool kind 契约：实现函数与工具同名；"
            f"http.handle 型：server.py 模块级 handle 函数）"
        )
    try:
        result = func(**args)
    except TypeError as exc:
        raise DebugError(f"入参不匹配实现签名: {exc}") from exc
    return {"tool": tool_name, "args": args, "result": result}


def validate_output(plugin_dir: Path, tool_name: str, result: Any) -> dict[str, Any]:
    """按 plugin.json 的 output_schema 校验结果（jsonschema 可用则硬校验）。"""
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    schema: dict[str, Any] | None = None
    for tool in manifest.get("capabilities", {}).get("tools", []):
        if tool.get("name") == tool_name:
            schema = tool.get("output_schema")
            break
    if schema is None:
        return {"schema_checked": False, "detail": "plugin.json 无该工具 output_schema 声明"}
    try:
        import jsonschema
    except ImportError:
        return {
            "schema_checked": False,
            "detail": "jsonschema 库不可用，跳过硬校验（结构见 result 原样输出）",
        }
    try:
        jsonschema.validate(instance=result, schema=schema)
        return {"schema_checked": True, "detail": "output_schema 校验通过"}
    except jsonschema.ValidationError as exc:
        return {"schema_checked": True, "detail": f"output_schema 校验失败: {exc.message}"}


def check_instance_visible(port: int, tool_name: str) -> dict[str, Any]:
    """集成级：运行中实例 /api/v1/tools 可见性确认（直调端点待内核补件）。"""
    url = f"http://127.0.0.1:{port}/api/v1/tools"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"instance": False, "detail": f"实例不可达（{url}）: {exc}"}
    items = payload.get("items") if isinstance(payload, dict) else []
    blob = json.dumps(items, ensure_ascii=False)
    return {
        "instance": tool_name in blob,
        "detail": (
            f"工具 {tool_name} 在实例面内可见"
            if tool_name in blob else
            f"工具 {tool_name} 不在实例面内——先跑 plugin_doctor.py 查装载链"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="工具直调试器（无 LLM）")
    parser.add_argument("plugin_dir", help="插件目录")
    parser.add_argument("--args", default="{}", help="入参 JSON 字符串（默认 {}）")
    parser.add_argument("--args-file", default=None, help="入参 JSON 文件（优先于 --args）")
    parser.add_argument("--port", type=int, default=None, help="运行中实例端口（集成级可见性确认）")
    args = parser.parse_args(argv)
    plugin_dir = Path(args.plugin_dir)
    try:
        raw = (
            Path(args.args_file).read_text(encoding="utf-8")
            if args.args_file else args.args
        )
        call_args = json.loads(raw)
        if not isinstance(call_args, dict):
            raise DebugError("--args 必须是 JSON 对象")
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        tools = manifest.get("capabilities", {}).get("tools", [])
        # LLM 工具取声明名；空声明面（theme/page 功能性插件）调试 http.handle 通道
        tool_name = str(tools[0]["name"]) if tools else "http.handle"
        report = call_tool(plugin_dir, tool_name, call_args)
        report["validation"] = validate_output(plugin_dir, tool_name, report["result"])
        if args.port:
            report["instance"] = check_instance_visible(args.port, tool_name)
    except (DebugError, OSError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

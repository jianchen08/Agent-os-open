#!/usr/bin/env python3
"""插件 E2E 探针：在独立子进程中加载单个插件，输出 JSON 状态报告。

为什么用子进程：
- 生产环境中每个插件由内核以独立 sidecar 进程启动（cwd=插件目录），
  插件内大量使用裸名导入（`from models import ...`、`from plugin import ...`、
  `from adapter import ...`），仅在自己的目录 + PYTHONPATH 下成立。
- 若在同一个 pytest 进程里批量加载全部插件，各插件目录会互相污染 sys.path，
  裸名模块被劫持（已实测：memory/scene/tasks/workspace/pipeline 插件全部撞车）。
- 子进程 + cwd=插件目录 = 与生产 sidecar 相同的加载语义，结果可信。

用法：
    python plugin_probe.py <plugin_dir> [--invoke '<json: {tool: {kwargs}}>']
输出（stdout，单行 JSON）：
    {dir, load_ok, load_error, plugin_name, tools, resources,
     lifecycle_on_load, lifecycle_on_load_error,
     lifecycle_on_unload, invocations: {tool: {ok, error}}}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _load_plugin(plugin_dir: Path) -> tuple[object | None, str | None]:
    """加载插件 server 模块并返回 (plugin 对象, 错误信息)。"""
    server_path = plugin_dir / "server.py"
    if not server_path.exists():
        return None, "no server.py"

    sys.path.insert(0, str(plugin_dir))  # 与 sidecar 启动一致：插件目录在首位
    # 注意：不插入插件目录的父目录——pipeline 等插件的 server.py 会自行
    # 把 plugins/shared/ 加入 sys.path（与生产行为一致），避免父目录裸名污染。

    import importlib.util

    mod_name = f"probe_{plugin_dir.name}"
    spec = importlib.util.spec_from_file_location(mod_name, server_path)
    if spec is None or spec.loader is None:
        return None, f"cannot create spec for {server_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 探针需要捕获任何加载失败
        return None, f"{type(exc).__name__}: {exc}"

    plugin = getattr(module, "plugin", None)
    if plugin is None:
        create = getattr(module, "create_plugin", None)
        if callable(create):
            try:
                plugin = create()
            except Exception as exc:  # noqa: BLE001
                return None, f"create_plugin failed: {type(exc).__name__}: {exc}"
    if plugin is None:
        return None, "no plugin object (wrapper-only server.py)"
    return plugin, None


def _fire(plugin: object, event: str, loop: asyncio.AbstractEventLoop | None = None) -> tuple[bool, str | None]:
    """触发生命周期钩子（模拟内核 notification）。

    全部事件共用同一个事件循环（与生产 sidecar 一致），避免插件把资源
    绑定到已关闭的旧循环导致 "Event loop is closed"。
    """
    handlers = getattr(plugin, "_lifecycle_handlers", {}) or {}
    handler = handlers.get(event)
    if handler is None:
        return True, None
    try:
        result = handler({})
        if asyncio.iscoroutine(result):
            if loop is None or loop.is_closed():
                loop = asyncio.new_event_loop()
            loop.run_until_complete(result)
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _invoke(
    plugin: object,
    tool_name: str,
    kwargs: dict,
    loop: asyncio.AbstractEventLoop | None = None,
) -> tuple[bool, str | None]:
    """调用工具 handler，返回 (ok, error)。"""
    tools = getattr(plugin, "_tools", {}) or {}
    td = tools.get(tool_name)
    if td is None:
        return False, f"tool not registered: {tool_name}"
    try:
        result = td.handler(**kwargs)
        if asyncio.iscoroutine(result):
            if loop is None or loop.is_closed():
                loop = asyncio.new_event_loop()
            result = loop.run_until_complete(result)
        if result is None:
            return False, "handler returned None"
        return True, None
    except Exception as exc:  # noqa: BLE001 - 捕获执行异常并上报
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin_dir", type=Path)
    parser.add_argument("--invoke", default="{}", help="JSON: {tool: {kwargs}}")
    args = parser.parse_args()

    report: dict = {"dir": str(args.plugin_dir)}
    plugin, load_error = _load_plugin(args.plugin_dir)
    report["load_ok"] = plugin is not None
    report["load_error"] = load_error
    if plugin is None:
        print(json.dumps(report, ensure_ascii=False))
        return 0

    report["plugin_name"] = getattr(plugin, "name", None)
    tools = sorted(getattr(plugin, "_tools", {}).keys())
    resources = sorted(getattr(plugin, "_resources", {}).keys())
    report["tools"] = tools
    report["resources"] = resources

    # 单一事件循环贯穿全部生命周期与工具调用（生产 sidecar 同一 loop）
    loop = asyncio.new_event_loop()

    ok, err = _fire(plugin, "on_load", loop)
    report["lifecycle_on_load"] = ok
    report["lifecycle_on_load_error"] = err

    invocations: dict[str, dict] = {}
    try:
        invoke_map = json.loads(args.invoke)
    except json.JSONDecodeError as exc:
        invoke_map = {}
        invocations["__json_error__"] = {"ok": False, "error": str(exc)}
    for tool_name, kwargs in invoke_map.items():
        ok, err = _invoke(plugin, tool_name, kwargs or {}, loop)
        invocations[tool_name] = {"ok": ok, "error": err}
    report["invocations"] = invocations

    ok, err = _fire(plugin, "on_unload", loop)
    report["lifecycle_on_unload"] = ok
    report["lifecycle_on_unload_error"] = err
    loop.close()

    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""插件注册面校验：改完插件后本地补跑——拦截 schema 漂移导致的 G2 拒注册。

背景（2026-08-22 教训）：memory 工具插件改 plugin.json（声明加 document_id）
但 tool.py 的 get_tool_definition()（sidecar 实际 MCP 上报面）没同步，G2 注册闸
boot 时净化 memory 工具，LLM 报"工具未注册"。单测/插件侧直连都绕过内核注册面，
抓不到这类漂移——唯一可靠拦截 = 内核注册校验（G2）本身。

用法：
  1) 本地直接比对（无需内核在线，纯静态）：
     python scripts/check_plugin_registration.py plugins/shared/tools/memory
     → 解析 plugin.json capabilities.tools + 动态加载实现模块取实际上报 schema，
       逐字比对（与内核 G2 compare_tools 同判据：name 集合差 + input_schema 逐字）
  2) 内核在线时全量巡检（G2 真校验面）：
     python scripts/check_plugin_registration.py --validate-all
     → 调 PUT /api/v1/plugins/{id}/enabled true 逐个触发 G2 复核，漂移插件列报告

退出码：0=全部一致，1=有漂移。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_impl_schema(plugin_dir: Path, entry: str) -> dict:
    """加载插件实现，取实际工具 schema（sidecar MCP tools/list 面）。

    支持 entry="python server.py" 的插件目录：优先尝试 server.py（SDK 的
    plugin._tools[name].schema），回退 tool.py 的 get_tool_definition()。
    纯工具类插件（memory/download 等）走 get_tool_definition() 路径。
    """
    sdk_src = PROJECT_ROOT / "plugins" / "sdk" / "src"
    if sdk_src.exists():
        sys.path.insert(0, str(sdk_src))
    sys.path.insert(0, str(plugin_dir))
    # 尝试 server.py：SDK 注册面
    server_py = plugin_dir / "server.py"
    if server_py.exists():
        spec = importlib.util.spec_from_file_location(
            f"check_reg_{plugin_dir.name}_server", server_py
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                return {}  # SDK 不可用等：回退 tool.py 路径
            try:
                tools = getattr(mod.plugin, "_tools", None)
            except AttributeError:
                return {}  # 非 SDK 插件（无 plugin 对象）
            if tools:
                return {name: td.schema for name, td in tools.items()}
    # 回退 tool.py：get_tool_definition()
    tool_py = plugin_dir / "tool.py"
    if tool_py.exists():
        spec = importlib.util.spec_from_file_location(
            f"check_{plugin_dir.stem}_tool", tool_py
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            for attr in ("get_tool_definition", "get_tools"):
                fn = getattr(mod, attr, None)
                if callable(fn):
                    if attr == "get_tool_definition":
                        td = fn()
                        return {td.name: td.input_schema}
                    return {t.name: t.input_schema for t in fn()}
    return {}


def check_plugin(plugin_dir: Path) -> list[str]:
    """比对单插件：manifest 声明 vs 实现实际上报。返回漂移清单（空=一致）。"""
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    declared: dict[str, dict] = {}
    for t in manifest.get("capabilities", {}).get("tools", []):
        declared[t["name"]] = t.get("input_schema") or {}
    for s in manifest.get("capabilities", {}).get("services", []):
        declared.setdefault(s["name"], s.get("input_schema") or {})
    if not declared:
        return []  # 无工具声明——无比对面

    entry = manifest.get("entry", "")
    actual = _load_impl_schema(plugin_dir, entry)
    if not actual:
        return [f"{plugin_dir.name}: 无法加载实现面（entry={entry}），跳过比对"]

    diffs: list[str] = []
    declared_names, actual_names = set(declared), set(actual)
    for name in sorted(declared_names - actual_names):
        diffs.append(f"{plugin_dir.name}: 声明有实现无 missing={name}")
    for name in sorted(actual_names - declared_names):
        diffs.append(f"{plugin_dir.name}: 实现有声明无 undeclared={name}")
    for name in sorted(declared_names & actual_names):
        if declared[name] != actual[name]:
            diffs.append(f"{plugin_dir.name}: schema 不一致 {name}")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plugins", nargs="*", help="插件目录（缺省=全量）")
    ap.add_argument("--validate-all", action="store_true", help="经内核 G2 复核（需内核在线）")
    args = ap.parse_args()

    if args.plugins:
        dirs = [PROJECT_ROOT / p for p in args.plugins]
    else:
        dirs = [
            p
            for d in ("tools", "system", "pipeline")
            for p in sorted((PROJECT_ROOT / "plugins" / "shared" / d).glob("*"))
            if (p / "plugin.json").exists()
        ]

    if args.validate_all:
        import os, urllib.request  # noqa: PLC0415
        base = os.environ.get("AGENTOS_KERNEL_URL", "http://127.0.0.1:9100")
        token = os.environ.get("AGENTOS_TOKEN", "")
        if not token:
            req = urllib.request.Request(
                f"{base}/api/v1/auth/login",
                data=json.dumps({"username": "admin", "password": os.environ.get("AGENTOS_ADMIN_PASS", "admin12345")}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                token = json.load(resp).get("access_token", "")
        for d in dirs:
            pid = json.loads((d / "plugin.json").read_text(encoding="utf-8"))["id"]
            req = urllib.request.Request(
                f"{base}/api/v1/plugins/{pid}/enabled",
                data=json.dumps({"enabled": True}).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="PUT",
            )
            try:
                with urllib.request.urlopen(req) as resp:
                    body = json.load(resp)
                tools = body.get("registered", {}).get("tools", 0)
                print(f"{pid}: registered tools={tools}")
            except Exception as e:
                print(f"{pid}: 校验失败 {e}")
        return 0

    all_diffs: list[str] = []
    for d in dirs:
        all_diffs.extend(check_plugin(d))
    if all_diffs:
        print("\n".join(all_diffs))
        return 1
    print(f"全部插件声明一致（{len(dirs)} 个）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

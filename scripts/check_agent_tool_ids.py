#!/usr/bin/env python3
"""agent tool_ids 静态闸：agent yaml 的每条 tool_ids 必须可解析到工具面。

判定面（三项并集；测试面同契约见 tests/plugins/test_tool_ids_contract.py，
本闸是 CI/本地可独立调用的静态入口，不依赖 pytest）：

1. ``plugins/shared/**/plugin.json`` 的 ``capabilities.tools[].name``——声明即注册；
2. 声明了 ≥1 工具的插件 id——tool_ids 条目等于插件 id = 整包注入配法
   （内核 tool-surface.schemas 契约，capability_router.rs）；
3. 外部/用户空间提供方工具 id（单源 = ``plugins/shared/tool_schema/plugin.py``
   的 ``EXTERNAL_PROVIDED_TOOL_IDS``）：用户空间插件根与 mcp:external 通道运行时
   注册，仓内 manifest 不可见（ADR 2026-09-14 单一存在）。

不在三项并集内的 id = 配置面漂移：该 agent 每轮 LLM 调用它必败（注册表无物），
tool_schema 只会缩面 + WARN（2026-10-17 日志 72 行/日同款实证：用户空间提供的
godot_run/universal_search 在临时用户根下被判漂移）。

用法：
    python scripts/check_agent_tool_ids.py                # 默认扫 config/agents
    python scripts/check_agent_tool_ids.py --root <目录>   # 追加扫描根（可重复；
                                                          # 目录不存在即 fail-closed）
    python scripts/check_agent_tool_ids.py --json

退出码：0 = 零违规；1 = 有违规；2 = 上下文解析失败（fail-closed，不静默放行）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOTS = (ROOT / "config" / "agents",)
SHARED_DIR = ROOT / "plugins" / "shared"

# 剪枝宿主/构建目录：dsh_adapter runtime 的 node_modules 连接点迷宫（rglob
# 会卡死）与各插件 .venv 均不下钻（tests/plugins/test_plugin_smoke_matrix.py 同款）。
_SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    "target",
    "runtime",
    "dsh_plugins",
    ".venv",
    ".venv-hindsight",
    ".ai_workspaces",
}


class ContextError(RuntimeError):
    """上下文解析失败（注册面/外部清单不可解析）——fail-closed，不静默放行。"""


def _pruned_files(base: Path) -> list[Path]:
    """剪枝遍历目录下全部文件（跳过宿主/构建巨目录）。"""
    found: list[Path] = []
    for cur, subdirs, files in os.walk(base):
        subdirs[:] = [d for d in subdirs if d not in _SKIP_DIRS]
        found.extend(Path(cur) / f for f in files)
    return sorted(found)


def load_repo_registry() -> tuple[dict[str, list[str]], set[str]]:
    """仓内注册面：工具名 → 声明来源清单；声明了 ≥1 工具的插件 id 集。"""
    registry: dict[str, list[str]] = {}
    plugin_ids: set[str] = set()
    for path in _pruned_files(SHARED_DIR):
        if path.name != "plugin.json":
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        tools = (manifest.get("capabilities") or {}).get("tools") or []
        rel = path.relative_to(ROOT).as_posix()
        for tool in tools:
            name = tool.get("name")
            if name:
                registry.setdefault(name, []).append(rel)
        plugin_id = manifest.get("id")
        if tools and plugin_id:
            plugin_ids.add(plugin_id)
    if not registry:
        raise ContextError(
            f"{SHARED_DIR.relative_to(ROOT).as_posix()} 下未发现任何工具声明（manifest 面漂移或遍历剪枝过宽）"
        )
    return registry, plugin_ids


def load_external_tool_ids() -> frozenset[str]:
    """外部提供方 id 单源：tool_schema 插件的 EXTERNAL_PROVIDED_TOOL_IDS 常量。"""
    plugin_path = SHARED_DIR / "tool_schema" / "plugin.py"
    if not plugin_path.is_file():
        raise ContextError(f"外部工具清单单源缺失：{plugin_path.relative_to(ROOT).as_posix()}")
    for entry in (str(plugin_path.parent), str(SHARED_DIR)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    spec = importlib.util.spec_from_file_location("tool_schema_external_ids", plugin_path)
    if spec is None or spec.loader is None:
        raise ContextError(f"无法加载外部工具清单单源：{plugin_path.relative_to(ROOT).as_posix()}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ids = frozenset(module.EXTERNAL_PROVIDED_TOOL_IDS)
    if not ids:
        raise ContextError("EXTERNAL_PROVIDED_TOOL_IDS 为空——外部提供方清单被清空，拒绝静默放行")
    return ids


def collect_violations(
    roots: tuple[Path, ...],
    registry: dict[str, list[str]],
    plugin_ids: set[str],
    external: frozenset[str],
) -> tuple[list[dict[str, Any]], int]:
    """逐 agent yaml 校验 tool_ids；返回 (违规清单, 受检 yaml 数)。"""
    for root in roots:
        if not root.is_dir():
            rel = root.relative_to(ROOT).as_posix() if root.is_relative_to(ROOT) else str(root)
            raise ContextError(f"扫描根不存在：{rel}（fail-closed，拒绝空跑）")
    violations: list[dict[str, Any]] = []
    scanned = 0
    for root in roots:
        for path in _pruned_files(root):
            if path.suffix not in (".yaml", ".yml"):
                continue
            scanned += 1
            rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            tool_ids = data.get("tool_ids")
            if tool_ids is None:
                continue
            if not isinstance(tool_ids, list):
                violations.append(
                    {
                        "file": rel,
                        "tool_ids": [],
                        "unregistered": [],
                        "error": f"tool_ids 应为列表，实为 {type(tool_ids).__name__}",
                    }
                )
                continue
            missing = sorted(set(tool_ids) - set(registry) - plugin_ids - external)
            if missing:
                violations.append({"file": rel, "tool_ids": list(tool_ids), "unregistered": missing})
    return violations, scanned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent tool_ids 静态闸")
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help="追加扫描根（仓内相对路径或绝对路径；可重复；默认 config/agents）",
    )
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    roots = tuple(DEFAULT_ROOTS) + tuple(Path(r) if Path(r).is_absolute() else ROOT / r for r in args.root)
    try:
        registry, plugin_ids = load_repo_registry()
        external = load_external_tool_ids()
        violations, scanned = collect_violations(roots, registry, plugin_ids, external)
        if scanned == 0:
            raise ContextError("扫描面为空（无 agent yaml）——发现逻辑失效，拒绝假绿")
    except ContextError as exc:
        print(f"[agent-tool-ids] 上下文解析失败：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "scanned_yaml": scanned,
                    "registry_tools": len(registry),
                    "plugin_ids_with_tools": len(plugin_ids),
                    "external_tools": sorted(external),
                    "violations": violations,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif violations:
        for v in violations:
            if "error" in v:
                print(f"[agent-tool-ids] {v['file']}: {v['error']}")
            else:
                print(
                    f"[agent-tool-ids] {v['file']}: tool_ids 引用未注册 id "
                    f"{v['unregistered']}——修对应插件 manifest 或修订 tool_ids；"
                    "外部/用户空间提供的 id 须登记 tool_schema.EXTERNAL_PROVIDED_TOOL_IDS"
                )
    else:
        print(
            f"[agent-tool-ids] OK：{scanned} 个 agent yaml 全绿"
            f"（注册面 {len(registry)} 工具 + {len(plugin_ids)} 整包插件 id + "
            f"{len(external)} 外部提供方 id）"
        )
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""管道步骤 声明↔注册 parity 静态门禁（STEP_NOT_FOUND 静默类防再犯）。

背景（2026-10-10 CI 防再犯清单 C 项）：声明/注册错位在运行时是
fail-closed 潜伏雷——调用时才以 StepNotFoundError 爆炸，无人触发的
步骤可静默带病存活数日。本门禁把错位前移到加载期。

语义锚（kernel/crates/engine/src/compiler.rs build_step_service_index +
plugins/sdk plugin.py step 装饰器契约）：
- manifest ``capabilities.steps[]`` 条目 name == 插件 id（自名条目）：
  编译期映射 method=None → ④插件直引（工具语义），**不要求**注册；
- 异名条目（name != 插件 id）：编译期映射 method=Some(name)，调用面 =
  SDK ``_steps`` 表 → **必须** ``@plugin.step(name)``（或等价调用形态
  ``plugin.step("name")``）注册，未注册 = 调用即 STEP_NOT_FOUND；
- 反向：``@plugin.step(异名)`` 注册必须出现在 manifest 声明——③级步骤
  服务索引仅从 manifests 聚合，未声明 = 管道永远引用不到（死注册）。

跳过目录：node_modules / .venv / __pycache__ / runtime（播种副本）。
退出码：0 = 全绿；1 = 存在错位（逐条打印插件 id + 步骤名 + 方向）。

用法：python scripts/check_step_declaration_parity.py [plugins_root]
（缺省 = 仓根 plugins/）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# 播种副本/依赖树不属声明面源码
SKIP_DIRS = {"node_modules", ".venv", "__pycache__", "runtime", "test_workspace"}

# @plugin.step("x") / plugin.step('x') ——装饰器与方法调用两种形态等价注册
_STEP_REGISTER_RE = re.compile(r"\.step\(\s*([\"'])([^\"']+)\1")


def declared_steps(manifest: dict) -> list[str]:
    """manifest 声明的步骤名（capabilities.steps[].name，保持原序）。"""
    steps = (manifest.get("capabilities") or {}).get("steps") or []
    return [s["name"] for s in steps if isinstance(s, dict) and s.get("name")]


def registered_steps(py_sources: dict[str, str]) -> set[str]:
    """Python 源码里注册的步骤名集合（``.step("name")`` 形态）。"""
    found: set[str] = set()
    for text in py_sources.values():
        found.update(m.group(2) for m in _STEP_REGISTER_RE.finditer(text))
    return found


def check_plugin(manifest_id: str, declared: list[str], registered: set[str]) -> list[str]:
    """单插件 parity 检查：返回违规描述（空 = 绿）。

    自名条目（name == 插件 id）豁免注册要求；其余双向求差。
    """
    problems: list[str] = []
    declared_foreign = [n for n in declared if n != manifest_id]
    registered_foreign = {n for n in registered if n != manifest_id}
    for name in declared_foreign:
        if name not in registered_foreign:
            problems.append(
                f"[{manifest_id}] 声明未注册: '{name}'（异名条目调用即 STEP_NOT_FOUND，"
                f"需 @plugin.step('{name}') 或改自名条目）"
            )
    for name in sorted(registered_foreign - set(declared_foreign)):
        problems.append(
            f"[{manifest_id}] 注册未声明: '{name}'（③级索引仅聚合 manifests，"
            f"管道引用不到=死注册，需写入 capabilities.steps）"
        )
    return problems


def _pruned_walk(root: Path, pattern: str) -> list[Path]:
    """剪枝遍历：SKIP_DIRS 目录整个不进入（node_modules 巨树不可遍历）。"""
    out: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir()):
            if entry.is_dir():
                if entry.name not in SKIP_DIRS:
                    stack.append(entry)
            elif entry.match(pattern):
                out.append(entry)
    return out


def scan_plugins(plugins_root: Path) -> list[str]:
    """扫全部 plugin.json，返回全部违规（空 = 绿）。"""
    problems: list[str] = []
    for manifest_path in _pruned_walk(plugins_root, "plugin.json"):
        # walker 竞态守卫（与 2b51ea4b6/f594fac3d 同族）：扫描窗口内插件目录
        # 可被 watcher/同步重写（plugin.json 消失/重挂），FileNotFound/权限
        # 抖动跳过该插件本轮扫描（下一轮恢复执法），不炸整门禁。
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, json.JSONDecodeError) as e:
            print(f"step-parity: 跳过瞬时不可读 {manifest_path}: {e}", file=sys.stderr)
            continue
        plugin_id = manifest.get("id")
        if not plugin_id:
            continue
        declared = declared_steps(manifest)
        py_sources = {}
        for p in _pruned_walk(manifest_path.parent, "*.py"):
            try:
                py_sources[str(p.relative_to(manifest_path.parent))] = p.read_text(encoding="utf-8", errors="replace")
            except (FileNotFoundError, PermissionError):
                continue
        problems.extend(check_plugin(plugin_id, declared, registered_steps(py_sources)))
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1] / "plugins"
    if not root.is_dir():
        print(f"plugins 根不存在: {root}", file=sys.stderr)
        return 1
    problems = scan_plugins(root)
    if problems:
        print(f"步骤声明↔注册 parity 违规 {len(problems)} 条:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"步骤声明↔注册 parity 全绿（plugins_root={root}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

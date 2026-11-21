#!/usr/bin/env python3
"""插件 `state.reads` 声明完备性机械闸（切片投喂契约）。

内核 invoker 按 manifest `state.reads` 切片投喂（`kernel/crates/invoker/src/shared.rs`
`state_feed_payload` / `assemble_state_slice`：声明非空 → 控制键白名单 ∪ 声明键
切片）。**未声明键插件永远读不到，静默缺省且无任何日志**——读点漏声明是静默
行为降级，只能靠静态扫描拦。

两起实证事故（均因漏声明，均已修）：
- `duplicate_check` 漏 `task.id` → 熔断失败归因三件套静默不写（2026-11-11）；
- `task_reminder` 漏 `reminder_seen_result_fp` → 失败轮豁免恒假，5 条任务被误判
  `reminder_exhausted`（2026-11-12）。

扫描面与判定：
- 目标 = `plugins/shared/*/plugin.py`（管道入口，唯一接收投喂 state 的模块；
  `server.py` 不读管道 state，姐妹模块由各插件测试车道治理）。
- 读点三形态（AST 级，注释/文档串天然排除）：
  1. `state.get("<字面量>")`（含 `ctx.state.get` / `self.state.get`）；
  2. `state["<字面量>"]`（仅 Load 语境；写入/自增目标不算读）；
  3. `state.get(<模块级常量>)`——同文件模块级字符串常量、`StateKeys.<ATTR>`
     （`_libs/pipeline_base/types.py`）、平铺共享模块常量（如
     `zone_policy.STATE_KEY`）。
  动态键（配置驱动的变量/call 参数）无法静态判定，不参与本闸（各插件手审）。
- 控制键白名单直接解析 Rust 真源 `STATE_SLICE_CONTROL_KEYS`，不镜像不漂移。
- `messages_tail:N` 投喂键仍是 `messages`（声明归一后比对）。

基线 `.github/state-reads-baseline.txt`（每行 `<plugin目录名>:<key>`，`#` 注释）：
双向棘轮——基线外新增违规 = 红；基线条目现已不违规（已修/已删读点）= 红，
必须同刀摘除，防基线腐化。

用法：
    python scripts/check_state_reads_declaration.py           # 校验（CI/本地）
    python scripts/check_state_reads_declaration.py --init    # 以实测重建基线
"""

from __future__ import annotations

import argparse
import ast
import functools
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "plugins" / "shared"
SDK_SRC = ROOT / "plugins" / "sdk" / "src" / "agentos_plugin_sdk"
LIB_SRC = SHARED / "_libs"
INVOKER_SHARED_RS = ROOT / "kernel" / "crates" / "invoker" / "src" / "shared.rs"
BASELINE_PATH = ROOT / ".github" / "state-reads-baseline.txt"

_CONTROL_KEYS_BLOCK = re.compile(
    r"pub const STATE_SLICE_CONTROL_KEYS:\s*&\[&str\]\s*=\s*&\[(?P<body>[^\]]*)\]",
    re.DOTALL,
)
_STRING_LITERAL = re.compile(r'"([^"]*)"')


def load_control_keys(path: Path | None = None) -> set[str]:
    """解析内核控制键白名单（切片投喂恒带键）——单一真源，禁镜像。

    Rust 侧新增控制键本闸自动跟随；解析失败 fail-closed 抛错（不静默退化为
    空集，否则全部控制键读点会误判为违规）。
    """
    src = (path or INVOKER_SHARED_RS).read_text(encoding="utf-8")
    m = _CONTROL_KEYS_BLOCK.search(src)
    if m is None:
        raise RuntimeError(
            f"STATE_SLICE_CONTROL_KEYS 未在 {INVOKER_SHARED_RS} 解析到——内核切片契约形态变更，本闸需同步"
        )
    keys = set(_STRING_LITERAL.findall(m.group("body")))
    if not keys:
        raise RuntimeError("STATE_SLICE_CONTROL_KEYS 解析为空——拒绝以空白名单判定")
    return keys


def _is_state_like(node: ast.expr) -> bool:
    """接收者是否管道 state：`state` 或 `<任意>.state`（`ctx.state` 等）。"""
    if isinstance(node, ast.Name):
        return node.id == "state"
    return isinstance(node, ast.Attribute) and node.attr == "state"


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """同文件模块级字符串常量（`_KEY = "x"` / `_KEY: str = "x"`）。"""
    out: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.target is not None:
            targets, value = [node.target], node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = value.value
    return out


def _module_file(module: str) -> Path | None:
    """按平铺/共享库/SDK 三处布局定位模块文件（均为一层，不做包递归）。"""
    for cand in (SHARED / f"{module}.py", LIB_SRC / f"{module}.py", SDK_SRC / f"{module}.py"):
        if cand.is_file():
            return cand
    return None


@functools.cache
def _class_index() -> dict[str, Path]:
    """类名 → 定义文件索引（`StateKeys` 这类「类名当模块名引用」的形态用）。

    覆盖面 = plugins/shared/*.py + _libs/**/*.py + SDK *.py；同名类多处定义取
    排序首个（常量键类为单点定义，重复定义本身就是缺陷）。
    """
    idx: dict[str, Path] = {}
    candidates: list[Path] = []
    candidates.extend(sorted(SHARED.glob("*.py")))
    candidates.extend(sorted(LIB_SRC.rglob("*.py")))
    if SDK_SRC.is_dir():
        candidates.extend(sorted(SDK_SRC.glob("*.py")))
    for path in candidates:
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                idx.setdefault(node.name, path)
    return idx


def resolve_attr_constant(module: str, attr: str) -> str | None:
    """`<module>.<attr>` 常量解析：模块文件里的模块级或类属性字符串常量。

    `module` 既可指真模块文件名（`zone_policy.STATE_KEY`），也可指常量类名
    （`StateKeys.TASK_ID` 定义在 `_libs/pipeline_base/types.py`）。
    """
    path = _module_file(module) or _class_index().get(module)
    if path is None:
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = _module_string_constants(tree)
    if attr in found:
        return found[attr]
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        cls = _module_string_constants(ast.Module(body=node.body, type_ignores=[]))
        if attr in cls:
            return cls[attr]
    return None


def _resolve_key_expr(node: ast.expr, constants: dict[str, str]) -> str | None:
    """读点键表达式 → 字面量键（不可静态解析返回 None）。"""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return resolve_attr_constant(node.value.id, node.attr)
    return None


def collect_state_reads(source: str, filename: str = "<plugin.py>") -> tuple[set[str], list[str]]:
    """扫描源码全部 state 读点。

    Returns:
        (静态解析出的键集合, 不可静态解析的读点描述列表)
    """
    tree = ast.parse(source, filename=filename)
    constants = _module_string_constants(tree)
    keys: set[str] = set()
    dynamic: list[str] = []
    for node in ast.walk(tree):
        # 形态 1：state.get(<key>[, default])
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get" and _is_state_like(func.value) and node.args:
                key = _resolve_key_expr(node.args[0], constants)
                if key is None:
                    dynamic.append(f"{filename}:{node.lineno} state.get(<动态键>)")
                else:
                    keys.add(key)
                continue
        # 形态 2：state[<key>]（仅读语境；写/自增目标不算读）
        if isinstance(node, ast.Subscript) and _is_state_like(node.value):
            if not isinstance(node.ctx, ast.Load):
                continue
            key = _resolve_key_expr(node.slice, constants)
            if key is None:
                dynamic.append(f"{filename}:{node.lineno} state[<动态键>]")
            else:
                keys.add(key)
    return keys, dynamic


def manifest_visible_keys(plugin_dir: Path) -> set[str]:
    """manifest 声明在切片后对插件可见的键集合（`messages_tail:N` → `messages`）。"""
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    reads = (manifest.get("state") or {}).get("reads") or []
    return {"messages" if str(r).startswith("messages_tail:") else str(r) for r in reads}


def scan_plugin(plugin_dir: Path) -> tuple[set[str], list[str]]:
    """单插件：plugin.py 全部读点（插件无 plugin.py 返回空集）。"""
    plugin_py = plugin_dir / "plugin.py"
    if not plugin_py.is_file():
        return set(), []
    return collect_state_reads(plugin_py.read_text(encoding="utf-8"), filename=f"{plugin_dir.name}/plugin.py")


def find_violations(root: Path = ROOT) -> dict[str, set[str]]:
    """全插件面违规表：`{插件目录名: {未声明的读取键}}`（空值不出现）。"""
    control = load_control_keys(root / INVOKER_SHARED_RS.relative_to(ROOT))
    shared = root / "plugins" / "shared"
    out: dict[str, set[str]] = {}
    for plugin_dir in sorted(p for p in shared.iterdir() if p.is_dir()):
        if not (plugin_dir / "plugin.json").is_file():
            continue
        used, _dynamic = scan_plugin(plugin_dir)
        undeclared = sorted(used - manifest_visible_keys(plugin_dir) - control)
        if undeclared:
            out[plugin_dir.name] = set(undeclared)
    return out


def format_entries(violations: dict[str, set[str]]) -> set[str]:
    """违规表 → 基线条目集（`<plugin>:<key>`）。"""
    return {f"{plugin}:{key}" for plugin, keys in violations.items() for key in keys}


def load_baseline(path: Path | None = None) -> set[str]:
    """基线条目集（`#` 注释与空行忽略）。"""
    p = path or BASELINE_PATH
    if not p.is_file():
        return set()
    out: set[str] = set()
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out


def diff_baseline(found: set[str], baseline: set[str]) -> tuple[list[str], list[str]]:
    """双向棘轮差分：返回 (基线外新增, 已登记但现已不违规)。"""
    return sorted(found - baseline), sorted(baseline - found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="插件 state.reads 声明完备性机械闸")
    parser.add_argument("--init", action="store_true", help="以当前实测重建基线（须单独 commit 留归因）")
    args = parser.parse_args(argv)

    violations = find_violations()
    found = format_entries(violations)

    if args.init:
        lines = [
            "# 插件 state.reads 漏声明存量基线（双向棘轮：基线外新增=红，条目失效=红须同刀摘除）",
            "# 执法器 scripts/check_state_reads_declaration.py；格式 <plugin目录名>:<key>",
            "# 语义：内核按 manifest state.reads 切片投喂，未声明键插件恒读不到（静默缺省无日志）",
            "# 存量为历史欠账：新增漏声明即红，存量随触碰逐插件清偿",
            "# 注：state_marker_parse / todo_track 当前 reads 为空（= 全量投喂，无实害），",
            "#     条目记录的是「一旦补 reads 就会静默失联」的读点，属声明欠账。",
            *sorted(found),
        ]
        BASELINE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"state.reads 声明闸：基线已重建（{len(found)} 条 / {len(violations)} 插件）。")
        return 0

    baseline = load_baseline()
    new, stale = diff_baseline(found, baseline)
    if new or stale:
        if new:
            print("state.reads 声明闸：发现基线外新增漏声明（切片投喂下恒读不到，行为静默降级）：")
            for entry in new:
                print(f"  + {entry}")
        if stale:
            print("state.reads 声明闸：基线条目已失效（已修/读点已删）——同刀摘除，防基线腐化：")
            for entry in stale:
                print(f"  - {entry}")
        return 1
    print(f"state.reads 声明闸通过（存量 {len(baseline)} 条 ∈ 基线、无失效条目；存量分布 {len(violations)} 插件）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

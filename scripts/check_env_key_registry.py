#!/usr/bin/env python
"""内核 AGENTOS_* 环境变量登记门禁（ADR 2026-11-17-kernel-env-key-registry-gate）。

执法契约（双向 fail-closed，索引恒等于真实读取面）：
- 源面：``kernel/crates/**/*.rs`` 经注释剥离后，凡出现在字符串字面量的
  ``AGENTOS_*`` 键都必须登记于 ``config/kernel/env_keys.yaml``
  （production 或 test_only）。字面量含 ``{}`` 的 format! 模板按动态前缀
  处理，登记进 ``dynamic_prefixes``。
- 索引面：production / test_only 每条必须带非空 ``description``；
  索引里存在而源面已读不到的键 = 过期条目，同样红。
- 已知局限（有意不做，防过度工程）：键名经变量拼接绕过字面量形态时
  检查不到（漏报方向，永不误红）；块注释内恰好呈字面量形态的提及可能
  误报（仓内现状无此形态）；仅执法 Rust 内核面，Python 插件侧不在 scope。

用法：
    python scripts/check_env_key_registry.py            # 门禁（exit 0/1）
    python scripts/check_env_key_registry.py --dump     # 打印提取结果（排查用）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL_DIR = ROOT / "kernel" / "crates"
REGISTRY_PATH = ROOT / "config" / "kernel" / "env_keys.yaml"

#: 字符串字面量形态的 AGENTOS_* 键（含 format! 模板的 {} 占位）。后缀
#: `+` 非空：裸 "AGENTOS_" 是保留名空间守卫（is_reserved_env_name 的
#: starts_with 前缀，非键），按形态排除而非特例豁免。
_KEY_RE = re.compile(r'"(AGENTOS_[A-Z0-9_{}]+)"')
#: 动态前缀截止点（第一个占位符）。
_PLACEHOLDER_RE = re.compile(r"\{")


def strip_rust_comments(text: str) -> list[str]:
    """逐行剥掉 // 与 /* */ 注释，保留其余内容与行号对齐。

    字符串字面量内的 ``//``（如 "http://x"）不被剥离；``/*`` 在字符串内
    的场景仓内不存在，不做字符串感知的块注释处理（局限见模块 docstring）。
    """
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        res: list[str] = []
        i = 0
        in_string = False
        escaped = False
        while i < len(line):
            ch = line[i]
            if in_block:
                if line.startswith("*/", i):
                    in_block = False
                    i += 2
                    continue
                i += 1
                continue
            if escaped:
                escaped = False
                res.append(ch)
                i += 1
                continue
            if in_string:
                if ch == "\\":
                    escaped = True
                    res.append(ch)
                    i += 1
                    continue
                if ch == '"':
                    in_string = False
                res.append(ch)
                i += 1
                continue
            if ch == '"':
                in_string = True
                res.append(ch)
                i += 1
                continue
            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            if line.startswith("//", i):
                break  # 行注释：余下全剪
            res.append(ch)
            i += 1
        out.append("".join(res))
    return out


def extract_keys(kernel_dir: Path) -> dict[str, list[tuple[str, int]]]:
    """提取源面全部 AGENTOS_* 字面量键 → {key: [(相对路径, 行号), ...]}。

    含 ``{}`` 的模板键保留花括号原样（调用方按前缀归类）。
    """
    found: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(kernel_dir.rglob("*.rs")):
        # 展示路径：真仓相对仓库根（违规信息带 kernel/crates 前缀）；注入
        # 的临时仓树（单测）不在 ROOT 下，回落相对扫描根。
        base = ROOT if path.is_relative_to(ROOT) else kernel_dir
        rel = path.relative_to(base).as_posix()
        for lineno, line in enumerate(strip_rust_comments(path.read_text(encoding="utf-8", errors="replace")), 1):
            for m in _KEY_RE.finditer(line):
                found.setdefault(m.group(1), []).append((rel, lineno))
    return found


def _parse_registry(text: str) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], list[str]]:
    """解析固定 schema 的 env_keys.yaml（stdlib 迷你解析器，CI 无 PyYAML）。

    schema：
        production:            # 部署面旋钮
          KEY:
            description: 一行描述（必填）
            default: 一行默认值语义（可选）
        test_only:             # 测试注入键（非部署面）
          KEY:
            description: 一行描述（必填）
        dynamic_prefixes:      # format! 模板构造的动态键前缀
          - AGENTOS_XXX_
    """
    production: dict[str, dict[str, str]] = {}
    test_only: dict[str, dict[str, str]] = {}
    prefixes: list[str] = []
    section = ""
    current_key = ""
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            m = re.match(r"(production|test_only|dynamic_prefixes):\s*$", stripped)
            if not m:
                raise SystemExit(f"env_keys.yaml:{lineno}: 未知顶层节（合法：production/test_only/dynamic_prefixes）: {stripped}")
            section = m.group(1)
            current_key = ""
            continue
        if section == "dynamic_prefixes":
            m = re.match(r"-\s*(AGENTOS_[A-Z0-9_]*)\s*$", stripped)
            if not m:
                raise SystemExit(f"env_keys.yaml:{lineno}: dynamic_prefixes 条目须为 `- AGENTOS_XXX_`: {stripped}")
            prefixes.append(m.group(1))
            continue
        if indent == 2:
            m = re.match(r"(AGENTOS_[A-Z0-9_]*):\s*$", stripped)
            if not m:
                raise SystemExit(f"env_keys.yaml:{lineno}: {section} 条目须为 `AGENTOS_XXX:`: {stripped}")
            current_key = m.group(1)
            (production if section == "production" else test_only)[current_key] = {}
            continue
        m = re.match(r"(description|default):\s*(.+)$", stripped)
        if not m or not current_key:
            raise SystemExit(f"env_keys.yaml:{lineno}: 条目字段须为 description/default: {stripped}")
        target = production if section == "production" else test_only
        target[current_key][m.group(1)] = m.group(2).strip()
    return production, test_only, prefixes


def check(kernel_dir: Path = KERNEL_DIR, registry_path: Path = REGISTRY_PATH) -> list[str]:
    """返回违规清单（空 = 通过）。参数注入供单测建临时仓树。"""
    production, test_only, prefixes = _parse_registry(registry_path.read_text(encoding="utf-8"))
    extracted = extract_keys(kernel_dir)
    literal_keys = {k: sites for k, sites in extracted.items() if not _PLACEHOLDER_RE.search(k)}
    dynamic_prefixes = {k.rstrip("{}"): sites for k, sites in extracted.items() if _PLACEHOLDER_RE.search(k)}
    registered = set(production) | set(test_only)
    violations: list[str] = []
    # 源面 → 索引：未登记即红。
    for key, sites in sorted(literal_keys.items()):
        if key not in registered:
            first = sites[0]
            violations.append(f"未登记 env 键 {key}（读取面如 {first[0]}:{first[1]}）→ 请登记 config/kernel/env_keys.yaml")
    for prefix, sites in sorted(dynamic_prefixes.items()):
        if prefix not in prefixes:
            first = sites[0]
            violations.append(
                f"未登记动态前缀 {prefix}*（构造面如 {first[0]}:{first[1]}）→ 请登记 env_keys.yaml dynamic_prefixes"
            )
    # 索引 → 源面：过期条目即红（索引恒等于真实读取面）。比对对象 =
    # 去花括号后的两类源面键（原始 extracted 含 "X_{}" 模板形态，直接
    # 比会把已登记前缀误判过期）。
    stale = (registered | set(prefixes)) - set(literal_keys) - set(dynamic_prefixes)
    for key in sorted(stale):
        violations.append(f"过期索引条目 {key}（源面已读不到）→ 请从 env_keys.yaml 移除")
    # 描述必填（fail-closed 防空壳登记）。
    for section_name, section_map in (("production", production), ("test_only", test_only)):
        for key, fields in section_map.items():
            if not fields.get("description"):
                violations.append(f"env_keys.yaml {section_name}.{key} 缺非空 description")
    return violations


def _dump(kernel_dir: Path = KERNEL_DIR) -> None:
    extracted = extract_keys(kernel_dir)
    print(f"提取到 {len(extracted)} 个字面量形态（含模板）：\n")
    for key, sites in sorted(extracted.items()):
        tag = " [动态前缀]" if _PLACEHOLDER_RE.search(key) else ""
        print(f"{key}{tag}  ({len(sites)} 处, 首处 {sites[0][0]}:{sites[0][1]})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dump", action="store_true", help="打印提取到的键清单（排查用，不做执法）")
    ap.add_argument("--kernel-dir", type=Path, default=KERNEL_DIR, help="扫描根（默认 kernel/crates；单测注入）")
    ap.add_argument("--registry", type=Path, default=REGISTRY_PATH, help="登记索引路径（单测注入）")
    args = ap.parse_args()
    if args.dump:
        _dump(args.kernel_dir)
        return 0
    violations = check(args.kernel_dir, args.registry)
    if violations:
        print(f"kernel-env-registry 门禁红：{len(violations)} 项违规\n")
        for v in violations:
            print(f"  - {v}")
        return 1
    production, test_only, prefixes = _parse_registry(args.registry.read_text(encoding="utf-8"))
    print(
        "kernel-env-registry 门禁绿："
        f"源面 {len(extract_keys(args.kernel_dir))} 键全部登记在案"
        f"（production {len(production)} / test_only {len(test_only)} / 动态前缀 {len(prefixes)}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

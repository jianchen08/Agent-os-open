"""变更关联测试选择：非核心插件/配置/技能面变更 → 对应单元+集成测试。

映射规则（确定性，宁可多选不漏选；命中不了任何测试则放行）：
  插件目录 plugins/shared/{system,tools}/<域>/<插件>/（含 external_mcp 清单）
    → 插件目录就地 test_*.py + tests/ 树内引用插件 id / 目录名的测试
      （内容 grep，上限 40 个，防止泛匹配 id 拖垮车道）
  config/**、.env.example、docker-compose.yml → tests/ 内引用文件基名的测试
  mcp-servers/<名>/**、skills/<名>/** → tests/ 内引用目录名的测试
  tests/** 自身 → 直接收选
全量车道（kernel / pipeline / SDK）的变更不适用本车道：打印说明后退出 0
（那类变更由 full / python_full 车道全量覆盖，见 ci_changed_areas.py）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ci_common import PLUGINS_ENV, ROOT, diff_names, resolve_changed_files

TESTS_DIR = ROOT / "tests"
# 泛匹配保险丝：内容 grep 命中超过该数视为 id 过于通用，只保留文件名级匹配。
GREP_LIMIT = 40

# 非产品测试面：venv 内第三方包自带测试、构建产物、手动/真后端 e2e、
# 全插件参数化的重型冒烟矩阵（归 plugins-heavy 全量车道，不进关联车道）。
_EXCLUDED_PARTS = {"__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", "target", "build"}
_EXCLUDED_TEST_PATHS = (
    "tests/e2e_02/",
    "tests/manual/",
    "tests/plugins/test_plugin_smoke_matrix.py",
)


def _excluded(p: Path) -> bool:
    return any(part in _EXCLUDED_PARTS for part in p.parts) or any(x in p.as_posix() for x in _EXCLUDED_TEST_PATHS)


def _plugin_root(path: str) -> Path | None:
    """从变更文件向上找所属插件目录（以 plugin.json 为界）。"""
    start = ROOT / path
    cur = start if start.is_dir() else start.parent
    for _ in range(6):
        if (cur / "plugin.json").is_file():
            return cur
        if cur in (ROOT, cur.parent):
            return None
        cur = cur.parent
    return None


def _plugin_ids(plugin_root: Path) -> set[str]:
    """插件 id = manifest name + 目录名（import 路径可能用任一形态）。"""
    ids = {plugin_root.name}
    manifest = plugin_root / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        name = data.get("name")
        if isinstance(name, str) and name:
            ids.add(name)
    except (OSError, json.JSONDecodeError):
        pass
    return ids


def _local_plugin_tests(plugin_root: Path) -> list[Path]:
    return sorted(p for p in plugin_root.rglob("test_*.py") if not _excluded(p))


def _grep_tests_containing(needle: str) -> list[Path]:
    hits: list[Path] = []
    for p in sorted(TESTS_DIR.rglob("*.py")):
        if _excluded(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if needle in text:
            hits.append(p)
            if len(hits) >= GREP_LIMIT:
                break
    return hits


def _name_hits(files: list[Path], needle: str) -> list[Path]:
    return [p for p in files if needle in p.name]


def select_tests(changed: list[str]) -> tuple[list[Path], list[str]]:
    """返回 (选中的测试文件, 选择过程说明)。"""
    selected: set[Path] = set()
    notes: list[str] = []

    plugin_roots: dict[Path, set[str]] = {}
    needles: set[str] = set()

    for f in changed:
        if f.startswith(("plugins/shared/system/", "plugins/shared/tools/")):
            root = _plugin_root(f)
            if root is not None:
                plugin_roots.setdefault(root, set()).update(_plugin_ids(root))
        elif f.startswith("config/") or f in (".env.example", "docker-compose.yml"):
            needles.add(Path(f).name)
        elif f.startswith("mcp-servers/") or f.startswith("skills/"):
            parts = Path(f).parts
            if len(parts) >= 2:
                needles.add(parts[1])
        elif f.startswith("tests/") and f.endswith(".py"):
            selected.add(ROOT / f)

    for root, ids in plugin_roots.items():
        local = _local_plugin_tests(root)
        selected.update(local)
        notes.append(f"{root.name}: 就地测试 {len(local)} 个")
        for pid in sorted(ids):
            hits = _grep_tests_containing(pid)
            if len(hits) >= GREP_LIMIT:
                # id 过于通用（如 "llm"），内容 grep 全收会拖垮车道——
                # 降级为文件名级匹配（宁可少选不拖垮，就地测试已兜底）。
                narrowed = _name_hits(hits, pid)
                notes.append(f"{pid}: 内容命中超限，降级文件名匹配 {len(narrowed)} 个")
                selected.update(narrowed)
            else:
                notes.append(f"{pid}: tests/ 引用 {len(hits)} 个")
                selected.update(hits)

    for needle in sorted(needles):
        hits = _grep_tests_containing(needle)
        notes.append(f"{needle}: tests/ 引用 {len(hits)} 个")
        selected.update(hits)

    return sorted(selected), notes


def main() -> int:
    parser = argparse.ArgumentParser(description="变更关联测试选择与执行")
    parser.add_argument("--base", help="基线分支/提交（默认 GITHUB_BASE_REF / push 口径）")
    parser.add_argument("--range", help="显式范围 A..B（本地手工模拟，优先于 --base）")
    parser.add_argument("--from-file", help="从文件读变更清单（每行一个路径），跳过 git diff")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的 pytest argv")
    args = parser.parse_args()

    if args.from_file:
        with open(args.from_file, encoding="utf-8") as fh:
            changed = [line.strip().replace("\\", "/") for line in fh if line.strip()]
    elif args.range:
        a, _, b = args.range.partition("..")
        changed = diff_names(a.strip(), (b.strip() or "HEAD"))
    else:
        resolved = resolve_changed_files(args.base or "")
        if resolved is None:
            print("[related-tests] 无可解析变更范围（空仓），放行")
            return 0
        changed = resolved[0]

    heavy_hits = [f for f in changed if f.startswith(("kernel/", "plugins/sdk/", "plugins/shared/pipeline/"))]
    if heavy_hits:
        print(
            f"[related-tests] 变更含全量车道面（{len(heavy_hits)} 文件，如 {heavy_hits[0]}），"
            "本车道不适用（由 full/python_full 全量覆盖），放行"
        )
        return 0

    selected, notes = select_tests(changed)
    if not selected:
        print("[related-tests] 变更面无关联测试命中，放行")
        return 0

    for note in notes:
        print(f"[related-tests] {note}")

    argv = [sys.executable, "-m", "pytest", "-q", *(str(p) for p in selected)]
    if args.dry_run:
        print("[related-tests] dry-run pytest argv:")
        print("  " + " ".join(argv))
        return 0

    print(f"[related-tests] 执行 {len(selected)} 个测试文件")
    proc = subprocess.run(
        argv,
        cwd=str(ROOT),
        env={**os.environ, **PLUGINS_ENV},
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""测试追溯标记校验（阶段 5.1/5.2）：把 testing_rules §9 的"软约束"焊成 CI 硬门禁。

校验每个测试文件头部的 @feature / @vision / @audit 标记（去中心化载体），
与 docs/test_traceability.md（中心载体）交叉校验，防止追溯链脱节：
  - @feature（必填）：ID 必须存在于 test_traceability.md 表 A/B。
  - @vision（建议）：若填，必须是 V1~V6。
  - @audit（按需）：若填，T5#<n> 必须存在于 reports/audit_round3/T5_tests.md §⑨。

两类失败：
  - 非法标记（@feature 引用了不存在的 ID / @vision / @audit 非法）→ 硬失败，必须修。
  - 缺 @feature（未标记）→ 按"只减不增"基线锁管控（.github/traceability-baseline.txt），
    首次接入用基线锁避免一次性把全仓锁死，鼓励逐步补标记。

用法：
    python scripts/check_test_traceability.py            # 校验（CI 用）
    python scripts/check_test_traceability.py --init     # 用当前未标记数写入基线
    python scripts/check_test_traceability.py --report   # 额外打印未标记文件清单
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 2026-08-26 目录归位(92248f07)后 test_traceability.md 移至 docs/working/
TRACEABILITY = ROOT / "docs" / "working" / "test_traceability.md"
T5_AUDIT = ROOT / "reports" / "audit_round3" / "T5_tests.md"
BASELINE_FILE = ROOT / ".github" / "traceability-baseline.txt"

# 测试文件发现范围（排除依赖/构建产物/缓存）
EXCLUDE_DIRS = {
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "build",
    "dist",
    ".mypy_cache",
    ".pytest_cache",
    "target",
    "site-packages",
    ".git",
    # 工具生成/同步的工作区副本（非产品测试，扫进会把未标记数虚高）
    ".ai_workspaces",
    ".zcode",
    ".zcode_e2e",
}

# 标记解析（兼容 @feature: / @feature 、| 分隔、注释包裹等格式）
FEATURE_RE = re.compile(r"@feature:?\s+(FP-[^\s|]+)")
VISION_RE = re.compile(r"@vision:?\s+(V[1-6])")
AUDIT_RE = re.compile(r"@audit:?\s+T5#(\d+)")
VALID_VISIONS = {f"V{i}" for i in range(1, 7)}


# ── 加载合法引用集 ──────────────────────────────────────────────────────


def load_valid_features() -> set[str]:
    """从 test_traceability.md 抽取合法功能点 ID 集合。

    表 A/B 中出现的 FP-* 单体 ID（〇~八/CFG/DB/MIGR）及组合（二/六、四/五）。
    另接受 FP-T<NN>（task 级功能点，doc 以范围 FP-T01~T12 定义）。
    """
    text = TRACEABILITY.read_text(encoding="utf-8")
    raw = set(re.findall(r"FP-[^\s|，,)>]+", text))
    # 去掉范围记号本身（〇~八、T01~T12），它们是文档表述而非单体引用
    singles = {t for t in raw if "~" not in t}
    return singles


def load_valid_audits() -> set[int]:
    """从 T5_tests.md §⑨ 表抽取合法 T5# 编号。"""
    if not T5_AUDIT.exists():
        return set()
    text = T5_AUDIT.read_text(encoding="utf-8")
    nums = {int(m.group(1)) for m in re.finditer(r"^\|\s*(\d+)\s*\|", text, re.MULTILINE)}
    return nums


def is_feature_valid(token: str, valid: set[str]) -> bool:
    """功能点引用是否合法：单体命中、或斜杠组合的各部分均命中、或 task 级 FP-T<NN>。"""
    if token.startswith("FP-T") and re.fullmatch(r"FP-T\d+", token):
        return True
    if token in valid:
        return True
    if "/" in token:
        parts = token.split("/")
        return all(p in valid for p in parts)
    return False


# ── 测试文件发现 ────────────────────────────────────────────────────────


def find_test_files() -> list[Path]:
    """发现 Python / 前端 / Rust 测试文件（含 Rust src 内嵌 #[cfg(test)]）。"""
    files: list[Path] = []

    def excluded(p: Path) -> bool:
        return any(part in EXCLUDE_DIRS for part in p.parts)

    def iter_pruned(root: Path):
        """带剪枝的递归文件枚举——`rglob` 会钻进 dsh_adapter 的递归 node_modules
        无限卡死；此处遍历时即剪枝 EXCLUDE_DIRS。"""
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in filenames:
                yield Path(dirpath) / fn

    # Python test_*.py（仓库内）
    for p in iter_pruned(ROOT):
        if p.name.startswith("test_") and p.suffix == ".py" and not excluded(p):
            files.append(p)

    # 前端 *.test.ts(x)（限定 frontend 下，避免扫到 node_modules）
    fe_root = ROOT / "frontend"
    if fe_root.exists():
        for p in iter_pruned(fe_root):
            if ".test." in p.name and p.suffix in (".ts", ".tsx") and not excluded(p):
                files.append(p)

    # Rust tests/*.rs（kernel 下）
    kernel = ROOT / "kernel"
    if kernel.exists():
        for p in iter_pruned(kernel):
            if "tests" in p.parts and p.suffix == ".rs" and not excluded(p):
                files.append(p)
        # Rust src 内嵌 #[cfg(test)] 的 .rs（按文件计，不展开每个 #[test]）
        for p in iter_pruned(kernel):
            if "src" not in p.parts or p.suffix != ".rs" or excluded(p):
                continue
            try:
                if "#[cfg(test)]" in p.read_text(encoding="utf-8", errors="replace"):
                    files.append(p)
            except OSError:
                continue

    # 去重（同一文件可能被多条规则命中）
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in files:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique


# ── 标记解析 ────────────────────────────────────────────────────────────


def parse_markers(path: Path) -> dict[str, str | None]:
    """读文件头部（前 40 行）解析 @feature/@vision/@audit。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"feature": None, "vision": None, "audit": None}
    head = "\n".join(text.splitlines()[:40])
    feat = FEATURE_RE.search(head)
    vis = VISION_RE.search(head)
    aud = AUDIT_RE.search(head)
    return {
        "feature": feat.group(1) if feat else None,
        "vision": vis.group(1) if vis else None,
        "audit": aud.group(1) if aud else None,
    }


# ── 主逻辑 ──────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="测试追溯标记校验")
    parser.add_argument("--init", action="store_true", help="用当前未标记数写入基线")
    parser.add_argument("--report", action="store_true", help="打印未标记/非法文件清单")
    args = parser.parse_args()

    valid_features = load_valid_features()
    valid_audits = load_valid_audits()
    baseline = read_baseline()

    files = find_test_files()
    unmarked: list[Path] = []
    invalid: list[tuple[Path, str]] = []

    for p in files:
        m = parse_markers(p)
        feat = m["feature"]
        if not feat:
            unmarked.append(p)
            continue
        # @feature 必须合法
        if not is_feature_valid(feat, valid_features):
            invalid.append((p, f"@feature 引用了未知功能点 '{feat}'"))
            continue
        # @vision 若填必须合法
        vis = m["vision"]
        if vis and vis not in VALID_VISIONS:
            invalid.append((p, f"@vision 非法 '{vis}'（须 V1~V6）"))
        # @audit 若填必须合法
        aud = m["audit"]
        if aud and int(aud) not in valid_audits:
            invalid.append((p, f"@audit T5#{aud} 不在 audit_round3 §⑨ 清单"))

    print("=" * 60)
    print("测试追溯标记校验（testing_rules §9）")
    print("=" * 60)
    print(f"扫描测试文件总数：{len(files)}")
    print(f"合法功能点 ID 集合：{len(valid_features)} 个；合法 T5# 编号：{sorted(valid_audits)}")
    print(f"已标记 @feature：{len(files) - len(unmarked)}；未标记：{len(unmarked)}")
    print(f"非法标记（硬失败）：{len(invalid)}")
    print(f"未标记基线（只减不增）：{baseline}")

    if args.report or invalid:
        if invalid:
            print("\n── 非法标记（必须修复）──")
            for p, msg in invalid:
                print(f"  ✗ {try_relpath(p)} — {msg}")
        if args.report and unmarked:
            print(f"\n── 未标记文件（前 50，共 {len(unmarked)}）──")
            for p in unmarked[:50]:
                print(f"  · {try_relpath(p)}")

    if args.init:
        write_baseline(len(unmarked))
        print(f"\n✅ 基线已写入：unmarked_files={len(unmarked)}")
        # --init 时非法标记仍要报，但不阻塞（用于首次建立基线）
        return 0

    # 硬失败：非法标记
    if invalid:
        print(f"\n❌ 存在 {len(invalid)} 处非法标记（引用了不存在的功能点/愿景/审查号）")
        print("   请修正标记，使其引用 test_traceability.md / T5_tests.md 中已定义的 ID。")
        return 1

    # 基线锁：未标记数只减不增
    if len(unmarked) > baseline:
        print(f"\n❌ 未标记测试文件数 {len(unmarked)} > 基线 {baseline}（只减不增）")
        print("   新增测试文件请补 @feature 标记（见 testing_rules §9）；")
        print("   或治理后运行 `python scripts/check_test_traceability.py --init` 收紧基线。")
        return 1

    status = "持平" if len(unmarked) == baseline else f"减少 {baseline - len(unmarked)}"
    print(f"\n✅ 校验通过：未标记数 {len(unmarked)} ≤ 基线 {baseline}（{status}）")
    if len(unmarked) < baseline:
        print("   💡 未标记数已低于基线，可运行 --init 收紧门禁。")
    return 0


def read_baseline() -> int:
    if not BASELINE_FILE.exists():
        return 0
    m = re.search(r"unmarked_files=(\d+)", BASELINE_FILE.read_text(encoding="utf-8"))
    return int(m.group(1)) if m else 0


def write_baseline(n: int) -> None:
    BASELINE_FILE.write_text(
        f"# 测试追溯基线（未标记 @feature 的测试文件数上限，只减不增）\n"
        f"# 见 scripts/check_test_traceability.py（阶段 5.1/5.2）\n"
        f"# 治理一批未标记文件后，运行 --init 收紧该数值。\n"
        f"unmarked_files={n}\n",
        encoding="utf-8",
    )


def try_relpath(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""文件长度机械闸：新/存分档四档阈值 + 逐件基线棘轮 + 显式豁免 + 触碰即拆。

四档（coding_domain_rules.md §九.3 唯一真相源；ADR
docs/decisions/2026-10-12-file-length-mechanical-gate.md，三步裁定链：
初拟统一 1500 → 用户上调统一 2500（冻结面 42→19）→ 用户再定新/存分档）：

  | 类别                     | 非测试 | 测试 | 语义                       |
  |--------------------------|--------|------|----------------------------|
  | 新文件（本 push 新增）   | 1500 硬 | 3000 硬 | 超线红，无基线可入，须拆 |
  | 存量未入册               | 2500 触线 | 5000 触线 | 成长权保留到触线，触线红须拆或缩 |
  | 存量冻结件（adoption 时超存量线，入基线） | 天花板=入册行数，只减不增 | 同左 | 降线内强制删条目 |
  | 软提醒（全类别报表）     | >1000 | >2000 | 列出不阻断                 |

触碰即拆（第五维，独立于四档；2026-10-15 用户拍板，ADR 同文「增补」节）：
  超「触碰拆分线」文件（非测试/测试同线 **>5000**，含已入册冻结件）一旦出现
  在本次 diff 触碰文件集 ⇒ 闸红，违规消息「触碰即拆：文件 X 行数 N 超触线值
  T，本次改动须同刀按职责拆分或缩行」；拆分回落线内（或缩行）前触碰即红——
  堵死"大单体借基线/灰带合法化后不碰就不红、无限期搁置"的口子。2500~5000
  区间属"可能内聚"灰带（装配/引导件、定义面、单职责路由面行数天然偏高），
  不设闸，仍按 coding_domain_rules.md §4.2 职责判据评审执法；既有四档的
  成长触线（2500/5000）不变——两档阀门：成长触线管"不再长大"，触碰拆分线
  管"大了就别再碰，碰就拆"。豁免清单先查（显式豁免件触碰不红）；逃生口
  commit message 声明 ``[skip-touch-split]``（与 diff-coverage
  ``[skip-diff-cov]`` 同款设计，滥用必追责）。触碰行数取 HEAD 时点现值——
  同刀拆分使骨架回落线内后，触碰检查自然转绿。
  触碰范围解析复用 check_diff_coverage.resolve_range 现成机制：
  ``--touch-range A..B`` > ``--touch-base``/环境变量 ``GITHUB_BASE_REF``
  （merge-base）> 回退 ``HEAD^..HEAD``（push 口径只查最后一个提交）；
  base 不可解析 fail-closed 红（与 diff-coverage 车道同形，checkout 需
  fetch-depth: 0）。

新文件判定：``git diff <base> --name-status --diff-filter=A`` ∪
``git diff --cached --diff-filter=A``（本地暂存即执法，无须先提交）。
base 解析顺序：``--base`` 参数 > 环境变量 ``GITHUB_EVENT_BEFORE``（CI push
事件 before-sha；全零 = 新分支推送不可用）> 回退 ``HEAD~1``；git 解析失败
fail-closed 红（checkout 需 fetch-depth: 0 或显式传 --base）。

基线棘轮（.github/file-length-baseline.txt，「路径 行数」每件一行，# 注释）：
  - 冻结件成员资格在 adoption 时固定，只减不增：文件超入册行数 → 红；
    降到线内（或文件删除/转新文件）→ 红 + 提示删除条目（机械强制收紧）；
  - ``--generate`` 重生成：成员不扩（吸收存量超线文件 = 放纵触线，禁止）、
    仅收紧数值，任一条目新值 > 旧值 → 拒绝（棘轮不可逆）；空基线时按存量
    线生成 adoption 快照；
  - ``--baseline-monotonic``：对照 base 提交的基线文件，加行/升数=红，
    删行/降数=放行（账本自身的棘轮机械化）；base 处基线文件尚不存在
    （闸门 adoption 前区间）= 无对照面跳过，base 引用不可解析仍 fail-closed 红。

豁免（.github/file-length-exemptions.txt）：每条一行「路径  # 归因理由」，
缺理由或路径已不在扫描面 → 红。当前为空模板。

扫描面（现状契约）与排除目录：
  扫描  kernel/crates/**/*.rs、plugins/**/*.py、
        frontend/src/**/*.{ts,tsx}、electron/**/*.{ts,tsx}、scripts/**/*.py
  排除  target/、node_modules/、.venv*/、venv/、site-packages/、dist/、
        .zctmp/、__pycache__/、.git/（产物与依赖目录，非人写源码）

测试文件判别：
  Rust    tests.rs、*_test.rs、tests/ 目录、benches/ 目录
  Python  test_*.py、*_test.py、conftest.py、tests/ 目录
  TS      *.test.*、*.spec.*、__tests__/ 目录

行数口径：二进制换行符计数（b"\\n" 个数，wc -l 口径）——CRLF/LF 同权，
与编辑器/格式化器无关，跨平台恒定。

用法：
    python scripts/check_file_length_baseline.py                  # 执法
    python scripts/check_file_length_baseline.py --base <ref>     # 指定新文件参照
    python scripts/check_file_length_baseline.py --touch-range <A..B>  # 指定触碰范围
    python scripts/check_file_length_baseline.py --generate       # 收紧式重生成基线
    python scripts/check_file_length_baseline.py --baseline-monotonic  # 账本棘轮对照 base

门禁归属：run_gates.py 的 file-length-baseline gate（fast，CI 侧带
--baseline-monotonic 与 GITHUB_EVENT_BEFORE/GITHUB_BASE_REF，经 ci.yml
traceability-gate job 调度）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".github" / "file-length-baseline.txt"
EXEMPTIONS_FILE = ROOT / ".github" / "file-length-exemptions.txt"

# 触碰即拆的范围解析/逃生口机制照搬 check_diff_coverage.py 现成实现
# （同目录脚本，提取公共部分参数化差异点，不整段复制）。
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_diff_coverage import range_has_skip_marker, resolve_range  # noqa: E402

# 触碰即拆逃生口（commit message 声明，与 [skip-diff-cov] 同款设计）
SKIP_TOUCH_MARKER = "[skip-touch-split]"

# 触碰拆分线（非测试/测试同线；2500~5000 属"可能内聚"灰带不设闸，见模块
# docstring 与 ADR 增补节——区别于上面四档的 2500/5000 成长触线）
TOUCH_SPLIT_LINE = 5000

# 四档阈值（唯一真相源 coding_domain_rules.md §九.3；2026-10-12 三次裁定值）
HARD_NEW = 1500  # 新文件硬闸（非测试）
HARD_NEW_TEST = 3000  # 新文件硬闸（测试）
HARD_EXISTING = 2500  # 存量触线（非测试）
HARD_EXISTING_TEST = 5000  # 存量触线（测试）
SOFT = 1000  # 软提醒（非测试，全类别）
SOFT_TEST = 2000  # 软提醒（测试，全类别）

# 扫描面：根目录 → 扫描的扩展名
SCAN_ROOTS: tuple[tuple[str, frozenset[str]], ...] = (
    ("kernel/crates", frozenset({".rs"})),
    ("plugins", frozenset({".py"})),
    ("frontend/src", frozenset({".ts", ".tsx"})),
    ("electron", frozenset({".ts", ".tsx"})),
    ("scripts", frozenset({".py"})),
)

# 排除目录（产物/依赖，现状契约见模块 docstring）
EXCLUDED_DIRS = frozenset({"target", "node_modules", "venv", "site-packages", "dist", ".zctmp", "__pycache__", ".git"})


def is_excluded_dir(name: str) -> bool:
    """目录名是否排除：精确命中排除清单，或 .venv*/ 前缀形态。"""
    return name in EXCLUDED_DIRS or name.startswith(".venv")


def is_test_file(rel: str) -> bool:
    """按语言惯例判别测试文件（现状契约见模块 docstring）。"""
    parts = rel.split("/")
    name = parts[-1]
    dirs = set(parts[:-1])
    if name.endswith(".rs"):
        stem = name[: -len(".rs")]
        return stem == "tests" or stem.endswith("_test") or "tests" in dirs or "benches" in dirs
    if name.endswith(".py"):
        return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" or "tests" in dirs
    # ts / tsx
    return ".test." in name or ".spec." in name or "__tests__" in dirs


def count_lines(path: Path) -> int:
    """行数 = 换行符计数（wc -l 口径，CRLF/LF 同权）。"""
    with open(path, "rb") as f:
        return f.read().count(b"\n")


def scan() -> dict[str, int]:
    """扫描全部受管源文件 → {仓库相对路径(正斜杠): 行数}。

    os.walk 自顶向下剪枝排除目录（rglob 全树遍历会被 node_modules/
    .venv 数万依赖文件拖死——实测分钟级；剪枝后秒级）。
    """
    result: dict[str, int] = {}
    for root, exts in SCAN_ROOTS:
        root_path = ROOT / root
        if not root_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if not is_excluded_dir(d)]
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if Path(name).suffix not in exts:
                    continue
                rel = path.relative_to(ROOT).as_posix()
                result[rel] = count_lines(path)
    return result


def resolve_base(explicit: str | None) -> str:
    """新文件判定参照：--base > GITHUB_EVENT_BEFORE（全零视同缺失）> HEAD~1。"""
    if explicit:
        return explicit
    env = os.environ.get("GITHUB_EVENT_BEFORE", "").strip()
    if env and set(env) != {"0"}:
        return env
    return "HEAD~1"


def _git_diff_added(args: list[str]) -> set[str]:
    """git diff --name-status --diff-filter=A（-z 防路径转义），取新增路径集。"""
    proc = subprocess.run(
        ["git", "diff", *args, "--name-status", "--diff-filter=A", "-z"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"❌ git diff {' '.join(args)} 解析失败：{proc.stderr.strip()}\n"
            "   （新文件档判定需可解析的 base：CI checkout 需 fetch-depth: 0，"
            "或本地显式传 --base <ref>）"
        )
    fields = proc.stdout.split("\0")
    added: set[str] = set()
    for i in range(0, len(fields) - 1, 2):
        status, path = fields[i].strip(), fields[i + 1].strip()
        if status.startswith("A") and path:
            added.add(path.replace("\\", "/"))
    return added


def added_files(base: str) -> set[str]:
    """本 push 新增文件 = 相对 base 的已提交新增 ∪ 相对 HEAD 的暂存新增。

    暂存面纳入：本地 gate 在提交前即可拦新超线文件（暂存即执法），
    不必先落一个注定红的 commit。
    """
    committed = _git_diff_added([base])
    staged = _git_diff_added(["--cached"])
    return committed | staged


def _git_diff_names(args: list[str]) -> set[str]:
    """git diff --name-only -z（-z 防路径转义），取改动路径集（含新增/修改）。"""
    proc = subprocess.run(
        ["git", "diff", *args, "--name-only", "-z"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"❌ git diff {' '.join(args)} 解析失败：{proc.stderr.strip()}\n"
            "   （触碰判定需可解析的 base：CI checkout 需 fetch-depth: 0，"
            "或本地显式传 --touch-range <A..B>）"
        )
    return {p.replace("\\", "/") for p in proc.stdout.split("\0") if p}


def resolve_touched(touch_range: str | None, touch_base: str | None) -> tuple[frozenset[str], str]:
    """触碰文件集（第五维执法面）+ 范围说明。

    范围解析照 check_diff_coverage.resolve_range 现成机制（tag 换本闸标签）：
    --touch-range > (--touch-base | GITHUB_BASE_REF) merge-base > HEAD^..HEAD；
    范围不可解析（根提交/无 HEAD^）返回空集跳过（同 diff-coverage 放行口径）；
    range 内 commit 声明 [skip-touch-split] 逃生口返回空集并如实注明。
    """
    rng = resolve_range(argparse.Namespace(range=touch_range, base=touch_base), tag="[touch-split]")
    if rng is None:
        return frozenset(), "（范围不可解析，未执行触碰检查）"
    a, b, desc = rng
    if range_has_skip_marker(a, b, SKIP_TOUCH_MARKER):
        return frozenset(), f"{desc}；⚠️ {SKIP_TOUCH_MARKER} 已声明，触碰即拆放行（同刀拆分承诺未兑现滥用必追责）"
    return frozenset(_git_diff_names([a, b])), desc


def limit_for(rel: str, is_new: bool) -> int:
    """该路径适用的硬闸档位（新/存 × 测试/非测试）。"""
    if is_new:
        return HARD_NEW_TEST if is_test_file(rel) else HARD_NEW
    return HARD_EXISTING_TEST if is_test_file(rel) else HARD_EXISTING


def soft_limit_for(rel: str) -> int:
    """该路径适用的软提醒线（全类别：非测试 >1000 / 测试 >2000）。"""
    return SOFT_TEST if is_test_file(rel) else SOFT


def parse_baseline() -> dict[str, int]:
    """解析基线文件；缺失视为空（adoption 首次 --generate 生成）。格式非法即红。"""
    entries: dict[str, int] = {}
    if not BASELINE_FILE.exists():
        return entries
    for raw in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise SystemExit(f"❌ 基线格式非法（须为「路径 行数」）: {line}")
        if parts[0] in entries:
            raise SystemExit(f"❌ 基线条目重复: {parts[0]}")
        entries[parts[0]] = int(parts[1])
    return entries


def parse_exemptions() -> dict[str, str]:
    """解析豁免清单 → {路径: 归因理由}。缺理由 = fail-closed（调用方红）。"""
    exemptions: dict[str, str] = {}
    if not EXEMPTIONS_FILE.exists():
        return exemptions
    for raw in EXEMPTIONS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path, sep, reason = line.partition("#")
        path = path.strip()
        if path:
            exemptions[path] = reason.strip() if sep else ""
    return exemptions


def check(
    files: dict[str, int],
    baseline: dict[str, int],
    exemptions: dict[str, str],
    new_files: frozenset[str] = frozenset(),
    touched: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str], list[str]]:
    """四档核对 + 触碰即拆 → (硬闸红清单, 软提醒清单, 账目红清单)。

    账目红（基线/豁免清单自身问题）单独一类，fail-closed：账烂了闸就不许绿。
    """
    hard: list[str] = []
    soft: list[str] = []
    ledger: list[str] = []

    for rel in sorted(files):
        lines = files[rel]
        if rel in exemptions:
            continue
        is_new = rel in new_files
        limit = limit_for(rel, is_new)
        if is_new:
            # 新文件从严：无基线可入，超线即红；基线条目覆盖新文件 = 账目冲突
            if lines > limit:
                hard.append(f"{rel}: {lines} > 新文件硬闸 {limit}（新文件无基线可入，须拆分）")
            if rel in baseline:
                ledger.append(f"{rel}: 基线条目覆盖新文件（天花板语义失效）→ 删除该条目")
        elif lines > limit:
            if rel in baseline:
                if lines > baseline[rel]:
                    hard.append(f"{rel}: {lines} > 基线天花板 {baseline[rel]}（冻结件只减不增）")
            else:
                hard.append(f"{rel}: {lines} > 存量触线 {limit}（成长权止于触线，须拆或缩）")
        elif rel in baseline:
            ledger.append(f"{rel}: 已降到线内（{lines} ≤ {limit}）→ 删除条目收紧基线")
        if lines > soft_limit_for(rel):
            soft.append(f"{rel}: {lines}（软提醒 {soft_limit_for(rel)}，不阻断）")

    # 触碰即拆（第五维，独立于四档）：超触碰拆分线（>5000，非测试/测试同线）
    # 文件——含已入册冻结件——被本次 diff 触碰 ⇒ 红；2500~5000 灰带放行
    # （职责判据评审执法）。豁免先查（显式豁免件触碰不红）；不在扫描面的触碰
    # 路径（删除文件/文档配置改动）不参与；行数取 HEAD 时点现值——同刀拆分
    # 回落线内后触碰检查自然转绿。
    for rel in sorted(touched):
        if rel in exemptions or rel not in files:
            continue
        lines = files[rel]
        if lines > TOUCH_SPLIT_LINE:
            hard.append(
                f"触碰即拆：文件 {rel} 行数 {lines} 超触线值 {TOUCH_SPLIT_LINE}，本次改动须同刀按职责拆分或缩行"
            )

    for rel in sorted(baseline):
        if rel not in files:
            ledger.append(f"{rel}: 基线条目对应文件已不在扫描面 → 删除该条目")
        elif rel in exemptions:
            ledger.append(f"{rel}: 基线条目与豁免清单冲突（豁免文件不入基线）→ 删除该条目")

    for rel in sorted(exemptions):
        if not exemptions[rel]:
            ledger.append(f"{rel}: 豁免缺归因理由 → 补理由或删除条目")
        if rel not in files:
            ledger.append(f"{rel}: 豁免路径已不在扫描面 → 删除条目")

    return hard, soft, ledger


def generate(files: dict[str, int], exemptions: dict[str, str], new_files: frozenset[str]) -> int:
    """重写基线：成员资格只减不增（不吸收存量超线文件），数值只紧不松。"""
    old = parse_baseline()
    if old:
        paths = set(old)  # 冻结件成员资格 adoption 时固定
    else:
        paths = {
            rel
            for rel, lines in files.items()
            if rel not in exemptions and rel not in new_files and lines > limit_for(rel, False)
        }
    new: dict[str, int] = {}
    dropped: list[str] = []
    for rel in sorted(paths):
        if rel not in files or rel in exemptions or rel in new_files:
            dropped.append(rel)
            continue
        if files[rel] > limit_for(rel, False):
            new[rel] = files[rel]
        else:
            dropped.append(rel)
    loosened = {rel: (old[rel], new[rel]) for rel in new if rel in old and new[rel] > old[rel]}
    if loosened:
        for rel, (o, n) in sorted(loosened.items()):
            print(f"❌ {rel}: {o} → {n}（基线值只减不增，先拆文件再 --generate）")
        print(f"\n❌ --generate 拒绝写入：{len(loosened)} 件既有条目行数上涨（棘轮不可逆）")
        return 1
    header = (
        "# 文件长度基线（逐件棘轮，只减不增）——scripts/check_file_length_baseline.py 消费\n"
        "# 格式：「路径 行数」每件一行；# 开头为注释。\n"
        "# 成员资格 = adoption（2026-10-12）时超存量线的冻结件，新文件无基线可入；\n"
        "# 天花板 = 入册行数；文件降到线内 = 红并须删除条目；超天花板 = 红。\n"
        "# 收紧：--generate（不回松不扩员）或手工删行；放宽：不允许。\n"
    )
    body = "".join(f"{rel} {new[rel]}\n" for rel in new)
    BASELINE_FILE.write_text(header + body, encoding="utf-8")
    tightened = sum(1 for r in new if r in old and new[r] < old[r])
    print(f"✅ 基线已写入 {BASELINE_FILE}：{len(new)} 件（收紧 {tightened} 件 / 移除降线或失效 {len(dropped)} 件）")
    for rel in dropped:
        print(f"  ↘ 移除条目: {rel}")
    return 0


def parse_baseline_text(text: str) -> dict[str, int]:
    """从文本解析基线（--baseline-monotonic 对照 base 版本用）；忽略非法行。"""
    entries: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            entries[parts[0]] = int(parts[1])
    return entries


def baseline_monotonic(base: str) -> int:
    """账本棘轮：对照 base 提交的基线文件——加行/升数 = 红，删行/降数 = 放行。

    base 处基线文件尚不存在（本闸 adoption 之前的推送区间，如闸门出生当次
    推送）时无对照面，跳过账本棘轮并显著说明；base 引用不可解析仍
    fail-closed 红。
    """
    rev = subprocess.run(
        ["git", "rev-parse", "--verify", f"{base}^{{commit}}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if rev.returncode != 0:
        print(f"❌ base 引用不可解析: {base}（须完整历史或显式 --base）")
        return 1
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{base}:.github/file-length-baseline.txt"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        print(
            f"ℹ️ base {base} 处基线文件尚不存在（闸门 adoption 前区间）——"
            "账本棘轮无对照面，本次跳过（下一起点起正常对照）"
        )
        return 0
    proc = subprocess.run(
        ["git", "show", f"{base}:.github/file-length-baseline.txt"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    old = parse_baseline_text(proc.stdout) if proc.returncode == 0 else {}
    current = parse_baseline()
    regressions: list[str] = []
    for rel in sorted(current):
        if rel not in old:
            regressions.append(f"{rel}: 新增基线条目（冻结成员资格只减不增）")
        elif current[rel] > old[rel]:
            regressions.append(f"{rel}: 天花板 {old[rel]} → {current[rel]}（只许下调）")
    if regressions:
        for item in regressions:
            print(f"❌ {item}")
        print(f"\n❌ 基线账本相对 {base} 出现 {len(regressions)} 处放宽（棘轮不可逆）")
        return 1
    print(f"✅ 基线账本相对 {base} 无放宽（{len(current)} 件）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="文件长度机械闸（新/存分档 + 基线棘轮 + 豁免 + 触碰即拆）")
    parser.add_argument("--generate", action="store_true", help="收紧式重写基线（不回松不扩员）")
    parser.add_argument("--base", metavar="REF", help="新文件判定参照（默认 GITHUB_EVENT_BEFORE > HEAD~1）")
    parser.add_argument(
        "--baseline-monotonic", action="store_true", help="对照 base 提交的基线文件执法账本棘轮（CI 用）"
    )
    parser.add_argument(
        "--touch-range",
        metavar="A..B",
        help="触碰即拆显式范围（本地手工；默认 --touch-base/GITHUB_BASE_REF > HEAD^..HEAD）",
    )
    parser.add_argument(
        "--touch-base", metavar="REF", help="触碰即拆基线分支（默认取 GITHUB_BASE_REF，merge-base 后对比 HEAD）"
    )
    args = parser.parse_args()

    base = resolve_base(args.base)

    if args.baseline_monotonic:
        return baseline_monotonic(base)

    files = scan()
    exemptions = parse_exemptions()

    if args.generate:
        return generate(files, exemptions, frozenset())

    new_files = added_files(base)
    baseline = parse_baseline()
    touched, touch_desc = resolve_touched(args.touch_range, args.touch_base)
    hard, soft, ledger = check(files, baseline, exemptions, frozenset(new_files), touched)

    n_test = sum(1 for rel in files if is_test_file(rel))
    print(
        f"扫描 {len(files)} 个源文件（非测试 {len(files) - n_test} / 测试 {n_test}）；"
        f"基线 {len(baseline)} 件；豁免 {len(exemptions)} 件；新文件（vs {base}）{len(new_files)} 件；"
        f"触碰（{touch_desc}）{len(touched)} 件"
    )

    if soft:
        print(f"\n— 软提醒（非测试>{SOFT} / 测试>{SOFT_TEST}，{len(soft)} 件，不阻断）—")
        for item in soft:
            print(f"  ⚠️ {item}")

    if hard:
        print("\n— 硬闸超线 —")
        for item in hard:
            print(f"  ❌ {item}")
    if ledger:
        print("\n— 基线/豁免账目 —")
        for item in ledger:
            print(f"  ❌ {item}")

    if hard or ledger:
        print("\n❌ 文件长度闸未过：新文件拆分 / 存量触线拆或缩 / 基线按棘轮收紧（不允许放宽）。")
        if any(item.startswith("触碰即拆") for item in hard):
            print("   触碰即拆：超触碰拆分线（>5000）文件（含冻结件）被本次改动触碰，须同刀按职责拆分或缩行")
            print(
                f"   （拆分回落触线内即绿；确属暂缓拆分在 commit message 声明 {SKIP_TOUCH_MARKER}"
                "泄压，滥用必追责；豁免走 .github/file-length-exemptions.txt）"
            )
        print("   决策依据：docs/decisions/2026-10-12-file-length-mechanical-gate.md")
        return 1

    print("\n✅ 文件长度闸通过：四档无超线，触碰即拆无命中，基线/豁免账目干净")
    return 0


if __name__ == "__main__":
    sys.exit(main())

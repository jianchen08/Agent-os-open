#!/usr/bin/env python3
"""ruff 提交面扫描器——ruff 车道（scripts-lint / plugins-lint）的评估面收敛。

裁定（ADR 2026-11-07-gate-evaluation-face-commit-scope，与 check_test_traceability
提交面过滤同款）：门禁评估面 = HEAD + git 索引——
  - 未跟踪文件（`??`）不属任何提交、CI 干净检出也无此物 → 不扫；
  - 已暂存/干净的文件按磁盘内容扫（磁盘 = 索引内容）；
  - 未暂存改动（Y=M/D，磁盘内容超前索引）按索引内容（`git show :<path>`）扫。
文件一旦 `git add` 即入索引面，届时照样计入基线，无逃逸口。

输出与 `ruff check <scopes>` 兼容：违规明细 + 末尾单条 `Found N errors.`
（check_ruff_baseline.py 的 FOUND_RE/SYNTAX_RE 原样消费，不需要改比对器）。
非 git 环境（独立运行）退化为全量扫描，与旧行为等价。

用法（run_gates.py 车道 shell）：
    python scripts/ruff_commit_face.py plugins/ mcp-servers/
    python scripts/ruff_commit_face.py scripts/
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FOUND_LINE_PREFIX = "Found "
# porcelain v1：X=索引 vs HEAD，Y=索引 vs 工作区；Y 非 ' ' 即磁盘内容超前索引
FACE_DIRTY_Y = {"M", "D"}


def _git(*args: str) -> bytes | None:
    """git 子执行；非仓/失败返回 None（调用方退化为全量扫描）。"""
    try:
        r = subprocess.run(("git", *args), capture_output=True, cwd=ROOT, check=False)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def split_faces(status_z: bytes) -> tuple[set[str], set[str]]:
    """解析 `git status --porcelain -z --no-renames -uall` 输出。

    返回 (excluded, dirty)：excluded=未跟踪（不扫）；dirty=未暂存改动
    （按索引内容扫）。其余路径由批量目录扫描自然覆盖。
    """
    excluded: set[str] = set()
    dirty: set[str] = set()
    for entry in status_z.decode("utf-8", "replace").split("\0"):
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy == "??":
            excluded.add(path.replace("\\", "/"))
        elif xy[1] in FACE_DIRTY_Y:
            dirty.add(path.replace("\\", "/"))
    return excluded, dirty


def _strip_and_count(out: str) -> tuple[str, int]:
    """剥掉 ruff 的 `Found N errors.` 汇总行，返回 (明细体, 计数)。

    汇总行只允许在 run() 末尾出现一条（check_ruff_baseline 的 FOUND_RE 取首个
    匹配，漏剥会导致部分计数被吞）。
    """
    count = 0
    lines: list[str] = []
    for line in out.splitlines(keepends=True):
        if line.startswith(FOUND_LINE_PREFIX) and " error" in line:
            count = int(line.split(FOUND_LINE_PREFIX, 1)[1].split(" ", 1)[0])
            continue
        lines.append(line)
    return "".join(lines), count


def _ruff(args: list[str], stdin: bytes | None = None) -> tuple[str, int | None]:
    """跑一次 ruff，返回 (剥离汇总行后的明细体, Found 计数)。

    ruff 启动失败（未安装/不可执行）→ 计数 None：调用方不得合成 `Found 0`，
    否则"跑不起来"会被下游 check_ruff_baseline 读成"零违规"（假绿）。
    """
    try:
        r = subprocess.run(
            ("ruff", "check", *args), capture_output=True, cwd=ROOT, input=stdin, check=False
        )
    except OSError as e:
        return f"ruff_commit_face: ruff 启动失败 {e}\n", None
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    if not out.strip():
        return f"ruff_commit_face: ruff 无输出（退出码 {r.returncode}）\n", None
    body, count = _strip_and_count(out)
    # 汇总行存在性复核（防内部错误/截断冒充零违规）。ruff 三种收尾形态：
    # - 多数：`Found N errors.`
    # - 单数：`Found 1 error.`（N=1 不加 s——stdin 单违规面实测触发的解析洞）
    # - 零违规：`All checks passed!`（退出码 0，无 Found 行——清洁扫描是
    #   确定语义，按退出码记 0，不是"没数到"）
    has_summary = (
        f"{FOUND_LINE_PREFIX}{count} errors" in out
        or (count == 1 and f"{FOUND_LINE_PREFIX}1 error." in out)
        or (r.returncode == 0 and "All checks passed!" in out)
    )
    if not has_summary:
        return body + f"ruff_commit_face: ruff 输出缺 `Found N errors.` 汇总行（退出码 {r.returncode}）\n", None
    return body, count


def run(scopes: list[str]) -> int:
    status_z = _git("status", "--porcelain", "-z", "--no-renames", "-uall", "--", *scopes)
    parts: list[str] = []
    total = 0
    measured = True
    if status_z is None:
        body, count = _ruff(scopes)
        parts.append(body)
        measured = measured and count is not None
        total += count or 0
    else:
        excluded, dirty = split_faces(status_z)
        bulk_args = list(scopes)
        # 未跟踪 + 未暂存改动都从批量扫描剔除（后者单独按索引内容扫）；
        # ruff --exclude 单值语义：逐路径重复旗标（逗号拼接对含逗号路径不安全）
        for rel in sorted(excluded | dirty):
            bulk_args.extend(("--exclude", rel))
        body, count = _ruff(bulk_args)
        parts.append(body)
        measured = measured and count is not None
        total += count or 0
        for rel in sorted(dirty):
            if not rel.lower().endswith((".py", ".pyi")):
                # 非 Python 文件（uv.lock 等数据文件）不进 lint：文件发现口径下
                # ruff 本就不扫它们，stdin 显式喂入反而破坏等价性（实测 uv.lock
                # 被当 Python 解析出 2000+ 假错，基线计数彻底失真）。
                continue
            blob = _git("show", f":{rel}")
            if blob is None:
                parts.append(f"ruff_commit_face: 索引内容读取失败 {rel}\n")
                measured = False
                continue
            body, count = _ruff(["--stdin-filename", str(ROOT / rel), "-"], stdin=blob)
            parts.append(f"── 提交面（索引内容）{rel} ──\n{body}")
            measured = measured and count is not None
            total += count or 0
    if not measured:
        # 有环节没测成 → 不合成汇总行：下游 check_ruff_baseline 见无汇总行即判红，
        # 不把"没数到"当"零违规"。
        parts.append("ruff_commit_face: 扫描未完成（见上），未输出汇总行——车道应按失败处理。\n")
        sys.stdout.write("".join(parts))
        return 1
    parts.append(f"{FOUND_LINE_PREFIX}{total} errors.\n")
    sys.stdout.write("".join(parts))
    return 0


def main() -> int:
    scopes = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not scopes:
        print("usage: ruff_commit_face.py <scope>...", file=sys.stderr)
        return 2
    return run(scopes)


if __name__ == "__main__":
    sys.exit(main())
